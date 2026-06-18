"""Client and job implementation for Codex SDK-backed jobs."""

from __future__ import annotations

import json
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
from dataclasses import replace
from typing import Any, Iterable, Optional, Sequence

from ._client_helpers import *  # noqa: F401,F403
from ._job import CodexJob
from ._process import _ActivityStreamEmitter, _SdkProcess
from ._session import ReviewSession
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
    _REVIEW_OUTPUT_SCHEMA,
    _RUNNER_ALWAYS_OFF_CONFIG_OVERRIDES,
    _RUNNING,
    _SUBCWD_TOKEN,
    _TIMEOUT,
)
from ._models import (
    ReviewConfig,
    _CodexAttempt,
    _ResolvedOptions,
    _ResolvedReviewConfig,
    _ReviewResult,
    _SubcwdFile,
    _ValidationResult,
)
from .errors import (
    CodexCancelledError,
    CodexExecutionError,
    CodexTimeoutError,
    CodexValidationError,
)
from .types import CodexActivity, CodexResponse










class CodexClient:
    """OpenAI-like entry point for a minimal Codex job wrapper."""

    def __init__(
        self,
        *,
        model=_DEFAULT_MODEL,
        reasoning_effort=_DEFAULT_REASONING_EFFORT,
        cwd=None,
        subcwd_root=None,
        timeout=None,
        ephemeral=True,
        ignore_global_context=False,
        allowed_skills=None,
        allow_shell_commands=True,
        env=None,
        log_dir=None,
    ):
        self.model = model
        self.reasoning_effort = _normalize_optional_reasoning_effort(
            reasoning_effort
        )
        self.cwd = cwd
        self.subcwd_root = subcwd_root
        self.timeout = timeout
        self.ephemeral = bool(ephemeral)
        self.ignore_global_context = ignore_global_context
        self.allowed_skills = _normalize_allowed_skills(allowed_skills)
        self.allow_shell_commands = bool(allow_shell_commands)
        self.env = dict(env or {})
        self.log_dir = pathlib.Path(log_dir).expanduser() if log_dir is not None else None

    def start(
        self,
        *,
        input,
        context=None,
        read_paths=None,
        write_paths=None,
        subcwd_files=None,
        model=None,
        reasoning_effort=None,
        cwd=None,
        timeout=None,
        ephemeral=None,
        review=None,
        ignore_global_context=None,
        allowed_skills=None,
        allow_shell_commands=None,
        output_schema_path=None,
        output_pydantic_model=None,
        output_validator=None,
        validator_timeout=30,
        repair_validation_failures=False,
        max_repairs=5,
        max_validator_repairs=None,
        max_review_repairs=None,
        repair_prompt_path=None,
        collect_files=False,
        max_files=_DEFAULT_MAX_FILES,
        max_file_bytes=_DEFAULT_MAX_FILE_BYTES,
        max_total_file_bytes=_DEFAULT_MAX_TOTAL_FILE_BYTES,
        inline_context=True,
        max_inline_context_bytes=_DEFAULT_MAX_INLINE_CONTEXT_BYTES,
        max_inline_context_total_bytes=_DEFAULT_MAX_INLINE_CONTEXT_TOTAL_BYTES,
        log_path=None,
    ):
        if not isinstance(input, str) or not input.strip():
            raise ValueError("input must be a non-empty string")
        if output_schema_path is not None and output_pydantic_model is not None:
            raise ValueError(
                "output_schema_path and output_pydantic_model are mutually exclusive"
            )

        options = self._resolve_options(
            model=model,
            reasoning_effort=reasoning_effort,
            cwd=cwd,
            timeout=timeout,
            ephemeral=ephemeral,
            ignore_global_context=ignore_global_context,
            allowed_skills=allowed_skills,
            allow_shell_commands=allow_shell_commands,
        )
        raw_read_paths = list(read_paths or [])
        raw_write_paths = list(write_paths or [])
        raw_context_for_reject = (
            [context] if isinstance(context, (str, os.PathLike)) else list(context or [])
        )
        _reject_subcwd_token(raw_context_for_reject, "context")
        context_entries = _normalize_context_items(context, options.cwd)
        context_paths = _context_paths_from_entries(context_entries)
        subcwd_files = _normalize_subcwd_files(subcwd_files)
        needs_subcwd = (
            bool(subcwd_files)
            or _contains_subcwd_token(raw_read_paths)
            or _contains_subcwd_token(raw_write_paths)
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
        except Exception:
            _cleanup_tree(subcwd_path)
            raise
        normalized_output_pydantic_model = _normalize_output_pydantic_model(
            output_pydantic_model
        )
        output_schema_path = _normalize_optional_path(
            output_schema_path,
            options.cwd,
            must_exist=True,
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
        output_validator = _normalize_output_validator(output_validator, options.cwd)
        normalized_log_path = _normalize_optional_path(
            log_path,
            options.cwd,
            must_exist=False,
        ) if log_path is not None else None
        repair_prompt_path = _normalize_optional_path(
            repair_prompt_path,
            options.cwd,
            must_exist=True,
        )
        repair_prompt_template = None
        if repair_prompt_path is not None:
            repair_prompt_template = repair_prompt_path.read_text(encoding="utf-8")
        if not isinstance(max_repairs, int) or max_repairs < 0:
            raise ValueError("max_repairs must be a non-negative integer")
        if max_validator_repairs is not None and (
            not isinstance(max_validator_repairs, int) or max_validator_repairs < 0
        ):
            raise ValueError("max_validator_repairs must be a non-negative integer or None")
        if max_review_repairs is not None and (
            not isinstance(max_review_repairs, int) or max_review_repairs < 0
        ):
            raise ValueError("max_review_repairs must be a non-negative integer or None")
        review_config = _normalize_review_config(review, options.cwd)
        review_notice = _build_review_notice() if review_config is not None else None
        prompt_subcwd_files = _prompt_subcwd_file_paths(subcwd_files)
        prompt = _build_prompt(
            input=input,
            context_paths=context_paths,
            read_paths=read_paths,
            write_paths=write_paths,
            subcwd_files=prompt_subcwd_files,
            subcwd_path=subcwd_path,
            ignore_global_context=options.ignore_global_context,
            context_sections=context_sections,
            output_schema_section=output_schema_section,
            output_model_source_section=output_model_source_section,
            review_notice=review_notice,
        )
        profile_name = "codex_job_runner_" + uuid.uuid4().hex
        env = os.environ.copy()
        env.update(self.env)
        main_sdk_state = {"codex": None, "thread": None}
        main_sdk_lock = threading.RLock()

        def close_main_session():
            with main_sdk_lock:
                codex = main_sdk_state.get("codex")
                main_sdk_state["codex"] = None
                main_sdk_state["thread"] = None
            if codex is not None:
                try:
                    codex.close()
                except Exception:
                    pass

        def spawn_attempt(*, prompt, attempt_index, repair_index):
            output_path = _make_output_path()
            argv = self._build_sdk_argv(
                profile_name=profile_name,
                options=options,
                read_paths=read_paths,
                write_paths=write_paths,
                output_schema_path=output_schema_path,
            )
            process = _SdkProcess(
                runner=lambda process_obj, prompt_text: self._run_sdk_attempt(
                    process=process_obj,
                    prompt=prompt_text,
                    output_path=pathlib.Path(output_path),
                    profile_name=profile_name,
                    options=options,
                    read_paths=read_paths,
                    write_paths=write_paths,
                    output_schema_path=output_schema_path,
                    sdk_state=main_sdk_state,
                    sdk_lock=main_sdk_lock,
                )
            )
            return _CodexAttempt(
                argv=list(argv),
                prompt=prompt,
                process=process,
                output_path=pathlib.Path(output_path),
                attempt_index=attempt_index,
                repair_index=repair_index,
            )

        review_session_cache = {"session": None}

        def close_cached_review_session():
            session = review_session_cache.get("session")
            if session is None:
                return
            review_session_cache["session"] = None
            session.close()

        def run_review(*, original_input, main_context, response, subcwd_path, review_config, stream_callback):
            review_options = self._review_options(
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
            schema_path = pathlib.Path(
                _make_temp_text_file(
                    prefix="codex_job_runner_review_",
                    suffix=".json",
                    text=_REVIEW_OUTPUT_SCHEMA,
                )
            )
            output_path = pathlib.Path(_make_output_path())
            try:
                output_schema_section, _ = _resolve_inline_output_schema_section(
                    output_schema_path=schema_path,
                    max_file_bytes=_DEFAULT_MAX_INLINE_CONTEXT_BYTES,
                )
                review_input = _build_review_input(
                    original_input=original_input,
                    main_context=main_context,
                    response=response,
                    subcwd_path=review_visible_subcwd_path,
                    review_config=review_config,
                    review_read_paths=review_read_paths,
                )
                review_session = None
                if review_config.reuse_session:
                    review_session = review_session_cache.get("session")
                if review_session is None:
                    review_session = self._create_review_session(
                        review_config=review_config,
                        options=review_options,
                        read_paths=review_read_paths,
                    )
                    if review_config.reuse_session:
                        review_session_cache["session"] = review_session
                try:
                    review_response = review_session._send(
                        review_input,
                        output_schema_path=schema_path,
                        activity_callback=stream_callback,
                    )
                finally:
                    if not review_config.reuse_session:
                        review_session.close()
                content = review_response.content
                parsed = _parse_review_output(
                    content,
                    stdout=review_response.stdout,
                    stderr=review_response.stderr,
                    argv=list(review_response.argv),
                )
                if review_config.reuse_session and parsed["passed"]:
                    close_cached_review_session()
                return _ReviewResult(
                    passed=parsed["passed"],
                    repair_text=parsed["repair_text"],
                    summary=parsed["summary"],
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
        job = CodexJob(
            attempt=attempt,
            spawn_attempt=spawn_attempt,
            original_input=input.strip(),
            context_paths=context_paths,
            context_sections=context_sections,
            output_schema_section=output_schema_section,
            output_model_source_section=output_model_source_section,
            read_paths=read_paths,
            write_paths=write_paths,
            subcwd_files=prompt_subcwd_files,
            default_timeout=options.timeout,
            subcwd_path=subcwd_path,
            ignore_global_context=options.ignore_global_context,
            review_config=review_config,
            review_runner=run_review,
            collect_files=collect_files,
            max_files=max_files,
            max_file_bytes=max_file_bytes,
            max_total_file_bytes=max_total_file_bytes,
            output_validator=output_validator,
            validator_timeout=validator_timeout,
            output_schema_path=output_schema_path,
            output_pydantic_model=normalized_output_pydantic_model,
            repair_validation_failures=repair_validation_failures,
            max_repairs=max_repairs,
            max_validator_repairs=max_validator_repairs,
            max_review_repairs=max_review_repairs,
            repair_prompt_template=repair_prompt_template,
            validator_env=env,
            validator_cwd=options.cwd,
            log_dir=(self.log_dir or options.cwd / ".codex-job-runner-logs"),
            log_path=normalized_log_path,
            review_session_cleanup=close_cached_review_session,
            main_session_cleanup=close_main_session,
        )
        job._temp_output_schema_path = temp_output_schema_path
        return job

    def run(self, **kwargs):
        timeout = kwargs.pop("timeout", None)
        job = self.start(timeout=timeout, **kwargs)
        return job.result(timeout=timeout)

    def start_review_session(
        self,
        *,
        review,
        read_paths=None,
        model=None,
        reasoning_effort=None,
        cwd=None,
        ephemeral=None,
        ignore_global_context=None,
        allowed_skills=None,
        allow_shell_commands=None,
    ):
        options = self._resolve_options(
            model=model,
            reasoning_effort=reasoning_effort,
            cwd=cwd,
            timeout=None,
            ephemeral=ephemeral,
            ignore_global_context=ignore_global_context,
            allowed_skills=allowed_skills,
            allow_shell_commands=allow_shell_commands,
        )
        _reject_subcwd_token(read_paths or [], "read_paths")
        review_read_paths = _normalize_paths(
            read_paths or [],
            options.cwd,
            must_exist=True,
        )
        review_config = _normalize_review_config(review, options.cwd)
        review_options = self._review_options(
            review_config=review_config,
            options=options,
        )
        return self._create_review_session(
            review_config=review_config,
            options=review_options,
            read_paths=review_read_paths,
        )

    def wait_any(self, jobs, timeout=None):
        jobs = list(jobs)
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            for job in jobs:
                if job.done():
                    return job
            if deadline is not None and time.monotonic() >= deadline:
                return None
            time.sleep(0.05)

    def _review_options(self, *, review_config, options):
        return _ResolvedOptions(
            model=(
                review_config.model
                if review_config.model is not None
                else options.model
            ),
            reasoning_effort=(
                review_config.reasoning_effort
                if review_config.reasoning_effort is not None
                else options.reasoning_effort
            ),
            cwd=options.cwd,
            subcwd_root=options.subcwd_root,
            timeout=review_config.timeout,
            ephemeral=options.ephemeral,
            ignore_global_context=options.ignore_global_context,
            allowed_skills=options.allowed_skills,
            allow_shell_commands=options.allow_shell_commands,
        )

    def _create_review_session(self, *, review_config, options, read_paths):
        review_profile_name = "codex_job_runner_review_" + uuid.uuid4().hex
        lock = threading.RLock()
        sdk_state = {"codex": None, "thread": None}

        def close_session():
            with lock:
                codex = sdk_state["codex"]
                sdk_state["codex"] = None
                sdk_state["thread"] = None
            if codex is not None:
                try:
                    codex.close()
                except Exception:
                    pass

        def send_turn(*, message, history, output_schema_path, activity_callback=None):
            return self._run_sdk_review_session_turn(
                review_config=review_config,
                options=options,
                read_paths=read_paths,
                profile_name=review_profile_name,
                message=message,
                history=history,
                output_schema_path=output_schema_path,
                activity_callback=activity_callback,
                sdk_state=sdk_state,
                lock=lock,
            )

        return ReviewSession(
            send_turn=send_turn,
            close_session=close_session,
        )

    def _resolve_options(
        self,
        *,
        model,
        reasoning_effort,
        cwd,
        timeout,
        ephemeral,
        ignore_global_context,
        allowed_skills,
        allow_shell_commands,
    ):
        resolved_cwd = pathlib.Path(cwd if cwd is not None else self.cwd or os.getcwd())
        resolved_cwd = resolved_cwd.expanduser().resolve()
        resolved_ignore = (
            ignore_global_context
            if ignore_global_context is not None
            else self.ignore_global_context
        )
        if allowed_skills is None:
            resolved_allowed_skills = _normalize_allowed_skills(self.allowed_skills)
        else:
            resolved_allowed_skills = _normalize_allowed_skills(allowed_skills)
        if resolved_ignore and resolved_allowed_skills is None:
            resolved_allowed_skills = []
        if allow_shell_commands is None:
            resolved_allow_shell_commands = self.allow_shell_commands
        else:
            resolved_allow_shell_commands = bool(allow_shell_commands)
        return _ResolvedOptions(
            model=model if model is not None else self.model,
            reasoning_effort=(
                _normalize_optional_reasoning_effort(reasoning_effort)
                if reasoning_effort is not None
                else self.reasoning_effort
            ),
            cwd=resolved_cwd,
            subcwd_root=_normalize_subcwd_root(self.subcwd_root, resolved_cwd),
            timeout=timeout if timeout is not None else self.timeout,
            ephemeral=self.ephemeral if ephemeral is None else bool(ephemeral),
            ignore_global_context=bool(resolved_ignore),
            allowed_skills=resolved_allowed_skills,
            allow_shell_commands=resolved_allow_shell_commands,
        )

    def _build_sdk_argv(
        self,
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
        for override in _RUNNER_ALWAYS_OFF_CONFIG_OVERRIDES:
            argv.extend(["-c", override])
        if options.ignore_global_context:
            argv.extend(["-c", "project_doc_max_bytes=0"])
            argv.extend(["-c", "features.hooks=false"])
        if not options.allow_shell_commands:
            argv.extend(["-c", "features.shell_tool=false"])
            argv.extend(["-c", "allow_login_shell=false"])
        if options.allowed_skills == []:
            argv.extend(["-c", "skills.include_instructions=false"])
        extra_read_paths = (
            _codex_runtime_read_paths() if options.allow_shell_commands else []
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

    def _run_sdk_attempt(
        self,
        *,
        process,
        prompt,
        output_path,
        profile_name,
        options,
        read_paths,
        write_paths,
        output_schema_path,
        sdk_state,
        sdk_lock,
    ):
        sdk = _import_openai_codex()
        config_overrides = list(_RUNNER_ALWAYS_OFF_CONFIG_OVERRIDES)
        if options.reasoning_effort:
            config_overrides.append(
                f"model_reasoning_effort={_toml_string(options.reasoning_effort)}"
            )
        if options.ignore_global_context:
            config_overrides.append("project_doc_max_bytes=0")
            config_overrides.append("features.hooks=false")
            config_overrides.append("skills.include_instructions=false")
        elif options.allowed_skills == []:
            config_overrides.append("skills.include_instructions=false")
        if not options.allow_shell_commands:
            config_overrides.append("features.shell_tool=false")
            config_overrides.append("allow_login_shell=false")
        extra_read_paths = (
            _codex_runtime_read_paths() if options.allow_shell_commands else []
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

        sdk_config = sdk.CodexConfig(
            config_overrides=tuple(config_overrides),
            cwd=str(options.cwd),
            env=dict(self.env),
        )
        sandbox = _sdk_sandbox_for_permissions(sdk, write_paths)
        output_schema = _load_output_schema_json(output_schema_path)

        stderr_lines = []
        returncode = 0
        try:
            with sdk_lock:
                codex = sdk_state["codex"]
                thread = sdk_state["thread"]
                if codex is None or thread is None:
                    codex = sdk.Codex(config=sdk_config)
                    thread = codex.thread_start(
                        cwd=str(options.cwd),
                        model=options.model,
                        developer_instructions=None,
                        ephemeral=options.ephemeral,
                        sandbox=sandbox,
                        approval_mode=sdk.ApprovalMode.deny_all,
                    )
                    sdk_state["codex"] = codex
                    sdk_state["thread"] = thread
            process.set_codex(codex)
            turn = thread.turn(
                prompt,
                cwd=str(options.cwd),
                model=options.model,
                effort=options.reasoning_effort,
                output_schema=output_schema,
                sandbox=sandbox,
            )
            process.set_turn_handle(turn)
            if hasattr(turn, "stream"):
                stdout, final_response, usage, items = _consume_turn_via_stream(process, turn)
            else:
                stdout_lines = []
                started_line = json.dumps({"type": "thread.started"})
                stdout_lines.append(started_line)
                result = turn.run()
                final_response = result.final_response or ""
                items = result.items
                usage = _normalize_sdk_usage(result.usage)
                for item in items:
                    item_payload = getattr(item, "root", item)
                    item_type = getattr(item_payload, "type", None)
                    if not isinstance(item_type, str):
                        item_type = type(item_payload).__name__
                    item_text = _stream_item_text(item_payload)
                    item_event = {"type": "item.completed", "item_type": item_type}
                    if item_text:
                        item_event["item_text"] = item_text
                    stdout_lines.append(json.dumps(item_event))
                if usage is not None:
                    stdout_lines.append(json.dumps({"type": "turn.completed", "usage": usage}))
                else:
                    stdout_lines.append(json.dumps({"type": "turn.completed"}))
                stdout = "\n".join(stdout_lines) + ("\n" if stdout_lines else "")
            if process.terminated():
                return "", "", 1
            output_path.write_text(final_response, encoding="utf-8")
        except Exception as exc:
            returncode = 1
            stderr_lines.append(traceback.format_exc().rstrip())
        return stdout if "stdout" in locals() else "", "\n".join(stderr_lines), returncode

    def _run_sdk_review_session_turn(
        self,
        *,
        review_config,
        options,
        read_paths,
        profile_name,
        message,
        history,
        output_schema_path,
        activity_callback,
        sdk_state,
        lock,
    ):
        sdk = _import_openai_codex()
        config_overrides = list(_RUNNER_ALWAYS_OFF_CONFIG_OVERRIDES)
        if options.reasoning_effort:
            config_overrides.append(
                f"model_reasoning_effort={_toml_string(options.reasoning_effort)}"
            )
        if options.ignore_global_context:
            config_overrides.append("project_doc_max_bytes=0")
            config_overrides.append("features.hooks=false")
            config_overrides.append("skills.include_instructions=false")
        elif options.allowed_skills == []:
            config_overrides.append("skills.include_instructions=false")
        if not options.allow_shell_commands:
            config_overrides.append("features.shell_tool=false")
            config_overrides.append("allow_login_shell=false")
        extra_read_paths = (
            _codex_runtime_read_paths() if options.allow_shell_commands else []
        )
        filesystem_profile = _filesystem_inline_table(
            options.cwd,
            read_paths,
            [],
            extra_read_paths=extra_read_paths,
        )
        config_overrides.extend(
            [
                f"default_permissions={_toml_string(profile_name)}",
                f"permissions.{profile_name}.filesystem={filesystem_profile}",
            ]
        )
        with lock:
            codex = sdk_state["codex"]
            thread = sdk_state["thread"]
            if codex is None or thread is None:
                sdk_config = sdk.CodexConfig(
                    config_overrides=tuple(config_overrides),
                    cwd=str(options.cwd),
                    env=dict(self.env),
                )
                codex = sdk.Codex(config=sdk_config)
                thread = codex.thread_start(
                    cwd=str(options.cwd),
                    model=options.model,
                    developer_instructions=_build_review_session_developer_instructions(
                        "\n\n".join(review_config.context)
                    ),
                    ephemeral=options.ephemeral,
                    sandbox=sdk.Sandbox.read_only,
                    approval_mode=sdk.ApprovalMode.deny_all,
                )
                sdk_state["codex"] = codex
                sdk_state["thread"] = thread
        stderr_lines = []
        stdout_lines = []
        turn_input = _build_review_session_turn_input(
            history=history,
            message=message,
        )
        live_streamed = False
        try:
            turn = thread.turn(
                turn_input,
                cwd=str(options.cwd),
                model=options.model,
                effort=options.reasoning_effort,
                output_schema=_load_output_schema_json(output_schema_path),
                sandbox=sdk.Sandbox.read_only,
            )
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
                argv=tuple(
                    self._build_sdk_argv(
                        profile_name=profile_name,
                        options=options,
                        read_paths=read_paths,
                        write_paths=[],
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
                "codex review job exited with status 1"
                + ("\nstderr:\n" + "\n".join(stderr_lines) if stderr_lines else ""),
                returncode=1,
                stdout="\n".join(stdout_lines) + ("\n" if stdout_lines else ""),
                stderr="\n".join(stderr_lines),
                argv=self._build_sdk_argv(
                    profile_name=profile_name,
                    options=options,
                    read_paths=read_paths,
                    write_paths=[],
                    output_schema_path=output_schema_path,
                ),
            )
