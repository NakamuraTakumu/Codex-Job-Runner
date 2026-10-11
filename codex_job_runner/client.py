"""Client and job implementation for Codex SDK-backed jobs."""

from __future__ import annotations

import importlib
import json
from contextlib import contextmanager
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
import weakref
from dataclasses import replace
from functools import partial
from types import SimpleNamespace
from typing import Any, Iterable, Optional, Sequence

from ._client_helpers import *  # noqa: F401,F403
from ._process import _ActivityStreamEmitter
from ._peer_review import reviewer_decision_schema
from ._session import (
    CodexSession,
    _ConversationState,
    _ConversationStore,
    _SdkSession,
)
from ._pool import CodexWorkerPool
from ._native_usage import _NativeUsageCollector
from ._tools import Tool, _ToolBridge, _normalize_tools
from ._constants import (
    _CANCELLED,
    _COMPLETED,
    _DEFAULT_MAX_FILE_BYTES,
    _DEFAULT_MAX_FILES,
    _DEFAULT_MAX_INLINE_CONTEXT_BYTES,
    _DEFAULT_MAX_INLINE_CONTEXT_TOTAL_BYTES,
    _DEFAULT_MAX_TOTAL_FILE_BYTES,
    _DEFAULT_MODEL,
    _DEFAULT_REASONING_EFFORT,
    _DEFAULT_REVIEW_TIMEOUT,
    _FAILED,
    _PEER_EXCHANGE_TOOL_NAME,
    _REVIEW_OUTPUT_SCHEMA,
    _RUNNER_ALWAYS_OFF_CONFIG_OVERRIDES,
    _RUNNER_RUNTIME_CONFIG_OVERRIDES,
    _RUNNING,
    _SUBCWD_TOKEN,
    _TIMEOUT,
)
from .config import SessionConfig, SubagentConfig, WorkspaceConfig
from ._models import (
    ReviewConfig,
    _CodexAttempt,
    _ResolvedOptions,
    _ResolvedReviewConfig,
    _ReviewResult,
    _SubcwdFile,
)
from .errors import (
    CodexCancelledError,
    CodexExecutionError,
    CodexReviewProtocolError,
    CodexReviewerDecisionValidationError,
    CodexTimeoutError,
    CodexValidationError,
)
from .types import CodexActivity, CodexResponse


_UNSET = object()


_RUNTIME_PUBLIC_METHODS = frozenset(
    {
        "__enter__",
        "__exit__",
        "close",
        "start",
        "run",
        "cancel",
        "result",
    }
)


def _configure_runtime(runtime, **kwargs):
    """Initialize execution state on its ``CodexJob`` or ``CodexSession`` owner."""
    _initialize_runtime(runtime, **kwargs)
    return runtime


def create_runtime(**kwargs):
    """Create a private compatibility runtime for legacy integration tests.

    Production code calls module-level lifecycle helpers with a Job or Session
    owner. The factory attaches explicit partial functions only; it does not
    use a runner class, a namespace scan, or method binding.
    """
    runtime = _configure_runtime(SimpleNamespace(), **kwargs)
    operations = {
        "close": _close_runtime,
        "start": _start_runtime,
        "run": _run_runtime,
        "worker_pool": _create_worker_pool,
        "start_review_session": _start_review_session_runtime,
        "start_session": _start_session_runtime,
        "wait_any": _wait_any,
        "_ensure_client_open": _ensure_runtime_open,
        "_ensure_tool_bridge": _ensure_runtime_tool_bridge,
        "_register_job": _register_runtime_job,
        "_discard_job": _discard_runtime_job,
        "_close_owned_tool_bridge": _close_owned_runtime_tool_bridge,
        "_review_options": _review_options,
        "_create_review_session": _create_review_session,
        "_resolve_options": _resolve_options,
        "_runtime_config_overrides": _runtime_config_overrides,
        "_append_tool_bridge_override": _append_tool_bridge_override,
        "_main_sdk_env": _main_sdk_env,
        "_build_sdk_argv": _build_sdk_argv,
        "_run_sdk_session_turn": _run_sdk_session_turn,
    }
    for name, operation in operations.items():
        setattr(runtime, name, partial(operation, runtime))
    runtime._default_review_session_factory = runtime._create_review_session
    return runtime


def _run_job_runtime(job, **kwargs):
    """Run a job using state owned by ``job`` itself."""
    return _start_runtime(job, **kwargs)


def _open_session_runtime(session, **kwargs):
    """Open a reusable SDK session using state owned by ``session`` itself."""
    return _start_session_runtime(session, **kwargs)


def _close_runtime(runtime):
    """Release execution resources without invoking a public owner method."""
    return _close_runtime_owner(runtime)


def _new_review_session(runtime, **kwargs):
    """Call a test runtime's explicit review-session seam when replaced."""
    factory = getattr(runtime, "_create_review_session", None)
    default_factory = getattr(runtime, "_default_review_session_factory", None)
    if callable(factory) and factory is not default_factory:
        return factory(**kwargs)
    return _create_review_session(runtime, **kwargs)

def _materialize_reviewer_subagent_configs(
    review_config,
    review_read_paths,
):
    """Bind reviewer-child reads to this main job's resolved artifact paths.

    ``${SUBCWD}`` is scoped to one Job invocation.  A Runner-managed child is
    a new Job, so passing the review session's literal token through would
    create and bind a second, empty SUBCWD.  The review paths have already
    been resolved against the main Job before this helper is called; use those
    absolute paths for both sides of the child permission check.
    """
    parent_session = review_config.session
    child_config = review_config.subagents
    if child_config is None:
        return parent_session, None
    effective_read_paths = tuple(review_read_paths)
    effective_parent = parent_session.with_overrides(
        workspace=parent_session.workspace.with_overrides(
            read_paths=effective_read_paths,
        )
    )
    child_session = child_config.session
    effective_child = child_session.with_overrides(
        workspace=child_session.workspace.with_overrides(
            read_paths=effective_read_paths,
            write_paths=(),
        )
    )
    return effective_parent, SubagentConfig(
        session=effective_child,
        allowed_models=child_config.allowed_models,
        max_concurrent=child_config.max_concurrent,
    )


def _sdk_turn_reasoning_effort(sdk, effort):
    """Return a typed-turn effort only when the installed SDK recognizes it."""
    if effort is None:
        return None
    effort_type = _sdk_reasoning_effort_type(sdk)
    if effort_type is None:
        return effort
    try:
        effort_type(effort)
    except (TypeError, ValueError):
        return None
    return effort


def _sdk_reasoning_effort_type(sdk):
    effort_type = getattr(sdk, "ReasoningEffort", None)
    if effort_type is not None:
        return effort_type
    types_module = getattr(sdk, "types", None)
    effort_type = getattr(types_module, "ReasoningEffort", None)
    if effort_type is not None:
        return effort_type
    module_name = getattr(sdk, "__name__", None)
    if not isinstance(module_name, str) or not module_name:
        return None
    try:
        types_module = importlib.import_module(f"{module_name}.types")
    except (ImportError, AttributeError):
        return None
    return getattr(types_module, "ReasoningEffort", None)


def _sdk_thread_start(codex, sdk, *, reasoning_effort, **kwargs):
    """Start a thread while widening stale SDK response effort enums when needed."""
    enable_raw_events = _sdk_supports_raw_response_usage(sdk)
    if (reasoning_effort is None or _sdk_turn_reasoning_effort(sdk, reasoning_effort)) and not enable_raw_events:
        return codex.thread_start(**kwargs)

    raw_client = getattr(codex, "_client", None)
    original_thread_start = getattr(raw_client, "thread_start", None)
    request = getattr(raw_client, "request", None)
    module_name = getattr(sdk, "__name__", None)
    if (
        raw_client is None
        or not callable(original_thread_start)
        or not callable(request)
        or not isinstance(module_name, str)
        or not module_name
    ):
        return codex.thread_start(**kwargs)

    try:
        sdk_client_module = importlib.import_module(f"{module_name}.client")
        response_model = sdk_client_module.ThreadStartResponse
        from pydantic import Field, create_model

        compat_response_model = create_model(
            "ThreadStartResponseReasoningEffortCompat",
            __base__=response_model,
            reasoning_effort=(
                str | None,
                Field(default=None, alias="reasoningEffort"),
            ),
        )
    except (ImportError, AttributeError, TypeError):
        return codex.thread_start(**kwargs)

    def compat_thread_start(params=None):
        if params is None:
            payload = {}
        elif hasattr(params, "model_dump"):
            payload = params.model_dump(
                by_alias=True,
                exclude_none=True,
                mode="json",
            )
        else:
            payload = params
        if isinstance(payload, dict) and enable_raw_events:
            payload = dict(payload)
            payload["experimentalRawEvents"] = True
        return request(
            "thread/start",
            payload,
            response_model=compat_response_model,
        )

    raw_client.thread_start = compat_thread_start
    try:
        return codex.thread_start(**kwargs)
    except Exception:
        if not enable_raw_events:
            raise
        raw_client.thread_start = original_thread_start
        return codex.thread_start(**kwargs)
    finally:
        raw_client.thread_start = original_thread_start


def _sdk_supports_raw_response_usage(sdk):
    """Gate the private wire field to the SDK generation live-probed for it."""
    version = getattr(sdk, "__version__", "")
    try:
        parts = tuple(int(part) for part in str(version).split(".")[:3])
    except ValueError:
        return False
    return parts >= (0, 144, 4)


def _sdk_thread_open(
    codex,
    sdk,
    *,
    conversation_state,
    reasoning_effort,
    **kwargs,
):
    """Start, resume, or fork the thread represented by conversation_state."""
    if conversation_state.thread_id is not None:
        resume_kwargs = dict(kwargs)
        resume_kwargs.pop("ephemeral", None)
        return codex.thread_resume(
            conversation_state.thread_id,
            **resume_kwargs,
        )
    if conversation_state.forked_from_id is not None:
        return codex.thread_fork(
            conversation_state.forked_from_id,
            **kwargs,
        )
    return _sdk_thread_start(
        codex,
        sdk,
        reasoning_effort=reasoning_effort,
        **kwargs,
    )


def _peer_exchange_session_config(*, base_config, options, read_paths):
    """Narrow one role's SessionConfig for a fork without configured write paths."""
    workspace = base_config.workspace.with_overrides(
        cwd=options.cwd,
        subcwd_root=None,
        read_paths=tuple(read_paths),
        write_paths=(),
        subcwd_files={},
        inline_context=False,
    )
    return base_config.with_overrides(
        context=(),
        timeout=options.timeout,
        ignore_global_context=True,
        allowed_skills=(),
        allow_shell_commands=base_config.allow_shell_commands,
        workspace=workspace,
        tools=None,
    )




def _initialize_runtime(
    runtime,
    *,
    context=None,
    model=_DEFAULT_MODEL,
    reasoning_effort=_DEFAULT_REASONING_EFFORT,
    web_search=None,
    codex_bin=None,
    cwd=None,
    subcwd_root=None,
    timeout=None,
    ignore_global_context=False,
    allowed_skills=None,
    allow_shell_commands=True,
    subagents_enabled=False,
    max_concurrent_subagents=2,
    subagent_context_policy="none",
    default_subagent_model=None,
    default_subagent_reasoning_effort=None,
    env=None,
    log_dir=None,
    tools=None,
    max_concurrent_tool_calls=8,
    tool_timeout=60,
):
    if context is None or isinstance(context, (str, os.PathLike)):
        runtime._runtime_context = context
    else:
        runtime._runtime_context = tuple(context)
    runtime._runtime_model = model
    runtime._runtime_reasoning_effort = _normalize_optional_reasoning_effort(
        reasoning_effort
    )
    runtime._runtime_web_search = _normalize_web_search(web_search)
    runtime._runtime_codex_bin = os.fspath(codex_bin) if codex_bin is not None else None
    runtime._runtime_cwd = cwd
    runtime._runtime_subcwd_root = subcwd_root
    runtime._runtime_timeout = timeout
    runtime._runtime_ignore_global_context = ignore_global_context
    runtime._runtime_allowed_skills = _normalize_allowed_skills(allowed_skills)
    runtime._runtime_allow_shell_commands = bool(allow_shell_commands)
    runtime._runtime_subagents_enabled = bool(subagents_enabled)
    if (
        not isinstance(max_concurrent_subagents, int)
        or isinstance(max_concurrent_subagents, bool)
        or max_concurrent_subagents < 1
    ):
        raise ValueError(
            "max_concurrent_subagents must be a positive integer"
        )
    if subagent_context_policy not in {"none", "all"}:
        raise ValueError(
            "subagent_context_policy must be 'none' or 'all'"
        )
    runtime._runtime_max_concurrent_subagents = max_concurrent_subagents
    runtime._runtime_subagent_context_policy = subagent_context_policy
    runtime._runtime_default_subagent_model = _normalize_optional_reasoning_effort(
        default_subagent_model
    )
    runtime._runtime_default_subagent_reasoning_effort = (
        _normalize_optional_reasoning_effort(
            default_subagent_reasoning_effort
        )
    )
    runtime._runtime_env = dict(env or {})
    runtime._runtime_log_dir = pathlib.Path(log_dir).expanduser() if log_dir is not None else None
    runtime._runtime_tools = _normalize_tools(tools)
    if (
        not isinstance(max_concurrent_tool_calls, int)
        or isinstance(max_concurrent_tool_calls, bool)
        or max_concurrent_tool_calls <= 0
    ):
        raise ValueError("max_concurrent_tool_calls must be a positive integer")
    if (
        not isinstance(tool_timeout, (int, float))
        or isinstance(tool_timeout, bool)
        or tool_timeout <= 0
    ):
        raise ValueError("tool_timeout must be a positive number")
    runtime._runtime_max_concurrent_tool_calls = max_concurrent_tool_calls
    runtime._runtime_tool_timeout = float(tool_timeout)
    runtime._tool_bridge_lock = threading.RLock()
    runtime._tool_bridge = None
    runtime._tool_env_name = "CODEX_JOB_RUNNER_MCP_TOKEN"
    runtime._client_closed = False
    runtime._active_jobs = weakref.WeakSet()

def _runtime_enter(runtime):
    _ensure_runtime_open(runtime, )
    return runtime

def _runtime_exit(runtime, exc_type, exc_value, traceback_value):
    _close_runtime_owner(runtime, )

def _close_runtime_owner(runtime):
    """Cancel active jobs and stop the shared function-tool bridge."""
    with runtime._tool_bridge_lock:
        if runtime._client_closed:
            return
        runtime._client_closed = True
        jobs = tuple(runtime._active_jobs)
        bridge = runtime._tool_bridge
        runtime._tool_bridge = None
    for job in jobs:
        try:
            job.cancel()
        except Exception:
            pass
    if bridge is not None:
        bridge.close()

def _ensure_runtime_open(runtime):
    with runtime._tool_bridge_lock:
        if runtime._client_closed:
            raise RuntimeError("CodexClient is closed")

def _ensure_runtime_tool_bridge(runtime):
    if not runtime._runtime_tools:
        return None
    with runtime._tool_bridge_lock:
        if runtime._client_closed:
            raise RuntimeError("CodexClient is closed")
        if runtime._tool_bridge is None:
            bridge = _ToolBridge(
                runtime._runtime_tools,
                max_concurrency=runtime._runtime_max_concurrent_tool_calls,
                tool_timeout=runtime._runtime_tool_timeout,
            )
            bridge.start()
            runtime._tool_bridge = bridge
        return runtime._tool_bridge

def _register_runtime_job(runtime, job):
    with runtime._tool_bridge_lock:
        if runtime._client_closed:
            raise RuntimeError("CodexClient is closed")
        runtime._active_jobs.add(job)

def _discard_runtime_job(runtime, job):
    with runtime._tool_bridge_lock:
        runtime._active_jobs.discard(job)

def _close_owned_runtime_tool_bridge(runtime):
    with runtime._tool_bridge_lock:
        bridge = runtime._tool_bridge
        runtime._tool_bridge = None
    if bridge is not None:
        bridge.close()

def _before_main_attempt(runtime, job):
    pass

def _after_main_attempt(runtime, job):
    pass

def _before_validator(runtime, job):
    pass

def _after_validator(runtime, job):
    pass

def _before_review(runtime, job):
    pass

def _after_review(runtime, job):
    pass

def _before_file_collection(runtime, job):
    pass

def _after_file_collection(runtime, job):
    pass

def _before_cleanup(runtime, job):
    pass

def _after_cleanup(runtime, job):
    pass

def _before_timeout(runtime, job):
    pass

def _after_timeout(runtime, job):
    pass

def _before_cancel(runtime, job):
    pass

def _after_cancel(runtime, job):
    pass

def _start_runtime(
    runtime,
    *,
    input,
    context=_UNSET,
    read_paths=None,
    write_paths=None,
    subcwd_files=None,
    model=None,
    reasoning_effort=None,
    cwd=None,
    timeout=None,
    review=None,
    ignore_global_context=None,
    allowed_skills=None,
    allow_shell_commands=None,
    output_schema_path=None,
    output_pydantic_model=None,
    validate_output=None,
    repair_validation_failures=False,
    repair_prompt_path=None,
    collect_files=False,
    max_files=_DEFAULT_MAX_FILES,
    max_file_bytes=_DEFAULT_MAX_FILE_BYTES,
    max_total_file_bytes=_DEFAULT_MAX_TOTAL_FILE_BYTES,
    inline_context=True,
    max_inline_context_bytes=_DEFAULT_MAX_INLINE_CONTEXT_BYTES,
    max_inline_context_total_bytes=_DEFAULT_MAX_INLINE_CONTEXT_TOTAL_BYTES,
    log_path=None,
    _hook_owner=_UNSET,
    _output_validators=None,
    _hooks=None,
    _borrowed_tool_bridge=None,
    _defer_background=False,
    _job_id=None,
    _conversation_state=None,
    _activity_actor=None,
    _rollout_dir=None,
    _job_owner=None,
):
    _ensure_runtime_open(runtime, )
    if _borrowed_tool_bridge is not None:
        if not runtime._runtime_tools:
            raise ValueError("a borrowed tool bridge requires configured tools")
        with runtime._tool_bridge_lock:
            runtime._tool_bridge = _borrowed_tool_bridge
    if context is not _UNSET:
        raise ValueError(
            "start context is not supported; pass fixed context to "
            "SessionConfig(context=...) and job-specific content to input"
        )
    if output_schema_path is not None and output_pydantic_model is not None:
        raise ValueError(
            "output_schema_path and output_pydantic_model are mutually exclusive"
        )
    if validate_output is not None and not callable(validate_output):
        raise TypeError("validate_output must be callable or None")

    options = _resolve_options(runtime,
        model=model,
        reasoning_effort=reasoning_effort,
        cwd=cwd,
        timeout=timeout,
        ignore_global_context=ignore_global_context,
        allowed_skills=allowed_skills,
        allow_shell_commands=allow_shell_commands,
    )
    raw_read_paths = list(read_paths or [])
    raw_write_paths = list(write_paths or [])
    raw_context_for_reject = (
        [runtime._runtime_context]
        if isinstance(runtime._runtime_context, (str, os.PathLike))
        else list(runtime._runtime_context or [])
    )
    _reject_subcwd_token(raw_context_for_reject, "context")
    context_entries = _normalize_context_items(runtime._runtime_context, options.cwd)
    context_paths = _context_paths_from_entries(context_entries)
    if isinstance(input, (str, os.PathLike)):
        raw_input_for_reject = [input]
    else:
        try:
            raw_input_for_reject = list(input)
        except TypeError as exc:
            raise ValueError(
                "input must be a string, path, or iterable of strings and paths"
            ) from exc
    if not raw_input_for_reject:
        raise ValueError("input must be non-empty")
    input_entries = _normalize_context_items(
        raw_input_for_reject,
        options.cwd,
        field_name="input",
        string_paths=False,
    )
    input_paths = _context_paths_from_entries(input_entries)
    subcwd_files = _normalize_subcwd_files(subcwd_files)
    needs_subcwd = (
        bool(subcwd_files)
        or _contains_subcwd_token(raw_read_paths)
        or _contains_subcwd_token(raw_write_paths)
        or (
            review is not None
            and _contains_subcwd_token(review.artifact_paths)
        )
    )
    subcwd_path = None
    if needs_subcwd:
        subcwd_path = _create_subcwd(options.subcwd_root)
    try:
        _stage_subcwd_files(subcwd_path, subcwd_files)
        read_paths = _normalize_subcwd_paths(
            raw_read_paths,
            options.cwd,
            subcwd_path,
            must_exist=True,
            field_name="read_paths",
        )
        write_paths = _normalize_subcwd_paths(
            raw_write_paths,
            options.cwd,
            subcwd_path,
            must_exist=False,
            field_name="write_paths",
        )
        _ensure_staged_subcwd_files_are_accessible(
            subcwd_path,
            subcwd_files,
            read_paths,
            write_paths,
        )
        _enforce_write_path_runtime_capability(
            cwd=options.cwd,
            raw_write_paths=raw_write_paths,
            write_paths=write_paths,
            subcwd_path=subcwd_path,
            codex_bin=runtime._runtime_codex_bin,
        )
    except Exception:
        _cleanup_tree(subcwd_path)
        raise
    normalized_output_pydantic_model = _normalize_output_pydantic_model(
        output_pydantic_model
    )
    if _output_validators is None:
        output_validators = []
        if normalized_output_pydantic_model is not None:
            output_validators.append(
                _make_pydantic_output_validator(
                    normalized_output_pydantic_model
                )
            )
        if validate_output is not None:
            output_validators.append(validate_output)
        output_validators = tuple(output_validators)
    else:
        output_validators = tuple(_output_validators)
    output_schema_path = _normalize_optional_path(
        output_schema_path,
        options.cwd,
        must_exist=True,
        field_name="output_schema_path",
    )
    temp_output_schema_path = None
    if normalized_output_pydantic_model is not None:
        temp_output_schema_path = pathlib.Path(
            _make_temp_text_file(
                prefix="codex_job_runner_output_schema_",
                suffix=".json",
                text=_render_pydantic_output_schema_json(
                    normalized_output_pydantic_model
                ),
            )
        )
        output_schema_path = temp_output_schema_path
    output_model_source_section = None
    if normalized_output_pydantic_model is not None:
        output_model_source_section = _render_pydantic_model_source_context(
            normalized_output_pydantic_model
        )
    output_schema_section = None
    output_schema_inline_bytes = 0
    if normalized_output_pydantic_model is None:
        output_schema_section, output_schema_inline_bytes = (
            _resolve_inline_output_schema_section(
                output_schema_path=output_schema_path,
                max_file_bytes=max_inline_context_bytes,
            )
        )
    context_sections = _resolve_inline_context_sections(
        context_entries=context_entries,
        inline_context=inline_context,
        max_file_bytes=max_inline_context_bytes,
        max_total_bytes=max_inline_context_total_bytes,
        initial_total_bytes=output_schema_inline_bytes,
    )
    input_sections = _resolve_inline_input_sections(
        context_entries=input_entries,
        inline_context=inline_context,
        max_file_bytes=max_inline_context_bytes,
        max_total_bytes=max_inline_context_total_bytes,
        initial_total_bytes=(
            output_schema_inline_bytes
            + _inline_context_file_bytes(
                context_entries, inline_context=inline_context
            )
        ),
    )
    normalized_log_path = _normalize_optional_path(
        log_path,
        options.cwd,
        must_exist=False,
        field_name="log_path",
    ) if log_path is not None else None
    normalized_log_dir = _normalize_optional_path(
        runtime._runtime_log_dir,
        options.cwd,
        must_exist=False,
        field_name="log_dir",
    ) if runtime._runtime_log_dir is not None else None
    repair_prompt_path = _normalize_optional_path(
        repair_prompt_path,
        options.cwd,
        must_exist=True,
        field_name="repair_prompt_path",
    )
    repair_prompt_template = None
    if repair_prompt_path is not None:
        repair_prompt_template = repair_prompt_path.read_text(encoding="utf-8")
    review_config = _normalize_review_config(review, options.cwd)
    if review_config is not None:
        review_config = replace(
            review_config,
            artifact_paths=tuple(
                _normalize_subcwd_paths(
                    review_config.artifact_paths,
                    options.cwd,
                    subcwd_path,
                    must_exist=False,
                    field_name="review.artifact_paths",
                )
            ),
        )
    review_notice = _build_review_notice() if review_config is not None else None
    prompt_subcwd_files = _prompt_subcwd_file_paths(subcwd_files)
    prompt = _build_prompt(
        context_paths=context_paths,
        input_paths=[],
        read_paths=read_paths,
        write_paths=write_paths,
        subcwd_files=prompt_subcwd_files,
        subcwd_path=subcwd_path,
        ignore_global_context=options.ignore_global_context,
        context_sections=context_sections,
        output_schema_section=output_schema_section,
        output_model_source_section=output_model_source_section,
        review_notice=review_notice,
        input_sections=input_sections,
        subagent_context_policy=(
            options.subagent_context_policy
            if options.subagents_enabled
            else None
        ),
    )
    normalized_input = "\n\n".join(input_sections)
    if input_paths and not input_sections:
        normalized_input = _format_path_section("Input paths", input_paths)
    profile_name = "codex_job_runner_" + uuid.uuid4().hex
    env = os.environ.copy()
    env.update(runtime._runtime_env)
    job_holder = {"job": None}
    hook_owner = runtime if _hook_owner is _UNSET else _hook_owner

    # Holds only a path: referencing the job would keep the store, and its home, alive.
    rollout_root = {"path": None}

    def archiving_cleanup(role):
        def cleanup(home):
            try:
                if rollout_root["path"] is not None:
                    _archive_rollouts(home, rollout_root["path"] / role)
            finally:
                _cleanup_tree(home)
        return cleanup

    owns_main_store = _conversation_state is None
    main_conversation_state = _conversation_state or _ConversationState(
        _ConversationStore(archiving_cleanup("main"))
    )
    main_base_config = getattr(hook_owner, "config", None)
    if not isinstance(main_base_config, SessionConfig):
        main_base_config = SessionConfig(
            model=options.model,
            reasoning_effort=options.reasoning_effort,
            web_search=options.web_search,
            codex_bin=runtime._runtime_codex_bin,
            timeout=options.timeout,
            env=runtime._runtime_env,
            ignore_global_context=options.ignore_global_context,
            allowed_skills=options.allowed_skills,
            allow_shell_commands=options.allow_shell_commands,
            workspace=WorkspaceConfig(cwd=options.cwd),
        )
    main_exchange_config = _peer_exchange_session_config(
        base_config=main_base_config,
        options=options,
        read_paths=read_paths,
    )
    peer_exchange_enabled = bool(
        review_config is not None and review_config.enable_peer_exchange
    )
    if peer_exchange_enabled and any(
        tool.name == _PEER_EXCHANGE_TOOL_NAME for tool in runtime._runtime_tools
    ):
        raise ValueError(
            f"tool name {_PEER_EXCHANGE_TOOL_NAME!r} is reserved "
            "when peer exchange is enabled"
        )
    peer_exchange_lock = threading.RLock()
    peer_exchange_state = {
        "role": None,
        "attempt": None,
        "repair_index": None,
        "reviewer_source": None,
        "reviewer_config": None,
        "closed": False,
        "active_forks": set(),
        "sequence": 0,
    }
    peer_exchange_bridge = {"bridge": None}
    review_subagent_state = {"manager": None, "bridge": None}
    peer_exchange_token_env_name = (
        "CODEX_JOB_RUNNER_PEER_EXCHANGE_TOKEN_" + uuid.uuid4().hex.upper()
    )

    def log_peer_exchange(
        event,
        message,
        *,
        stderr="",
        usage=None,
        estimated_cost_usd=None,
        model=None,
    ):
        job = job_holder.get("job")
        if job is None:
            return
        with peer_exchange_lock:
            attempt_index = peer_exchange_state["attempt"]
            repair_index = peer_exchange_state["repair_index"]
        job._log_activity(
            "peer_exchange",
            event,
            message,
            attempt=attempt_index,
            repair_index=repair_index,
            stderr=stderr,
        )
        if usage is not None:
            job._record_additional_usage(
                usage=usage,
                estimated_cost_usd=estimated_cost_usd,
                model=model,
                role="peer exchange",
            )
            job._log_usage_activity(
                phase="peer_exchange",
                model=model,
                usage=usage,
                attempt=attempt_index,
                repair_index=repair_index,
            )

    def run_with_peer_role(role, attempt_index, repair_index, operation):
        if not peer_exchange_enabled:
            return operation()
        with peer_exchange_lock:
            if peer_exchange_state["closed"]:
                raise RuntimeError("peer exchange is closed")
            if peer_exchange_state["role"] is not None:
                raise RuntimeError("peer exchange role is already active")
            peer_exchange_state["role"] = role
            peer_exchange_state["attempt"] = attempt_index
            peer_exchange_state["repair_index"] = repair_index
        try:
            return operation()
        finally:
            with peer_exchange_lock:
                peer_exchange_state["role"] = None
                peer_exchange_state["attempt"] = None
                peer_exchange_state["repair_index"] = None

    @contextmanager
    def answer_fork(source_state, readonly_config, message):
        """Share restricted fork lifetime management with ask_peer."""
        source = CodexSession(config=readonly_config, _conversation_state=source_state)
        fork = None
        try:
            fork = source.fork(input=message, config=readonly_config)
            with peer_exchange_lock:
                if peer_exchange_state["closed"]:
                    raise RuntimeError("peer exchange is closed")
                peer_exchange_state["active_forks"].add(fork)
            yield fork
        finally:
            if fork is not None:
                with peer_exchange_lock:
                    peer_exchange_state["active_forks"].discard(fork)
                fork.close()
            source.close()

    def open_answer_fork(message):
        return answer_fork(main_conversation_state, main_exchange_config, message)

    def ask_peer(message: str) -> str:
        with peer_exchange_lock:
            if peer_exchange_state["closed"]:
                raise RuntimeError("peer exchange is closed")
            role = peer_exchange_state["role"]
            if role == "main":
                reviewer_source = peer_exchange_state["reviewer_source"]
                readonly_config = peer_exchange_state["reviewer_config"]
                if reviewer_source is None or readonly_config is None:
                    raise RuntimeError(
                        "no completed reviewer thread is available"
                    )
                source_state = reviewer_source._conversation_state
                target_role = "reviewer"
            elif role == "reviewer":
                source_state = main_conversation_state
                readonly_config = main_exchange_config
                target_role = "main"
            else:
                raise RuntimeError(
                    "peer exchange is unavailable outside a main or review turn"
                )
            if source_state.thread_id is None:
                raise RuntimeError(
                    f"no completed {target_role} thread is available"
                )
            peer_exchange_state["sequence"] += 1
            exchange_id = (
                f"{job_holder.get('job').id if job_holder.get('job') else 'pending'}"
                f":{peer_exchange_state['sequence']}"
            )
            source_thread_id = source_state.thread_id
        with answer_fork(source_state, readonly_config, message) as fork:
            return run_peer_answer(fork, readonly_config, message, exchange_id,
                                   role, target_role, source_thread_id)

    def run_peer_answer(fork, readonly_config, message, exchange_id,
                        role, target_role, source_thread_id):
        fork_thread_id = fork._conversation_state.thread_id
        try:
            log_peer_exchange(
                "start",
                f"peer exchange started: id={exchange_id} "
                f"caller={role} target={target_role} "
                f"source_thread={source_thread_id} "
                f"fork_thread={fork_thread_id}; "
                + _format_prompt_summary_for_log(
                    message,
                    prefix="message",
                ),
            )
            response = fork.run()
            log_peer_exchange(
                "end",
                f"peer exchange finished: id={exchange_id} "
                f"caller={role} target={target_role} "
                f"source_thread={source_thread_id} "
                f"fork_thread={fork_thread_id}; "
                + _format_prompt_summary_for_log(
                    response.content,
                    prefix="response",
                ),
                usage=response.usage,
                estimated_cost_usd=response.estimated_cost_usd,
                model=readonly_config.model,
            )
            return response.content
        except Exception as exc:
            log_peer_exchange(
                "error",
                f"peer exchange failed: id={exchange_id} "
                f"caller={role} target={target_role} "
                f"source_thread={source_thread_id} "
                f"error={type(exc).__name__}",
                stderr=type(exc).__name__,
            )
            raise

    peer_exchange_tool = (
        Tool(
            ask_peer,
            "Ask the other role one question using its latest completed "
            "conversation. The other role answers once on a temporary "
            "restricted fork without configured write paths. The exchange "
            "is not recorded in the other role's original conversation, "
            "so do not use it to establish agreement or obtain approval.",
            args={"message": "The plain-text question to send to the other role."},
            returns="The other role's plain-text answer.",
            name=_PEER_EXCHANGE_TOOL_NAME,
        )
        if peer_exchange_enabled
        else None
    )

    def ensure_peer_exchange_bridge():
        if peer_exchange_tool is None:
            return None
        with peer_exchange_lock:
            bridge = peer_exchange_bridge["bridge"]
            if bridge is None:
                bridge = _ToolBridge(
                    (peer_exchange_tool,),
                    max_concurrency=1,
                    tool_timeout=runtime._runtime_tool_timeout,
                )
                bridge.start()
                peer_exchange_bridge["bridge"] = bridge
            return bridge

    def peer_exchange_bridge_spec():
        bridge = ensure_peer_exchange_bridge()
        if bridge is None:
            return None
        return {
            "server_name": "codex_job_runner_peer_exchange",
            "bridge": bridge,
            "token_env_name": peer_exchange_token_env_name,
            "tool_names": (_PEER_EXCHANGE_TOOL_NAME,),
            "tool_timeout": runtime._runtime_tool_timeout,
        }

    def close_peer_exchange():
        with peer_exchange_lock:
            if peer_exchange_state["closed"]:
                return
            peer_exchange_state["closed"] = True
            forks = tuple(peer_exchange_state["active_forks"])
            peer_exchange_state["active_forks"].clear()
            reviewer_source = peer_exchange_state["reviewer_source"]
            peer_exchange_state["reviewer_source"] = None
            peer_exchange_state["reviewer_config"] = None
            bridge = peer_exchange_bridge["bridge"]
            peer_exchange_bridge["bridge"] = None
        for fork in forks:
            try:
                fork.close()
            except Exception:
                pass
        if reviewer_source is not None:
            _close_session_and_delete_store(reviewer_source)
        if bridge is not None:
            bridge.close()

    def close_review_resources():
        try:
            close_cached_review_session()
        finally:
            try:
                manager = review_subagent_state["manager"]
                if manager is not None:
                    manager.close()
                bridge = review_subagent_state["bridge"]
                if bridge is not None:
                    bridge.close()
            finally:
                close_peer_exchange()

    native_usage_collectors = []
    main_native_usage_collector = _NativeUsageCollector(
        model=options.model,
        default_subagent_model=options.default_subagent_model,
        role="main",
    )
    native_usage_collectors.append(main_native_usage_collector)

    def native_usage_snapshot():
        return tuple(
            record
            for collector in native_usage_collectors
            for record in collector.snapshot()
        )

    def create_main_sdk_session():
        sdk = _import_openai_codex()
        config_overrides = _runtime_config_overrides(runtime,
            options,
            include_tools=True,
        )
        exchange_bridge_spec = peer_exchange_bridge_spec()
        if exchange_bridge_spec is not None:
            _append_tool_bridge_override(runtime,
                config_overrides,
                server_name=exchange_bridge_spec["server_name"],
                bridge=exchange_bridge_spec["bridge"],
                token_env_name=exchange_bridge_spec["token_env_name"],
                tool_names=exchange_bridge_spec["tool_names"],
                tool_timeout=exchange_bridge_spec["tool_timeout"],
            )
        config_overrides.append(_project_untrusted_config_override(options.cwd))
        if options.reasoning_effort:
            config_overrides.append(
                f"model_reasoning_effort={_toml_string(options.reasoning_effort)}"
            )
        if options.ignore_global_context:
            config_overrides.extend(
                [
                    "project_doc_max_bytes=0",
                    "features.hooks=false",
                    "skills.include_instructions=false",
                ]
            )
        elif options.allowed_skills == []:
            config_overrides.append("skills.include_instructions=false")
        if not options.allow_shell_commands:
            config_overrides.extend(
                ["features.shell_tool=false", "allow_login_shell=false"]
            )
        extra_read_paths = (
            _codex_runtime_read_paths(runtime._runtime_codex_bin)
            if options.allow_shell_commands
            else []
        )
        filesystem_profile = _filesystem_inline_table(
            options.cwd,
            read_paths,
            write_paths,
            extra_read_paths=extra_read_paths,
        )
        config_overrides.extend(
            [
                f"default_permissions={_toml_string(profile_name)}",
                f"permissions.{profile_name}.filesystem={filesystem_profile}",
            ]
        )
        main_sdk_env = _main_sdk_env(runtime, )
        if exchange_bridge_spec is not None:
            main_sdk_env[exchange_bridge_spec["token_env_name"]] = (
                exchange_bridge_spec["bridge"].token
            )
        sdk_env, config_home = _permission_profile_sdk_env(
            main_sdk_env,
            existing_home=(
                None
                if _conversation_state is None
                else _conversation_state.store.home
            ),
        )
        config_overrides.append(
            "sqlite_home=" + _toml_string(sdk_env["CODEX_SQLITE_HOME"])
        )
        codex = None
        try:
            sdk_config = sdk.CodexConfig(
                codex_bin=runtime._runtime_codex_bin,
                config_overrides=tuple(config_overrides),
                cwd=str(options.cwd),
                env=sdk_env,
            )
            codex = sdk.Codex(config=sdk_config)
            main_native_usage_collector.attach(codex)
            thread = _sdk_thread_open(
                codex,
                sdk,
                conversation_state=main_conversation_state,
                reasoning_effort=options.reasoning_effort,
                cwd=str(options.cwd),
                model=options.model,
                developer_instructions=None,
                ephemeral=False,
                approval_mode=sdk.ApprovalMode.deny_all,
            )
            return codex, thread, config_home
        except Exception:
            if codex is not None:
                try:
                    codex.close()
                except Exception:
                    pass
            if _conversation_state is None:
                _cleanup_tree(config_home)
            raise

    main_sdk_session = _SdkSession(
        create_session=create_main_sdk_session,
        cleanup_tree=_cleanup_tree,
        conversation_state=main_conversation_state,
        native_usage_collector=main_native_usage_collector,
    )

    def send_main_turn(message, *, output_schema_path=None, activity_callback=None):
        """Run the Main turn through the same raw Session primitive as review."""
        return _run_sdk_session_turn(runtime,
            options=options,
            read_paths=read_paths,
            write_paths=write_paths,
            profile_name=profile_name,
            message=message,
            output_schema_path=output_schema_path,
            activity_callback=activity_callback,
            sdk_session=main_sdk_session,
            failure_label="main",
            argv=getattr(main_sdk_session, "_current_argv", None),
        )

    # CodexSession owns the SDK turn contract.  The Job state machine only
    # schedules turns and observes their terminal result.
    main_sdk_session._send = send_main_turn
    if isinstance(_hook_owner, CodexSession):
        # The pending Job is also the public Main conversation.  Bind its
        # raw Session resource before its worker starts so every Main
        # attempt, including repairs, uses CodexSession.send().
        with _hook_owner._session_lock:
            _hook_owner._turn_session = main_sdk_session
            _hook_owner._conversation_state = main_sdk_session.conversation_state
            _hook_owner._context_prefix = ""

    def close_main_session():
        job = job_holder.get("job")
        if job is not None:
            job._conversation_state = main_sdk_session.conversation_state
        main_sdk_session.close()
        # A job deletes only a store it created; injected states (resume,
        # fork, subagent follow-up) belong to whoever supplied them.
        if owns_main_store and not getattr(
            job, "_retain_conversation_store", False
        ):
            main_conversation_state.store.delete()
        if job is not None:
            _discard_runtime_job(runtime, job)

    def spawn_attempt(*, prompt, attempt_index, repair_index, turn_output_schema_path=None):
        effective_output_schema_path = (
            output_schema_path
            if turn_output_schema_path is None
            else turn_output_schema_path
        )
        output_path = _make_output_path()
        argv = _build_sdk_argv(runtime,
            profile_name=profile_name,
            options=options,
            read_paths=read_paths,
            write_paths=write_paths,
            output_schema_path=effective_output_schema_path,
        )
        # The job worker already provides the background thread.  Keep a
        # tiny subprocess-shaped value only for its existing cancellation
        # and activity plumbing; it delegates the actual turn to the
        # public CodexJob/CodexSession.send() path.
        state = SimpleNamespace(
            started=False,
            finished=threading.Event(),
            terminated=threading.Event(),
            returncode=None,
            stdout="",
            stderr="",
            activity_callback=None,
            emitted_stdout_lines=False,
        )

        def emit_stdout_line(line):
            state.emitted_stdout_lines = True
            if state.activity_callback is not None:
                state.activity_callback(line)

        def communicate(prompt_text=None, timeout=None):
            if state.started:
                if prompt_text is not None:
                    raise RuntimeError("communicate() prompt may only be provided once")
                state.finished.wait(timeout)
                return state.stdout, state.stderr
            if prompt_text is None:
                raise RuntimeError("communicate() requires prompt on first call")
            state.started = True
            try:
                main_sdk_session._current_argv = tuple(argv)
                owner = (
                    _hook_owner
                    if isinstance(_hook_owner, CodexSession)
                    else None
                )
                send = (
                    (lambda: owner.send(
                        prompt_text,
                        output_schema_path=effective_output_schema_path,
                    ))
                    if owner is not None
                    else (lambda: send_main_turn(
                        prompt_text,
                        output_schema_path=effective_output_schema_path,
                        activity_callback=emit_stdout_line,
                    ))
                )
                response = run_with_peer_role(
                    "main",
                    attempt_index,
                    repair_index,
                    send,
                )
                state.stdout = response.stdout or ""
                state.stderr = response.stderr or ""
                state.returncode = response.returncode
                if state.terminated.is_set():
                    state.returncode = 1
                else:
                    pathlib.Path(output_path).write_text(
                        response.content,
                        encoding="utf-8",
                    )
            except Exception as exc:
                state.returncode = 1
                state.stderr = getattr(exc, "stderr", "") or traceback.format_exc().rstrip()
                state.stdout = getattr(exc, "stdout", "") or ""
            finally:
                process.returncode = state.returncode
                state.finished.set()
            return state.stdout, state.stderr

        def poll():
            return state.returncode if state.finished.is_set() else None

        def wait(timeout=None):
            if not state.finished.wait(timeout):
                raise subprocess.TimeoutExpired(cmd="openai-codex", timeout=timeout)
            return state.returncode

        def terminate():
            state.terminated.set()
            main_sdk_session.interrupt()

        process = SimpleNamespace(
            communicate=communicate,
            poll=poll,
            wait=wait,
            terminate=terminate,
            kill=terminate,
            returncode=None,
            set_activity_callback=lambda callback: setattr(
                state,
                "activity_callback",
                callback,
            ),
            emitted_stdout_lines=lambda: state.emitted_stdout_lines,
            emit_stdout_line=emit_stdout_line,
        )
        process.__dict__["_state"] = state
        return _CodexAttempt(
            argv=list(argv),
            prompt=prompt,
            process=process,
            output_path=pathlib.Path(output_path),
            attempt_index=attempt_index,
            repair_index=repair_index,
        )

    review_session_cache = {"session": None}

    def ensure_review_subagent_bridge(review_config, review_read_paths):
        if review_config.subagents is None:
            return None
        manager = review_subagent_state["manager"]
        if manager is None:
            from ._subagents import _DirectChildManager

            parent_config, subagent_config = (
                _materialize_reviewer_subagent_configs(
                    review_config,
                    review_read_paths,
                )
            )
            manager = _DirectChildManager(
                parent_config=parent_config,
                config=subagent_config,
                rollout_dir=lambda: (
                    None
                    if rollout_root["path"] is None
                    else rollout_root["path"] / "reviewer_subagents"
                ),
                activity_callback=lambda event, message: (
                    job_holder["job"]._log_activity(
                        "reviewer_subagent",
                        event,
                        message,
                    )
                    if job_holder["job"] is not None
                    else None
                ),
                delegation_callback=lambda record: (
                    job_holder["job"]._subagent_delegation(
                        record,
                        phase="reviewer_subagent",
                    )
                    if job_holder["job"] is not None
                    else None
                ),
            )
            manager.validate_permissions(parent_tools=())
            manager.activate(timeout=review_config.timeout)
            bridge = _ToolBridge(
                manager.tools(),
                max_concurrency=review_config.subagents.max_concurrent,
                tool_timeout=runtime._runtime_tool_timeout,
            )
            bridge.start()
            review_subagent_state["manager"] = manager
            review_subagent_state["bridge"] = bridge
        bridge = review_subagent_state["bridge"]
        return {
            "server_name": "codex_job_runner_reviewer_subagents",
            "bridge": bridge,
            "token_env_name": (
                "CODEX_JOB_RUNNER_REVIEWER_SUBAGENT_TOKEN_"
                + uuid.uuid4().hex.upper()
            ),
            "tool_names": tuple(
                tool.name for tool in review_subagent_state["manager"].tools()
            ),
            "tool_timeout": runtime._runtime_tool_timeout,
        }

    def close_cached_review_session():
        session = review_session_cache.get("session")
        if session is None:
            return
        review_session_cache["session"] = None
        with peer_exchange_lock:
            is_peer_source = peer_exchange_state["reviewer_source"] is session
        if is_peer_source:
            session.close()  # peer exchange still forks it; released with it
        else:
            _close_session_and_delete_store(session)

    def run_review(
        *,
        original_input,
        main_context,
        response,
        subcwd_path,
        review_config,
        attempt_index,
        repair_index,
        stream_callback,
        protocol=None,
    ):
        review_options = _review_options(runtime,
            review_config=review_config,
            options=options,
        )
        if review_config.read_paths is None:
            review_read_paths = list(read_paths)
        else:
            review_read_paths = _normalize_subcwd_paths(
                review_config.read_paths,
                options.cwd,
                subcwd_path,
                must_exist=True,
                field_name="review.read_paths",
            )
        review_visible_subcwd_path = None
        if subcwd_path is not None and any(
            _is_relative_to(path, subcwd_path) or path == subcwd_path
            for path in review_read_paths
        ):
            review_visible_subcwd_path = subcwd_path
        schema_text = (
            _REVIEW_OUTPUT_SCHEMA
            if protocol is None
            else json.dumps(
                reviewer_decision_schema(
                    open_ids=tuple(
                        item["finding_id"] for item in protocol["open_findings"]
                    ),
                    initial=protocol["initial"],
                    closed_findings=protocol["package"]["closed_findings"],
                ),
                ensure_ascii=False,
            )
        )
        schema_path = pathlib.Path(
            _make_temp_text_file(
                prefix="codex_job_runner_review_",
                suffix=".json",
                text=schema_text,
            )
        )
        output_path = pathlib.Path(_make_output_path())
        try:
            output_schema_section, _ = _resolve_inline_output_schema_section(
                output_schema_path=schema_path,
                max_file_bytes=_DEFAULT_MAX_INLINE_CONTEXT_BYTES,
            )
            review_session = None
            if review_config.reuse_session:
                review_session = review_session_cache.get("session")
            if protocol is not None:
                # A new reviewer thread has not seen the job request yet.
                original_request_section = (
                    f"Original job request:\n{original_input}\n\n"
                    if review_session is None
                    else ""
                )
                artifact_files = "".join(
                    f"- {path}\n" for path in protocol["artifact_files"]
                ) or "- (none)\n"
                # Artifacts are read from the workspace; only the package is passed.
                review_input = (
                    "You are a peer reviewer. Return only a strict JSON object with "
                    "exactly decisions and new_findings. decisions must decide every "
                    "open finding using resolved, withdrawn, unresolved, or conflicts, with "
                    "conflicts_with naming another open or closed finding for conflicts "
                    "(null otherwise). closed_findings contains finding_id, comment "
                    "(the requirement), decision_comment (the closing reviewer comment, "
                    "or null), resolution, and main_response. "
                    "Every new finding has a non-empty comment and "
                    "relation {kind, finding_id}: new means unrelated to closed findings "
                    "and uses null; reopens requests the same correction as closed X "
                    "(restored under its original ID); revises undoes a change accepted "
                    "for resolved X but both can be satisfied (describe a fix satisfying "
                    "both); conflicts undoes a change accepted for resolved X and both "
                    "cannot be satisfied (the job stops). Non-new relations name closed X. "
                    "Undoing an accepted change takes precedence: use revises or conflicts "
                    "even if another closed finding matches. When Main disputed a finding, "
                    "answer its stated reason in the decision comment: withdraw if accepted, "
                    "use conflicts if incompatible with another finding, otherwise unresolved "
                    "with a rebuttal. Before deciding, review the "
                    "current artifact files in the workspace as the review context "
                    "requires.\n\n"
                    + original_request_section
                    + "Current main final response:\n"
                    + protocol["response_content"]
                    + "\n\nArtifact files:\n"
                    + artifact_files
                    + "\nreview-package.json:\n"
                    + json.dumps(protocol["package"], ensure_ascii=False)
                )
            elif review_session is None:
                review_input = _build_review_input(
                    original_input=original_input,
                    main_context=main_context,
                    response=response,
                    subcwd_path=review_visible_subcwd_path,
                    review_config=review_config,
                    review_read_paths=review_read_paths,
                )
            else:
                review_input = _build_review_delta_input(response=response)
            if review_session is None:
                create_review_kwargs = {
                    "review_config": review_config,
                    "options": review_options,
                    "read_paths": review_read_paths,
                }
                create_review_kwargs["native_usage_collectors"] = native_usage_collectors
                create_review_kwargs["cleanup_tree"] = archiving_cleanup("reviewer")
                additional_bridges = []
                if peer_exchange_enabled:
                    additional_bridges.append(peer_exchange_bridge_spec())
                reviewer_bridge = ensure_review_subagent_bridge(
                    review_config,
                    review_read_paths,
                )
                if reviewer_bridge is not None:
                    additional_bridges.append(reviewer_bridge)
                additional_bridges = tuple(
                    bridge for bridge in additional_bridges if bridge is not None
                )
                if additional_bridges:
                    create_review_kwargs["additional_tool_bridges"] = (
                        additional_bridges
                    )
                review_session = _new_review_session(
                    runtime,
                    **create_review_kwargs,
                )
                if review_config.reuse_session:
                    review_session_cache["session"] = review_session
            try:
                trusted_internal_schema_paths = getattr(
                    review_session,
                    "_trusted_internal_output_schema_paths",
                    None,
                )
                if trusted_internal_schema_paths is not None:
                    trusted_internal_schema_paths.add(schema_path.resolve())
                decision_validation_failures = []
                current_review_input = review_input
                review_send_turn = getattr(review_session, "_send_turn", None)
                if review_send_turn is None:
                    review_send_turn = review_session._send
                while True:
                    review_response = run_with_peer_role(
                        "reviewer",
                        attempt_index,
                        repair_index,
                        lambda: review_send_turn(
                            current_review_input,
                            output_schema_path=schema_path,
                            activity_callback=stream_callback,
                        ),
                    )
                    reviewer_model = (
                        _response_model_name(review_response)
                        or review_config.session.model
                    )
                    job = job_holder.get("job")
                    if job is not None:
                        job._record_usage_observation(
                            usage=review_response.usage,
                            estimated_cost_usd=_estimate_cost_usd_for_usage(
                                usage=review_response.usage,
                                pricing=_resolve_model_pricing(reviewer_model),
                            ),
                            model=reviewer_model,
                            role="reviewer turn",
                        )
                    if review_response.returncode != 0:
                        raise CodexExecutionError(
                            "reviewer turn failed", returncode=review_response.returncode,
                            stdout=review_response.stdout, stderr=review_response.stderr,
                            argv=review_response.argv,
                        )
                    if protocol is None:
                        break
                    from ._peer_review import MAX_CORRECTIONS as _PEER_MAX_CORRECTIONS, dump as peer_dump, parse_reviewer_decision
                    try:
                        parsed = parse_reviewer_decision(
                            review_response.content,
                            open_ids=tuple(
                                item["finding_id"]
                                for item in protocol["open_findings"]
                            ),
                            initial=protocol["initial"],
                            closed_findings=protocol["package"]["closed_findings"],
                        )
                        break
                    except CodexReviewerDecisionValidationError as exc:
                        diagnostic = exc.diagnostic or exc.message
                        index = len(decision_validation_failures)
                        decision_validation_failures.append(
                            (review_response, diagnostic)
                        )
                        review_dir = protocol["review_dir"]
                        peer_dump(review_dir / f"reviewer-decision-attempt-{index:02d}.txt", review_response.content or "")
                        peer_dump(review_dir / f"reviewer-decision-attempt-{index:02d}.diagnostic.txt", diagnostic)
                        if index >= _PEER_MAX_CORRECTIONS:
                            raise CodexReviewProtocolError(
                                f"reviewer decision remained invalid after {_PEER_MAX_CORRECTIONS} corrections",
                                diagnostic=diagnostic,
                            ) from exc
                        current_review_input = (
                            "Your immediately preceding reviewer decision is invalid.\n\n"
                            "Decision validation diagnostic:\n"
                            f"{diagnostic}\n\n"
                            "Correct that decision for the same peer-review protocol "
                            "already provided in this conversation. Return only one "
                            "strict JSON object with exactly these top-level fields: "
                            "decisions and new_findings. Do not add prose, Markdown, "
                            "or fields. Do not invent finding IDs, omit an open finding, "
                            "or use any resolution other than resolved, withdrawn, or "
                            "unresolved, or conflicts. Include conflicts_with and every new "
                            "finding\'s relation as required by the provided schema."
                        )
                if peer_exchange_enabled:
                    reviewer_exchange_config = _peer_exchange_session_config(
                        base_config=review_config.session,
                        options=review_options,
                        read_paths=review_read_paths,
                    )
                    with peer_exchange_lock:
                        previous_source = peer_exchange_state[
                            "reviewer_source"
                        ]
                        peer_exchange_state["reviewer_source"] = review_session
                        peer_exchange_state["reviewer_config"] = (
                            reviewer_exchange_config
                        )
                    if (
                        previous_source is not None
                        and previous_source is not review_session
                    ):
                        _close_session_and_delete_store(previous_source)
            finally:
                if not review_config.reuse_session:
                    with peer_exchange_lock:
                        is_peer_source = (
                            peer_exchange_state["reviewer_source"]
                            is review_session
                        )
                    if is_peer_source:
                        review_session.close()
                    else:
                        _close_session_and_delete_store(review_session)
            content = review_response.content
            if protocol is not None:
                passed = not parsed["new_findings"] and not any(
                    x["resolution"] in {"unresolved", "conflicts"} for x in parsed["decisions"]
                )
                findings = tuple(x["comment"] for x in parsed["new_findings"])
                summary = "accepted" if passed else "peer review has open findings"
            else:
                parsed = _parse_review_output(
                    content, stdout=review_response.stdout, stderr=review_response.stderr,
                    argv=list(review_response.argv),
                )
                passed, findings, summary = parsed["passed"], parsed["findings"], parsed["summary"]
            if review_config.reuse_session and passed:
                close_cached_review_session()
            return _ReviewResult(
                passed=passed,
                findings=findings,
                summary=summary,
                response=review_response,
                stdout=review_response.stdout,
                stderr=review_response.stderr,
                argv=list(review_response.argv),
                input_summary=_format_prompt_summary_for_log(
                    review_input,
                    prefix="review input prepared",
                ),
                read_paths=tuple(review_read_paths),
                stream_emitted=True,
                decision=(parsed if protocol is not None else None),
                decision_validation_failures=tuple(
                    decision_validation_failures
                ),
            )
        finally:
            try:
                schema_path.unlink()
            except FileNotFoundError:
                pass

    try:
        attempt = spawn_attempt(
            prompt=prompt,
            attempt_index=0,
            repair_index=0,
        )
    except Exception:
        if temp_output_schema_path is not None:
            try:
                temp_output_schema_path.unlink()
            except FileNotFoundError:
                pass
        _cleanup_tree(subcwd_path)
        raise
    if _job_owner is None:
        from .job import CodexJob
        job = object.__new__(CodexJob)
        job._legacy_client_runtime = True
    else:
        job = _job_owner
    job._initialize_runtime(
        attempt=attempt,
        spawn_attempt=spawn_attempt,
        open_answer_fork=open_answer_fork,
        original_input=normalized_input,
        context_paths=context_paths,
        context_sections=context_sections,
        output_schema_section=output_schema_section,
        output_model_source_section=output_model_source_section,
        read_paths=read_paths,
        write_paths=write_paths,
        subcwd_files=prompt_subcwd_files,
        default_timeout=options.timeout,
        cwd=options.cwd,
        subcwd_path=subcwd_path,
        ignore_global_context=options.ignore_global_context,
        review_config=review_config,
        review_runner=run_review,
        collect_files=collect_files,
        max_files=max_files,
        max_file_bytes=max_file_bytes,
        max_total_file_bytes=max_total_file_bytes,
        output_validators=output_validators,
        output_schema_path=output_schema_path,
        repair_validation_failures=repair_validation_failures,
        max_review_repairs=(None if review_config is None else review_config.max_repairs),
        repair_prompt_template=repair_prompt_template,
        log_dir=(normalized_log_dir or options.cwd / ".codex-job-runner-logs"),
        log_path=normalized_log_path,
        review_session_cleanup=close_review_resources,
        main_session_cleanup=close_main_session,
        hook_owner=hook_owner,
        hook_no_args=_hook_owner is not _UNSET,
        hooks=_hooks,
        job_id=_job_id,
        native_usage_snapshot=native_usage_snapshot,
        activity_actor=_activity_actor,
        finish_callback=(
            None
            if _borrowed_tool_bridge is not None
            else lambda: _close_owned_runtime_tool_bridge(runtime)
        ),
    )
    job._temp_output_schema_path = temp_output_schema_path
    job._review_subagent_manager_getter = (
        lambda: review_subagent_state["manager"]
    )
    job_holder["job"] = job
    if _rollout_dir is not None:
        rollout_root["path"] = pathlib.Path(_rollout_dir)
    elif job.log_path is not None:
        rollout_root["path"] = pathlib.Path(job.log_path).parent / job.id / "rollouts"
    try:
        _register_runtime_job(runtime, job)
    except Exception:
        job.cancel()
        raise
    if not _defer_background:
        job.start_background()
    return job

def _run_runtime(runtime, **kwargs):
    timeout = kwargs.pop("timeout", None)
    job = _start_runtime(runtime, timeout=timeout, **kwargs)
    return job.result(timeout=timeout)

def _create_worker_pool(runtime, *, max_workers, warmup_jobs=1, fail_fast=False):
    return CodexWorkerPool(
        client=runtime,
        max_workers=max_workers,
        warmup_jobs=warmup_jobs,
        fail_fast=fail_fast,
    )

def _start_review_session_runtime(
    runtime,
    *,
    review,
    read_paths=None,
    model=None,
    reasoning_effort=None,
    cwd=None,
    ignore_global_context=None,
    allowed_skills=None,
    allow_shell_commands=None,
):
    _ensure_runtime_open(runtime, )
    options = _resolve_options(runtime,
        model=model,
        reasoning_effort=reasoning_effort,
        cwd=cwd,
        timeout=None,
        ignore_global_context=ignore_global_context,
        allowed_skills=allowed_skills,
        allow_shell_commands=allow_shell_commands,
    )
    _reject_subcwd_token(read_paths or [], "read_paths")
    review_read_paths = _normalize_paths(
        read_paths or [],
        options.cwd,
        must_exist=True,
        field_name="read_paths",
    )
    review_config = _normalize_review_config(review, options.cwd)
    review_options = _review_options(runtime,
        review_config=review_config,
        options=options,
    )
    return _new_review_session(
        runtime,
        review_config=review_config,
        options=review_options,
        read_paths=review_read_paths,
    )

def _start_session_runtime(
    runtime,
    *,
    config,
    read_paths=None,
    write_paths=None,
    conversation_state=None,
):
    """Create a minimal reusable SDK turn session."""
    _ensure_runtime_open(runtime, )
    options = _resolve_options(runtime,
        model=None,
        reasoning_effort=None,
        cwd=None,
        timeout=None,
        ignore_global_context=None,
        allowed_skills=None,
        allow_shell_commands=None,
    )
    workspace = config.workspace
    raw_read_paths = list(read_paths or [])
    raw_write_paths = list(write_paths or [])
    subcwd_files = _normalize_subcwd_files(dict(workspace.subcwd_files))
    needs_subcwd = (
        bool(subcwd_files)
        or _contains_subcwd_token(raw_read_paths)
        or _contains_subcwd_token(raw_write_paths)
    )
    subcwd_path = _create_subcwd(options.subcwd_root) if needs_subcwd else None
    try:
        _stage_subcwd_files(subcwd_path, subcwd_files)
        normalized_read_paths = _normalize_subcwd_paths(
            raw_read_paths,
            options.cwd,
            subcwd_path,
            must_exist=True,
            field_name="read_paths",
        )
        normalized_write_paths = _normalize_subcwd_paths(
            raw_write_paths,
            options.cwd,
            subcwd_path,
            must_exist=False,
            field_name="write_paths",
        )
        _ensure_staged_subcwd_files_are_accessible(
            subcwd_path,
            subcwd_files,
            normalized_read_paths,
            normalized_write_paths,
        )
        context_entries = _normalize_context_items(
            runtime._runtime_context,
            options.cwd,
        )
        context_sections = _resolve_inline_context_sections(
            context_entries=context_entries,
            inline_context=workspace.inline_context,
            max_file_bytes=workspace.max_inline_context_bytes,
            max_total_bytes=workspace.max_inline_context_total_bytes,
        )
        if not context_sections:
            context_paths = _context_paths_from_entries(context_entries)
            if context_paths:
                context_sections = [
                    _format_path_section("Context paths", context_paths)
                ]
        session = _new_review_session(
            runtime,
            review_config=None,
            options=options,
            read_paths=normalized_read_paths,
            write_paths=normalized_write_paths,
            developer_instructions=None,
            include_tools=True,
            session_config=config,
            conversation_state=conversation_state,
        )
    except Exception:
        _cleanup_tree(subcwd_path)
        raise
    return (
        session,
        "\n\n".join(context_sections),
        lambda: _cleanup_tree(subcwd_path),
    )

def _wait_any(runtime, jobs, timeout=None):
    jobs = list(jobs)
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        for job in jobs:
            if job.done():
                return job
        if deadline is not None and time.monotonic() >= deadline:
            return None
        time.sleep(0.05)

def _review_options(runtime, *, review_config, options):
    session_config = review_config.session
    return _ResolvedOptions(
        model=session_config.model,
        reasoning_effort=session_config.reasoning_effort,
        web_search=session_config.web_search,
        cwd=(
            session_config.workspace.cwd.expanduser().resolve()
            if session_config.workspace.cwd is not None
            else options.cwd
        ),
        subcwd_root=(
            _normalize_subcwd_root(
                session_config.workspace.subcwd_root,
                options.cwd,
            )
            if session_config.workspace.subcwd_root is not None
            else options.subcwd_root
        ),
        timeout=review_config.timeout,
        ignore_global_context=session_config.ignore_global_context,
        allowed_skills=list(session_config.allowed_skills)
        if session_config.allowed_skills is not None
        else None,
        allow_shell_commands=session_config.allow_shell_commands,
        subagents_enabled=False,
        max_concurrent_subagents=2,
        subagent_context_policy="none",
        default_subagent_model=None,
        default_subagent_reasoning_effort=None,
    )

def _close_session_and_delete_store(session):
    """Close a review session this runtime created and delete its store."""
    try:
        session.close()
    except Exception:
        pass
    state = getattr(session, "_conversation_state", None)
    if state is not None:
        state.store.delete()


def _create_review_session(
    runtime,
    *,
    review_config,
    options,
    read_paths,
    write_paths=(),
    developer_instructions=_UNSET,
    include_tools=False,
    session_config=None,
    conversation_state=None,
    additional_tool_bridge=None,
    additional_tool_bridges=(),
    native_usage_collectors=None,
    cleanup_tree=_cleanup_tree,
):
    review_profile_name = "codex_job_runner_session_" + uuid.uuid4().hex
    trusted_internal_output_schema_paths = set()

    review_conversation_state = conversation_state or _ConversationState(
        _ConversationStore(cleanup_tree)
    )
    native_usage_collector = _NativeUsageCollector(
        model=options.model,
        default_subagent_model=options.default_subagent_model,
        role="reviewer",
    )
    if native_usage_collectors is not None:
        native_usage_collectors.append(native_usage_collector)

    def create_review_sdk_session():
        sdk = _import_openai_codex()
        config_overrides = _runtime_config_overrides(runtime,
            options,
            include_tools=include_tools,
        )
        bridges = tuple(additional_tool_bridges)
        if additional_tool_bridge is not None:
            bridges = bridges + (additional_tool_bridge,)
        for bridge_spec in bridges:
            _append_tool_bridge_override(runtime,
                config_overrides,
                server_name=bridge_spec["server_name"],
                bridge=bridge_spec["bridge"],
                token_env_name=bridge_spec["token_env_name"],
                tool_names=bridge_spec["tool_names"],
                tool_timeout=bridge_spec["tool_timeout"],
            )
        config_overrides.append(_project_untrusted_config_override(options.cwd))
        if options.reasoning_effort:
            config_overrides.append(
                f"model_reasoning_effort={_toml_string(options.reasoning_effort)}"
            )
        if options.ignore_global_context:
            config_overrides.extend(
                [
                    "project_doc_max_bytes=0",
                    "features.hooks=false",
                    "skills.include_instructions=false",
                ]
            )
        elif options.allowed_skills == []:
            config_overrides.append("skills.include_instructions=false")
        if not options.allow_shell_commands:
            config_overrides.extend(
                ["features.shell_tool=false", "allow_login_shell=false"]
            )
        extra_read_paths = (
            _codex_runtime_read_paths(runtime._runtime_codex_bin)
            if options.allow_shell_commands
            else []
        )
        filesystem_profile = _filesystem_inline_table(
            options.cwd,
            read_paths,
            write_paths,
            extra_read_paths=extra_read_paths,
        )
        config_overrides.extend(
            [
                f"default_permissions={_toml_string(review_profile_name)}",
                f"permissions.{review_profile_name}.filesystem={filesystem_profile}",
            ]
        )
        session_env = dict(
            _main_sdk_env(runtime, ) if include_tools else runtime._runtime_env
        )
        for bridge_spec in bridges:
            session_env[bridge_spec["token_env_name"]] = (
                bridge_spec["bridge"].token
            )
        sdk_env, config_home = _permission_profile_sdk_env(
            session_env,
            existing_home=(
                None
                if conversation_state is None
                else conversation_state.store.home
            ),
        )
        config_overrides.append(
            "sqlite_home=" + _toml_string(sdk_env["CODEX_SQLITE_HOME"])
        )
        codex = None
        try:
            sdk_config = sdk.CodexConfig(
                codex_bin=runtime._runtime_codex_bin,
                config_overrides=tuple(config_overrides),
                cwd=str(options.cwd),
                env=sdk_env,
            )
            codex = sdk.Codex(config=sdk_config)
            native_usage_collector.attach(codex)
            thread = _sdk_thread_open(
                codex,
                sdk,
                conversation_state=review_conversation_state,
                reasoning_effort=options.reasoning_effort,
                cwd=str(options.cwd),
                model=options.model,
                developer_instructions=(
                    _build_review_session_developer_instructions(
                        "\n\n".join(review_config.context),
                        subagent_context_policy=(
                            options.subagent_context_policy
                            if options.subagents_enabled
                            else None
                        ),
                    )
                    if developer_instructions is _UNSET
                    else developer_instructions
                ),
                ephemeral=False,
                approval_mode=sdk.ApprovalMode.deny_all,
            )
            return codex, thread, config_home
        except Exception:
            if codex is not None:
                try:
                    codex.close()
                except Exception:
                    pass
            if conversation_state is None:
                _cleanup_tree(config_home)
            raise

    sdk_session = _SdkSession(
        create_session=create_review_sdk_session,
        cleanup_tree=_cleanup_tree,
        conversation_state=review_conversation_state,
        native_usage_collector=native_usage_collector,
    )

    def normalize_output_schema_path(output_schema_path):
        if output_schema_path is not None:
            resolved = pathlib.Path(output_schema_path).expanduser().resolve()
            if resolved in trusted_internal_output_schema_paths:
                return resolved
        return _normalize_optional_path(
            output_schema_path,
            options.cwd,
            must_exist=True,
            field_name="session.output_schema_path",
        )

    def send_turn(message, *, output_schema_path=None, activity_callback=None):
        return _run_sdk_session_turn(runtime,
            options=options,
            read_paths=read_paths,
            write_paths=write_paths,
            profile_name=review_profile_name,
            message=message,
            output_schema_path=normalize_output_schema_path(output_schema_path),
            activity_callback=activity_callback,
            sdk_session=sdk_session,
            failure_label="review",
        )

    session = CodexSession(
        config=(
            session_config
            if session_config is not None
            else review_config.session
        ),
    )
    sdk_session._send = send_turn
    sdk_session._trusted_internal_output_schema_paths = trusted_internal_output_schema_paths
    session._turn_session = sdk_session
    session._turn_cleanup = sdk_session.close
    session._conversation_state = sdk_session.conversation_state
    session._trusted_internal_output_schema_paths = (
        trusted_internal_output_schema_paths
    )
    return session

def _resolve_options(
    runtime,
    *,
    model,
    reasoning_effort,
    cwd,
    timeout,
    ignore_global_context,
    allowed_skills,
    allow_shell_commands,
):
    resolved_cwd = pathlib.Path(cwd if cwd is not None else runtime._runtime_cwd or os.getcwd())
    resolved_cwd = resolved_cwd.expanduser().resolve()
    resolved_ignore = (
        ignore_global_context
        if ignore_global_context is not None
        else runtime._runtime_ignore_global_context
    )
    if allowed_skills is None:
        resolved_allowed_skills = _normalize_allowed_skills(runtime._runtime_allowed_skills)
    else:
        resolved_allowed_skills = _normalize_allowed_skills(allowed_skills)
    if resolved_ignore and resolved_allowed_skills is None:
        resolved_allowed_skills = []
    if allow_shell_commands is None:
        resolved_allow_shell_commands = runtime._runtime_allow_shell_commands
    else:
        resolved_allow_shell_commands = bool(allow_shell_commands)
    return _ResolvedOptions(
        model=model if model is not None else runtime._runtime_model,
        web_search=runtime._runtime_web_search,
        reasoning_effort=(
            _normalize_optional_reasoning_effort(reasoning_effort)
            if reasoning_effort is not None
            else runtime._runtime_reasoning_effort
        ),
        cwd=resolved_cwd,
        subcwd_root=_normalize_subcwd_root(runtime._runtime_subcwd_root, resolved_cwd),
        timeout=timeout if timeout is not None else runtime._runtime_timeout,
        ignore_global_context=bool(resolved_ignore),
        allowed_skills=resolved_allowed_skills,
        allow_shell_commands=resolved_allow_shell_commands,
        subagents_enabled=runtime._runtime_subagents_enabled,
        max_concurrent_subagents=runtime._runtime_max_concurrent_subagents,
        subagent_context_policy=runtime._runtime_subagent_context_policy,
        default_subagent_model=runtime._runtime_default_subagent_model,
        default_subagent_reasoning_effort=(
            runtime._runtime_default_subagent_reasoning_effort
        ),
    )

def _runtime_config_overrides(runtime, options, *, include_tools=False):
    overrides = list(_RUNNER_RUNTIME_CONFIG_OVERRIDES)
    web_search = getattr(options, "web_search", None)
    if web_search is not None:
        overrides.append(f"web_search={_toml_string(web_search)}")
    if include_tools and runtime._runtime_tools:
        bridge = _ensure_runtime_tool_bridge(runtime, )
        _append_tool_bridge_override(runtime,
            overrides,
            server_name="codex_job_runner",
            bridge=bridge,
            token_env_name=runtime._tool_env_name,
            tool_names=tuple(tool.name for tool in runtime._runtime_tools),
            tool_timeout=runtime._runtime_tool_timeout,
        )
    if options.subagents_enabled:
        overrides[overrides.index("features.multi_agent=false")] = (
            "features.multi_agent=true"
        )
        overrides.append(
            "max_concurrent_threads_per_session="
            + str(options.max_concurrent_subagents)
        )
    default_subagent_model = getattr(
        options, "default_subagent_model", None
    )
    if default_subagent_model is not None:
        overrides.append(
            "agents.default_subagent_model="
            + _toml_string(default_subagent_model)
        )
    default_subagent_reasoning_effort = getattr(
        options, "default_subagent_reasoning_effort", None
    )
    if default_subagent_reasoning_effort is not None:
        overrides.append(
            "agents.default_subagent_reasoning_effort="
            + _toml_string(default_subagent_reasoning_effort)
        )
    return overrides

def _append_tool_bridge_override(
    runtime,
    overrides,
    *,
    server_name,
    bridge,
    token_env_name,
    tool_names,
    tool_timeout,
):
    if "mcp_servers={}" in overrides:
        overrides.remove("mcp_servers={}")
    enabled_tools = "[" + ",".join(
        _toml_string(name) for name in tool_names
    ) + "]"
    overrides.append(
        f"mcp_servers.{server_name}={{"
        f"url={_toml_string(bridge.url)},"
        f"bearer_token_env_var={_toml_string(token_env_name)},"
        "required=true,"
        "startup_timeout_sec=10.0,"
        f"tool_timeout_sec={float(tool_timeout) + 5.0},"
        f"enabled_tools={enabled_tools},"
        'default_tools_approval_mode="approve"'
        "}"
    )

def _main_sdk_env(runtime):
    env = dict(runtime._runtime_env)
    if runtime._runtime_tools:
        bridge = _ensure_runtime_tool_bridge(runtime, )
        env[runtime._tool_env_name] = bridge.token
    return env

def _build_sdk_argv(
    runtime,
    *,
    profile_name,
    options,
    read_paths,
    write_paths,
    output_schema_path,
):
    argv = [
        "openai-codex",
        "thread.run",
        "--cwd",
        str(options.cwd),
    ]
    if options.model:
        argv.extend(["--model", options.model])
    if options.reasoning_effort:
        argv.extend(["--effort", options.reasoning_effort])
    if output_schema_path is not None:
        argv.extend(["--output-schema", str(output_schema_path)])
    for override in _runtime_config_overrides(runtime,
        options,
        include_tools=True,
    ):
        argv.extend(["-c", override])
    argv.extend(["-c", _project_untrusted_config_override(options.cwd)])
    if options.ignore_global_context:
        argv.extend(["-c", "project_doc_max_bytes=0"])
        argv.extend(["-c", "features.hooks=false"])
    if not options.allow_shell_commands:
        argv.extend(["-c", "features.shell_tool=false"])
        argv.extend(["-c", "allow_login_shell=false"])
    if options.allowed_skills == []:
        argv.extend(["-c", "skills.include_instructions=false"])
    extra_read_paths = (
        _codex_runtime_read_paths(runtime._runtime_codex_bin)
        if options.allow_shell_commands
        else []
    )
    filesystem_profile = _filesystem_inline_table(
        options.cwd,
        read_paths,
        write_paths,
        extra_read_paths=extra_read_paths,
    )
    argv.extend(
        [
            "-c",
            f"default_permissions={_toml_string(profile_name)}",
            "-c",
            f"permissions.{profile_name}.filesystem={filesystem_profile}",
        ]
    )
    return argv

def _run_sdk_session_turn(
    runtime,
    *,
    options,
    read_paths,
    write_paths,
    profile_name,
    message,
    output_schema_path,
    activity_callback,
    sdk_session,
    failure_label="session",
    argv=None,
):
    sdk = _import_openai_codex()
    _codex, thread, _config_home = sdk_session.get_or_create()
    stderr_lines = []
    stdout_lines = []
    live_streamed = False
    turn = None
    try:
        turn = thread.turn(
            message,
            cwd=str(options.cwd),
            model=options.model,
            effort=_sdk_turn_reasoning_effort(sdk, options.reasoning_effort),
            output_schema=_load_output_schema_json(output_schema_path),
        )
        sdk_session.set_active_turn(turn)
        if hasattr(turn, "stream"):
            process = None
            if activity_callback is not None:
                process = _ActivityStreamEmitter(activity_callback)
                live_streamed = True
            stdout, final_response, usage, _items = _consume_turn_via_stream(process=process, turn=turn)
        else:
            started_line = json.dumps({"type": "thread.started"})
            stdout_lines.append(started_line)
            result = turn.run()
            final_response = result.final_response or ""
            for item in result.items:
                item_payload = getattr(item, "root", item)
                item_type = getattr(item_payload, "type", None)
                if not isinstance(item_type, str):
                    item_type = type(item_payload).__name__
                item_text = _stream_item_text(item_payload)
                item_event = {"type": "item.completed", "item_type": item_type}
                if item_text:
                    item_event["item_text"] = item_text
                stdout_lines.append(json.dumps(item_event))
            usage = _normalize_sdk_usage(result.usage)
            if usage is not None:
                stdout_lines.append(json.dumps({"type": "turn.completed", "usage": usage}))
            else:
                stdout_lines.append(json.dumps({"type": "turn.completed"}))
            stdout = "\n".join(stdout_lines) + ("\n" if stdout_lines else "")
        return CodexResponse(
            content=final_response,
            returncode=0,
            stdout=stdout,
            stderr="",
            argv=tuple(argv) if argv is not None else tuple(
                _build_sdk_argv(runtime,
                    profile_name=profile_name,
                    options=options,
                    read_paths=read_paths,
                    write_paths=write_paths,
                    output_schema_path=output_schema_path,
                )
            ),
            usage=usage,
            pricing=_copy_optional_pricing(_resolve_model_pricing(options.model)),
            estimated_cost_usd=_estimate_cost_usd_for_usage(
                usage=usage,
                pricing=_resolve_model_pricing(options.model),
            ),
            files={},
            activity_log=(),
        )
    except Exception as exc:
        stderr_lines.append(traceback.format_exc().rstrip())
        raise CodexExecutionError(
            f"codex {failure_label} job exited with status 1"
            + ("\nstderr:\n" + "\n".join(stderr_lines) if stderr_lines else ""),
            returncode=1,
            stdout="\n".join(stdout_lines) + ("\n" if stdout_lines else ""),
            stderr="\n".join(stderr_lines),
            argv=(tuple(argv) if argv is not None else _build_sdk_argv(runtime,
                profile_name=profile_name,
                options=options,
                read_paths=read_paths,
                write_paths=[],
                output_schema_path=output_schema_path,
            )),
        )
    finally:
        if turn is not None:
            sdk_session.clear_active_turn(turn)
