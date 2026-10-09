"""Public pending job API."""

from __future__ import annotations

import threading
import pathlib
import json
import shutil
import sys
import tempfile
from dataclasses import replace
import time
import copy
import inspect
import os
import uuid
from collections.abc import MutableSequence
from typing import Self

from ._client_helpers import *  # noqa: F401,F403
from ._client_helpers import (
    _estimate_cost_usd_for_usage,
    _make_pydantic_output_validator,
    _resolve_model_pricing,
)
from ._constants import (
    _CANCELLED, _COMPLETED, _DEFAULT_MAX_FILE_BYTES, _DEFAULT_MAX_FILES,
    _DEFAULT_MAX_TOTAL_FILE_BYTES, _DEFAULT_MODEL, _FAILED, _RUNNING, _SUBCWD_TOKEN, _TIMEOUT,
)
from ._tools import Tool, _normalize_tools
from .client import _close_runtime, _configure_runtime, _run_job_runtime
from .config import (
    FileCollectionConfig,
    LoggingConfig,
    SessionConfig,
    SubagentConfig,
    ValidationConfig,
)
from .errors import (
    CodexAlreadyStartedError,
    CodexCancelledError,
    CodexExecutionError,
    CodexNotStartedError,
    CodexTimeoutError,
    CodexValidationError,
    CodexReviewProtocolError,
)
from ._session import CodexSession
from ._job import _ValidationStepOutcome
from ._subagents import _DirectChildManager
from .types import CodexActivity, CodexResponse
from ._peer_review import (
    MAX_CONSECUTIVE_UNRESOLVED, MAX_CORRECTIONS as _PEER_MAX_CORRECTIONS,
    SCHEMA_VERSION as PEER_SCHEMA_VERSION,
    capture_artifacts, changes as peer_changes,
    dump as peer_dump, dump_package as peer_dump_package,
    main_finding_response_schema, parse_main_response,
)



_PENDING = "pending"
_HOOK_NAMES = (
    "before_main_attempt",
    "after_main_attempt",
    "before_validator",
    "after_validator",
    "before_review",
    "after_review",
    "before_file_collection",
    "after_file_collection",
    "before_cleanup",
    "after_cleanup",
    "before_timeout",
    "after_timeout",
    "before_cancel",
    "after_cancel",
)


def _validate_sync_callable(value, *, registry_name):
    if not callable(value):
        raise TypeError(f"{registry_name} must contain only callables")
    if inspect.iscoroutinefunction(value):
        raise TypeError(f"{registry_name} does not support async callables")
    return value


def _response_cost_incomplete_reasons(response, *, role):
    if response is None:
        return (f"{role} response unavailable",)
    reasons = []
    if response.usage is None:
        reasons.append(f"{role} response usage unavailable")
    if response.estimated_cost_usd is None:
        reasons.append(f"{role} response pricing unavailable")
    return tuple(reasons)


def _merge_usage(current, incoming):
    merged = dict(current)
    for key, value in (incoming or {}).items():
        merged[key] = merged.get(key, 0) + value
    return merged


def _merge_usage_by_model(current, incoming):
    merged = {
        model: dict(usage)
        for model, usage in current.items()
    }
    for model, usage in (incoming or {}).items():
        merged[model] = _merge_usage(merged.get(model, {}), usage)
    return merged


def _manager_results(manager):
    if manager is None:
        return ()
    results = getattr(manager, "results", None)
    return () if results is None else results()


def _manager_state_incomplete_reasons(manager):
    if manager is None:
        return ()
    reasons = []
    for turn in _manager_results(manager):
        status = turn.get("status")
        if status != "completed":
            reasons.append(
                f"{turn.get('target', 'subagent')} turn[{turn.get('turn_index', '?')}] "
                f"status is {status or 'unknown'}"
            )
    list_agents = getattr(manager, "list_agents", None)
    if list_agents is not None:
        for child in list_agents():
            if child.get("status") == "completed":
                continue
            if child.get("turns", 0):
                continue
            reasons.append(
                f"{child.get('target', 'subagent')} has no usage-bearing turn "
                f"and status is {child.get('status') or 'unknown'}"
            )
    return tuple(dict.fromkeys(reasons))


def _manager_usage_by_model(manager):
    usage_by_model = {}
    for turn in _manager_results(manager):
        model = turn.get("model")
        usage = turn.get("usage")
        if not isinstance(model, str) or not model.strip() or usage is None:
            continue
        usage_by_model[model] = _merge_usage(
            usage_by_model.get(model, {}),
            usage,
        )
    return usage_by_model


def _manager_cost_by_model(manager):
    cost_by_model = {}
    for turn in _manager_results(manager):
        model = turn.get("model")
        cost = turn.get("estimated_cost_usd")
        if not isinstance(model, str) or not model.strip() or cost is None:
            continue
        cost_by_model[model] = cost_by_model.get(model, 0.0) + cost
    return cost_by_model


def _exact_native_usage_by_model(response):
    records = () if response is None else getattr(response, "native_usage", ())
    if not records or any(
        not record.exact
        or record.usage is None
        or not isinstance(record.model, str)
        or not record.model.strip()
        for record in records
    ):
        return None
    usage_by_model = {}
    for record in records:
        model = record.model.strip()
        usage_by_model[model] = _merge_usage(
            usage_by_model.get(model, {}),
            record.usage,
        )
    return usage_by_model


def _exact_native_cost_by_model(response):
    usage_by_model = _exact_native_usage_by_model(response)
    if usage_by_model is None:
        return None
    cost_by_model = {}
    for model, usage in usage_by_model.items():
        cost = _estimate_cost_usd_for_usage(
            usage=usage,
            pricing=_resolve_model_pricing(model),
        )
        if cost is None:
            return None
        cost_by_model[model] = cost
    return cost_by_model


class _PendingRegistry(MutableSequence):
    """A list-like Job registry that becomes immutable when execution starts."""

    def __init__(self, owner, name, values=(), *, validator=None):
        self._owner = owner
        self._name = name
        self._validator = validator
        self._items = []
        self.extend(values)

    def __len__(self):
        return len(self._items)

    def __getitem__(self, index):
        return self._items[index]

    def __delitem__(self, index):
        self._mutate(lambda: self._items.__delitem__(index))

    def __setitem__(self, index, value):
        if isinstance(index, slice):
            values = [self._validate(item) for item in value]
            self._mutate(lambda: self._items.__setitem__(index, values))
        else:
            value = self._validate(value)
            self._mutate(lambda: self._items.__setitem__(index, value))

    def insert(self, index, value):
        value = self._validate(value)
        self._mutate(lambda: self._items.insert(index, value))

    def extend(self, values):
        values = [self._validate(value) for value in values]
        self._mutate(lambda: self._items.extend(values))

    def _mutate(self, operation):
        self._owner._mutate_registry(self._name, operation)

    def _validate(self, value):
        return value if self._validator is None else self._validator(value)

    def _snapshot(self):
        return tuple(self._items)


class _HookRegistries:
    """Namespace containing one pending registry for each lifecycle phase."""

    def __init__(self, owner):
        for name in _HOOK_NAMES:
            setattr(
                self,
                name,
                _PendingRegistry(
                    owner,
                    f"hooks.{name}",
                    validator=lambda value, phase=name: _validate_sync_callable(
                        value,
                        registry_name=f"hooks.{phase}",
                    ),
                ),
            )

    def _snapshot(self):
        return {
            name: getattr(self, name)._snapshot()
            for name in _HOOK_NAMES
        }



def _format_open_finding(record):
    """Give Main the finding ID and, when unresolved, why its last repair fell short."""
    text = f"{record['finding_id']}: {record['comment']}"
    if record.get("status") == "unresolved":
        text += f"\n  Reviewer (unresolved): {record['decision_comment']}"
    return text

class CodexJob(CodexSession):
    """A job definition that remains inert until explicitly started."""

    def __init__(
        self,
        *,
        config: SessionConfig,
        input,
        review=None,
        output_schema_path=None,
        output_pydantic_model=None,
        validation: ValidationConfig | None = None,
        files: FileCollectionConfig | None = None,
        logging: LoggingConfig | None = None,
        subagents: SubagentConfig | None = None,
        _conversation_state=None,
        _activity_actor=None,
        _rollout_dir=None,
    ) -> None:
        if not isinstance(config, SessionConfig):
            raise TypeError("config must be a SessionConfig")
        validation = validation or ValidationConfig()
        files = files or FileCollectionConfig()
        logging = logging or LoggingConfig()
        if not isinstance(validation, ValidationConfig):
            raise TypeError("validation must be a ValidationConfig or None")
        if not isinstance(files, FileCollectionConfig):
            raise TypeError("files must be a FileCollectionConfig or None")
        if not isinstance(logging, LoggingConfig):
            raise TypeError("logging must be a LoggingConfig or None")
        if subagents is not None and not isinstance(subagents, SubagentConfig):
            raise TypeError("subagents must be a SubagentConfig or None")
        if output_schema_path is not None and output_pydantic_model is not None:
            raise ValueError(
                "output_schema_path and output_pydantic_model are mutually exclusive"
            )
        if isinstance(input, (str, os.PathLike)):
            if isinstance(input, str) and not input:
                raise ValueError("input must be non-empty")
            stored_input = input
        else:
            try:
                stored_input = tuple(input)
            except TypeError as exc:
                raise ValueError(
                    "input must be a string, path, or iterable of strings and paths"
                ) from exc
            if not stored_input:
                raise ValueError("input must be non-empty")
        super().__init__(
            config=config,
            input=stored_input,
            output_schema_path=output_schema_path,
            output_pydantic_model=output_pydantic_model,
            logging=logging,
            _conversation_state=_conversation_state,
        )
        self.id = "codex_job_" + uuid.uuid4().hex
        self._lock = threading.RLock()
        self._status = _PENDING
        self._error = None
        self._claim_owner = None
        self._registry_snapshot = None
        self._borrowed_tool_bridge = None
        self._conversation_recorded = False
        self._activity_actor = _activity_actor
        self._rollout_dir = _rollout_dir
        self.subagents = subagents
        self._subagent_manager = (
            None
            if subagents is None
            else _DirectChildManager(
                parent_config=config,
                config=subagents,
                activity_callback=self._subagent_activity,
                delegation_callback=self._subagent_delegation,
                rollout_dir=self._subagent_rollout_dir,
            )
        )
        self.output_validators = _PendingRegistry(
            self,
            "output_validators",
            validator=lambda value: _validate_sync_callable(
                value,
                registry_name="output_validators",
            ),
        )
        self.tools = _PendingRegistry(
            self,
            "tools",
            () if config.tools is None else config.tools.tools,
            validator=self._validate_tool,
        )
        self.hooks = _HookRegistries(self)
        if output_pydantic_model is not None:
            self.output_validators.append(
                _make_pydantic_output_validator(output_pydantic_model)
            )
        if type(self).validate_output is not CodexJob.validate_output:
            self.output_validators.append(self.validate_output)
        for hook_name in _HOOK_NAMES:
            if getattr(type(self), hook_name) is not getattr(CodexJob, hook_name):
                getattr(self.hooks, hook_name).append(getattr(self, hook_name))
        workspace = config.workspace
        self._definition = {
            "input": stored_input,
            "read_paths": workspace.read_paths,
            "write_paths": workspace.write_paths,
            "subcwd_files": {
                path: copy.deepcopy(dict(content))
                for path, content in workspace.subcwd_files.items()
            },
            "review": review,
            "output_schema_path": output_schema_path,
            "output_pydantic_model": output_pydantic_model,
            "repair_validation_failures": validation.repair,
            "repair_prompt_path": validation.repair_prompt_path,
            "collect_files": files.enabled,
            "max_files": files.max_files,
            "max_file_bytes": files.max_file_bytes,
            "max_total_file_bytes": files.max_total_file_bytes,
            "inline_context": workspace.inline_context,
            "max_inline_context_bytes": workspace.max_inline_context_bytes,
            "max_inline_context_total_bytes": workspace.max_inline_context_total_bytes,
            "log_path": logging.path,
        }

    @property
    def status(self):
        runtime = getattr(self, "_runner_handle", None)
        return self._status if runtime is None else runtime.status

    @property
    def error(self):
        runtime = getattr(self, "_runner_handle", None)
        return self._error if runtime is None else runtime.error

    @property
    def response(self):
        runtime = getattr(self, "_runner_handle", None)
        if runtime is not None:
            return runtime.response
        return getattr(self, "_response", None) or getattr(self, "_last_response", None)

    @property
    def cwd(self):
        return self._value("cwd", None)

    @property
    def model_cwd(self):
        return self._value("model_cwd", None)

    @property
    def subcwd_path(self):
        return self._value("subcwd_path", None)

    @property
    def argv(self):
        runtime = getattr(self, "_runner_handle", None)
        argv = runtime.argv if runtime is not None else self.__dict__.get("_argv", ())
        return list(argv) if getattr(self, "_legacy_client_runtime", False) else tuple(argv)

    @property
    def log_path(self):
        return self._value("log_path", None)

    @property
    def current_attempt(self):
        return self._value("current_attempt", 0)

    @property
    def repair_index(self):
        return self._value("repair_index", 0)

    @property
    def stdout(self):
        return self._value("stdout", "")

    @property
    def stderr(self):
        return self._value("stderr", "")

    @property
    def returncode(self):
        return self._value("returncode", None)

    @property
    def activity_log(self):
        return self._value("activity_log", ())

    @property
    def subagent_results(self):
        manager = self._subagent_manager
        return () if manager is None else manager.results()

    @property
    def subagent_estimated_cost_usd(self):
        manager = self._subagent_manager
        return 0.0 if manager is None else manager.estimated_cost_usd()

    @property
    def subagent_cost_complete(self):
        manager = self._subagent_manager
        return True if manager is None else manager.cost_complete()

    @property
    def subagent_usage_incomplete_reasons(self):
        manager = self._subagent_manager
        if manager is None:
            return ()
        return tuple(dict.fromkeys(
            (*manager.usage_incomplete_reasons(), *_manager_state_incomplete_reasons(manager))
        ))

    @property
    def reviewer_subagent_results(self):
        manager = self._review_subagent_manager()
        return () if manager is None else manager.results()

    @property
    def reviewer_subagent_estimated_cost_usd(self):
        manager = self._review_subagent_manager()
        return 0.0 if manager is None else manager.estimated_cost_usd()

    @property
    def reviewer_subagent_cost_complete(self):
        manager = self._review_subagent_manager()
        return True if manager is None else manager.cost_complete()

    @property
    def reviewer_subagent_usage_incomplete_reasons(self):
        manager = self._review_subagent_manager()
        if manager is None:
            return ()
        return tuple(dict.fromkeys(
            (*manager.usage_incomplete_reasons(), *_manager_state_incomplete_reasons(manager))
        ))

    @property
    def total_usage(self):
        usage = {}
        for model_usage in self.total_usage_by_model.values():
            usage = _merge_usage(usage, model_usage)
        return usage

    @property
    def total_usage_by_model(self):
        # The turn ledger is the source of truth.  Raw app-server events are
        # best-effort telemetry and may omit peer forks or arrive late.
        usage_by_model = _merge_usage_by_model(
            {}, self.__dict__.get("_usage_by_model", {})
        )
        usage_by_model = _merge_usage_by_model(
            usage_by_model,
            _manager_usage_by_model(self._subagent_manager),
        )
        usage_by_model = _merge_usage_by_model(
            usage_by_model,
            _manager_usage_by_model(self._review_subagent_manager()),
        )
        return usage_by_model

    @property
    def total_cost_complete(self):
        return not self.total_usage_incomplete_reasons

    @property
    def total_usage_incomplete_reasons(self):
        reasons = list(_response_cost_incomplete_reasons(
            self._accounting_response(),
            role="main",
        ))
        reasons.extend(self.__dict__.get("_usage_incomplete_reasons", ()))
        review = self._definition["review"]
        if review is not None:
            reasons.extend(
                _response_cost_incomplete_reasons(
                    self.review_response,
                    role="reviewer",
                )
            )
        reasons.extend(self.subagent_usage_incomplete_reasons)
        reasons.extend(self.reviewer_subagent_usage_incomplete_reasons)
        if not self.subagent_cost_complete:
            reasons.append("main subagent cost is incomplete")
        if not self.reviewer_subagent_cost_complete:
            reasons.append("reviewer subagent cost is incomplete")
        return tuple(dict.fromkeys(reasons))

    @property
    def total_estimated_cost_usd(self):
        cost_by_model = self.total_estimated_cost_usd_by_model
        if cost_by_model is None:
            return None
        return sum(cost_by_model.values())

    @property
    def total_estimated_cost_usd_by_model(self):
        if not self.total_cost_complete:
            return None
        cost_by_model = dict(
            self.__dict__.get("_estimated_cost_usd_by_model", {})
        )
        for manager in (
            self._subagent_manager,
            self._review_subagent_manager(),
        ):
            for model, cost in _manager_cost_by_model(manager).items():
                cost_by_model[model] = cost_by_model.get(model, 0.0) + cost
        return cost_by_model

    def _accounting_response(self):
        error = self._error
        error_response = getattr(error, "response", None)
        return error_response if error_response is not None else self.response

    @property
    def review_passed(self):
        if hasattr(self, "_last_review_result"):
            result = self._last_review_result
            return None if result is None else result.passed
        return self._value("review_passed", None)

    @property
    def review_summary(self):
        if hasattr(self, "_last_review_result"):
            result = self._last_review_result
            return "" if result is None else result.summary
        return self._value("review_summary", "")

    @property
    def review_findings(self):
        if hasattr(self, "_last_review_result"):
            result = self._last_review_result
            return () if result is None else result.findings
        return self._value("review_findings", ())

    @property
    def review_response(self):
        if hasattr(self, "_last_review_result"):
            result = self._last_review_result
            return None if result is None else result.response
        return self._value("review_response", None)

    @property
    def review_stdout(self):
        if hasattr(self, "_last_review_result"):
            result = self._last_review_result
            return "" if result is None else result.stdout
        return self._value("review_stdout", "")

    @property
    def review_stderr(self):
        if hasattr(self, "_last_review_result"):
            result = self._last_review_result
            return "" if result is None else result.stderr
        return self._value("review_stderr", "")

    @property
    def review_argv(self):
        if hasattr(self, "_last_review_result"):
            result = self._last_review_result
            return () if result is None else tuple(result.argv)
        return self._value("review_argv", ())

    def done(self):
        runtime = getattr(self, "_runner_handle", None)
        if runtime is not None:
            return runtime.done()
        done = getattr(self, "_done", None)
        return (
            done.is_set()
            if done is not None
            else self._status in {_COMPLETED, _FAILED, _TIMEOUT, _CANCELLED}
        )

    def start(self) -> Self:
        return self._start_claimed(None)

    def _start_claimed(self, owner_token, *, tool_bridge=None) -> Self:
        with self._lock:
            if self._status != _PENDING:
                raise CodexAlreadyStartedError("codex job has already been started")
            if self._claim_owner is not None and self._claim_owner is not owner_token:
                raise CodexAlreadyStartedError("codex job is claimed by another batch")
            try:
                snapshot = self._freeze_registries_locked()
                if self._subagent_manager is not None:
                    self._subagent_manager.validate_permissions(
                        parent_tools=snapshot["tools"],
                    )
                    self._subagent_manager.activate(timeout=self.config.timeout)
            except BaseException as exc:
                self._status = _FAILED
                self._error = exc
                self._session_closed = True
                raise
            self._status = _RUNNING
            self._borrowed_tool_bridge = tool_bridge
        runner_kwargs = self.config._runner_kwargs()
        runner_kwargs["log_dir"] = self.logging.log_dir
        runner_kwargs["tools"] = snapshot["tools"]
        runner = _configure_runtime(self, **runner_kwargs)
        with self._session_lock:
            self._session_runner = runner
        try:
            runtime_kwargs = dict(
                self._definition,
                _hook_owner=self,
                _output_validators=snapshot["output_validators"],
                _hooks=snapshot["hooks"],
                _borrowed_tool_bridge=tool_bridge,
                _defer_background=True,
                _job_id=self.id,
                _conversation_state=self._conversation_state,
                _activity_actor=self._activity_actor,
                _rollout_dir=self._rollout_dir,
                _job_owner=self,
            )
            runtime = (
                _run_job_runtime(runner, **runtime_kwargs)
                if runner is self
                else runner.start(**runtime_kwargs)
            )
        except BaseException as exc:
            if self._subagent_manager is not None:
                self._subagent_manager.close()
            if tool_bridge is not None:
                with runner._tool_bridge_lock:
                    runner._tool_bridge = None
            if runner is self:
                _close_runtime(runner)
            else:
                runner.close()
            with self._lock:
                self._status = _FAILED
                self._error = exc
            raise
        # Third-party runners predating owner binding may still return a
        # private handle.  Keep this compatibility path out of the normal
        # SDK runner; new runners bind all state directly to this Job.
        if runtime is not self:
            self._runner_handle = runtime
            runtime.start_background()
            return self
        self.start_background()
        return self

    def _mutate_registry(self, name, operation):
        lock = getattr(self, "_lock", None)
        if lock is None:
            return operation()
        with lock:
            if self._status != _PENDING or self._registry_snapshot is not None:
                raise RuntimeError(
                    f"{name} cannot be changed after job execution has started"
                )
            return operation()

    @staticmethod
    def _validate_tool(value):
        if not isinstance(value, Tool):
            raise TypeError("tools must contain only Tool instances")
        return value

    def _freeze_registries_locked(self):
        if self._registry_snapshot is None:
            subagent_tools = (
                ()
                if self._subagent_manager is None
                else self._subagent_manager.tools()
            )
            tools = _normalize_tools(
                self.tools._snapshot() + subagent_tools
            )
            self._registry_snapshot = {
                "output_validators": self.output_validators._snapshot(),
                "tools": tools,
                "hooks": self.hooks._snapshot(),
            }
            if self._subagent_manager is not None:
                hooks = dict(self._registry_snapshot["hooks"])
                hooks["before_cleanup"] = (
                    hooks["before_cleanup"]
                    + (self._subagent_manager.close,)
                )
                self._registry_snapshot["hooks"] = hooks
        return self._registry_snapshot

    def _tool_bridge_spec(self):
        with self._lock:
            if "_registry_snapshot" not in self.__dict__:
                tool_config = self.config.tools
                return (
                    () if tool_config is None else tool_config.tools,
                    8 if tool_config is None else tool_config.max_concurrent_calls,
                    60.0 if tool_config is None else tool_config.timeout,
                )
            snapshot = self._freeze_registries_locked()
            tool_config = self.config.tools
            return (
                snapshot["tools"],
                8 if tool_config is None else tool_config.max_concurrent_calls,
                60.0 if tool_config is None else tool_config.timeout,
            )

    def run(self):
        return self.start().result()

    def validate_output(self, response: CodexResponse) -> None:
        """Validate one successful response.

        Subclasses may raise ``ValueError`` with a repair diagnostic.  Other
        exceptions are execution failures and are not converted to validation
        failures.
        """
        return None

    def result(self, timeout=None):
        status = self._status
        error = self._error
        legacy_runtime = getattr(self, "_runner_handle", None)
        if legacy_runtime is not None:
            try:
                return legacy_runtime.result(timeout=timeout)
            finally:
                if legacy_runtime.done():
                    self._close_job_session()
        if hasattr(self, "_done"):
            try:
                return self._runtime_result(timeout=timeout)
            finally:
                if self.done() and hasattr(self, "_session_lock"):
                    self._close_job_session()
        if status == _PENDING:
            raise CodexNotStartedError("codex job has not been started")
        if status == _CANCELLED:
            raise CodexCancelledError("codex job was cancelled")
        if error is not None:
            raise error
        raise CodexExecutionError("codex job failed")

    def cancel(self):
        manager = getattr(self, "_subagent_manager", None)
        if manager is not None:
            manager.close()
        legacy_runtime = getattr(self, "_runner_handle", None)
        if legacy_runtime is not None:
            legacy_runtime.cancel()
            self._close_job_session()
            return
        if not hasattr(self, "_done"):
            if self._status == _PENDING:
                self._status = _CANCELLED
                self._session_closed = True
            return
        self._runtime_cancel()
        self._close_job_session()

    def close(self):
        self.cancel()
        self._close_job_session()

    def _close_job_session(self):
        if not hasattr(self, "_session_lock"):
            return
        manager = getattr(self, "_subagent_manager", None)
        if manager is not None:
            manager.close()
        with self._session_lock:
            runner = self._session_runner
            self._session_runner = None
            self._session_closed = True
            if hasattr(self, "_done"):
                conversation_state = getattr(self, "_conversation_state", None)
                if conversation_state is not None:
                    self._conversation_state = conversation_state
                response = self.response
                if (
                    not self._conversation_recorded
                    and response is not None
                    and self._conversation_state is not None
                ):
                    input_text = (
                        self._definition["input"]
                        if isinstance(self._definition["input"], str)
                        else "\n".join(
                            str(value) for value in self._definition["input"]
                        )
                    )
                    self._conversation_state.record_turn(
                        input_text,
                        response.content,
                    )
                    self._conversation_recorded = True
        if runner is not None:
            borrowed_tool_bridge = self._borrowed_tool_bridge
            if borrowed_tool_bridge is not None:
                tool_bridge_lock = getattr(runner, "_tool_bridge_lock", None)
                if tool_bridge_lock is not None:
                    with tool_bridge_lock:
                        if runner._tool_bridge is borrowed_tool_bridge:
                            runner._tool_bridge = None
            try:
                if runner is self:
                    _close_runtime(runner)
                else:
                    runner.close()
            except Exception:
                pass

    def _subagent_activity(self, event, message):
        if hasattr(self, "_done"):
            self._log_activity("subagent", event, message)

    def _subagent_delegation(self, record, *, phase="subagent"):
        if not hasattr(self, "_done"):
            return
        self._log_subagent_delegation(
            {"parent_job_id": self.id, **record},
            phase=phase,
        )

    def _review_subagent_manager(self):
        if not hasattr(self, "_done"):
            return None
        getter = getattr(
            self,
            "_review_subagent_manager_getter",
            None,
        )
        return None if getter is None else getter()

    def _claim(self, owner_token):
        with self._lock:
            if self._status != _PENDING or self._claim_owner is not None:
                raise CodexAlreadyStartedError(
                    "only an unclaimed pending job can be submitted"
                )
            self._claim_owner = owner_token

    def _release_claim(self, owner_token):
        with self._lock:
            if self._status == _PENDING and self._claim_owner is owner_token:
                self._claim_owner = None

    def _cancel_claimed(self, owner_token):
        with self._lock:
            if self._claim_owner is not owner_token:
                return
        self.cancel()

    @classmethod
    def wait_any(cls, jobs, timeout=None):
        jobs = tuple(jobs)
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            for job in jobs:
                if job.done():
                    return job
            if deadline is not None and time.monotonic() >= deadline:
                return None
            time.sleep(0.05)

    def _value(self, name, pending_default):
        return self.__dict__.get(name, self.__dict__.get("_" + name, pending_default))

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        raise AttributeError(name)

    def _initialize_runtime(
        self,
        *,
        attempt,
        spawn_attempt,
        original_input,
        context_paths,
        context_sections,
        output_schema_section,
        output_model_source_section,
        read_paths,
        write_paths,
        subcwd_files,
        default_timeout=None,
        cwd=None,
        subcwd_path=None,
        ignore_global_context=False,
        review_config=None,
        review_runner=None,
        collect_files=False,
        max_files=_DEFAULT_MAX_FILES,
        max_file_bytes=_DEFAULT_MAX_FILE_BYTES,
        max_total_file_bytes=_DEFAULT_MAX_TOTAL_FILE_BYTES,
        output_validators=(),
        output_schema_path=None,
        repair_validation_failures=False,
        max_review_repairs=None,
        repair_prompt_template=None,
        log_dir=None,
        log_path=None,
        review_session_cleanup=None,
        main_session_cleanup=None,
        hook_owner=None,
        hooks=None,
        finish_callback=None,
        hook_no_args=False,
        job_id=None,
        native_usage_snapshot=None,
        activity_actor=None,
    ):
        self.id = job_id or ("codex_job_" + uuid.uuid4().hex)
        self._log_path = _prepare_job_log_path(log_dir, self.id, log_path=log_path)
        self._argv = list(attempt.argv)
        self._spawn_attempt = spawn_attempt
        self._original_input = original_input
        self._initial_main_context = attempt.prompt
        self._context_paths = list(context_paths)
        self._context_sections = list(context_sections)
        self._output_schema_section = output_schema_section
        self._output_model_source_section = output_model_source_section
        self._read_paths = list(read_paths)
        self._write_paths = list(write_paths)
        self._subcwd_files = list(subcwd_files)
        self._attempt = attempt
        self._process = attempt.process
        self._output_paths = [attempt.output_path]
        self._default_timeout = default_timeout
        self._cwd = pathlib.Path(cwd) if cwd is not None else None
        self._subcwd_path = pathlib.Path(subcwd_path) if subcwd_path else None
        self._ignore_global_context = ignore_global_context
        self._review_config = review_config
        self._review_runner = review_runner
        self._should_collect_files = collect_files
        self._max_files = max_files
        self._max_file_bytes = max_file_bytes
        self._max_total_file_bytes = max_total_file_bytes
        self._output_validators = tuple(output_validators)
        self._validate_output = (
            None if not self._output_validators else self._output_validators[-1]
        )
        self._output_schema_path = pathlib.Path(output_schema_path) if output_schema_path else None
        self._temp_output_schema_path = None
        self._repair_validation_failures = bool(repair_validation_failures)
        self._max_review_repairs = max_review_repairs
        self._repair_prompt_template = repair_prompt_template
        self._review_session_cleanup = review_session_cleanup
        self._main_session_cleanup = main_session_cleanup
        self._hook_owner = hook_owner
        self._hooks = None if hooks is None else {
            name: tuple(callbacks)
            for name, callbacks in hooks.items()
        }
        self._finish_callback = finish_callback
        self._hook_no_args = hook_no_args
        self._lock = threading.RLock()
        self._done = threading.Event()
        self._status = _RUNNING
        self._stdout = ""
        self._stderr = ""
        self._returncode = None
        self._response = None
        self._error = None
        self._last_response = None
        self._last_validation_result = None
        self._last_review_result = None
        self._peer_ledger = {}
        self._peer_findings = []
        self._peer_stalled = False
        self._peer_next_finding = 1
        self._peer_before = None
        self._peer_round = 0
        self._additional_usage = None
        self._additional_estimated_cost_usd = 0.0
        self._usage_by_model = {}
        self._estimated_cost_usd_by_model = {}
        self._usage_incomplete_reasons = []
        self._activity_log = []
        self._native_usage_snapshot = native_usage_snapshot
        self._activity_actor = activity_actor
        if hasattr(self._process, "set_activity_callback"):
            self._set_attempt_activity_callback(attempt)
        self._log_activity(
            "main_attempt",
            "start",
            "main attempt started",
            attempt=attempt.attempt_index,
            repair_index=attempt.repair_index,
            argv=attempt.argv,
        )
        self._thread = threading.Thread(
            target=self._communicate,
            name=f"{self.id}_communicate",
            daemon=True,
        )


    def start_background(self):
        """Start communication after the public job has attached this execution."""
        self._thread.start()

    def _runtime_result(self, timeout=None):
        if timeout is None:
            timeout = self._default_timeout
        if not self._done.wait(timeout):
            self._timeout()
            raise CodexTimeoutError(
                "codex job timed out", returncode=None, stdout=self._stdout,
                stderr=self._stderr, argv=self.argv,
                activity_log=self._activity_log_snapshot(), log_path=self.log_path,
            )
        with self._lock:
            if self._status == _COMPLETED:
                return self._response
            if self._status == _CANCELLED:
                raise CodexCancelledError("codex job was cancelled")
            if self._error is not None:
                raise self._error
            raise CodexExecutionError("codex job failed")

    def _runtime_cancel(self):
        with self._lock:
            if self._done.is_set():
                return
            self._status = _CANCELLED
        self._invoke_hook("cancel", "before", raise_errors=False)
        self._log_activity("cancel", "start", "job cancellation requested")
        self._terminate_process("cancel")
        self._invoke_hook("cancel", "after", raise_errors=False)
        self._done.wait(1)

    def _timeout(self):
        with self._lock:
            if self._done.is_set():
                return
            self._status = _TIMEOUT
        self._invoke_hook("timeout", "before", raise_errors=False)
        self._log_activity("timeout", "start", "job timeout requested")
        self._terminate_process("timeout")
        self._invoke_hook("timeout", "after", raise_errors=False)
        self._done.wait(1)

    def _communicate(self):
        attempt = self._attempt
        repairs = 0
        repairs_by_stage = {}
        usage_totals = None
        try:
            while True:
                with self._lock:
                    if self._status in (_TIMEOUT, _CANCELLED):
                        raise CodexReviewProtocolError("peer review stopped", round_index=self._peer_round, log_path=self.log_path)
                self._invoke_hook("main_attempt", "before")
                stdout, stderr, returncode, usage = self._run_attempt(attempt)
                usage_totals = _merge_usage_totals(usage_totals, usage)
                model = self._resolved_model_name(attempt.argv)
                estimated_attempt_cost = _estimate_cost_usd_for_usage(
                    usage=usage,
                    pricing=_resolve_model_pricing(model),
                )
                self._record_usage_observation(
                    role=(
                        f"main attempt[{attempt.attempt_index}]"
                        f" repair[{attempt.repair_index}]"
                    ),
                    model=model,
                    usage=usage,
                    estimated_cost_usd=estimated_attempt_cost,
                )
                with self._lock:
                    self._stdout = stdout or ""
                    self._stderr = stderr or ""
                    self._returncode = returncode

                    if self._status in (_TIMEOUT, _CANCELLED):
                        self._finish()
                        return

                if returncode != 0:
                    self._log_activity(
                        "main_attempt",
                        "end",
                        f"main attempt exited with status {returncode}",
                        attempt=attempt.attempt_index,
                        repair_index=attempt.repair_index,
                        argv=attempt.argv,
                        returncode=returncode,
                        stdout=stdout or "",
                        stderr=stderr or "",
                    )
                    self._invoke_hook("main_attempt", "after")
                    self._set_execution_error(returncode)
                    self._finish()
                    return
                self._log_activity(
                    "main_attempt",
                    "end",
                    "main attempt completed successfully",
                    attempt=attempt.attempt_index,
                    repair_index=attempt.repair_index,
                    argv=attempt.argv,
                    returncode=returncode,
                    stdout=stdout or "",
                    stderr=stderr or "",
                )
                self._log_usage_activity(
                    phase="main_attempt",
                    model=model,
                    usage=usage,
                    attempt=attempt.attempt_index,
                    repair_index=attempt.repair_index,
                )
                content = self._read_output_file(attempt.output_path)
                pricing = _resolve_model_pricing(self._resolved_model_name(attempt.argv))
                estimated_cost_usd = _estimate_cost_usd_for_usage(
                    usage=_copy_optional_usage(usage_totals),
                    pricing=pricing,
                )
                last_response = CodexResponse(
                    content=content,
                    returncode=returncode,
                    stdout=stdout or "",
                    stderr=stderr or "",
                    argv=tuple(attempt.argv),
                    usage=_copy_optional_usage(usage_totals),
                    pricing=_copy_optional_pricing(pricing),
                    estimated_cost_usd=estimated_cost_usd,
                    files={},
                    activity_log=(),
                    log_path=self.log_path,
                )
                with self._lock:
                    self._last_response = last_response
                self._invoke_hook("main_attempt", "after")

                if self._output_validators:
                    outcome = self._validation_outcome(
                        self._run_validator(
                            last_response,
                            attempt_index=attempt.attempt_index,
                            repair_index=attempt.repair_index,
                        )
                    )
                    with self._lock:
                        if self._status in (_TIMEOUT, _CANCELLED):
                            self._finish()
                            return
                    if not outcome.success:
                        attempt, repairs = self._schedule_repair(
                            outcome=outcome,
                            response=last_response,
                            attempt=attempt,
                            repairs=repairs,
                            repairs_by_stage=repairs_by_stage,
                        )
                        if attempt is None:
                            self._finish()
                            return
                        continue

                if self._review_config is None:
                    self._set_success(last_response)
                    self._finish()
                    return

                outcome = self._review_outcome(
                    self._run_peer_review(
                        response=last_response,
                        main_context=self._initial_main_context,
                        attempt_index=attempt.attempt_index,
                        repair_index=attempt.repair_index,
                    )
                )
                with self._lock:
                    if self._status in (_TIMEOUT, _CANCELLED):
                        self._finish()
                        return

                if outcome.success:
                    self._set_success(last_response)
                    self._finish()
                    return
                attempt, repairs = self._schedule_repair(
                    outcome=outcome,
                    response=last_response,
                    attempt=attempt,
                    repairs=repairs,
                    repairs_by_stage=repairs_by_stage,
                )
                if attempt is None:
                    self._finish()
                    return
        except (CodexExecutionError, CodexValidationError) as exc:
            self._attach_activity_log(exc)
            with self._lock:
                if self._status == _RUNNING:
                    self._status = _FAILED
                self._error = exc
            self._finish()
            return
        except Exception as exc:
            self._log_activity(
                "failure",
                "error",
                f"unexpected wrapper communication failure: {exc}",
                stdout=self._stdout,
                stderr=self._stderr,
                argv=self.argv,
            )
            with self._lock:
                if self._status == _RUNNING:
                    self._status = _FAILED
                self._error = CodexExecutionError(
                    f"codex job communication failed: {exc}",
                    returncode=None,
                    stdout=self._stdout,
                    stderr=self._stderr,
                    argv=self.argv,
                    activity_log=self._activity_log_snapshot(),
                    log_path=self.log_path,
                )
            self._finish()
            return

    def _run_attempt(self, attempt):
        stdout, stderr = attempt.process.communicate(attempt.prompt)
        if not getattr(attempt.process, "emitted_stdout_lines", lambda: False)():
            self._record_stream_events_from_stdout(
                stdout or "",
                attempt=attempt.attempt_index,
                repair_index=attempt.repair_index,
            )
        return (
            stdout,
            stderr,
            attempt.process.returncode,
            _extract_usage_from_stdout(stdout or ""),
        )

    def _set_success(self, response):
        native_usage = () if self._native_usage_snapshot is None else self._native_usage_snapshot()
        incomplete = [record for record in native_usage if not record.exact]
        if native_usage:
            self._log_activity("native_usage", "usage", f"responses={len(native_usage)} incomplete={len(incomplete)}")
        for record in incomplete:
            self._log_activity("native_usage", "usage", f"incomplete {record.role} usage response={record.response_id} thread={record.thread_id} turn={record.turn_id} model={record.model}")
        response = self._with_additional_usage(response)
        response = replace(
            response,
            native_usage=native_usage,
            usage_cost_source="runner_ledger",
        )
        files, collection_error = self._collect_output_files()
        with self._lock:
            if collection_error is None:
                self._response = CodexResponse(
                    content=response.content,
                    returncode=response.returncode,
                    stdout=response.stdout,
                    stderr=response.stderr,
                    argv=tuple(response.argv),
                    usage=_copy_optional_usage(response.usage),
                    pricing=_copy_optional_pricing(response.pricing),
                    estimated_cost_usd=response.estimated_cost_usd,
                    native_usage=tuple(response.native_usage),
                    usage_cost_source=response.usage_cost_source,
                    files=files,
                    activity_log=tuple(self._activity_log_snapshot()),
                    log_path=self.log_path,
                )
                self._status = _COMPLETED
            else:
                self._status = _FAILED
                self._error = CodexExecutionError(
                    f"codex job file collection failed: {collection_error}",
                    returncode=response.returncode,
                    stdout=self._stdout,
                    stderr=self._stderr,
                    argv=self.argv,
                    activity_log=self._activity_log_snapshot(),
                    log_path=self.log_path,
                )

    def _set_execution_error(self, returncode):
        files, collection_error = self._collect_output_files()
        message = f"codex job exited with status {returncode}"
        if collection_error is not None:
            message += f"; file collection failed: {collection_error}"
        if self._stderr:
            message += f"\nstderr:\n{self._stderr}"
        if self._stdout:
            message += f"\nstdout:\n{self._stdout}"
        with self._lock:
            self._status = _FAILED
            self._error = CodexExecutionError(
                message,
                returncode=returncode,
                stdout=self._stdout,
                stderr=self._stderr,
                argv=self.argv,
                files=files,
                activity_log=self._activity_log_snapshot(),
                log_path=self.log_path,
            )

    def _set_validation_error(self, *, response, validation, attempts, repairs):
        response = self._with_additional_usage(response)
        files, collection_error = self._collect_output_files()
        if collection_error is not None:
            with self._lock:
                self._status = _FAILED
                self._error = CodexExecutionError(
                    f"codex job file collection failed: {collection_error}",
                    returncode=response.returncode,
                    stdout=self._stdout,
                    stderr=self._stderr,
                    argv=self.argv,
                )
            return
        response = CodexResponse(
            content=response.content,
            returncode=response.returncode,
            stdout=response.stdout,
            stderr=response.stderr,
            argv=tuple(response.argv),
            usage=_copy_optional_usage(response.usage),
            pricing=_copy_optional_pricing(response.pricing),
            estimated_cost_usd=response.estimated_cost_usd,
            files=files,
            activity_log=tuple(self._activity_log_snapshot()),
            log_path=self.log_path,
        )
        message = "codex output validation failed"
        if validation.diagnostic:
            message += f": {validation.diagnostic}"
        with self._lock:
            self._status = _FAILED
            self._error = CodexValidationError(
                message,
                response=response,
                validation_diagnostic=validation.diagnostic,
                attempts=attempts,
                repairs=repairs,
                files=files,
                activity_log=self._activity_log_snapshot(),
                log_path=self.log_path,
            )

    def _set_review_error(self, *, response, review, attempts, repairs):
        response = self._with_additional_usage(response)
        files, collection_error = self._collect_output_files()
        if collection_error is not None:
            with self._lock:
                self._status = _FAILED
                self._error = CodexExecutionError(
                    f"codex job file collection failed: {collection_error}",
                    returncode=response.returncode,
                    stdout=self._stdout,
                    stderr=self._stderr,
                    argv=self.argv,
                )
            return
        response = CodexResponse(
            content=response.content,
            returncode=response.returncode,
            stdout=response.stdout,
            stderr=response.stderr,
            argv=tuple(response.argv),
            usage=_copy_optional_usage(response.usage),
            pricing=_copy_optional_pricing(response.pricing),
            estimated_cost_usd=response.estimated_cost_usd,
            files=files,
            activity_log=tuple(self._activity_log_snapshot()),
            log_path=self.log_path,
        )
        with self._lock:
            self._status = _FAILED
            self._error = CodexValidationError(
                _review_failure_message(review),
                response=response,
                attempts=attempts,
                repairs=repairs,
                files=files,
                failure_stage="review",
                review_response=review.response,
                review_stdout=review.stdout,
                review_stderr=review.stderr,
                review_argv=review.argv,
                activity_log=self._activity_log_snapshot(),
                log_path=self.log_path,
            )

    def _collect_output_files(self):
        if not self._should_collect_files or self._subcwd_path is None:
            return {}, None
        self._invoke_hook("file_collection", "before")
        self._log_activity("file_collection", "start", "file collection started")
        try:
            files = _collect_subcwd_files(
                self._subcwd_path,
                max_files=self._max_files,
                max_file_bytes=self._max_file_bytes,
                max_total_file_bytes=self._max_total_file_bytes,
            )
            total_bytes = sum(len(file.bytes) for file in files.values())
            self._log_activity(
                "file_collection",
                "end",
                f"file collection finished: files={len(files)} bytes={total_bytes}",
            )
            return files, None
        except Exception as exc:
            self._log_activity(
                "file_collection",
                "error",
                f"file collection failed: {exc}",
                stderr=str(exc),
            )
            return {}, exc
        finally:
            self._invoke_hook("file_collection", "after")

    def _run_validator(self, response, *, attempt_index, repair_index):
        with self._lock:
            self._last_validation_result = None
        self._invoke_hook("validator", "before")
        self._log_activity(
            "validator",
            "start",
            "validator started",
            attempt=attempt_index,
            repair_index=repair_index,
        )
        result = None
        try:
            try:
                for validator in self._output_validators:
                    validator(response)
                result = _ValidationResult(
                    success=True,
                )
                return result
            except ValueError as exc:
                result = _ValidationResult(
                    success=False,
                    diagnostic=str(exc) or "output validation failed",
                )
                return result
        finally:
            if result is not None:
                with self._lock:
                    self._last_validation_result = result
            self._log_activity(
                "validator",
                "end",
                "validator finished",
                attempt=attempt_index,
                repair_index=repair_index,
            )
            if result is not None and result.diagnostic:
                self._log_activity(
                    "validator",
                    "detail",
                    f"validation diagnostic:\n{result.diagnostic.rstrip()}",
                    attempt=attempt_index,
                    repair_index=repair_index,
                    detail=result.diagnostic,
                )
            self._invoke_hook("validator", "after")

    def _validation_outcome(self, validation):
        return _ValidationStepOutcome(
            success=validation.success,
            failure_stage="validator",
            repair_limit=None,
            validation=validation,
        )

    def _review_outcome(self, review):
        return _ValidationStepOutcome(
            success=review.passed,
            failure_stage="review",
            repair_limit=0 if self._peer_stalled else self._max_review_repairs,
            review=review,
        )

    def _schedule_repair(
        self,
        *,
        outcome,
        response,
        attempt,
        repairs,
        repairs_by_stage,
    ):
        source_repairs = repairs_by_stage.get(outcome.failure_stage, 0)
        if (
            (outcome.failure_stage == "validator" and not self._repair_validation_failures)
            or (outcome.repair_limit is not None and source_repairs >= outcome.repair_limit)
        ):
            outcome.set_failure(
                self,
                response=response,
                attempts=attempt.attempt_index + 1,
                repairs=repairs,
            )
            return None, repairs

        if outcome.failure_stage == "review":
            try:
                self._peer_before = capture_artifacts(
                    cwd=self._cwd, paths=self._review_config.artifact_paths,
                    content=response.content, initial=False,
                )
            except CodexReviewProtocolError as exc:
                raise self._protocol_error(exc, response) from exc
        repairs += 1
        repairs_by_stage[outcome.failure_stage] = source_repairs + 1
        prompt = self._build_repair_prompt(
            response=response,
            outcome=outcome,
            attempt_index=attempt.attempt_index + 1,
            repair_index=repairs,
        )
        self._log_repair_prompt_activity(
            prompt,
            failure_stage=outcome.failure_stage,
            attempt=attempt.attempt_index + 1,
            repair_index=repairs,
        )
        with self._lock:
            if self._status in (_TIMEOUT, _CANCELLED):
                return None, repairs
        next_attempt = self._spawn_attempt(
            prompt=prompt,
            attempt_index=attempt.attempt_index + 1,
            repair_index=repairs,
        )
        self._set_attempt_activity_callback(next_attempt)
        self._log_activity(
            "main_attempt",
            "start",
            "repair main attempt started",
            attempt=next_attempt.attempt_index,
            repair_index=next_attempt.repair_index,
            argv=next_attempt.argv,
        )
        with self._lock:
            if self._status in (_TIMEOUT, _CANCELLED):
                self._output_paths.append(next_attempt.output_path)
                self._terminate_one_process(next_attempt.process)
                return None, repairs
            self._argv = list(next_attempt.argv)
            self._attempt = next_attempt
            self._process = next_attempt.process
            self._output_paths.append(next_attempt.output_path)
        return next_attempt, repairs

    def _run_review(self, *, response, main_context, attempt_index, repair_index, protocol=None):
        if self._review_runner is None or self._review_config is None:
            raise CodexExecutionError(
                "codex review is not configured",
                returncode=None,
                stdout=self._stdout,
                stderr=self._stderr,
                argv=self.argv,
            )
        with self._lock:
            self._last_review_result = None
        self._invoke_hook("review", "before")
        self._log_activity(
            "review",
            "start",
            "review started",
            attempt=attempt_index,
            repair_index=repair_index,
        )
        self._log_activity(
            "review",
            "reviewed_response",
            _format_review_response_for_log(response.content),
            attempt=attempt_index,
            repair_index=repair_index,
        )
        try:
            try:
                result = self._review_runner(
                    original_input=self._original_input,
                    main_context=main_context,
                    response=response,
                    subcwd_path=self._subcwd_path,
                    review_config=self._review_config,
                    attempt_index=attempt_index,
                    repair_index=repair_index,
                    stream_callback=lambda line: self._record_stream_event_line(
                        line,
                        detail="review",
                        attempt=attempt_index,
                        repair_index=repair_index,
                    ),
                    protocol=protocol,
                )
            except CodexExecutionError as exc:
                self._log_activity(
                    "review",
                    "error",
                    f"review failed: {exc.message}",
                    attempt=attempt_index,
                    repair_index=repair_index,
                    argv=exc.argv,
                    returncode=exc.returncode,
                    stdout=exc.stdout,
                    stderr=exc.stderr,
                )
                raise
            except Exception as exc:
                self._log_activity(
                    "review",
                    "error",
                    f"review failed before producing a result: {exc}",
                    attempt=attempt_index,
                    repair_index=repair_index,
                )
                raise
            if result.stdout and not result.stream_emitted:
                self._record_stream_events_from_stdout(
                    result.stdout,
                    detail="review",
                    attempt=attempt_index,
                    repair_index=repair_index,
                )
            if result.input_summary:
                self._log_activity(
                    "review",
                    "input",
                    result.input_summary,
                    attempt=attempt_index,
                    repair_index=repair_index,
                )
            if result.read_paths:
                self._log_activity(
                    "review",
                    "detail",
                    _format_review_read_paths_for_log(
                        result.read_paths,
                        subcwd_path=self._subcwd_path,
                    ),
                    attempt=attempt_index,
                    repair_index=repair_index,
                )
            for correction_index, (invalid_response, diagnostic) in enumerate(
                result.decision_validation_failures,
                start=1,
            ):
                self._log_activity(
                    "review",
                    "invalid",
                    "reviewer decision validation failed",
                    attempt=attempt_index,
                    repair_index=repair_index,
                    detail=diagnostic,
                    argv=invalid_response.argv,
                    returncode=invalid_response.returncode,
                    stdout=invalid_response.stdout,
                    stderr=invalid_response.stderr,
                )
                self._log_activity(
                    "review",
                    "regenerate",
                    f"reviewer decision correction turn started: {correction_index}",
                    attempt=attempt_index,
                    repair_index=repair_index,
                )
                self._log_usage_activity(
                    phase="review",
                    model=_response_model_name(invalid_response),
                    usage=invalid_response.usage,
                    attempt=attempt_index,
                    repair_index=repair_index,
                )
                self._record_additional_usage(
                    usage=invalid_response.usage,
                    estimated_cost_usd=getattr(
                        invalid_response,
                        "estimated_cost_usd",
                        None,
                    ),
                    model=_response_model_name(invalid_response),
                    role=(
                        f"reviewer decision correction[{correction_index}] "
                        f"attempt[{attempt_index}] repair[{repair_index}]"
                    ),
                )
            self._log_activity(
                "review",
                "end",
                "review finished",
                attempt=attempt_index,
                repair_index=repair_index,
                argv=result.argv,
                returncode=result.response.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
            )
            self._log_usage_activity(
                phase="review",
                model=_response_model_name(result.response),
                usage=result.response.usage,
                attempt=attempt_index,
                repair_index=repair_index,
            )
            self._record_additional_usage(
                usage=result.response.usage,
                estimated_cost_usd=getattr(
                    result.response,
                    "estimated_cost_usd",
                    None,
                ),
                model=_response_model_name(result.response),
                role=(
                    f"reviewer attempt[{attempt_index}]"
                    f" repair[{repair_index}]"
                ),
            )
            if isinstance(result.summary, str) and result.summary.strip():
                self._log_activity(
                    "review",
                    "detail",
                    f"review summary: {result.summary.strip()}",
                    attempt=attempt_index,
                    repair_index=repair_index,
                )
            if not result.passed and result.findings:
                self._log_activity(
                    "review",
                    "detail",
                    "review findings:\n- " + "\n- ".join(result.findings),
                    attempt=attempt_index,
                    repair_index=repair_index,
                )
            with self._lock:
                self._last_review_result = result
            return result
        finally:
            self._invoke_hook("review", "after")

    def _subagent_rollout_dir(self):
        """Keep child conversations under this job so one directory holds the whole job."""
        root = self._rollout_dir
        if root is None and self.log_path is not None:
            root = pathlib.Path(self.log_path).parent / self.id / "rollouts"
        return None if root is None else root / "subagents"

    def _peer_review_root(self):
        return pathlib.Path(self.log_path).parent / self.id / "review"

    def _peer_review_dir(self, round_index):
        return self._peer_review_root() / f"round-{round_index:03d}"

    def _run_peer_review(self, *, response, main_context, attempt_index, repair_index):
        """Run a protocol round; the runner, not either model, owns IDs and diffs."""
        initial = self._peer_round == 0
        try:
            after = capture_artifacts(cwd=self._cwd, paths=self._review_config.artifact_paths,
                                      content=response.content, initial=initial)
        except CodexReviewProtocolError as exc:
            raise self._protocol_error(exc, response) from exc
        directory = self._peer_review_dir(self._peer_round)
        open_findings = [{"finding_id": key, "comment": value} for key, value in self._peer_ledger.items()]
        if initial:
            self._log_activity("review", "detail", f"peer review artifacts: {self._peer_review_root()}",
                               attempt=attempt_index, repair_index=repair_index)
            peer_dump(directory / "initial-response.txt", response.content)
            peer_dump(directory / "after-manifest.json", after.manifest)
            change_list, finding_responses = [], []
        else:
            before = self._peer_before
            if before is None:
                raise CodexReviewProtocolError("review repair is missing its pre-repair artifacts", round_index=self._peer_round, log_path=self.log_path)
            change_list = peer_changes(before, after)
            peer_dump(directory / "before-manifest.json", before.manifest)
            peer_dump(directory / "after-manifest.json", after.manifest)
            finding_responses = self._run_finding_response_turn(
                open_findings=open_findings, directory=directory,
                attempt_index=attempt_index, repair_index=repair_index,
            )
            try:
                stable = capture_artifacts(cwd=self._cwd, paths=self._review_config.artifact_paths,
                                           content=response.content, initial=False)
            except CodexReviewProtocolError as exc:
                raise self._protocol_error(exc, response) from exc
            if stable != after:
                raise CodexReviewProtocolError("finding-response turn changed the final artifacts", round_index=self._peer_round, log_path=self.log_path)
        package = {
            "schema_version": PEER_SCHEMA_VERSION,
            "round": self._peer_round,
            "open_findings": open_findings,
            "changes": change_list,
            "finding_responses": finding_responses,
        }
        peer_dump_package(directory / "review-package.json", package)
        protocol = {
            "initial": initial,
            "open_findings": open_findings,
            "package": package,
            "response_content": response.content,
            "artifact_files": sorted(after.files),
            "review_dir": directory,
        }
        try:
            result = self._run_review(response=response, main_context=main_context,
                                      attempt_index=attempt_index, repair_index=repair_index,
                                      protocol=protocol)
        except CodexReviewProtocolError as exc:
            raise self._protocol_error(exc, response) from exc
        decision = result.decision
        if decision is None:
            raise CodexReviewProtocolError("reviewer did not return a protocol decision", round_index=self._peer_round, log_path=self.log_path)
        peer_dump(directory / "reviewer-decision.json", decision)
        records = {item["finding_id"]: item for item in self._peer_findings}
        for item in decision["decisions"]:
            record = records[item["finding_id"]]
            record["status"] = item["resolution"]
            record["decision_comment"] = item["comment"]
            record["unresolved_streak"] = record.get("unresolved_streak", 0) + 1 if item["resolution"] == "unresolved" else 0
            if item["resolution"] != "unresolved": self._peer_ledger.pop(item["finding_id"], None)
        for item in decision["new_findings"]:
            fid = f"F{self._peer_next_finding:03d}"; self._peer_next_finding += 1
            self._peer_ledger[fid] = item["comment"]
            self._peer_findings.append({"finding_id": fid, "comment": item["comment"],
                                        "opened_round": self._peer_round, "status": "open"})
        peer_dump(self._peer_review_root() / "finding-ledger.json", self._peer_findings)
        self._peer_round += 1
        open_records = [item for item in self._peer_findings if item["finding_id"] in self._peer_ledger]
        # A finding that survives consecutive repairs despite reviewer feedback means no convergence.
        stalled = [item["finding_id"] for item in open_records
                   if item.get("unresolved_streak", 0) >= MAX_CONSECUTIVE_UNRESOLVED]
        self._peer_stalled = bool(stalled)
        if not self._peer_ledger:
            summary = "accepted"
        elif stalled:
            summary = f"peer review did not converge: {', '.join(stalled)} unresolved in {MAX_CONSECUTIVE_UNRESOLVED} consecutive rounds"
        else:
            summary = "open peer-review findings"
        result = replace(result, passed=not self._peer_ledger,
                         findings=tuple(_format_open_finding(item) for item in open_records),
                         summary=summary)
        self._last_review_result = result
        return result

    def _run_finding_response_turn(self, *, open_findings, directory, attempt_index, repair_index):
        """Ask Main to answer every open finding; this turn never owns changes or content."""
        open_ids = tuple(item["finding_id"] for item in open_findings)
        base_prompt = (
            "Do not edit any file in this turn. Return only a JSON object with finding_responses. "
            "Answer every open finding exactly once with disposition addressed, not_addressed, "
            "or disputed and a non-empty response.\n"
            + json.dumps({"open_findings": open_findings}, ensure_ascii=False)
        )
        prompt = base_prompt
        descriptor, schema_name = tempfile.mkstemp(prefix="codex_job_runner_finding_response_", suffix=".json")
        schema_path = pathlib.Path(schema_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as schema_file:
                json.dump(main_finding_response_schema(open_ids=open_ids), schema_file, ensure_ascii=False)
            for correction in range(_PEER_MAX_CORRECTIONS + 1):
                turn = self._spawn_attempt(prompt=prompt, attempt_index=attempt_index,
                                           repair_index=repair_index,
                                           turn_output_schema_path=schema_path)
                self._log_activity("finding_response", "start", "main finding-response turn started", attempt=attempt_index, repair_index=repair_index)
                stdout, stderr, returncode, usage = self._run_attempt(turn)
                if returncode != 0:
                    raise CodexReviewProtocolError("main finding-response turn failed", round_index=self._peer_round,
                                                   stdout=stdout, stderr=stderr, argv=turn.argv, log_path=self.log_path)
                content = self._read_output_file(turn.output_path)
                model = self._resolved_model_name(turn.argv)
                self._record_additional_usage(usage=usage, estimated_cost_usd=_estimate_cost_usd_for_usage(usage=usage, pricing=_resolve_model_pricing(model)), model=model, role=f"main finding response round[{self._peer_round}] correction[{correction}]")
                try:
                    return parse_main_response(content, open_ids=open_ids)
                except CodexReviewProtocolError as exc:
                    peer_dump(directory / f"main-finding-response-attempt-{correction:02d}.txt", content or "")
                    peer_dump(directory / f"main-finding-response-attempt-{correction:02d}.diagnostic.txt", exc.message)
                    self._log_activity("finding_response", "invalid", exc.message, attempt=attempt_index, repair_index=repair_index)
                    prompt = "Your prior finding response was invalid: " + exc.message + "\nReturn a corrected JSON object only.\n" + base_prompt
            raise CodexReviewProtocolError(
                f"main finding response remained invalid after {_PEER_MAX_CORRECTIONS} corrections",
                round_index=self._peer_round, log_path=self.log_path,
            )
        finally:
            try:
                schema_path.unlink()
            except FileNotFoundError:
                pass

    def _protocol_error(self, exc, response):
        return CodexReviewProtocolError(exc.message, failure_stage="review", round_index=self._peer_round,
                                        diagnostic=exc.diagnostic or exc.message,
                                        review_artifact_paths=tuple(map(str, self._review_config.artifact_paths)),
                                        response=response, log_path=self.log_path,
                                        stdout=getattr(exc, "stdout", ""), stderr=getattr(exc, "stderr", ""), argv=getattr(exc, "argv", ()))

    def _resolved_model_name(self, argv):
        for index, token in enumerate(argv):
            if token == "--model" and index + 1 < len(argv):
                model = argv[index + 1]
                if isinstance(model, str) and model.strip():
                    return model.strip()
        return self._infer_default_model()

    def _infer_default_model(self):
        attempt = getattr(self, "_attempt", None)
        if attempt is not None:
            for index, token in enumerate(attempt.argv):
                if token == "--model" and index + 1 < len(attempt.argv):
                    model = attempt.argv[index + 1]
                    if isinstance(model, str) and model.strip():
                        return model.strip()
        return _DEFAULT_MODEL

    def _log_usage_activity(self, *, phase, model, usage, attempt=None, repair_index=None):
        if usage is None:
            return
        self._log_activity(
            phase,
            "usage",
            _format_usage_summary(model=model, usage=usage),
            attempt=attempt,
            repair_index=repair_index,
        )

    def _record_additional_usage(
        self,
        *,
        usage,
        estimated_cost_usd=None,
        model=None,
        role="additional response",
    ):
        with self._lock:
            self._additional_usage = _merge_usage_totals(
                self._additional_usage,
                usage,
            )
            if estimated_cost_usd is not None:
                self._additional_estimated_cost_usd += estimated_cost_usd
        self._record_usage_observation(
            role=role,
            model=model,
            usage=usage,
            estimated_cost_usd=estimated_cost_usd,
        )

    def _record_usage_observation(
        self,
        *,
        role,
        model,
        usage,
        estimated_cost_usd=None,
    ):
        reasons = []
        if usage is None:
            reasons.append(f"{role} usage unavailable")
        if usage is not None and (
            not isinstance(model, str) or not model.strip()
        ):
            reasons.append(f"{role} model attribution unavailable")
        if usage is not None and estimated_cost_usd is None:
            reasons.append(f"{role} pricing unavailable")
        if reasons:
            with self._lock:
                self._usage_incomplete_reasons.extend(reasons)
        if usage is None or not isinstance(model, str) or not model.strip():
            return
        model = model.strip()
        with self._lock:
            self._usage_by_model[model] = _merge_usage_totals(
                self._usage_by_model.get(model),
                usage,
            )
            if estimated_cost_usd is not None:
                self._estimated_cost_usd_by_model[model] = (
                    self._estimated_cost_usd_by_model.get(model, 0.0)
                    + estimated_cost_usd
                )

    def _with_additional_usage(self, response):
        with self._lock:
            additional_usage = _copy_optional_usage(self._additional_usage)
            additional_cost = self._additional_estimated_cost_usd
        if additional_usage is None and additional_cost == 0:
            return response
        estimated_cost_usd = response.estimated_cost_usd
        if additional_cost:
            estimated_cost_usd = (estimated_cost_usd or 0.0) + additional_cost
        return replace(
            response,
            usage=_merge_usage_totals(response.usage, additional_usage),
            estimated_cost_usd=estimated_cost_usd,
        )

    def _build_repair_prompt(
        self,
        *,
        response,
        outcome=None,
        validation=None,
        review=None,
        attempt_index,
        repair_index,
    ):
        if outcome is None:
            outcome = _ValidationStepOutcome(
                success=False,
                failure_stage="review" if review is not None else "validator",
                repair_limit=(
                    self._max_review_repairs if review is not None else None
                ),
                validation=validation,
                review=review,
            )
        values = {
            "original_input": self._original_input,
            "attempt": str(attempt_index),
            "repair_attempt": str(repair_index),
            "failure_stage": outcome.failure_stage,
            "validation_diagnostic": outcome.diagnostic,
            "checklist_feedback": _extract_checklist_feedback(outcome.validation),
            "previous_content": response.content,
            "subcwd_section": self._repair_subcwd_section(),
            "review_summary": outcome.review_summary,
            "review_findings": "\n".join(
                f"- {finding}" for finding in outcome.review_findings
            ),
        }
        if self._repair_prompt_template is None:
            repair_input = _default_repair_prompt(values)
        else:
            repair_input = self._repair_prompt_template
            for key, value in values.items():
                repair_input = repair_input.replace("{" + key + "}", value)
        return _build_repair_turn_prompt(input=repair_input)

    def _log_repair_prompt_activity(self, prompt, *, failure_stage, attempt, repair_index):
        self._log_activity(
            "repair",
            "prompt",
            _format_prompt_summary_for_log(
                prompt,
                prefix=f"repair prompt prepared: source={failure_stage}",
            ),
            attempt=attempt,
            repair_index=repair_index,
        )

    def _set_attempt_activity_callback(self, attempt):
        if not hasattr(attempt.process, "set_activity_callback"):
            return
        attempt.process.set_activity_callback(
            lambda line, attempt_index=attempt.attempt_index, repair_index=attempt.repair_index: (
                self._record_stream_event_line(
                    line,
                    attempt=attempt_index,
                    repair_index=repair_index,
                )
            )
        )

    def _repair_subcwd_section(self):
        if self._subcwd_path is None:
            return ""
        return (
            f"Previous attempt files remain under {_SUBCWD_TOKEN}.\n"
            "Read and update only the files already allowed by the job's "
            f"read_paths/write_paths. Write generated files only to explicitly "
            f"permitted {_SUBCWD_TOKEN} paths."
        )

    def _read_output_file(self, output_path):
        try:
            return output_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def _log_activity(
        self,
        phase,
        event,
        message,
        *,
        detail=None,
        attempt=None,
        repair_index=None,
        argv=None,
        returncode=None,
        stdout="",
        stderr="",
    ):
        entry = CodexActivity(
            phase=phase,
            event=event,
            message=message,
            detail=detail,
            attempt=attempt,
            repair_index=repair_index,
            argv=tuple(argv or ()),
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )
        with self._lock:
            self._activity_log.append(entry)
        line = self._format_activity_line(entry)
        if self._activity_actor is None:
            _emit_activity_stdout(entry)
        else:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
        self._append_log_line(line)

    def _log_subagent_delegation(self, record, *, phase):
        """Write an exact delegation record without flattening its message."""
        encoded = json.dumps(record, ensure_ascii=False, sort_keys=True)
        entry = CodexActivity(
            phase=phase,
            event="delegation",
            message="exact delegation message recorded",
            detail=encoded,
        )
        with self._lock:
            self._activity_log.append(entry)
        _emit_activity_stdout(entry)
        self._append_log_line(encoded)

    def _format_activity_line(self, entry):
        line = _format_activity_stdout(entry)
        if self._activity_actor is None or not line.startswith("["):
            return line
        closing = line.find("]")
        if closing < 0:
            return line
        return f"[{self._activity_actor}]" + line[closing + 1:]

    def _invoke_hook(self, phase, timing, *, raise_errors=True):
        name = f"{timing}_{phase}"
        if self._hooks is not None:
            callbacks = self._hooks.get(name, ())
            for callback in callbacks:
                try:
                    callback()
                except Exception as exc:
                    if raise_errors:
                        raise
                    self._log_activity(
                        phase,
                        "error",
                        f"{name} hook failed: {exc}",
                        stderr=str(exc),
                    )
            return None
        hook_owner = self._hook_owner
        if hook_owner is None:
            return None
        method = getattr(hook_owner, name, None)
        if method is None:
            return None
        try:
            return method() if self._hook_no_args else method(self)
        except Exception as exc:
            if raise_errors:
                raise
            self._log_activity(
                phase,
                "error",
                f"{timing}_{phase} hook failed: {exc}",
                stderr=str(exc),
            )
            return None

    def _append_log_line(self, line):
        if self.log_path is None:
            return
        try:
            pathlib.Path(self.log_path).parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except Exception as exc:
            sys.stdout.write(f"[Log][Error] file log write failed: {_truncate_activity_text(str(exc))}\n")
            sys.stdout.flush()

    def _record_stream_events_from_stdout(self, stdout, *, detail=None, attempt=None, repair_index=None):
        for line in stdout.splitlines():
            self._record_stream_event_line(
                line,
                detail=detail,
                attempt=attempt,
                repair_index=repair_index,
            )

    def _record_stream_event_line(self, line, *, detail=None, attempt=None, repair_index=None):
        entry = _stream_activity_from_line(
            line,
            detail=detail,
            attempt=attempt,
            repair_index=repair_index,
        )
        if entry is None:
            return
        self._log_activity(
            entry.phase,
            entry.event,
            entry.message,
            detail=entry.detail,
            attempt=entry.attempt,
            repair_index=entry.repair_index,
            stdout=entry.stdout,
        )

    def _activity_log_snapshot(self):
        with self._lock:
            return list(self._activity_log)

    def _attach_activity_log(self, error):
        error.activity_log = self._activity_log_snapshot()
        return error

    def _finish(self):
        self._invoke_hook("cleanup", "before", raise_errors=False)
        self._cleanup_output_files()
        self._cleanup_temp_output_schema()
        self._cleanup_review_session()
        self._cleanup_main_session()
        self._cleanup_subcwd()
        self._invoke_hook("cleanup", "after", raise_errors=False)
        self._sync_activity_log_to_outcome()
        self._done.set()
        if self._finish_callback is not None:
            try:
                self._finish_callback()
            except Exception:
                pass

    def _cleanup_review_session(self):
        if self._review_session_cleanup is None:
            return
        try:
            self._review_session_cleanup()
        except Exception as exc:
            self._log_activity(
                "cleanup",
                "error",
                f"review session cleanup failed: {exc}",
                stderr=str(exc),
            )

    def _cleanup_main_session(self):
        if self._main_session_cleanup is None:
            return
        try:
            self._main_session_cleanup()
        except Exception as exc:
            self._log_activity(
                "cleanup",
                "error",
                f"main session cleanup failed: {exc}",
                stderr=str(exc),
            )

    def _cleanup_output_files(self):
        self._log_activity(
            "cleanup",
            "start",
            f"output file cleanup started: files={len(self._output_paths)}",
        )
        removed = 0
        missing = 0
        for output_path in self._output_paths:
            try:
                output_path.unlink()
                removed += 1
            except FileNotFoundError:
                missing += 1
        self._log_activity(
            "cleanup",
            "end",
            f"output file cleanup finished: removed={removed} missing={missing}",
        )

    def _cleanup_subcwd(self):
        if self._subcwd_path is None:
            return
        self._log_activity("cleanup", "start", f"subcwd cleanup started: path={self._subcwd_path}")
        try:
            shutil.rmtree(self._subcwd_path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            with self._lock:
                suffix = f"\n[subcwd cleanup failed: {exc}]"
                self._stderr = (self._stderr or "") + suffix
            self._log_activity(
                "cleanup",
                "error",
                f"subcwd cleanup failed: {exc}",
                stderr=str(exc),
            )
            return
        self._log_activity("cleanup", "end", f"subcwd cleanup finished: path={self._subcwd_path}")

    def _cleanup_temp_output_schema(self):
        if self._temp_output_schema_path is None:
            return
        self._log_activity(
            "cleanup",
            "start",
            f"temp output schema cleanup started: path={self._temp_output_schema_path}",
        )
        try:
            self._temp_output_schema_path.unlink()
        except FileNotFoundError:
            pass
        self._log_activity(
            "cleanup",
            "end",
            f"temp output schema cleanup finished: path={self._temp_output_schema_path}",
        )

    def _terminate_process(self, phase):
        with self._lock:
            process = self._process
        targets = 1
        self._terminate_one_process(process)
        self._log_activity(
            phase,
            "end",
            f"job termination requested for processes={targets}",
        )

    def _terminate_one_process(self, process):
        _terminate_process_object(process)

    def _sync_activity_log_to_outcome(self):
        snapshot = tuple(self._activity_log_snapshot())
        with self._lock:
            if self._response is not None:
                self._response = replace(
                    self._response,
                    stderr=self._stderr,
                    activity_log=snapshot,
                    log_path=self.log_path,
                )
            if self._error is not None:
                self._error.stderr = self._stderr
                self._error.activity_log = list(snapshot)
                self._error.log_path = self.log_path

    def _refresh_status(self):
        if self._done.is_set():
            return
        if self._process.poll() is not None:
            self._done.wait(0.05)


    def before_main_attempt(self):
        pass

    def after_main_attempt(self):
        pass

    def before_validator(self):
        pass

    def after_validator(self):
        pass

    def before_review(self):
        pass

    def after_review(self):
        pass

    def before_file_collection(self):
        pass

    def after_file_collection(self):
        pass

    def before_cleanup(self):
        pass

    def after_cleanup(self):
        pass

    def before_timeout(self):
        pass

    def after_timeout(self):
        pass

    def before_cancel(self):
        pass

    def after_cancel(self):
        pass
