"""Parent-owned direct child jobs exposed through native-shaped tools."""

from __future__ import annotations

import os
import pathlib
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .config import SessionConfig, SubagentConfig, ToolConfig
from ._tools import Tool


_TASK_NAME = re.compile(r"^[a-z0-9_]+$")
_ACTIVE_STATES = frozenset(
    {"starting", "running", "interrupt_requested", "cancelling"}
)
_TERMINAL_STATES = frozenset(
    {"completed", "failed", "timeout", "interrupted", "cancelled"}
)
_WAIT_LOG_PAGE_BYTES = 16 * 1024
_DEFAULT_CLOSE_TIMEOUT_SECONDS = 5.0


@dataclass
class _PendingParentQuestion:
    message: str
    answer: str | None = None
    error: str | None = None


@dataclass
class _ChildEntry:
    target: str
    task_name: str
    model: str
    reasoning_effort: str
    status: str = "starting"
    job: Any = None
    conversation_state: Any = None
    messages: list[str] = field(default_factory=list)
    turns: list[dict[str, Any]] = field(default_factory=list)
    version: int = 0
    error: str | None = None
    log_sources: list[dict[str, Any]] = field(default_factory=list)
    log_page_pending: bool = False
    pending_parent_question: _PendingParentQuestion | None = None


def _resolved_cwd(config: SessionConfig) -> pathlib.Path:
    cwd = config.workspace.cwd
    return pathlib.Path.cwd().resolve() if cwd is None else pathlib.Path(cwd).resolve()


def _resolved_path(value, *, cwd: pathlib.Path) -> pathlib.Path:
    path = pathlib.Path(os.fspath(value)).expanduser()
    return (cwd / path).resolve() if not path.is_absolute() else path.resolve()


def _path_is_covered(path: pathlib.Path, roots: tuple[pathlib.Path, ...]) -> bool:
    return any(path == root or root in path.parents for root in roots)


def _workspace_roots(values, *, cwd):
    return tuple(_resolved_path(value, cwd=cwd) for value in values)


def _validate_child_permissions(
    parent: SessionConfig,
    child: SessionConfig,
    *,
    parent_tools,
) -> SessionConfig:
    """Reject child settings that exceed the parent's effective permissions."""
    parent_cwd = _resolved_cwd(parent)
    child_cwd = _resolved_cwd(child)
    if child.workspace.cwd is None:
        child = child.with_overrides(
            workspace=child.workspace.with_overrides(cwd=parent_cwd)
        )
        child_cwd = parent_cwd
    if child_cwd != parent_cwd:
        raise ValueError("subagent session cwd must match the parent cwd")
    if child.codex_bin != parent.codex_bin:
        raise ValueError("subagent codex_bin must match the parent")
    if parent.timeout is not None and (
        child.timeout is None or child.timeout > parent.timeout
    ):
        child = child.with_overrides(timeout=parent.timeout)

    parent_subcwd_root = parent.workspace.subcwd_root
    child_subcwd_root = child.workspace.subcwd_root
    if parent_subcwd_root is None:
        if child_subcwd_root is not None:
            raise ValueError("subagent subcwd_root exceeds parent permissions")
    elif child_subcwd_root is not None:
        parent_subcwd = _resolved_path(parent_subcwd_root, cwd=parent_cwd)
        child_subcwd = _resolved_path(child_subcwd_root, cwd=child_cwd)
        if not _path_is_covered(child_subcwd, (parent_subcwd,)):
            raise ValueError("subagent subcwd_root exceeds parent permissions")

    parent_read_roots = (parent_cwd,) + _workspace_roots(
        parent.workspace.read_paths + parent.workspace.write_paths,
        cwd=parent_cwd,
    )
    parent_write_roots = _workspace_roots(
        parent.workspace.write_paths,
        cwd=parent_cwd,
    )
    child_read_paths = (
        child.workspace.read_paths + tuple(child.context)
    )
    for value in child_read_paths:
        path = _resolved_path(value, cwd=child_cwd)
        if not _path_is_covered(path, parent_read_roots):
            raise ValueError(
                f"subagent readable path exceeds parent permissions: {value}"
            )
    for value in child.workspace.write_paths:
        path = _resolved_path(value, cwd=child_cwd)
        if not _path_is_covered(path, parent_write_roots):
            raise ValueError(
                f"subagent writable path exceeds parent permissions: {value}"
            )

    if child.allow_shell_commands and not parent.allow_shell_commands:
        raise ValueError("subagent shell permission exceeds parent permissions")
    if parent.ignore_global_context and not child.ignore_global_context:
        raise ValueError(
            "subagent cannot enable global context disabled by the parent"
        )

    parent_skills = parent.allowed_skills
    child_skills = child.allowed_skills
    if parent_skills is not None:
        if child_skills is None or not set(child_skills).issubset(parent_skills):
            raise ValueError("subagent skills exceed parent permissions")

    parent_environment = dict(parent.env)
    for key, value in child.env.items():
        if key not in parent_environment or parent_environment[key] != value:
            raise ValueError(
                f"subagent environment exceeds parent permissions: {key}"
            )

    allowed_tool_ids = {id(tool) for tool in parent_tools}
    child_tools = () if child.tools is None else child.tools.tools
    if any(id(tool) not in allowed_tool_ids for tool in child_tools):
        raise ValueError("subagent tools must be inherited from the parent")
    return child


class _DirectChildManager:
    """Own direct child Jobs and expose one parent-bound tool set."""

    def __init__(
        self,
        *,
        parent_config: SessionConfig,
        config: SubagentConfig,
        activity_callback=None,
        delegation_callback=None,
        rollout_dir=None,
    ):
        self._parent_config = parent_config
        self._rollout_dir = rollout_dir
        self._config = config
        self._activity_callback = activity_callback
        self._delegation_callback = delegation_callback
        self._condition = threading.Condition(threading.RLock())
        self._entries: dict[str, _ChildEntry] = {}
        self._closing = False
        self._version = 0
        self._wait_cursor = 0
        self._threads: set[threading.Thread] = set()
        self._starting_calls = 0
        self._deadline: float | None = None
        self._child_session = config.session
        self._tools = self._build_tools()

    def validate_permissions(self, *, parent_tools) -> None:
        self._child_session = _validate_child_permissions(
            self._parent_config,
            self._config.session,
            parent_tools=parent_tools,
        )

    def activate(self, *, timeout: float | None) -> None:
        with self._condition:
            self._deadline = (
                None if timeout is None else time.monotonic() + timeout
            )

    def tools(self) -> tuple[Tool, ...]:
        return self._tools

    def _build_tools(self) -> tuple[Tool, ...]:
        return (
            Tool(
                self.spawn_agent,
                "Start a direct child job owned by this parent job.",
                args={
                    "message": "The concrete task for the child.",
                    "task_name": "A unique lowercase identifier using letters, numbers, and underscores.",
                    "model": "Optional allowed child model.",
                    "reasoning_effort": "Optional allowed child reasoning effort.",
                },
                returns="The child task path and its initial state.",
                name="spawn_agent",
            ),
            Tool(
                self.send_message,
                "Answer the child's pending ask_parent question. If there is no pending question, queue the message for the child's next turn.",
                args={
                    "target": "The direct child task path.",
                    "message": "The answer or message to deliver.",
                },
                returns="The target and delivery state.",
                name="send_message",
            ),
            Tool(
                self.followup_task,
                "Start a follow-up turn for an idle direct child.",
                args={
                    "target": "The direct child task path.",
                    "message": "The follow-up task.",
                },
                returns="The target and its new running state.",
                name="followup_task",
            ),
            Tool(
                self.interrupt_agent,
                "Interrupt a running direct child.",
                args={"target": "The direct child task path."},
                returns="The target and interruption state.",
                name="interrupt_agent",
            ),
            Tool(
                self.list_agents,
                "List direct children owned by this parent.",
                args={},
                returns="Snapshots of all direct children.",
                name="list_agents",
            ),
            Tool(
                self.wait_agent,
                "Wait until a direct child asks its parent a question, reaches a terminal state, or the timeout expires. This does not cancel children.",
                args={"timeout_ms": "Maximum wait in milliseconds."},
                returns="Changed direct child snapshots with exact unread job log text, or an empty list on timeout.",
                name="wait_agent",
            ),
        )

    def spawn_agent(
        self,
        message: str,
        task_name: str,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        message = _non_empty(message, field_name="message")
        task_name = _non_empty(task_name, field_name="task_name")
        if _TASK_NAME.fullmatch(task_name) is None:
            raise ValueError(
                "task_name must contain only lowercase letters, numbers, and underscores"
            )
        selected_model = self._child_session.model if model is None else model.strip()
        selected_effort = (
            self._child_session.reasoning_effort
            if reasoning_effort is None
            else reasoning_effort.strip()
        )
        allowed_efforts = self._config.allowed_models.get(selected_model)
        if allowed_efforts is None or selected_effort not in allowed_efforts:
            raise ValueError(
                f"subagent model and reasoning_effort are not allowed: "
                f"{selected_model}/{selected_effort}"
            )
        target = f"/root/{task_name}"
        with self._condition:
            self._ensure_open()
            if target in self._entries:
                raise ValueError(f"task_name is already in use: {task_name}")
            if self._active_count() >= self._config.max_concurrent:
                raise RuntimeError("subagent concurrency limit reached")
            entry = _ChildEntry(
                target=target,
                task_name=task_name,
                model=selected_model,
                reasoning_effort=selected_effort,
            )
            self._entries[target] = entry
            self._touch(entry)
            self._emit("spawn", entry, "child turn reserved")
            self._emit_delegation(
                operation="spawn_agent",
                entry=entry,
                message=message,
                turn_index=len(entry.turns),
            )
        child_config = self._child_session.with_overrides(
            model=selected_model,
            reasoning_effort=selected_effort,
        )
        child_config = self._with_remaining_timeout(child_config)
        try:
            self._start_entry(entry, input=message, config=child_config)
        except BaseException as exc:
            with self._condition:
                entry.status = "failed"
                entry.error = str(exc)
                self._touch(entry)
            return self._snapshot(entry)
        return self._snapshot(entry)

    def send_message(self, target: str, message: str) -> dict[str, Any]:
        message = _non_empty(message, field_name="message")
        with self._condition:
            entry = self._entry(target)
            question = entry.pending_parent_question
            if (
                question is not None
                and question.answer is None
                and question.error is None
            ):
                question.answer = message
                self._touch(entry)
                self._emit("answer", entry, "parent answered child question")
                return {
                    "target": target,
                    "delivery": "answered_parent_question",
                    "queued": len(entry.messages),
                }
            entry.messages.append(message)
            self._touch(entry)
            self._emit("message", entry, "queued_for_next_turn")
            return {
                "target": target,
                "delivery": "queued_for_next_turn",
                "queued": len(entry.messages),
            }

    def followup_task(self, target: str, message: str) -> dict[str, Any]:
        message = _non_empty(message, field_name="message")
        with self._condition:
            self._ensure_open()
            entry = self._entry(target)
            if entry.status in _ACTIVE_STATES:
                raise RuntimeError("subagent is already running")
            if entry.conversation_state is None:
                raise RuntimeError("subagent has no resumable conversation")
            if self._active_count() >= self._config.max_concurrent:
                raise RuntimeError("subagent concurrency limit reached")
            queued = tuple(entry.messages)
            entry.messages.clear()
            entry.status = "starting"
            entry.error = None
            self._touch(entry)
            self._emit("followup", entry, "child follow-up reserved")
            self._emit_delegation(
                operation="followup_task",
                entry=entry,
                message=message,
                turn_index=len(entry.turns),
            )
        combined = "\n\n".join((*queued, message))
        config = self._child_session.with_overrides(
            model=entry.model,
            reasoning_effort=entry.reasoning_effort,
        )
        config = self._with_remaining_timeout(config)
        try:
            self._start_entry(
                entry,
                input=combined,
                config=config,
                conversation_state=entry.conversation_state,
            )
        except BaseException as exc:
            with self._condition:
                entry.status = "failed"
                entry.error = str(exc)
                self._touch(entry)
        return self._snapshot(entry)

    def interrupt_agent(self, target: str) -> dict[str, Any]:
        with self._condition:
            entry = self._entry(target)
            job = entry.job
            if entry.status not in _ACTIVE_STATES or job is None:
                return self._snapshot(entry)
            entry.status = "interrupt_requested"
            self._fail_pending_question(entry, "subagent was interrupted")
            self._touch(entry)
            self._emit("interrupt", entry, "child interruption requested")
        job.cancel()
        return self._snapshot(entry)

    def list_agents(self) -> list[dict[str, Any]]:
        with self._condition:
            return [
                self._snapshot(entry)
                for entry in self._entries.values()
            ]

    def wait_agent(self, timeout_ms: int = 30000) -> list[dict[str, Any]]:
        if (
            not isinstance(timeout_ms, int)
            or isinstance(timeout_ms, bool)
            or timeout_ms < 0
        ):
            raise ValueError("timeout_ms must be a non-negative integer")
        timeout_seconds = min(timeout_ms / 1000.0, 60.0)
        with self._condition:
            if self._deadline is not None:
                timeout_seconds = min(
                    timeout_seconds,
                    max(self._deadline - time.monotonic(), 0.0),
                )
            tool_timeout = (
                60.0
                if self._parent_config.tools is None
                else self._parent_config.tools.timeout
            )
            timeout_seconds = min(
                timeout_seconds,
                max(tool_timeout - 0.1, 0.0),
            )
            cursor = self._wait_cursor
            terminal_change = any(
                entry.version > cursor and entry.status in _TERMINAL_STATES
                for entry in self._entries.values()
            )
            pending_page = any(
                entry.log_page_pending for entry in self._entries.values()
            )
            pending_question = any(
                entry.version > cursor
                and entry.pending_parent_question is not None
                and entry.pending_parent_question.answer is None
                and entry.pending_parent_question.error is None
                for entry in self._entries.values()
            )
            if (
                not terminal_change
                and not pending_page
                and not pending_question
                and not self._closing
            ):
                self._condition.wait_for(
                    lambda: (
                        any(
                            entry.version > cursor
                            and entry.status in _TERMINAL_STATES
                            for entry in self._entries.values()
                        )
                        or any(
                            entry.log_page_pending
                            for entry in self._entries.values()
                        )
                        or any(
                            entry.version > cursor
                            and entry.pending_parent_question is not None
                            and entry.pending_parent_question.answer is None
                            and entry.pending_parent_question.error is None
                            for entry in self._entries.values()
                        )
                        or self._closing
                    ),
                    timeout=timeout_seconds,
                )
            changed = []
            remaining = _WAIT_LOG_PAGE_BYTES
            for entry in self._entries.values():
                has_unread_log = self._entry_has_unread_log(entry)
                if (
                    entry.version <= cursor
                    and not entry.log_page_pending
                    and not has_unread_log
                ):
                    continue
                snapshot = self._snapshot(entry)
                log_delta, has_more, consumed = self._read_log_delta(
                    entry,
                    max_bytes=remaining,
                )
                snapshot["log_delta"] = log_delta
                snapshot["has_more"] = has_more
                entry.log_page_pending = has_more
                changed.append(snapshot)
                remaining -= consumed
                if remaining <= 0:
                    break
            for entry in self._entries.values():
                if self._entry_has_unread_log(entry):
                    entry.log_page_pending = True
            self._wait_cursor = self._version
            return changed

    def results(self) -> tuple[dict[str, Any], ...]:
        with self._condition:
            return tuple(
                dict(turn)
                for entry in self._entries.values()
                for turn in entry.turns
            )

    def estimated_cost_usd(self) -> float | None:
        results = self.results()
        if any(not turn["cost_complete"] for turn in results):
            return None
        return sum(turn["estimated_cost_usd"] for turn in results)

    def cost_complete(self) -> bool:
        with self._condition:
            return all(
                turn["cost_complete"]
                for entry in self._entries.values()
                for turn in entry.turns
            )

    def usage_incomplete_reasons(self) -> tuple[str, ...]:
        with self._condition:
            return tuple(
                reason
                for entry in self._entries.values()
                for turn in entry.turns
                for reason in turn["usage_incomplete_reasons"]
            )

    def close(self, *, timeout: float | None = None) -> None:
        """Stop all children, then delete the conversation stores they own."""
        try:
            self._close(timeout=timeout)
        finally:
            with self._condition:
                states = [
                    entry.conversation_state
                    for entry in self._entries.values()
                    if entry.conversation_state is not None
                ]
            for state in states:
                store = getattr(state, "store", None)
                if store is not None:
                    store.delete()

    def _close(self, *, timeout: float | None = None) -> None:
        deadline = time.monotonic() + _close_timeout_seconds(timeout)
        with self._condition:
            if self._closing:
                return
            self._closing = True
            self._emit(None, None, "subagent manager closing")
            self._condition.wait_for(
                lambda: self._starting_calls == 0,
                timeout=_remaining_seconds(deadline),
            )
            active_jobs = tuple(
                (entry, entry.job)
                for entry in self._entries.values()
                if entry.status in _ACTIVE_STATES and entry.job is not None
            )
            threads = tuple(self._threads)
            for entry in self._entries.values():
                self._fail_pending_question(
                    entry, "subagent manager is closing"
                )
            self._version += 1
            self._condition.notify_all()
        for _entry, job in active_jobs:
            try:
                job.cancel()
            except Exception:
                pass
        for thread in threads:
            remaining = _remaining_seconds(deadline)
            if remaining <= 0:
                break
            thread.join(remaining)
        if not any(thread.is_alive() for thread in threads):
            return
        for entry, job in active_jobs:
            if not any(
                thread.is_alive() and thread.name == f"codex_subagent_{entry.task_name}"
                for thread in threads
            ):
                continue
            self._force_terminate(job)
            self._emit(
                "cleanup_timeout",
                entry,
                "child did not stop before cleanup deadline; forced termination requested",
            )

    @staticmethod
    def _force_terminate(job) -> None:
        """Kill a child process after its cooperative cancellation missed cleanup."""
        terminate = getattr(job, "_terminate_process", None)
        if callable(terminate):
            try:
                terminate("subagent_cleanup")
            except Exception:
                pass

    def _start_entry(
        self,
        entry: _ChildEntry,
        *,
        input: str,
        config: SessionConfig,
        conversation_state=None,
    ) -> None:
        from .job import CodexJob

        config = self._with_ask_parent_tool(entry, config)
        rollout_root = None if self._rollout_dir is None else self._rollout_dir()
        job = CodexJob(
            config=config,
            input=input,
            _conversation_state=conversation_state,
            _activity_actor=f"Subagent {entry.target}",
            _rollout_dir=None if rollout_root is None else rollout_root / entry.task_name,
        )
        # The manager owns the child's store so followup_task can continue it.
        job._retain_conversation_store = True
        with self._condition:
            self._ensure_open()
            entry.job = job
            entry.status = "running"
            self._starting_calls += 1
            self._touch(entry)
        try:
            job.start()
            with self._condition:
                if job.log_path is not None:
                    entry.log_sources.append(
                        {"path": pathlib.Path(job.log_path), "offset": 0}
                    )
                self._touch(entry)
            watcher = threading.Thread(
                target=self._watch,
                args=(entry, job),
                name=f"codex_subagent_{entry.task_name}",
                daemon=True,
            )
            with self._condition:
                self._threads.add(watcher)
            watcher.start()
        finally:
            with self._condition:
                self._starting_calls -= 1
                self._condition.notify_all()

    def _watch(self, entry: _ChildEntry, job) -> None:
        response = None
        error = None
        try:
            response = job.result()
            status = "completed"
        except BaseException as exc:
            error = exc
            status = _status_after_error(job)
            response = getattr(exc, "response", None)
        with self._condition:
            self._fail_pending_question(
                entry, f"child turn ended with status {status}"
            )
            entry.conversation_state = getattr(job, "_conversation_state", None)
            entry.status = status
            entry.error = None if error is None else str(error)
            usage_complete = response is not None and response.usage is not None
            cost_complete = (
                usage_complete
                and response.estimated_cost_usd is not None
            )
            reasons = []
            if not usage_complete:
                reasons.append(f"{entry.target} turn usage unavailable")
            if response is not None and response.estimated_cost_usd is None:
                reasons.append(f"{entry.target} turn pricing unavailable")
            entry.turns.append(
                {
                    "target": entry.target,
                    "turn_index": len(entry.turns),
                    "status": status,
                    "model": entry.model,
                    "reasoning_effort": entry.reasoning_effort,
                    "usage": (
                        None
                        if response is None or response.usage is None
                        else dict(response.usage)
                    ),
                    "estimated_cost_usd": (
                        None
                        if response is None
                        else response.estimated_cost_usd
                    ),
                    "usage_complete": usage_complete,
                    "cost_complete": cost_complete,
                    "usage_incomplete_reasons": tuple(reasons),
                    "log_path": str(job.log_path) if job.log_path else None,
                    "content": (
                        None if response is None else response.content
                    ),
                    "error": None if error is None else str(error),
                }
            )
            self._threads.discard(threading.current_thread())
            self._touch(entry)
            self._emit("completed", entry, f"child turn {status}")

    @staticmethod
    def _entry_has_unread_log(entry: _ChildEntry) -> bool:
        for source in entry.log_sources:
            try:
                if source["path"].stat().st_size > source["offset"]:
                    return True
            except OSError:
                continue
        return False

    @staticmethod
    def _read_log_delta(
        entry: _ChildEntry,
        *,
        max_bytes: int,
    ) -> tuple[str, bool, int]:
        if max_bytes <= 0:
            return "", _DirectChildManager._entry_has_unread_log(entry), 0
        chunks = []
        consumed = 0
        for source in entry.log_sources:
            if consumed >= max_bytes:
                break
            try:
                with source["path"].open("rb") as handle:
                    handle.seek(source["offset"])
                    data = handle.read(max_bytes - consumed)
            except OSError:
                continue
            if not data:
                continue
            if len(data) == max_bytes - consumed:
                newline = data.rfind(b"\n")
                if newline >= 0:
                    data = data[: newline + 1]
            if not data:
                break
            source["offset"] += len(data)
            consumed += len(data)
            chunks.append(data.decode("utf-8", errors="replace"))
        return (
            "".join(chunks),
            _DirectChildManager._entry_has_unread_log(entry),
            consumed,
        )

    def _with_remaining_timeout(self, config: SessionConfig) -> SessionConfig:
        with self._condition:
            deadline = self._deadline
        if deadline is None:
            return config
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("parent deadline has expired")
        timeout = config.timeout
        return config.with_overrides(
            timeout=remaining if timeout is None else min(timeout, remaining)
        )

    def _with_ask_parent_tool(
        self,
        entry: _ChildEntry,
        config: SessionConfig,
    ) -> SessionConfig:
        existing = config.tools
        existing_tools = () if existing is None else existing.tools
        existing_timeout = 60.0 if existing is None else existing.timeout
        tool_timeout = max(
            existing_timeout,
            0.0 if config.timeout is None else config.timeout,
        )

        def ask_parent(message: str) -> str:
            return self._ask_parent(
                entry,
                message,
                timeout=max(tool_timeout - 0.1, 0.0),
            )

        tool = Tool(
            ask_parent,
            "Ask the parent job a question and wait for its answer before continuing this same turn.",
            args={"message": "The specific question for the parent."},
            returns="The parent's answer.",
            name="ask_parent",
        )
        return config.with_overrides(
            tools=ToolConfig(
                tools=(*existing_tools, tool),
                max_concurrent_calls=(
                    8 if existing is None else existing.max_concurrent_calls
                ),
                timeout=tool_timeout,
            )
        )

    def _ask_parent(
        self,
        entry: _ChildEntry,
        message: str,
        *,
        timeout: float,
    ) -> str:
        message = _non_empty(message, field_name="message")
        with self._condition:
            self._ensure_open()
            if self._entries.get(entry.target) is not entry:
                raise RuntimeError("subagent is not owned by this parent")
            if entry.status not in _ACTIVE_STATES:
                raise RuntimeError("subagent is not running")
            if entry.pending_parent_question is not None:
                raise RuntimeError("parent question is already pending")
            question = _PendingParentQuestion(message=message)
            entry.pending_parent_question = question
            self._touch(entry)
            self._emit("question", entry, "child asked parent a question")
            wait_timeout = timeout
            if self._deadline is not None:
                wait_timeout = min(
                    wait_timeout,
                    max(self._deadline - time.monotonic(), 0.0),
                )
            answered = self._condition.wait_for(
                lambda: (
                    question.answer is not None
                    or question.error is not None
                ),
                timeout=wait_timeout,
            )
            if not answered:
                question.error = "parent answer timed out"
            if entry.pending_parent_question is question:
                entry.pending_parent_question = None
                self._touch(entry)
            if question.error is not None:
                raise RuntimeError(question.error)
            assert question.answer is not None
            return question.answer

    def _fail_pending_question(
        self,
        entry: _ChildEntry,
        error: str,
    ) -> None:
        question = entry.pending_parent_question
        if (
            question is None
            or question.answer is not None
            or question.error is not None
        ):
            return
        question.error = error
        self._condition.notify_all()

    def _emit(self, event, entry, message):
        callback = self._activity_callback
        if callback is None:
            return
        target = "" if entry is None else f" target={entry.target}"
        try:
            callback(event or "cleanup", message + target)
        except Exception:
            pass

    def _emit_delegation(self, *, operation, entry, message, turn_index):
        """Persist the exact parent-to-child tool argument through the owner."""
        callback = self._delegation_callback
        if callback is None:
            return
        try:
            callback(
                {
                    "type": "subagent.delegation",
                    "operation": operation,
                    "child_task_path": entry.target,
                    "child_task_name": entry.task_name,
                    "child_turn_index": turn_index,
                    "message": message,
                }
            )
        except Exception:
            pass

    def _entry(self, target: str) -> _ChildEntry:
        entry = self._entries.get(target)
        if entry is None:
            raise ValueError("subagent not found")
        return entry

    def _active_count(self) -> int:
        return sum(
            entry.status in _ACTIVE_STATES
            for entry in self._entries.values()
        )

    def _ensure_open(self) -> None:
        if self._closing:
            raise RuntimeError("subagent manager is closing")

    def _touch(self, entry: _ChildEntry) -> None:
        self._version += 1
        entry.version = self._version
        self._condition.notify_all()

    @staticmethod
    def _snapshot(entry: _ChildEntry) -> dict[str, Any]:
        latest = None if not entry.turns else dict(entry.turns[-1])
        question = entry.pending_parent_question
        return {
            "target": entry.target,
            "task_name": entry.task_name,
            "status": entry.status,
            "model": entry.model,
            "reasoning_effort": entry.reasoning_effort,
            "queued_messages": len(entry.messages),
            "turns": len(entry.turns),
            "latest": latest,
            "error": entry.error,
            "pending_question": (
                None
                if question is None
                or question.answer is not None
                or question.error is not None
                else {"message": question.message}
            ),
        }


def _non_empty(value, *, field_name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _close_timeout_seconds(timeout: float | None) -> float:
    if timeout is None:
        return _DEFAULT_CLOSE_TIMEOUT_SECONDS
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ValueError("close timeout must be a non-negative number or None")
    if timeout < 0:
        raise ValueError("close timeout must be a non-negative number or None")
    return float(timeout)


def _remaining_seconds(deadline: float) -> float:
    return max(deadline - time.monotonic(), 0.0)


def _input_text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, os.PathLike):
        return os.fspath(value)
    return "\n".join(os.fspath(item) for item in value)


def _status_after_error(job) -> str:
    status = getattr(job, "status", "failed")
    if status == "timeout":
        return "timeout"
    if status == "cancelled":
        return "interrupted"
    return "failed"


__all__ = ["_DirectChildManager", "_validate_child_permissions"]
