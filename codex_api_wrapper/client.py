"""Client and job implementation for Codex SDK-backed jobs."""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import json
import hashlib
import importlib
import inspect
import traceback
from dataclasses import dataclass, replace
from typing import Any, Iterable, Optional, Sequence

from .errors import (
    CodexCancelledError,
    CodexExecutionError,
    CodexTimeoutError,
    CodexValidationError,
)
from .types import CodexActivity, CodexFile, CodexResponse


_SUBCWD_TOKEN = "${SUBCWD}"
_DEFAULT_MAX_FILES = 10000
_DEFAULT_MAX_FILE_BYTES = 1073741824
_DEFAULT_MAX_TOTAL_FILE_BYTES = 4294967296
_DEFAULT_MAX_INLINE_ATTACHMENT_BYTES = 262144
_DEFAULT_MAX_INLINE_ATTACHMENT_TOTAL_BYTES = 1048576
_DEFAULT_MODEL = "gpt-5.4-mini"
_DEFAULT_REASONING_EFFORT = "medium"
_RUNNING = "running"
_COMPLETED = "completed"
_FAILED = "failed"
_TIMEOUT = "timeout"
_CANCELLED = "cancelled"
_DEFAULT_REVIEW_TIMEOUT = None
_API_PRICING_SOURCE_URL = "https://developers.openai.com/api/docs/pricing"
_MODEL_PRICING_USD_PER_MILLION = {
    "gpt-5.5": {"input": 5.00, "cached_input": 0.50, "output": 30.00},
    "gpt-5.5-pro": {"input": 30.00, "cached_input": None, "output": 180.00},
    "gpt-5.4": {"input": 2.50, "cached_input": 0.25, "output": 15.00},
    "gpt-5.4-mini": {"input": 0.75, "cached_input": 0.075, "output": 4.50},
    "gpt-5.4-nano": {"input": 0.20, "cached_input": 0.02, "output": 1.25},
    "gpt-5.4-pro": {"input": 30.00, "cached_input": None, "output": 180.00},
    "gpt-5.3-codex": {"input": 1.75, "cached_input": 0.175, "output": 14.00},
}
_REVIEW_OUTPUT_SCHEMA = json.dumps(
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "passed": {"type": "boolean"},
            "repair_text": {"type": "string"},
            "summary": {"type": "string"},
        },
        "required": ["passed", "repair_text", "summary"],
    },
    separators=(",", ":"),
)


@dataclass(frozen=True)
class ReviewConfig:
    instructions: str
    prompt_path: str | None = None
    read_paths: tuple[str, ...] | None = None
    timeout: int | float | None = _DEFAULT_REVIEW_TIMEOUT
    model: str | None = None
    reasoning_effort: str | None = None
    reuse_session: bool = False
    include_main_prompt: bool = False


@dataclass(frozen=True)
class _ResolvedOptions:
    model: Optional[str]
    reasoning_effort: Optional[str]
    cwd: pathlib.Path
    subcwd_root: pathlib.Path
    timeout: Optional[float]
    ephemeral: bool
    ignore_global_context: bool
    allowed_skills: Optional[list[str]]
    allow_shell_commands: bool


@dataclass(frozen=True)
class _CodexAttempt:
    argv: list[str]
    prompt: str
    process: Any
    output_path: pathlib.Path
    attempt_index: int
    repair_index: int


@dataclass(frozen=True)
class _ValidationResult:
    returncode: Optional[int]
    stdout: str
    stderr: str
    argv: list[str]
    timed_out: bool = False


@dataclass(frozen=True)
class _ResolvedReviewConfig:
    instructions: str
    prompt_template: str | None
    read_paths: tuple[str, ...] | None
    timeout: int | float | None
    model: str | None
    reasoning_effort: str | None
    reuse_session: bool
    include_main_prompt: bool


@dataclass(frozen=True)
class _SubcwdFile:
    relative_path: str
    data: bytes


@dataclass(frozen=True)
class _ReviewResult:
    passed: bool
    repair_text: str
    summary: str
    response: CodexResponse
    stdout: str
    stderr: str
    argv: list[str]
    input_summary: str = ""
    read_paths: tuple[pathlib.Path, ...] = ()
    stream_emitted: bool = False


class ReviewSession:
    """Conversation session for a reviewer."""

    def __init__(
        self,
        *,
        send_turn,
        close_session,
    ):
        self._send_turn = send_turn
        self._close_session = close_session
        self._lock = threading.RLock()
        self._closed = False
        self._history: list[tuple[str, str]] = []

    @property
    def history(self):
        with self._lock:
            return tuple(self._history)

    def send(self, message, *, output_schema_path=None):
        activity_log = []

        def activity_callback(line):
            entry = _stream_activity_from_line(line, detail="review")
            if entry is None:
                return
            activity_log.append(entry)
            _emit_activity_stdout(entry)

        response = self._send(
            message,
            output_schema_path=output_schema_path,
            activity_callback=activity_callback,
        )
        if not activity_log and response.stdout:
            for line in response.stdout.splitlines():
                entry = _stream_activity_from_line(line, detail="review")
                if entry is not None:
                    activity_log.append(entry)
                    _emit_activity_stdout(entry)
        if activity_log:
            return replace(response, activity_log=tuple(activity_log))
        return response

    def _send(self, message, *, output_schema_path=None, activity_callback=None):
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message must be a non-empty string")
        with self._lock:
            if self._closed:
                raise RuntimeError("review session is closed")
            history = tuple(self._history)
        response = self._send_turn(
            message=message.strip(),
            history=history,
            output_schema_path=output_schema_path,
            activity_callback=activity_callback,
        )
        with self._lock:
            if self._closed:
                raise RuntimeError("review session is closed")
            self._history.append(("runner", message.strip()))
            self._history.append(("reviewer", response.content))
        return response

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._close_session()


class _SdkProcess:
    """Subprocess-like adapter backed by the ``openai-codex`` SDK."""

    def __init__(self, *, runner):
        self._runner = runner
        self._thread = None
        self._lock = threading.Lock()
        self._terminated = threading.Event()
        self._finished = threading.Event()
        self._stdout = ""
        self._stderr = ""
        self._returncode = None
        self._turn_handle = None
        self._codex = None
        self._error = None
        self._activity_callback = None
        self._emitted_stdout_lines = False

    @property
    def returncode(self):
        return self._returncode

    def communicate(self, prompt=None, timeout=None):
        if self._thread is None:
            if prompt is None:
                raise RuntimeError("communicate() requires prompt on first call")

            def target():
                try:
                    stdout, stderr, returncode = self._runner(self, prompt)
                except Exception as exc:
                    with self._lock:
                        self._stdout = ""
                        self._stderr = str(exc)
                        self._returncode = 1
                        self._error = exc
                else:
                    with self._lock:
                        self._stdout = stdout or ""
                        self._stderr = stderr or ""
                        self._returncode = returncode
                finally:
                    self._finished.set()

            self._thread = threading.Thread(target=target, name="codex_sdk_attempt", daemon=True)
            self._thread.start()
        elif prompt is not None:
            raise RuntimeError("communicate() prompt may only be provided once")

        if not self._finished.wait(timeout):
            raise subprocess.TimeoutExpired(cmd="openai-codex", timeout=timeout)
        return self._stdout, self._stderr

    def poll(self):
        if self._finished.is_set():
            return self._returncode
        return None

    def wait(self, timeout=None):
        if not self._finished.wait(timeout):
            raise subprocess.TimeoutExpired(cmd="openai-codex", timeout=timeout)
        return self._returncode

    def terminate(self):
        self._terminated.set()
        turn_handle = self._turn_handle
        codex = self._codex
        if turn_handle is not None:
            try:
                turn_handle.interrupt()
            except Exception:
                pass
        if codex is not None:
            try:
                codex.close()
            except Exception:
                pass

    def kill(self):
        self.terminate()

    def set_turn_handle(self, turn_handle):
        self._turn_handle = turn_handle

    def set_codex(self, codex):
        self._codex = codex

    def set_activity_callback(self, callback):
        self._activity_callback = callback

    def emit_stdout_line(self, line):
        callback = None
        with self._lock:
            self._emitted_stdout_lines = True
            callback = self._activity_callback
        if callback is not None:
            callback(line)

    def emitted_stdout_lines(self):
        with self._lock:
            return self._emitted_stdout_lines

    def terminated(self):
        return self._terminated.is_set()


class _ActivityStreamEmitter:
    def __init__(self, callback):
        self._callback = callback

    def emit_stdout_line(self, line):
        self._callback(line)


class CodexJob:
    """A non-blocking Codex job handle."""

    def __init__(
        self,
        *,
        attempt,
        spawn_attempt,
        original_input,
        attachments,
        inline_attachment_sections,
        output_schema_section,
        output_model_source_section,
        read_paths,
        write_paths,
        subcwd_files,
        default_timeout=None,
        subcwd_path=None,
        ignore_global_context=False,
        review_config=None,
        review_runner=None,
        collect_files=False,
        max_files=_DEFAULT_MAX_FILES,
        max_file_bytes=_DEFAULT_MAX_FILE_BYTES,
        max_total_file_bytes=_DEFAULT_MAX_TOTAL_FILE_BYTES,
        output_validator=None,
        validator_timeout=30,
        output_schema_path=None,
        output_pydantic_model=None,
        repair_validation_failures=False,
        max_repairs=5,
        max_validator_repairs=None,
        max_review_repairs=None,
        repair_prompt_template=None,
        validator_env=None,
        validator_cwd=None,
        log_dir=None,
        log_path=None,
        review_session_cleanup=None,
    ):
        self.id = "codex_job_" + uuid.uuid4().hex
        self.log_path = _prepare_job_log_path(log_dir, self.id, log_path=log_path)
        self.argv = list(attempt.argv)
        self._spawn_attempt = spawn_attempt
        self._original_input = original_input
        self._initial_main_prompt = attempt.prompt
        self._attachments = list(attachments)
        self._inline_attachment_sections = list(inline_attachment_sections)
        self._output_schema_section = output_schema_section
        self._output_model_source_section = output_model_source_section
        self._read_paths = list(read_paths)
        self._write_paths = list(write_paths)
        self._subcwd_files = list(subcwd_files)
        self._attempt = attempt
        self._process = attempt.process
        self._output_paths = [attempt.output_path]
        self._default_timeout = default_timeout
        self._subcwd_path = pathlib.Path(subcwd_path) if subcwd_path else None
        self._ignore_global_context = ignore_global_context
        self._review_config = review_config
        self._review_runner = review_runner
        self._should_collect_files = collect_files
        self._max_files = max_files
        self._max_file_bytes = max_file_bytes
        self._max_total_file_bytes = max_total_file_bytes
        self._output_validator = list(output_validator) if output_validator else None
        self._validator_timeout = validator_timeout
        self._output_schema_path = pathlib.Path(output_schema_path) if output_schema_path else None
        self._output_pydantic_model = output_pydantic_model
        self._temp_output_schema_path = None
        self._repair_validation_failures = bool(repair_validation_failures)
        self._max_repairs = max_repairs
        self._max_validator_repairs = (
            max_repairs if max_validator_repairs is None else max_validator_repairs
        )
        self._max_review_repairs = (
            max_repairs if max_review_repairs is None else max_review_repairs
        )
        self._repair_prompt_template = repair_prompt_template
        self._validator_env = dict(validator_env or {})
        self._validator_cwd = pathlib.Path(validator_cwd) if validator_cwd else None
        self._review_session_cleanup = review_session_cleanup
        self._validator_process = None
        self._lock = threading.RLock()
        self._done = threading.Event()
        self._status = _RUNNING
        self._stdout = ""
        self._stderr = ""
        self._returncode = None
        self._response = None
        self._error = None
        self._activity_log = []
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
        self._thread.start()

    @property
    def status(self):
        self._refresh_status()
        with self._lock:
            return self._status

    def done(self):
        return self._done.is_set()

    def result(self, timeout=None):
        if timeout is None:
            timeout = self._default_timeout
        if not self._done.wait(timeout):
            self._timeout()
            raise CodexTimeoutError(
                "codex job timed out",
                returncode=None,
                stdout=self._stdout,
                stderr=self._stderr,
                argv=self.argv,
                activity_log=self._activity_log_snapshot(),
                log_path=self.log_path,
            )

        with self._lock:
            if self._status == _COMPLETED:
                return self._response
            if self._status == _CANCELLED:
                raise CodexCancelledError(
                    "codex job was cancelled",
                    returncode=self._returncode,
                    stdout=self._stdout,
                    stderr=self._stderr,
                    argv=self.argv,
                    activity_log=self._activity_log_snapshot(),
                    log_path=self.log_path,
                )
            if self._error is not None:
                raise self._error
            raise CodexExecutionError(
                "codex job failed",
                returncode=self._returncode,
                stdout=self._stdout,
                stderr=self._stderr,
                argv=self.argv,
                activity_log=self._activity_log_snapshot(),
                log_path=self.log_path,
            )

    def cancel(self):
        with self._lock:
            if self._done.is_set():
                return
            self._status = _CANCELLED
        self._log_activity("cancel", "start", "job cancellation requested")
        self._terminate_process("cancel")
        self._done.wait(1)

    def _timeout(self):
        with self._lock:
            if self._done.is_set():
                return
            self._status = _TIMEOUT
        self._log_activity("timeout", "start", "job timeout requested")
        self._terminate_process("timeout")
        self._done.wait(1)

    def _communicate(self):
        attempt = self._attempt
        repairs = 0
        validator_repairs = 0
        review_repairs = 0
        last_validation = None
        usage_totals = None
        try:
            while True:
                stdout, stderr, returncode, usage = self._run_attempt(attempt)
                usage_totals = _merge_usage_totals(usage_totals, usage)
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
                    model=self._resolved_model_name(attempt.argv),
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

                if self._output_validator is None and self._output_pydantic_model is None:
                    last_validation = None
                else:
                    last_validation = self._run_validator(
                        last_response,
                        attempt_index=attempt.attempt_index,
                        repair_index=attempt.repair_index,
                    )
                    with self._lock:
                        if self._status in (_TIMEOUT, _CANCELLED):
                            self._finish()
                            return

                    if last_validation.returncode != 0:
                        if last_validation.timed_out or (
                            not self._repair_validation_failures
                            or validator_repairs >= self._max_validator_repairs
                        ):
                            self._set_validation_error(
                                response=last_response,
                                validation=last_validation,
                                attempts=attempt.attempt_index + 1,
                                repairs=repairs,
                            )
                            self._finish()
                            return

                        repairs += 1
                        validator_repairs += 1
                        prompt = self._build_repair_prompt(
                            response=last_response,
                            validation=last_validation,
                            attempt_index=attempt.attempt_index + 1,
                            repair_index=repairs,
                        )
                        self._log_repair_prompt_activity(
                            prompt,
                            failure_stage="validator",
                            attempt=attempt.attempt_index + 1,
                            repair_index=repairs,
                        )
                        with self._lock:
                            if self._status in (_TIMEOUT, _CANCELLED):
                                self._finish()
                                return
                        attempt = self._spawn_attempt(
                            prompt=prompt,
                            attempt_index=attempt.attempt_index + 1,
                            repair_index=repairs,
                        )
                        self._set_attempt_activity_callback(attempt)
                        self._log_activity(
                            "main_attempt",
                            "start",
                            "repair main attempt started",
                            attempt=attempt.attempt_index,
                            repair_index=attempt.repair_index,
                            argv=attempt.argv,
                        )
                        with self._lock:
                            if self._status in (_TIMEOUT, _CANCELLED):
                                self._output_paths.append(attempt.output_path)
                                self._terminate_one_process(attempt.process)
                                self._finish()
                                return
                            self.argv = list(attempt.argv)
                            self._attempt = attempt
                            self._process = attempt.process
                            self._output_paths.append(attempt.output_path)
                        continue

                if self._review_config is None:
                    self._set_success(last_response)
                    self._finish()
                    return

                review_result = self._run_review(
                    response=last_response,
                    main_prompt=self._initial_main_prompt,
                    attempt_index=attempt.attempt_index,
                    repair_index=attempt.repair_index,
                )
                with self._lock:
                    if self._status in (_TIMEOUT, _CANCELLED):
                        self._finish()
                        return

                if review_result.passed:
                    self._set_success(last_response)
                    self._finish()
                    return

                if (
                    not self._repair_validation_failures
                    or review_repairs >= self._max_review_repairs
                ):
                    self._set_review_error(
                        response=last_response,
                        review=review_result,
                        attempts=attempt.attempt_index + 1,
                        repairs=repairs,
                    )
                    self._finish()
                    return

                repairs += 1
                review_repairs += 1
                prompt = self._build_repair_prompt(
                    response=last_response,
                    validation=last_validation,
                    review=review_result,
                    attempt_index=attempt.attempt_index + 1,
                    repair_index=repairs,
                )
                self._log_repair_prompt_activity(
                    prompt,
                    failure_stage="review",
                    attempt=attempt.attempt_index + 1,
                    repair_index=repairs,
                )
                with self._lock:
                    if self._status in (_TIMEOUT, _CANCELLED):
                        self._finish()
                        return
                attempt = self._spawn_attempt(
                    prompt=prompt,
                    attempt_index=attempt.attempt_index + 1,
                    repair_index=repairs,
                )
                self._set_attempt_activity_callback(attempt)
                self._log_activity(
                    "main_attempt",
                    "start",
                    "repair main attempt started",
                    attempt=attempt.attempt_index,
                    repair_index=attempt.repair_index,
                    argv=attempt.argv,
                )
                with self._lock:
                    if self._status in (_TIMEOUT, _CANCELLED):
                        self._output_paths.append(attempt.output_path)
                        self._terminate_one_process(attempt.process)
                        self._finish()
                        return
                    self.argv = list(attempt.argv)
                    self._attempt = attempt
                    self._process = attempt.process
                    self._output_paths.append(attempt.output_path)
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
        if validation.timed_out:
            message = "codex output validator timed out"
        else:
            message = f"codex output validator exited with status {validation.returncode}"
        with self._lock:
            self._status = _FAILED
            self._error = CodexValidationError(
                message,
                response=response,
                validator_returncode=validation.returncode,
                validator_stdout=validation.stdout,
                validator_stderr=validation.stderr,
                validator_argv=validation.argv,
                attempts=attempts,
                repairs=repairs,
                files=files,
                activity_log=self._activity_log_snapshot(),
                log_path=self.log_path,
            )

    def _set_review_error(self, *, response, review, attempts, repairs):
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
                validator_returncode=None,
                validator_stdout="",
                validator_stderr="",
                validator_argv=[],
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

    def _run_validator(self, response, *, attempt_index, repair_index):
        primary_argv = self._output_validator
        if primary_argv is None and self._output_pydantic_model is not None:
            primary_argv = ["pydantic", _pydantic_model_label(self._output_pydantic_model)]
        self._log_activity(
            "validator",
            "start",
            "validator started",
            attempt=attempt_index,
            repair_index=repair_index,
            argv=primary_argv,
        )
        timed_out = False
        validation_returncode = 0
        stdout = ""
        stderr = ""
        env = dict(self._validator_env)
        env["CODEX_WRAPPER_CWD"] = str(self._validator_cwd)
        env["CODEX_WRAPPER_ATTEMPT"] = str(attempt_index)
        env["CODEX_WRAPPER_REPAIR_ATTEMPT"] = str(repair_index)
        if self._output_schema_path is not None:
            env["CODEX_WRAPPER_OUTPUT_SCHEMA_PATH"] = str(self._output_schema_path)
        if self._subcwd_path is not None:
            env["CODEX_WRAPPER_SUBCWD"] = str(self._subcwd_path)

        process = None
        try:
            if self._output_pydantic_model is not None:
                pydantic_result = _run_pydantic_validation(
                    model=self._output_pydantic_model,
                    content=response.content,
                )
                if pydantic_result.returncode != 0:
                    validation_returncode = pydantic_result.returncode
                    stdout = pydantic_result.stdout
                    stderr = pydantic_result.stderr
                    return pydantic_result
                if pydantic_result.stderr:
                    self._log_activity(
                        "validator",
                        "detail",
                        pydantic_result.stderr,
                        attempt=attempt_index,
                        repair_index=repair_index,
                        argv=pydantic_result.argv,
                        stderr=pydantic_result.stderr,
                    )
            if self._output_validator is None:
                return _ValidationResult(
                    returncode=0,
                    stdout=stdout,
                    stderr=stderr,
                    argv=primary_argv or [],
                )
            process = subprocess.Popen(
                self._output_validator,
                cwd=str(self._validator_cwd),
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
            with self._lock:
                self._validator_process = process
            try:
                stdout, stderr = process.communicate(
                    response.content,
                    timeout=self._validator_timeout,
                )
                return _ValidationResult(
                    returncode=process.returncode,
                    stdout=stdout or "",
                    stderr=stderr or "",
                    argv=self._output_validator,
                )
            except subprocess.TimeoutExpired:
                timed_out = True
                self._terminate_one_process(process)
                stdout, stderr = process.communicate()
                return _ValidationResult(
                    returncode=None,
                    stdout=stdout or "",
                    stderr=stderr or "",
                    argv=self._output_validator,
                    timed_out=True,
                )
        finally:
            self._log_activity(
                "validator",
                "timeout" if timed_out else "end",
                (
                    "validator timed out"
                    if timed_out
                    else "validator finished"
                ),
                attempt=attempt_index,
                repair_index=repair_index,
                argv=(self._output_validator or primary_argv),
                returncode=(
                    process.returncode if process is not None else validation_returncode
                ),
                stdout=locals().get("stdout", "") or "",
                stderr=locals().get("stderr", "") or "",
            )
            if stdout:
                self._log_activity(
                    "validator",
                    "detail",
                    f"validator stdout:\n{stdout.rstrip()}",
                    attempt=attempt_index,
                    repair_index=repair_index,
                    argv=(self._output_validator or primary_argv),
                    stdout=stdout,
                )
            if stderr:
                self._log_activity(
                    "validator",
                    "detail",
                    f"validator stderr:\n{stderr.rstrip()}",
                    attempt=attempt_index,
                    repair_index=repair_index,
                    argv=(self._output_validator or primary_argv),
                    stderr=stderr,
                )
            with self._lock:
                if process is not None and self._validator_process is process:
                    self._validator_process = None

    def _run_review(self, *, response, main_prompt, attempt_index, repair_index):
        if self._review_runner is None or self._review_config is None:
            raise CodexExecutionError(
                "codex review is not configured",
                returncode=None,
                stdout=self._stdout,
                stderr=self._stderr,
                argv=self.argv,
            )
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
            result = self._review_runner(
                original_input=self._original_input,
                main_prompt=main_prompt,
                response=response,
                subcwd_path=self._subcwd_path,
                review_config=self._review_config,
                stream_callback=lambda line: self._record_stream_event_line(
                    line,
                    detail="review",
                    attempt=attempt_index,
                    repair_index=repair_index,
                ),
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
        if isinstance(result.summary, str) and result.summary.strip():
            self._log_activity(
                "review",
                "detail",
                f"review summary: {result.summary.strip()}",
                attempt=attempt_index,
                repair_index=repair_index,
            )
        if not result.passed and isinstance(result.repair_text, str) and result.repair_text.strip():
            self._log_activity(
                "review",
                "detail",
                f"review repair request: {result.repair_text.strip()}",
                attempt=attempt_index,
                repair_index=repair_index,
            )
        return result

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

    def _build_repair_prompt(
        self,
        *,
        response,
        validation,
        review=None,
        attempt_index,
        repair_index,
    ):
        values = {
            "original_input": self._original_input,
            "attempt": str(attempt_index),
            "repair_attempt": str(repair_index),
            "failure_stage": (
                "review"
                if review is not None
                else "validator"
                if validation is not None
                else "unknown"
            ),
            "validator_returncode": (
                "" if validation is None else str(validation.returncode)
            ),
            "validator_stdout": "" if validation is None else validation.stdout,
            "validator_stderr": "" if validation is None else validation.stderr,
            "checklist_feedback": _extract_checklist_feedback(validation),
            "previous_content": response.content,
            "subcwd_section": self._repair_subcwd_section(),
            "review_summary": "" if review is None else review.summary,
            "review_repair_text": "" if review is None else review.repair_text,
        }
        if self._repair_prompt_template is None:
            repair_input = _default_repair_prompt(values)
        else:
            repair_input = self._repair_prompt_template
            for key, value in values.items():
                repair_input = repair_input.replace("{" + key + "}", value)
        return _build_prompt(
            input=repair_input,
            attachments=self._attachments,
            read_paths=self._read_paths,
            write_paths=self._write_paths,
            subcwd_files=self._subcwd_files,
            subcwd_path=self._subcwd_path,
            ignore_global_context=self._ignore_global_context,
            inline_attachment_sections=self._inline_attachment_sections,
            output_schema_section=self._output_schema_section,
            output_model_source_section=self._output_model_source_section,
        )

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
        _emit_activity_stdout(entry)
        self._append_log_line(_format_activity_stdout(entry))

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
        self._cleanup_output_files()
        self._cleanup_temp_output_schema()
        self._cleanup_subcwd()
        self._cleanup_review_session()
        self._sync_activity_log_to_outcome()
        self._done.set()

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
            validator_process = self._validator_process
        targets = 1 + (1 if validator_process is not None else 0)
        self._terminate_one_process(process)
        if validator_process is not None:
            self._terminate_one_process(validator_process)
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
                self._response = replace(self._response, activity_log=snapshot, log_path=self.log_path)
            if self._error is not None:
                self._error.activity_log = list(snapshot)
                self._error.log_path = self.log_path

    def _refresh_status(self):
        if self._done.is_set():
            return
        if self._process.poll() is not None:
            self._done.wait(0.05)


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
        attachments=None,
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
        inline_attachments=True,
        max_inline_attachment_bytes=_DEFAULT_MAX_INLINE_ATTACHMENT_BYTES,
        max_inline_attachment_total_bytes=_DEFAULT_MAX_INLINE_ATTACHMENT_TOTAL_BYTES,
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
        _reject_subcwd_token(attachments or [], "attachments")
        attachments = _normalize_paths(attachments or [], options.cwd, must_exist=True)
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
                    prefix="codex_api_wrapper_output_schema_",
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
                    max_file_bytes=max_inline_attachment_bytes,
                )
            )
        inline_attachment_sections = _resolve_inline_attachment_sections(
            attachments=attachments,
            inline_attachments=inline_attachments,
            max_file_bytes=max_inline_attachment_bytes,
            max_total_bytes=max_inline_attachment_total_bytes,
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
        prompt_subcwd_files = _prompt_subcwd_file_paths(subcwd_files)
        prompt = _build_prompt(
            input=input,
            attachments=attachments,
            read_paths=read_paths,
            write_paths=write_paths,
            subcwd_files=prompt_subcwd_files,
            subcwd_path=subcwd_path,
            ignore_global_context=options.ignore_global_context,
            inline_attachment_sections=inline_attachment_sections,
            output_schema_section=output_schema_section,
            output_model_source_section=output_model_source_section,
        )
        profile_name = "codex_api_wrapper_" + uuid.uuid4().hex
        env = os.environ.copy()
        env.update(self.env)

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

        def run_review(*, original_input, main_prompt, response, subcwd_path, review_config, stream_callback):
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
                    prefix="codex_api_wrapper_review_",
                    suffix=".json",
                    text=_REVIEW_OUTPUT_SCHEMA,
                )
            )
            output_path = pathlib.Path(_make_output_path())
            try:
                output_schema_section, _ = _resolve_inline_output_schema_section(
                    output_schema_path=schema_path,
                    max_file_bytes=_DEFAULT_MAX_INLINE_ATTACHMENT_BYTES,
                )
                review_input = _build_review_input(
                    original_input=original_input,
                    main_prompt=main_prompt,
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
            attachments=attachments,
            inline_attachment_sections=inline_attachment_sections,
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
        review_profile_name = "codex_api_wrapper_review_" + uuid.uuid4().hex
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
        if options.ignore_global_context:
            argv.extend(["-c", "project_doc_max_bytes=0"])
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
    ):
        sdk = _import_openai_codex()
        config_overrides = []
        if options.reasoning_effort:
            config_overrides.append(
                f"model_reasoning_effort={_toml_string(options.reasoning_effort)}"
            )
        if options.ignore_global_context:
            config_overrides.append("project_doc_max_bytes=0")
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
        codex = None
        try:
            codex = sdk.Codex(config=sdk_config)
            process.set_codex(codex)
            thread = codex.thread_start(
                cwd=str(options.cwd),
                model=options.model,
                developer_instructions=None,
                ephemeral=options.ephemeral,
                sandbox=sandbox,
            )
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
        finally:
            if codex is not None:
                try:
                    codex.close()
                except Exception:
                    pass
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
        config_overrides = []
        if options.reasoning_effort:
            config_overrides.append(
                f"model_reasoning_effort={_toml_string(options.reasoning_effort)}"
            )
        if options.ignore_global_context:
            config_overrides.append("project_doc_max_bytes=0")
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
                        review_config.instructions
                    ),
                    ephemeral=options.ephemeral,
                    sandbox=sdk.Sandbox.read_only,
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


def _build_prompt(
    *,
    input,
    attachments,
    read_paths,
    write_paths,
    subcwd_files,
    subcwd_path,
    ignore_global_context,
    inline_attachment_sections=None,
    output_schema_section=None,
    output_model_source_section=None,
):
    sections = []
    if ignore_global_context:
        sections.append(
            "Global AGENTS.md and global skills should not be used for this job. "
            "Use only the request below and explicitly attached paths as task context."
        )
    sections.append(input.strip())
    if output_schema_section:
        sections.append("Output schema:\n" + output_schema_section)
    if output_model_source_section:
        sections.append("Output model source:\n" + output_model_source_section)
    if attachments:
        sections.append(_format_path_section("Attachments", attachments))
    if inline_attachment_sections:
        sections.append(
            "Attached file contents:\n"
            + "\n\n".join(inline_attachment_sections)
        )
    if read_paths:
        sections.append(_format_path_section("Readable paths", read_paths))
    if write_paths:
        sections.append(_format_path_section("Writable paths", write_paths))
    if subcwd_path is not None:
        sections.append(
            "Temporary output directory:\n"
            f"{_SUBCWD_TOKEN} = {subcwd_path}\n\n"
            "Create output files only under this directory. "
            "This directory name is temporary; do not mention it in the final "
            "response unless necessary."
        )
    if subcwd_files:
        sections.append(
            "Workspace files staged under "
            f"{_SUBCWD_TOKEN}:\n"
            + "\n".join(f"- {path}" for path in subcwd_files)
            + "\n\nThese files are pre-populated before the job starts. "
            "Only read or edit a staged file when the corresponding "
            f"{_SUBCWD_TOKEN}/... path is covered by this job's read_paths "
            "or write_paths."
        )
    return "\n\n".join(sections) + "\n"


def _build_review_input(
    *,
    original_input,
    main_prompt,
    response,
    subcwd_path,
    review_config,
    review_read_paths,
):
    values = {
        "review_instructions": review_config.instructions,
        "original_input": original_input,
        "main_prompt": main_prompt if review_config.include_main_prompt else "",
        "response_content": response.content,
        "subcwd_path": "" if subcwd_path is None else str(subcwd_path),
        "review_read_paths": _format_review_read_paths_for_log(
            review_read_paths,
            subcwd_path=subcwd_path,
        )
        if review_read_paths
        else "",
    }
    if review_config.prompt_template is None:
        return _default_review_prompt(values)
    review_input = review_config.prompt_template
    for key, value in values.items():
        review_input = review_input.replace("{" + key + "}", value)
    return review_input


def _format_path_section(title, paths):
    lines = [f"{title}:"]
    lines.extend(f"- {path}" for path in paths)
    return "\n".join(lines)


def _format_review_read_paths_for_log(paths, *, subcwd_path):
    display_paths = []
    for path in paths:
        if subcwd_path is not None and (path == subcwd_path or _is_relative_to(path, subcwd_path)):
            relative = path.relative_to(subcwd_path).as_posix()
            display_paths.append(
                _SUBCWD_TOKEN if not relative or relative == "." else f"{_SUBCWD_TOKEN}/{relative}"
            )
            continue
        display_paths.append(str(path))
    return _format_path_section("Review readable paths", display_paths)


def _resolve_inline_attachment_sections(
    *,
    attachments,
    inline_attachments,
    max_file_bytes,
    max_total_bytes,
    initial_total_bytes=0,
):
    should_inline = True if inline_attachments is None else bool(inline_attachments)
    if not should_inline:
        return []
    if not isinstance(max_file_bytes, int) or max_file_bytes < 0:
        raise ValueError("max_inline_attachment_bytes must be a non-negative integer")
    if not isinstance(max_total_bytes, int) or max_total_bytes < 0:
        raise ValueError(
            "max_inline_attachment_total_bytes must be a non-negative integer"
        )

    sections = []
    if not isinstance(initial_total_bytes, int) or initial_total_bytes < 0:
        raise ValueError("initial_total_bytes must be a non-negative integer")

    total = initial_total_bytes
    for path in attachments:
        if path.is_dir():
            continue
        if path.is_symlink() or not path.is_file():
            continue
        size = path.stat().st_size
        if size > max_file_bytes:
            raise ValueError(
                f"attachment exceeds max_inline_attachment_bytes: {path}"
            )
        if total + size > max_total_bytes:
            raise ValueError(
                "inline attachments exceed max_inline_attachment_total_bytes"
            )
        data = path.read_bytes()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"attachment is not valid UTF-8 text: {path}") from exc
        sections.append(_format_inline_attachment(path, text))
        total += len(data)
    return sections


def _resolve_inline_output_schema_section(
    *,
    output_schema_path,
    max_file_bytes,
):
    if output_schema_path is None:
        return None, 0
    if not isinstance(max_file_bytes, int) or max_file_bytes < 0:
        raise ValueError("max_inline_attachment_bytes must be a non-negative integer")
    if output_schema_path.is_dir():
        raise ValueError(
            f"output_schema_path must be a regular file: {output_schema_path}"
        )
    if output_schema_path.is_symlink() or not output_schema_path.is_file():
        raise ValueError(
            f"output_schema_path must be a regular file: {output_schema_path}"
        )
    size = output_schema_path.stat().st_size
    if size > max_file_bytes:
        raise ValueError(
            f"output_schema_path exceeds max_inline_attachment_bytes: {output_schema_path}"
        )
    data = output_schema_path.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"output_schema_path is not valid UTF-8 text: {output_schema_path}"
        ) from exc
    return _format_inline_output_schema(output_schema_path, text), len(data)


def _format_inline_output_schema(path, text):
    return (
        f"<OutputSchema path={_xml_attr(str(path))}>\n"
        f"{text}\n"
        "</OutputSchema>"
    )


def _format_inline_attachment(path, text):
    return (
        f"<AttachedFile path={_xml_attr(str(path))}>\n"
        f"{text}\n"
        "</AttachedFile>"
    )


def _extract_usage_from_stdout(stdout):
    usage = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") != "turn.completed":
            continue
        raw_usage = event.get("usage")
        if not isinstance(raw_usage, dict):
            continue
        normalized_usage = {}
        for key, value in raw_usage.items():
            if isinstance(key, str) and isinstance(value, int) and not isinstance(value, bool):
                normalized_usage[key] = value
        if normalized_usage:
            usage = normalized_usage
    return usage


def _merge_usage_totals(current, incoming):
    if incoming is None:
        return _copy_optional_usage(current)
    merged = {} if current is None else dict(current)
    for key, value in incoming.items():
        merged[key] = merged.get(key, 0) + value
    return merged


def _copy_optional_usage(usage):
    if usage is None:
        return None
    return dict(usage)


def _copy_optional_pricing(pricing):
    if pricing is None:
        return None
    return dict(pricing)


def _resolve_model_pricing(model):
    if not isinstance(model, str) or not model.strip():
        return None
    rates = _MODEL_PRICING_USD_PER_MILLION.get(model.strip())
    if rates is None:
        return None
    pricing = {"model": model.strip(), "source_url": _API_PRICING_SOURCE_URL}
    pricing["input_usd_per_million"] = rates["input"]
    if rates["cached_input"] is not None:
        pricing["cached_input_usd_per_million"] = rates["cached_input"]
    pricing["output_usd_per_million"] = rates["output"]
    return pricing


def _estimate_cost_usd_for_usage(*, usage, pricing):
    if usage is None or pricing is None:
        return None
    input_tokens = usage.get("input_tokens", 0)
    cached_input_tokens = usage.get("cached_input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    reasoning_output_tokens = usage.get("reasoning_output_tokens", 0)
    output_billed_tokens = output_tokens + reasoning_output_tokens
    input_cost = input_tokens * float(pricing["input_usd_per_million"]) / 1_000_000
    cached_input_rate = float(pricing.get("cached_input_usd_per_million", 0.0))
    cached_input_cost = cached_input_tokens * cached_input_rate / 1_000_000
    output_cost = output_billed_tokens * float(pricing["output_usd_per_million"]) / 1_000_000
    return input_cost + cached_input_cost + output_cost


def _format_usage_summary(*, model, usage):
    parts = []
    if isinstance(model, str) and model.strip():
        parts.append(f"model={model.strip()}")
    for key in (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "total_tokens",
    ):
        value = usage.get(key)
        if isinstance(value, int):
            parts.append(f"{key}={value}")
    pricing = _resolve_model_pricing(model)
    if pricing is not None:
        parts.append(
            "usd_per_million="
            + "input:"
            + str(pricing["input_usd_per_million"])
            + ",cached_input:"
            + str(pricing.get("cached_input_usd_per_million", 0.0))
            + ",output:"
            + str(pricing["output_usd_per_million"])
        )
        estimated_cost_usd = _estimate_cost_usd_for_usage(usage=usage, pricing=pricing)
        if estimated_cost_usd is not None:
            parts.append(f"estimated_cost_usd={estimated_cost_usd:.6f}")
    return " ".join(parts)


def _response_model_name(response):
    if response is None:
        return None
    pricing = getattr(response, "pricing", None)
    if isinstance(pricing, dict):
        model = pricing.get("model")
        if isinstance(model, str) and model.strip():
            return model.strip()
    return _model_name_from_argv(getattr(response, "argv", ()))


def _model_name_from_argv(argv):
    for index, token in enumerate(argv):
        if token == "--model" and index + 1 < len(argv):
            model = argv[index + 1]
            if isinstance(model, str) and model.strip():
                return model.strip()
    return None


def _emit_activity_stdout(entry):
    sys.stdout.write(_format_activity_stdout(entry) + "\n")
    sys.stdout.flush()


def _stream_activity_from_line(line, *, detail=None, attempt=None, repair_index=None):
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(event, dict):
        return None
    event_type = event.get("type")
    if not isinstance(event_type, str):
        return None
    message = f"stream event observed: {event_type}"
    if event_type in {"item.completed", "item.delta"}:
        item_type = event.get("item_type")
        item_text = event.get("item_text") or event.get("delta_text")
        if isinstance(item_type, str) and isinstance(item_text, str) and item_text:
            message = f"stream event observed: {event_type} ({item_type}): {item_text}"
        elif isinstance(item_type, str):
            message = f"stream event observed: {event_type} ({item_type})"
    elif event_type == "plan.updated":
        summary = event.get("summary")
        if isinstance(summary, str) and summary:
            message = f"stream event observed: plan.updated: {summary}"
    return CodexActivity(
        phase="stream",
        event=event_type,
        message=message,
        detail=detail,
        attempt=attempt,
        repair_index=repair_index,
        stdout=line,
    )


def _stream_item_text(item):
    item_type = getattr(item, "type", None)
    if item_type == "agentMessage":
        return _summarize_agent_message_text(getattr(item, "text", ""))
    if item_type == "reasoning":
        summary = _extract_reasoning_text(getattr(item, "summary", None))
        if summary:
            return _truncate_activity_text(summary)
        content = _extract_reasoning_text(getattr(item, "content", None))
        if content:
            return _truncate_activity_text(content)
    if item_type == "webSearch":
        return _truncate_activity_text(_describe_web_search(item))
    if item_type == "mcpToolCall":
        return _truncate_activity_text(_describe_mcp_tool_call(item))
    if item_type == "fileChange":
        return _truncate_activity_text(_describe_file_change(item))
    return ""


def _extract_reasoning_text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            text = _extract_reasoning_text(item)
            if text:
                parts.append(text)
        return " ".join(parts)
    text = getattr(value, "text", None)
    if isinstance(text, str) and text.strip():
        return text
    summary_text = getattr(value, "summary_text", None)
    if isinstance(summary_text, str) and summary_text.strip():
        return summary_text
    if isinstance(value, dict):
        for key in ("text", "summary_text", "summaryText", "content"):
            text = _extract_reasoning_text(value.get(key))
            if text:
                return text
    return str(value) if isinstance(value, (int, float)) else ""


def _describe_web_search(item):
    action = getattr(item, "action", None)
    action_root = getattr(action, "root", action)
    action_type = getattr(action_root, "type", None)
    if action_type == "search":
        query = getattr(action_root, "query", None)
        if isinstance(query, str) and query.strip():
            return f"search {query}"
        queries = getattr(action_root, "queries", None) or []
        if queries:
            return "search " + ", ".join(str(part) for part in queries[:3] if part)
    if action_type == "openPage":
        url = getattr(action_root, "url", None)
        if isinstance(url, str) and url.strip():
            return f"open {url}"
    if action_type == "findInPage":
        pattern = getattr(action_root, "pattern", None)
        url = getattr(action_root, "url", None)
        if isinstance(pattern, str) and pattern.strip() and isinstance(url, str) and url.strip():
            return f"find {pattern} in {url}"
        if isinstance(pattern, str) and pattern.strip():
            return f"find {pattern}"
    query = getattr(item, "query", None)
    if isinstance(query, str) and query.strip():
        return f"query {query}"
    return ""


def _summarize_agent_message_text(text):
    if not isinstance(text, str):
        return ""
    stripped = text.strip()
    if not stripped:
        return ""
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return _truncate_activity_text(stripped)
    if isinstance(payload, dict):
        parts = []
        company_name = payload.get("company_name")
        if isinstance(company_name, str) and company_name.strip():
            parts.append(f"company_name={company_name.strip()}")
        verdict = payload.get("verdict")
        if isinstance(verdict, str) and verdict.strip():
            parts.append(f"verdict={verdict.strip()}")
        references = payload.get("references")
        if isinstance(references, list):
            parts.append(f"references={len(references)}")
        if not parts:
            keys = [str(key) for key in list(payload.keys())[:5]]
            if keys:
                parts.append("keys=" + ", ".join(keys))
        return _truncate_activity_text("JSON response " + ", ".join(parts))
    if isinstance(payload, list):
        return _truncate_activity_text(f"JSON response items={len(payload)}")
    return _truncate_activity_text(stripped)


def _describe_mcp_tool_call(item):
    server = getattr(item, "server", None)
    tool = getattr(item, "tool", None)
    status = getattr(item, "status", None)
    result = getattr(item, "result", None)
    details = []
    if isinstance(server, str) and server:
        details.append(server)
    if isinstance(tool, str) and tool:
        details.append(tool)
    if isinstance(status, str) and status:
        details.append(status)
    result_text = _extract_tool_result_text(result)
    if result_text:
        details.append(result_text)
    return " ".join(details)


def _extract_tool_result_text(result):
    if result is None:
        return ""
    content = getattr(result, "content", None)
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str) and item.strip():
                parts.append(item)
                continue
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text)
                    continue
                inner = item.get("content")
                text = _extract_reasoning_text(inner)
                if text:
                    parts.append(text)
        if parts:
            return " ".join(parts[:2])
    structured = getattr(result, "structured_content", None)
    if structured:
        return _summarize_mapping(structured)
    return ""


def _describe_file_change(item):
    changes = getattr(item, "changes", None) or []
    paths = []
    for change in changes:
        path = getattr(change, "path", None)
        kind = getattr(change, "kind", None)
        if isinstance(path, str) and path:
            if isinstance(kind, str) and kind:
                paths.append(f"{kind}:{path}")
            else:
                paths.append(path)
    status = getattr(item, "status", None)
    parts = []
    if isinstance(status, str) and status:
        parts.append(status)
    if paths:
        preview = ", ".join(paths[:3])
        if len(paths) > 3:
            preview += f" (+{len(paths) - 3})"
        parts.append(preview)
    return " ".join(parts)


def _summarize_mapping(value):
    if isinstance(value, dict):
        parts = []
        for key, item in list(value.items())[:3]:
            if isinstance(item, (str, int, float)) and not isinstance(item, bool):
                parts.append(f"{key}={item}")
            else:
                parts.append(str(key))
        return ", ".join(parts)
    return ""


def _truncate_activity_text(text, *, limit=240):
    if not isinstance(text, str):
        return ""
    normalized = " ".join(text.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3] + "..."


def _stdout_field_text(text):
    if not isinstance(text, str):
        return '""'
    normalized = " ".join(text.split())
    escaped = normalized.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _format_activity_stdout(entry):
    if entry.phase == "stream":
        return _format_stream_activity_stdout(entry)
    if entry.phase == "review" and entry.event == "reviewed_response":
        return f"[Reviewer][Reviewed Response] {_stdout_field_text(entry.message)}"

    actor = _activity_actor(entry.phase)
    kind = _non_stream_kind(entry)
    parts = [f"[{actor}][{kind}]"]
    if entry.attempt is not None:
        parts.append(f"attempt={entry.attempt}")
    if entry.repair_index is not None:
        parts.append(f"repair={entry.repair_index}")
    if entry.returncode is not None:
        parts.append(f"returncode={entry.returncode}")
    summary = _non_stream_summary(entry)
    if summary:
        parts.append(summary)
    if _needs_explicit_message(entry):
        parts.append(_stdout_field_text(entry.message))
    return " ".join(parts)


def _format_stream_activity_stdout(entry):
    try:
        event = json.loads(entry.stdout) if entry.stdout else {}
    except json.JSONDecodeError:
        event = {}
    event_type = event.get("type")
    actor = _stream_actor(entry)
    if event_type == "item.completed":
        item_type = event.get("item_type", "?")
        item_text = event.get("item_text")
        parts = [f"[{actor}][{_stream_item_label(item_type)}]"]
        _append_activity_context_parts(parts, entry)
        if isinstance(item_text, str) and item_text:
            parts.append(_stdout_field_text(item_text))
        return " ".join(parts)
    if event_type == "item.delta":
        item_type = event.get("item_type", "?")
        delta_text = event.get("delta_text")
        parts = [f"[{actor}][{_stream_item_label(item_type)}]"]
        _append_activity_context_parts(parts, entry)
        if isinstance(delta_text, str) and delta_text:
            parts.append(_stdout_field_text(delta_text))
        return " ".join(parts)
    if event_type == "plan.updated":
        summary = event.get("summary")
        parts = [f"[{actor}][Plan]"]
        _append_activity_context_parts(parts, entry)
        if isinstance(summary, str) and summary:
            parts.append(_stdout_field_text(summary))
        return " ".join(parts)
    if event_type == "thread.started":
        parts = [f"[{actor}][Start]"]
        _append_activity_context_parts(parts, entry)
        return " ".join(parts)
    if event_type == "turn.completed":
        parts = [f"[{actor}][Result]"]
        _append_activity_context_parts(parts, entry)
        return " ".join(parts)
    parts = [f"[{actor}][{_stream_event_kind(entry)}]"]
    _append_activity_context_parts(parts, entry)
    fallback = _stream_fallback_summary(entry)
    if fallback:
        parts.append(fallback)
    parts.append(_stdout_field_text(entry.message))
    return " ".join(parts)


def _append_activity_context_parts(parts, entry):
    if entry.attempt is not None:
        parts.append(f"attempt={entry.attempt}")
    if entry.repair_index is not None:
        parts.append(f"repair={entry.repair_index}")


def _activity_actor(phase):
    return {
        "main_attempt": "Main Attempt",
        "validator": "Validator",
        "review": "Reviewer",
        "repair": "Repair",
        "file_collection": "File Collection",
        "cleanup": "Cleanup",
        "cancel": "Cancel",
        "timeout": "Timeout",
    }.get(phase, phase.replace("_", " ").title())


def _stream_actor(entry):
    origin = getattr(entry, "detail", None)
    if origin == "review":
        return "Reviewer"
    if origin == "validator":
        return "Validator"
    return "Main Attempt"


def _non_stream_kind(entry):
    if entry.event == "usage":
        return "Usage"
    if entry.event == "detail":
        return "Message"
    if entry.phase == "main_attempt":
        if entry.event == "error" or "exited with status" in entry.message:
            return "Error"
        if entry.attempt and entry.attempt > 0:
            return "Repair"
        return _event_kind(entry.event)
    if entry.phase == "repair" and entry.event == "prompt":
        return "Prompt"
    if entry.phase in {"validator", "review", "file_collection", "cleanup", "cancel", "timeout"}:
        return _event_kind(entry.event)
    return "Event"


def _non_stream_summary(entry):
    if entry.event == "usage":
        return entry.message
    if entry.phase == "cleanup":
        if "output file cleanup" in entry.message:
            return "output cleanup"
        if "subcwd cleanup" in entry.message:
            return "subcwd cleanup"
    if entry.phase == "main_attempt":
        if "exited with status" in entry.message:
            return ""
        return ""
    if entry.phase == "validator":
        return ""
    if entry.phase == "review":
        return ""
    if entry.phase == "file_collection":
        return ""
    return ""


def _event_kind(event):
    return {
        "start": "Start",
        "end": "Result",
        "error": "Error",
        "usage": "Usage",
        "input": "Input",
    }.get(event, "Event")


def _stream_item_label(item_type):
    return {
        "reasoning": "Thinking",
        "agentMessage": "Message",
        "webSearch": "Search",
        "mcpToolCall": "Tool",
        "fileChange": "Edit",
    }.get(item_type, "Item")


def _stream_event_kind(entry):
    return {
        "start": "Start",
        "end": "Result",
        "error": "Error",
        "thread.started": "Start",
        "turn.completed": "Result",
        "item.completed": "Item",
    }.get(entry.event, "Event")


def _stream_fallback_summary(entry):
    return {
        "start": "started",
        "end": "finished",
        "error": "failed",
        "thread.started": "thread started",
        "turn.completed": "turn completed",
        "item.completed": "item completed",
    }.get(entry.event, entry.event)


def _needs_explicit_message(entry):
    if entry.phase == "stream":
        return False
    if entry.event == "usage":
        return False
    if entry.phase == "cleanup":
        return entry.message not in {
            "output file cleanup started",
            "output file cleanup finished",
            "subcwd cleanup started",
            "subcwd cleanup finished",
        }
    if entry.phase == "main_attempt":
        return entry.message not in {
            "main attempt started",
            "repair main attempt started",
            "main attempt completed successfully",
        } and "main attempt exited with status" not in entry.message
    if entry.phase == "validator":
        return entry.message not in {"validator started", "validator finished"}
    if entry.phase == "review":
        return entry.message not in {"review started", "review finished"}
    if entry.phase == "file_collection":
        return entry.message not in {"file collection started", "file collection finished"}
    if entry.phase == "cancel":
        return entry.message != "job cancellation requested"
    if entry.phase == "timeout":
        return entry.message != "job timeout requested"
    return True


def _import_openai_codex():
    try:
        return importlib.import_module("openai_codex")
    except ImportError as exc:
        raise RuntimeError(
            "openai-codex is not installed in the current Python environment"
        ) from exc


def _codex_runtime_read_paths():
    try:
        spec = importlib.util.find_spec("openai_codex")
    except (ImportError, AttributeError, ValueError):
        return []
    if spec is None:
        return []

    candidate = None
    locations = getattr(spec, "submodule_search_locations", None)
    if locations:
        candidate = next(iter(locations), None)
    if candidate is None:
        origin = getattr(spec, "origin", None)
        if origin and origin != "built-in":
            candidate = pathlib.Path(origin).parent
    if candidate is None:
        return []

    try:
        path = pathlib.Path(candidate).expanduser().resolve()
    except OSError:
        return []
    if not path.exists():
        return []
    return [path]


def _normalize_sdk_usage(usage):
    if usage is None:
        return None
    breakdown = usage
    last_breakdown = getattr(usage, "last", None)
    if last_breakdown is not None:
        breakdown = last_breakdown
    elif getattr(usage, "total", None) is not None:
        breakdown = getattr(usage, "total")
    values = {}
    for key in (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "total_tokens",
    ):
        value = getattr(breakdown, key, None)
        if isinstance(value, int) and not isinstance(value, bool):
            values[key] = value
    return values or None


def _load_output_schema_json(output_schema_path):
    if output_schema_path is None:
        return None
    return json.loads(output_schema_path.read_text(encoding="utf-8"))


def _normalize_output_pydantic_model(output_pydantic_model):
    if output_pydantic_model is None:
        return None
    if not callable(getattr(output_pydantic_model, "model_json_schema", None)):
        raise ValueError(
            "output_pydantic_model must define model_json_schema()"
        )
    if not callable(getattr(output_pydantic_model, "model_validate_json", None)):
        raise ValueError(
            "output_pydantic_model must define model_validate_json()"
        )
    return output_pydantic_model


def _pydantic_model_label(model):
    name = getattr(model, "__name__", None)
    if isinstance(name, str) and name.strip():
        return name
    return str(model)


def _render_pydantic_output_schema_json(model):
    schema = model.model_json_schema(ref_template="#/$defs/{model}")
    schema = _strip_structured_output_schema_metadata(schema)
    return json.dumps(schema, ensure_ascii=False, indent=2)


def _strip_structured_output_schema_metadata(value):
    if isinstance(value, list):
        return [_strip_structured_output_schema_metadata(item) for item in value]
    if not isinstance(value, dict):
        return value
    if "$ref" in value:
        return {"$ref": value["$ref"]}
    stripped = {}
    for key, item in value.items():
        if key in {"description", "title", "examples", "default"}:
            continue
        if isinstance(key, str) and key.startswith("x-"):
            continue
        stripped[key] = _strip_structured_output_schema_metadata(item)
    return stripped


def _render_pydantic_model_source_context(model):
    try:
        source = inspect.getsource(model)
    except (OSError, TypeError):
        source = None
    if not source:
        try:
            module = inspect.getmodule(model)
            if module is not None:
                source = inspect.getsource(module)
        except (OSError, TypeError):
            source = None
    if not source:
        return f"<PydanticModel name=\"{_pydantic_model_label(model)}\" />"
    return (
        f"<PydanticModel name=\"{_pydantic_model_label(model)}\">\n"
        f"{source.rstrip()}\n"
        "</PydanticModel>"
    )


def _run_pydantic_validation(*, model, content):
    argv = ["pydantic", _pydantic_model_label(model)]
    try:
        model.model_validate_json(content)
    except Exception as exc:
        return _ValidationResult(
            returncode=1,
            stdout="",
            stderr=f"pydantic validation failed for {_pydantic_model_label(model)}:\n{exc}",
            argv=argv,
        )
    return _ValidationResult(
        returncode=0,
        stdout="",
        stderr=f"pydantic validation passed for {_pydantic_model_label(model)}",
        argv=argv,
    )


def _append_stream_event(stdout_lines, process, event):
    line = json.dumps(event)
    stdout_lines.append(line)
    emit = getattr(process, "emit_stdout_line", None)
    if emit is not None:
        emit(line)


def _consume_turn_via_stream(process, turn):
    stdout_lines = []
    items = []
    usage = None
    final_response_chunks = []
    completed_turn = None
    _append_stream_event(stdout_lines, process, {"type": "thread.started"})
    stream = turn.stream()
    try:
        for event in stream:
            method = getattr(event, "method", "")
            payload = getattr(event, "payload", None)
            if method in {"item/agentMessage/delta", "item.agentMessage.delta"}:
                delta = getattr(payload, "delta", None)
                if isinstance(delta, str) and delta:
                    final_response_chunks.append(delta)
                continue
            if method in {
                "item/reasoning/summaryTextDelta",
                "item/reasoning/textDelta",
                "item.reasoning.summaryTextDelta",
                "item.reasoning.textDelta",
            }:
                delta = getattr(payload, "delta", None)
                if isinstance(delta, str) and delta:
                    _append_stream_event(
                        stdout_lines,
                        process,
                        {"type": "item.delta", "item_type": "reasoning", "delta_text": delta},
                    )
                continue
            if method in {"turn/plan/updated", "turn.plan.updated"}:
                summary = _summarize_turn_plan(payload)
                event_payload = {"type": "plan.updated"}
                if summary:
                    event_payload["summary"] = summary
                _append_stream_event(stdout_lines, process, event_payload)
                continue
            if method in {"item/completed", "item.completed"}:
                item = getattr(payload, "item", None)
                if item is None:
                    continue
                items.append(item)
                item_payload = getattr(item, "root", item)
                item_type = getattr(item_payload, "type", None)
                if not isinstance(item_type, str):
                    item_type = type(item_payload).__name__
                item_text = _stream_item_text(item_payload)
                event_payload = {"type": "item.completed", "item_type": item_type}
                if item_text:
                    event_payload["item_text"] = item_text
                _append_stream_event(stdout_lines, process, event_payload)
                continue
            if method in {"thread/tokenUsage/updated", "thread.tokenUsage.updated"}:
                usage = _normalize_sdk_usage(
                    getattr(payload, "token_usage", getattr(payload, "tokenUsage", None))
                )
                continue
            if method in {"turn/completed", "turn.completed"}:
                completed_turn = getattr(payload, "turn", None)
                event_payload = {"type": "turn.completed"}
                if usage is not None:
                    event_payload["usage"] = usage
                _append_stream_event(stdout_lines, process, event_payload)
                continue
    finally:
        close = getattr(stream, "close", None)
        if close is not None:
            close()

    if completed_turn is None:
        raise RuntimeError("turn completed event not received")
    if usage is None:
        usage = _normalize_sdk_usage(getattr(completed_turn, "token_usage", None))
    if usage is None:
        usage = _normalize_sdk_usage(getattr(completed_turn, "tokenUsage", None))
    if usage is None:
        usage = _normalize_sdk_usage(getattr(completed_turn, "usage", None))
    _raise_for_failed_sdk_turn(completed_turn)
    final_response = _final_response_from_stream(items, final_response_chunks)
    return (
        "\n".join(stdout_lines) + ("\n" if stdout_lines else ""),
        final_response,
        usage,
        items,
    )


def _raise_for_failed_sdk_turn(turn):
    status = getattr(turn, "status", None)
    status_value = getattr(status, "value", status)
    if status_value != "failed":
        return
    error = getattr(turn, "error", None)
    message = getattr(error, "message", None)
    if isinstance(message, str) and message:
        raise RuntimeError(message)
    raise RuntimeError(f"turn failed with status {status_value}")


def _final_response_from_stream(items, delta_chunks):
    for item in reversed(items):
        item_payload = getattr(item, "root", item)
        if getattr(item_payload, "type", None) != "agentMessage":
            continue
        text = getattr(item_payload, "text", None)
        if isinstance(text, str):
            return text
    return "".join(delta_chunks)


def _summarize_turn_plan(payload):
    explanation = getattr(payload, "explanation", None)
    if isinstance(explanation, str) and explanation.strip():
        return _truncate_activity_text(explanation)
    plan = getattr(payload, "plan", None) or []
    steps = []
    for step in plan[:3]:
        step_text = getattr(step, "step", None)
        if isinstance(step_text, str) and step_text.strip():
            steps.append(step_text.strip())
    return _truncate_activity_text(" / ".join(steps))


def _xml_attr(value):
    return (
        '"'
        + str(value)
        .replace("&", "&amp;")
        .replace("\"", "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        + '"'
    )


def _normalize_paths(paths, cwd, *, must_exist):
    normalized = []
    for raw in paths:
        normalized.append(_normalize_path(raw, cwd, must_exist=must_exist))
    return normalized


def _normalize_optional_path(path, cwd, *, must_exist):
    if path is None:
        return None
    return _normalize_path(path, cwd, must_exist=must_exist)


def _normalize_review_config(review, cwd):
    if review is None:
        return None
    if not isinstance(review, ReviewConfig):
        raise ValueError("review must be a ReviewConfig")
    if not isinstance(review.instructions, str) or not review.instructions.strip():
        raise ValueError("review.instructions must be a non-empty string")
    prompt_path = _normalize_optional_path(
        review.prompt_path,
        cwd,
        must_exist=True,
    )
    prompt_template = None
    if prompt_path is not None:
        prompt_template = prompt_path.read_text(encoding="utf-8")
    normalized_read_paths = None
    if review.read_paths is not None:
        normalized_read_paths = tuple(_normalize_raw_path_list(review.read_paths, "review.read_paths"))
    timeout = review.timeout
    if timeout is not None and not isinstance(timeout, (int, float)):
        raise ValueError("review.timeout must be numeric or None")
    if not isinstance(review.reuse_session, bool):
        raise ValueError("review.reuse_session must be a bool")
    if not isinstance(review.include_main_prompt, bool):
        raise ValueError("review.include_main_prompt must be a bool")
    return _ResolvedReviewConfig(
        instructions=review.instructions.strip(),
        prompt_template=prompt_template,
        read_paths=normalized_read_paths,
        timeout=timeout,
        model=review.model,
        reasoning_effort=_normalize_optional_reasoning_effort(
            review.reasoning_effort
        )
        if review.reasoning_effort is not None
        else None,
        reuse_session=review.reuse_session,
        include_main_prompt=review.include_main_prompt,
    )


def _normalize_output_validator(output_validator, cwd):
    if output_validator is None:
        return None
    if isinstance(output_validator, (str, os.PathLike)):
        argv = [os.fspath(output_validator)]
    else:
        argv = [os.fspath(item) for item in output_validator]
    if not argv:
        raise ValueError("output_validator must not be empty")
    if not all(isinstance(item, str) and item for item in argv):
        raise ValueError("output_validator entries must be non-empty strings")
    head = argv[0]
    if _looks_like_path(head):
        argv[0] = str(_normalize_path(head, cwd, must_exist=True))
    return argv


def _normalize_raw_path_list(paths, field_name):
    if not isinstance(paths, (list, tuple)):
        raise ValueError(f"{field_name} must be a list or tuple of paths")
    normalized = []
    for item in paths:
        text = os.fspath(item)
        if not isinstance(text, str) or not text:
            raise ValueError(f"{field_name} entries must be non-empty strings")
        normalized.append(text)
    return normalized


def _looks_like_path(value):
    return (
        value.startswith(".")
        or value.startswith("~")
        or os.path.isabs(value)
        or os.sep in value
        or (os.altsep is not None and os.altsep in value)
    )


def _contains_subcwd_token(paths):
    return any(_SUBCWD_TOKEN in _path_text(path) for path in paths)


def _reject_subcwd_token(paths, field_name):
    if _contains_subcwd_token(paths):
        raise ValueError(f"{field_name} does not accept {_SUBCWD_TOKEN}")


def _normalize_subcwd_paths(paths, cwd, subcwd_path, *, must_exist, field_name):
    normalized = []
    for raw in paths:
        text = _path_text(raw)
        if _SUBCWD_TOKEN in text:
            normalized.append(
                _expand_subcwd_path(
                    raw,
                    subcwd_path,
                    field_name=field_name,
                    allow_root=False,
                )
            )
            if must_exist and not normalized[-1].exists():
                raise FileNotFoundError(normalized[-1])
            continue
        normalized.append(_normalize_path(raw, cwd, must_exist=must_exist))
    return normalized


def _expand_subcwd_path(raw, subcwd_path, *, field_name, allow_root):
    if subcwd_path is None:
        raise ValueError(f"{field_name} uses {_SUBCWD_TOKEN} but no subcwd was created")
    text = _path_text(raw)
    if text != _SUBCWD_TOKEN and not text.startswith(_SUBCWD_TOKEN + "/"):
        raise ValueError(f"{_SUBCWD_TOKEN} must be the first path segment")
    if text == _SUBCWD_TOKEN and not allow_root:
        raise ValueError(f"{field_name} does not accept bare {_SUBCWD_TOKEN}")
    suffix = text[len(_SUBCWD_TOKEN) :].lstrip("/")
    path = subcwd_path if not suffix else subcwd_path / suffix
    path = path.resolve()
    if not _is_relative_to(path, subcwd_path):
        raise ValueError(f"{_SUBCWD_TOKEN} path escapes the job directory: {raw}")
    return path


def _create_subcwd(subcwd_root):
    subcwd_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    for _ in range(20):
        path = subcwd_root / uuid.uuid4().hex
        try:
            path.mkdir(mode=0o700)
            return path.resolve()
        except FileExistsError:
            continue
    raise FileExistsError(f"could not create unique subcwd under {subcwd_root}")


def _normalize_subcwd_root(raw, cwd):
    if raw is None:
        return cwd
    text = _path_text(raw)
    if _SUBCWD_TOKEN in text:
        raise ValueError(f"subcwd_root does not accept {_SUBCWD_TOKEN}")
    path = _normalize_path(raw, cwd, must_exist=False)
    if not _is_relative_to(path, cwd):
        raise ValueError(f"subcwd_root must be inside cwd: {path}")
    return path


def _normalize_subcwd_files(subcwd_files):
    if subcwd_files is None:
        return []
    if not isinstance(subcwd_files, dict):
        raise ValueError("subcwd_files must be a dict")
    normalized = []
    for raw_path, raw_value in subcwd_files.items():
        relative_path = _normalize_subcwd_relative_path(raw_path)
        data = _normalize_subcwd_file_value(raw_value)
        normalized.append(_SubcwdFile(relative_path=relative_path, data=data))
    return normalized


def _normalize_subcwd_relative_path(raw_path):
    if not isinstance(raw_path, str):
        raise ValueError("subcwd_files keys must be strings")
    if not raw_path:
        raise ValueError("subcwd_files keys must be non-empty")
    path = pathlib.PurePosixPath(raw_path)
    if path.is_absolute():
        raise ValueError(f"subcwd_files path must be relative: {raw_path}")
    normalized = path.as_posix()
    _validate_collected_relative_path(normalized)
    return normalized


def _normalize_subcwd_file_value(raw_value):
    if not isinstance(raw_value, dict):
        raise ValueError("subcwd_files values must be dicts")
    has_text = "text" in raw_value
    has_bytes = "bytes" in raw_value
    if has_text == has_bytes:
        raise ValueError("subcwd_files values must contain exactly one of text or bytes")
    if has_text:
        text = raw_value["text"]
        if not isinstance(text, str):
            raise ValueError("subcwd_files text values must be strings")
        encoding = raw_value.get("encoding", "utf-8")
        if not isinstance(encoding, str) or not encoding:
            raise ValueError("subcwd_files encoding must be a non-empty string")
        extra_keys = set(raw_value) - {"text", "encoding"}
        if extra_keys:
            raise ValueError(f"unsupported subcwd_files text keys: {sorted(extra_keys)}")
        return text.encode(encoding)
    binary = raw_value["bytes"]
    if not isinstance(binary, bytes):
        raise ValueError("subcwd_files bytes values must be bytes")
    extra_keys = set(raw_value) - {"bytes"}
    if extra_keys:
        raise ValueError(f"unsupported subcwd_files bytes keys: {sorted(extra_keys)}")
    return binary


def _stage_subcwd_files(subcwd_path, subcwd_files):
    if not subcwd_files:
        return
    if subcwd_path is None:
        raise ValueError("subcwd_files requires a subcwd path")
    for item in subcwd_files:
        path = (subcwd_path / item.relative_path).resolve()
        if not _is_relative_to(path, subcwd_path):
            raise ValueError(f"subcwd_files path escapes the job directory: {item.relative_path}")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_bytes(item.data)


def _ensure_staged_subcwd_files_are_accessible(
    subcwd_path,
    subcwd_files,
    read_paths,
    write_paths,
):
    if subcwd_path is None or not subcwd_files:
        return
    allowed_paths = list(read_paths) + list(write_paths)
    for item in subcwd_files:
        path = (subcwd_path / item.relative_path).resolve()
        if any(path == allowed or _is_relative_to(path, allowed) for allowed in allowed_paths):
            continue
        raise ValueError(
            "subcwd_files path must be covered by read_paths or write_paths: "
            f"{_SUBCWD_TOKEN}/{item.relative_path}"
        )


def _prompt_subcwd_file_paths(subcwd_files):
    return [f"{_SUBCWD_TOKEN}/{item.relative_path}" for item in subcwd_files]


def _parse_review_output(content, *, stdout, stderr, argv):
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise CodexExecutionError(
            f"codex review output is not valid JSON: {exc}",
            returncode=None,
            stdout=stdout,
            stderr=stderr,
            argv=argv,
        ) from exc
    if not isinstance(parsed, dict):
        raise CodexExecutionError(
            "codex review output must be a JSON object",
            returncode=None,
            stdout=stdout,
            stderr=stderr,
            argv=argv,
        )
    passed = parsed.get("passed")
    repair_text = parsed.get("repair_text")
    summary = parsed.get("summary", "")
    if not isinstance(passed, bool):
        raise CodexExecutionError(
            "codex review output requires boolean passed",
            returncode=None,
            stdout=stdout,
            stderr=stderr,
            argv=argv,
        )
    if not isinstance(repair_text, str):
        raise CodexExecutionError(
            "codex review output requires string repair_text",
            returncode=None,
            stdout=stdout,
            stderr=stderr,
            argv=argv,
        )
    if not isinstance(summary, str):
        raise CodexExecutionError(
            "codex review output requires string summary",
            returncode=None,
            stdout=stdout,
            stderr=stderr,
            argv=argv,
        )
    if not passed and not repair_text.strip():
        raise CodexExecutionError(
            "codex review output requires non-empty repair_text when passed is false",
            returncode=None,
            stdout=stdout,
            stderr=stderr,
            argv=argv,
        )
    return {
        "passed": passed,
        "repair_text": repair_text,
        "summary": summary,
    }


def _review_failure_message(review):
    summary = review.summary.strip() if isinstance(review.summary, str) else ""
    if summary:
        return f"codex review reported issues: {summary}"
    return "codex review reported issues"


def _normalize_path(raw, cwd, *, must_exist):
    path = pathlib.Path(raw).expanduser()
    if not path.is_absolute():
        path = cwd / path
    path = path.resolve()
    if must_exist and not path.exists():
        raise FileNotFoundError(str(path))
    return path


def _collect_subcwd_files(
    subcwd_path,
    *,
    max_files,
    max_file_bytes,
    max_total_file_bytes,
):
    if not subcwd_path.exists():
        return {}
    files = {}
    total_bytes = 0
    for path in sorted(subcwd_path.rglob("*")):
        if path.is_symlink() or path.is_dir():
            continue
        if not path.is_file():
            continue
        if len(files) >= max_files:
            raise ValueError(f"too many files to collect: limit is {max_files}")
        size = path.stat().st_size
        if size > max_file_bytes:
            raise ValueError(
                f"file exceeds max_file_bytes: {path.relative_to(subcwd_path)}"
            )
        if total_bytes + size > max_total_file_bytes:
            raise ValueError(
                f"collected files exceed max_total_file_bytes: "
                f"limit is {max_total_file_bytes}"
            )
        relative = path.relative_to(subcwd_path).as_posix()
        _validate_collected_relative_path(relative)
        data = path.read_bytes()
        files[relative] = CodexFile(path=relative, bytes=data)
        total_bytes += len(data)
    return files


def _validate_collected_relative_path(path):
    if not path or path.startswith("/") or path == ".." or path.startswith("../"):
        raise ValueError(f"invalid collected file path: {path}")
    if "/../" in path or path.endswith("/.."):
        raise ValueError(f"invalid collected file path: {path}")


def _cleanup_tree(path):
    if path is None:
        return
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        pass


def _make_output_path():
    output_file = tempfile.NamedTemporaryFile(
        prefix="codex_api_wrapper_",
        suffix=".txt",
        delete=False,
    )
    output_path = output_file.name
    output_file.close()
    return output_path


def _prepare_job_log_path(log_dir, job_id, *, log_path=None):
    if log_path is not None:
        path = pathlib.Path(log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        return str(path)
    if log_dir is None:
        return None
    directory = pathlib.Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)
    return str(directory / f"{job_id}.log")


def _format_review_response_for_log(content):
    if not isinstance(content, str):
        return ""
    return _summarize_agent_message_text(content)


def _format_prompt_summary_for_log(prompt, *, prefix):
    if not isinstance(prompt, str):
        prompt = ""
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    preview = _truncate_activity_text(prompt, limit=320)
    return f"{prefix}: bytes={len(prompt.encode('utf-8'))} sha256={digest} preview={preview}"


def _make_temp_text_file(*, prefix, suffix, text):
    handle = tempfile.NamedTemporaryFile(
        prefix=prefix,
        suffix=suffix,
        delete=False,
        mode="w",
        encoding="utf-8",
    )
    try:
        handle.write(text)
        return handle.name
    finally:
        handle.close()


def _terminate_process_object(process):
    if process.poll() is not None:
        return
    try:
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
    except ProcessLookupError:
        pass


def _extract_checklist_feedback(validation):
    if validation is None:
        return ""
    stderr = validation.stderr or ""
    marker = "workflow checklist is incomplete:"
    if marker not in stderr:
        return ""
    return stderr.split(marker, 1)[1].strip()


def _default_repair_prompt(values):
    stage = values["failure_stage"]
    parts = [
        "The previous output failed validation or review.",
        "Do not broaden the task. Correct the previous output so it satisfies "
        "the reported failure feedback.",
        "",
        "Original request:",
        values["original_input"],
        "",
        "Failure stage:",
        stage,
        "",
        "Attempt:",
        values["attempt"],
        "",
        "Repair attempt:",
        values["repair_attempt"],
        "",
        "Validator return code:",
        values["validator_returncode"],
        "",
        "Validator stdout:",
        values["validator_stdout"],
        "",
        "Validator stderr:",
        values["validator_stderr"],
        "",
        "Previous final response:",
        values["previous_content"],
    ]
    if values["checklist_feedback"]:
        parts.extend(["", "Incomplete checklist items:", values["checklist_feedback"]])
    if values["review_summary"]:
        parts.extend(["", "Review summary:", values["review_summary"]])
    if values["review_repair_text"]:
        parts.extend(
            ["", "Review-requested repair changes:", values["review_repair_text"]]
        )
    if values["subcwd_section"]:
        parts.extend(["", values["subcwd_section"]])
    parts.extend(["", "Return only the corrected final response."])
    return "\n".join(parts)


def _default_review_prompt(values):
    parts = [
        "Internal review mode: codex_api_wrapper",
        "Review the completed job output and determine whether it follows the "
        "original request and the review instructions.",
        "",
        "Review instructions:",
        values["review_instructions"],
        "",
        "Original request:",
        values["original_input"],
        "",
        "Main job final response:",
        values["response_content"],
    ]
    if values["main_prompt"]:
        parts.extend(["", "Main job prompt:", values["main_prompt"]])
    if values["subcwd_path"]:
        parts.extend(["", "Subcwd path:", values["subcwd_path"]])
    if values["review_read_paths"]:
        parts.extend(["", values["review_read_paths"]])
    parts.extend(
        [
            "",
            "Return JSON with fields passed (bool), repair_text (string), and summary (string).",
            "When passed is true, repair_text may be empty.",
            "When passed is false, repair_text must explain the changes the main job should make.",
        ]
    )
    return "\n".join(parts)


def _build_review_session_developer_instructions(instructions):
    return (
        "You are the reviewer in a runner-reviewer conversation session. "
        "Continue the conversation as the reviewer, follow the review instructions, "
        "and answer only with the reviewer's next reply.\n\n"
        "Review instructions:\n"
        f"{instructions}"
    )


def _build_review_session_turn_input(*, history, message):
    if not history:
        return message
    lines = [
        "Conversation history:",
    ]
    for role, content in history:
        label = "Runner" if role == "runner" else "Reviewer"
        lines.extend([f"{label}:", content, ""])
    lines.extend(["Runner:", message])
    return "\n".join(lines)


def _build_review_session_prompt(
    *,
    instructions,
    history,
    message,
    ignore_global_context,
    output_schema_path,
):
    sections = []
    if ignore_global_context:
        sections.append(
            "Global AGENTS.md and global skills should not be used for this job. "
            "Use only the request below and explicitly attached paths as task context."
        )
    sections.extend(
        [
            "Internal review session mode: codex_api_wrapper",
            "Continue the conversation as the reviewer.",
            "",
            "Review instructions:",
            instructions,
            "",
            _build_review_session_turn_input(history=history, message=message),
        ]
    )
    if output_schema_path is not None:
        output_schema_section, _ = _resolve_inline_output_schema_section(
            output_schema_path=output_schema_path,
            max_file_bytes=_DEFAULT_MAX_INLINE_ATTACHMENT_BYTES,
        )
        sections.extend(["", "Output schema:", output_schema_section])
    sections.extend(["", "Return only the reviewer's next reply."])
    return "\n".join(sections).strip() + "\n"


def _path_text(path):
    return os.fspath(path)


def _is_relative_to(path, parent):
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _filesystem_inline_table(cwd, read_paths, write_paths, extra_read_paths=None):
    workspace_modes = {}
    absolute_modes = {}
    for path in read_paths:
        workspace_modes[_workspace_relative_permission_path(path, cwd)] = "read"
    for path in write_paths:
        workspace_modes[_workspace_relative_permission_path(path, cwd)] = "write"
    for path in extra_read_paths or []:
        resolved = pathlib.Path(path).expanduser().resolve()
        if _is_relative_to(resolved, cwd):
            permission_path = _workspace_relative_permission_path(resolved, cwd)
            workspace_modes.setdefault(permission_path, "read")
        else:
            absolute_modes.setdefault(str(resolved), "read")
    workspace_parts = [
        f"{_toml_string(path)}={_toml_string(mode)}"
        for path, mode in sorted(workspace_modes.items())
    ]
    absolute_parts = [
        f"{_toml_string(path)}={_toml_string(mode)}"
        for path, mode in sorted(absolute_modes.items())
    ]
    parts = [f"{_toml_string(':minimal')}={_toml_string('read')}"]
    parts.extend(absolute_parts)
    if workspace_parts:
        parts.append(
            f"{_toml_string(':workspace_roots')}="
            "{" + ",".join(workspace_parts) + "}"
        )
    return "{" + ",".join(parts) + "}"


def _sdk_sandbox_for_permissions(sdk, write_paths):
    # The SDK sandbox is coarse. Path-level boundaries are carried by the
    # wrapper-generated Permission Profile.
    return sdk.Sandbox.workspace_write if write_paths else sdk.Sandbox.read_only


def _workspace_relative_permission_path(path, cwd):
    if not _is_relative_to(path, cwd):
        raise ValueError(f"path must be inside cwd for permission profiles: {path}")
    relative = path.relative_to(cwd)
    if not relative.parts:
        return "."
    return relative.as_posix()


def _toml_string(value):
    text = str(value)
    escaped = text.replace("\\", "\\\\").replace("\"", "\\\"")
    return f"\"{escaped}\""


def _normalize_optional_reasoning_effort(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        raise ValueError("reasoning_effort must be a non-empty string")
    return text


def _normalize_allowed_skills(value):
    if value is None:
        return None
    if not isinstance(value, (list, tuple)):
        raise ValueError("allowed_skills must be None or []")
    skills = list(value)
    if not skills:
        return []
    raise ValueError(
        "allowed_skills supports only None or [] in v1; use [] to disable "
        "automatic skill instructions"
    )
