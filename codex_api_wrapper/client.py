"""Client and job implementation for running ``codex exec``."""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import json
from dataclasses import dataclass, replace
from typing import Iterable, Optional, Sequence

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
_DEFAULT_REVIEW_TIMEOUT = 30
_REVIEW_OUTPUT_SCHEMA = json.dumps(
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "passed": {"type": "boolean"},
            "repair_text": {"type": "string"},
            "summary": {"type": "string"},
        },
        "required": ["passed", "repair_text"],
    },
    separators=(",", ":"),
)


@dataclass(frozen=True)
class ReviewConfig:
    instructions: str
    prompt_path: str | None = None
    timeout: int | float | None = _DEFAULT_REVIEW_TIMEOUT
    model: str | None = None
    reasoning_effort: str | None = None


@dataclass(frozen=True)
class _ResolvedOptions:
    model: Optional[str]
    reasoning_effort: Optional[str]
    cwd: pathlib.Path
    subcwd_root: pathlib.Path
    timeout: Optional[float]
    ignore_global_context: bool
    allowed_skills: Optional[list[str]]
    allow_shell_commands: bool


@dataclass(frozen=True)
class _CodexAttempt:
    argv: list[str]
    prompt: str
    process: subprocess.Popen
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
    timeout: int | float | None
    model: str | None
    reasoning_effort: str | None


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


class CodexJob:
    """A non-blocking ``codex exec`` subprocess."""

    def __init__(
        self,
        *,
        attempt,
        spawn_attempt,
        original_input,
        attachments,
        inline_attachment_sections,
        output_schema_section,
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
        repair_validation_failures=False,
        max_repairs=1,
        repair_prompt_template=None,
        validator_env=None,
        validator_cwd=None,
    ):
        self.id = "codex_job_" + uuid.uuid4().hex
        self.argv = list(attempt.argv)
        self._spawn_attempt = spawn_attempt
        self._original_input = original_input
        self._attachments = list(attachments)
        self._inline_attachment_sections = list(inline_attachment_sections)
        self._output_schema_section = output_schema_section
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
        self._repair_validation_failures = bool(repair_validation_failures)
        self._max_repairs = max_repairs
        self._repair_prompt_template = repair_prompt_template
        self._validator_env = dict(validator_env or {})
        self._validator_cwd = pathlib.Path(validator_cwd) if validator_cwd else None
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
                "codex exec timed out",
                returncode=None,
                stdout=self._stdout,
                stderr=self._stderr,
                argv=self.argv,
                activity_log=self._activity_log_snapshot(),
            )

        with self._lock:
            if self._status == _COMPLETED:
                return self._response
            if self._status == _CANCELLED:
                raise CodexCancelledError(
                    "codex exec was cancelled",
                    returncode=self._returncode,
                    stdout=self._stdout,
                    stderr=self._stderr,
                    argv=self.argv,
                    activity_log=self._activity_log_snapshot(),
                )
            if self._error is not None:
                raise self._error
            raise CodexExecutionError(
                "codex exec failed",
                returncode=self._returncode,
                stdout=self._stdout,
                stderr=self._stderr,
                argv=self.argv,
                activity_log=self._activity_log_snapshot(),
            )

    def cancel(self):
        with self._lock:
            if self._done.is_set():
                return
            self._status = _CANCELLED
        self._log_activity("cancel", "start", "job cancellation requested")
        self._terminate_process()
        self._done.wait(1)

    def _timeout(self):
        with self._lock:
            if self._done.is_set():
                return
            self._status = _TIMEOUT
        self._log_activity("timeout", "start", "job timeout requested")
        self._terminate_process()
        self._done.wait(1)

    def _communicate(self):
        attempt = self._attempt
        repairs = 0
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

                content = self._read_output_file(attempt.output_path)
                last_response = CodexResponse(
                    content=content,
                    returncode=returncode,
                    stdout=stdout or "",
                    stderr=stderr or "",
                    argv=tuple(attempt.argv),
                    usage=_copy_optional_usage(usage_totals),
                    files={},
                    activity_log=(),
                )

                if self._output_validator is None:
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
                            or repairs >= self._max_repairs
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
                        self._log_activity(
                            "repair",
                            "start",
                            "repair attempt requested after validator failure",
                            attempt=attempt.attempt_index + 1,
                            repair_index=repairs,
                        )
                        prompt = self._build_repair_prompt(
                            response=last_response,
                            validation=last_validation,
                            attempt_index=attempt.attempt_index + 1,
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
                        self._log_activity(
                            "main_attempt",
                            "start",
                            "repair main attempt started",
                            attempt=attempt.attempt_index,
                            repair_index=attempt.repair_index,
                            argv=attempt.argv,
                        )
                        self._log_activity(
                            "repair",
                            "end",
                            "repair attempt spawned",
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
                    main_prompt=attempt.prompt,
                )
                with self._lock:
                    if self._status in (_TIMEOUT, _CANCELLED):
                        self._finish()
                        return

                if review_result.passed:
                    self._set_success(last_response)
                    self._finish()
                    return

                if repairs >= self._max_repairs:
                    self._set_review_error(
                        response=last_response,
                        review=review_result,
                        attempts=attempt.attempt_index + 1,
                        repairs=repairs,
                    )
                    self._finish()
                    return

                repairs += 1
                self._log_activity(
                    "repair",
                    "start",
                    "repair attempt requested after review failure",
                    attempt=attempt.attempt_index + 1,
                    repair_index=repairs,
                )
                prompt = self._build_repair_prompt(
                    response=last_response,
                    validation=last_validation,
                    review=review_result,
                    attempt_index=attempt.attempt_index + 1,
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
                self._log_activity(
                    "main_attempt",
                    "start",
                    "repair main attempt started",
                    attempt=attempt.attempt_index,
                    repair_index=attempt.repair_index,
                    argv=attempt.argv,
                )
                self._log_activity(
                    "repair",
                    "end",
                    "repair attempt spawned",
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
                    f"codex exec communication failed: {exc}",
                    returncode=None,
                    stdout=self._stdout,
                    stderr=self._stderr,
                    argv=self.argv,
                    activity_log=self._activity_log_snapshot(),
                )
            self._finish()
            return

    def _run_attempt(self, attempt):
        stdout, stderr = attempt.process.communicate(attempt.prompt)
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
                    files=files,
                    activity_log=tuple(self._activity_log_snapshot()),
                )
                self._status = _COMPLETED
            else:
                self._status = _FAILED
                self._error = CodexExecutionError(
                    f"codex exec file collection failed: {collection_error}",
                    returncode=response.returncode,
                    stdout=self._stdout,
                    stderr=self._stderr,
                    argv=self.argv,
                    activity_log=self._activity_log_snapshot(),
                )

    def _set_execution_error(self, returncode):
        files, collection_error = self._collect_output_files()
        message = f"codex exec exited with status {returncode}"
        if collection_error is not None:
            message += f"; file collection failed: {collection_error}"
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
            )

    def _set_validation_error(self, *, response, validation, attempts, repairs):
        files, collection_error = self._collect_output_files()
        if collection_error is not None:
            with self._lock:
                self._status = _FAILED
                self._error = CodexExecutionError(
                    f"codex exec file collection failed: {collection_error}",
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
            files=files,
            activity_log=tuple(self._activity_log_snapshot()),
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
            )

    def _set_review_error(self, *, response, review, attempts, repairs):
        files, collection_error = self._collect_output_files()
        if collection_error is not None:
            with self._lock:
                self._status = _FAILED
                self._error = CodexExecutionError(
                    f"codex exec file collection failed: {collection_error}",
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
            files=files,
            activity_log=tuple(self._activity_log_snapshot()),
        )
        with self._lock:
            self._status = _FAILED
            self._error = CodexValidationError(
                "codex review reported issues",
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
            self._log_activity("file_collection", "end", "file collection finished")
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
        self._log_activity(
            "validator",
            "start",
            "validator started",
            attempt=attempt_index,
            repair_index=repair_index,
            argv=self._output_validator,
        )
        timed_out = False
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
                argv=self._output_validator,
                returncode=process.returncode,
                stdout=locals().get("stdout", "") or "",
                stderr=locals().get("stderr", "") or "",
            )
            with self._lock:
                if self._validator_process is process:
                    self._validator_process = None

    def _run_review(self, *, response, main_prompt):
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
        )
        try:
            result = self._review_runner(
                original_input=self._original_input,
                main_prompt=main_prompt,
                response=response,
                subcwd_path=self._subcwd_path,
                review_config=self._review_config,
            )
        except CodexExecutionError as exc:
            self._log_activity(
                "review",
                "error",
                f"review failed: {exc.message}",
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
            )
            raise
        self._log_activity(
            "review",
            "end",
            "review finished",
            argv=result.argv,
            returncode=result.response.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )
        return result

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
            "validator_returncode": (
                "" if validation is None else str(validation.returncode)
            ),
            "validator_stdout": "" if validation is None else validation.stdout,
            "validator_stderr": "" if validation is None else validation.stderr,
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
        )

    def _repair_subcwd_section(self):
        if self._subcwd_path is None:
            return ""
        return (
            f"Previous attempt files remain under {_SUBCWD_TOKEN}.\n"
            "Read and update those files when needed. Write generated files under "
            f"{_SUBCWD_TOKEN}."
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
            attempt=attempt,
            repair_index=repair_index,
            argv=tuple(argv or ()),
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )
        with self._lock:
            self._activity_log.append(entry)

    def _activity_log_snapshot(self):
        with self._lock:
            return list(self._activity_log)

    def _attach_activity_log(self, error):
        error.activity_log = self._activity_log_snapshot()
        return error

    def _finish(self):
        self._cleanup_output_files()
        self._cleanup_subcwd()
        self._sync_activity_log_to_outcome()
        self._done.set()

    def _cleanup_output_files(self):
        self._log_activity("cleanup", "start", "output file cleanup started")
        for output_path in self._output_paths:
            try:
                output_path.unlink()
            except FileNotFoundError:
                pass
        self._log_activity("cleanup", "end", "output file cleanup finished")

    def _cleanup_subcwd(self):
        if self._subcwd_path is None:
            return
        self._log_activity("cleanup", "start", "subcwd cleanup started")
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
        self._log_activity("cleanup", "end", "subcwd cleanup finished")

    def _terminate_process(self):
        with self._lock:
            process = self._process
            validator_process = self._validator_process
        self._terminate_one_process(process)
        if validator_process is not None:
            self._terminate_one_process(validator_process)

    def _terminate_one_process(self, process):
        _terminate_process_object(process)

    def _sync_activity_log_to_outcome(self):
        snapshot = tuple(self._activity_log_snapshot())
        with self._lock:
            if self._response is not None:
                self._response = replace(self._response, activity_log=snapshot)
            if self._error is not None:
                self._error.activity_log = list(snapshot)

    def _refresh_status(self):
        if self._done.is_set():
            return
        if self._process.poll() is not None:
            self._done.wait(0.05)


class CodexClient:
    """OpenAI-like entry point for a minimal ``codex exec`` wrapper."""

    def __init__(
        self,
        *,
        codex_bin="codex",
        model=_DEFAULT_MODEL,
        reasoning_effort=_DEFAULT_REASONING_EFFORT,
        cwd=None,
        subcwd_root=None,
        timeout=None,
        ignore_global_context=False,
        allowed_skills=None,
        allow_shell_commands=True,
        env=None,
    ):
        self.codex_bin = codex_bin
        self.model = model
        self.reasoning_effort = _normalize_optional_reasoning_effort(
            reasoning_effort
        )
        self.cwd = cwd
        self.subcwd_root = subcwd_root
        self.timeout = timeout
        self.ignore_global_context = ignore_global_context
        self.allowed_skills = _copy_optional_list(allowed_skills)
        self.allow_shell_commands = bool(allow_shell_commands)
        self.env = dict(env or {})

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
        review=None,
        ignore_global_context=None,
        allowed_skills=None,
        allow_shell_commands=None,
        output_schema_path=None,
        output_validator=None,
        validator_timeout=30,
        repair_validation_failures=False,
        max_repairs=1,
        repair_prompt_path=None,
        collect_files=False,
        max_files=_DEFAULT_MAX_FILES,
        max_file_bytes=_DEFAULT_MAX_FILE_BYTES,
        max_total_file_bytes=_DEFAULT_MAX_TOTAL_FILE_BYTES,
        inline_attachments=True,
        max_inline_attachment_bytes=_DEFAULT_MAX_INLINE_ATTACHMENT_BYTES,
        max_inline_attachment_total_bytes=_DEFAULT_MAX_INLINE_ATTACHMENT_TOTAL_BYTES,
    ):
        if not isinstance(input, str) or not input.strip():
            raise ValueError("input must be a non-empty string")

        options = self._resolve_options(
            model=model,
            reasoning_effort=reasoning_effort,
            cwd=cwd,
            timeout=timeout,
            ignore_global_context=ignore_global_context,
            allowed_skills=allowed_skills,
            allow_shell_commands=allow_shell_commands,
        )
        _reject_subcwd_token(attachments or [], "attachments")
        _reject_subcwd_token(read_paths or [], "read_paths")
        attachments = _normalize_paths(attachments or [], options.cwd, must_exist=True)
        read_paths = _normalize_paths(read_paths or [], options.cwd, must_exist=True)
        subcwd_files = _normalize_subcwd_files(subcwd_files)
        output_schema_path = _normalize_optional_path(
            output_schema_path,
            options.cwd,
            must_exist=True,
        )
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
        review_config = _normalize_review_config(review, options.cwd)
        write_paths, subcwd_path = _normalize_write_paths(
            write_paths or [],
            options.cwd,
            options.subcwd_root,
            require_subcwd=bool(subcwd_files),
        )
        try:
            _stage_subcwd_files(subcwd_path, subcwd_files)
        except Exception:
            _cleanup_tree(subcwd_path)
            raise

        prompt_subcwd_files = _prompt_subcwd_file_paths(subcwd_files)
        review_profile_name = "codex_api_wrapper_review_" + uuid.uuid4().hex

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
        )
        profile_name = "codex_api_wrapper_" + uuid.uuid4().hex
        env = os.environ.copy()
        env.update(self.env)

        def spawn_attempt(*, prompt, attempt_index, repair_index):
            output_path = _make_output_path()
            argv = self._build_argv(
                output_path=output_path,
                profile_name=profile_name,
                options=options,
                read_paths=read_paths,
                write_paths=write_paths,
                output_schema_path=output_schema_path,
            )
            try:
                process = subprocess.Popen(
                    argv,
                    cwd=str(options.cwd),
                    env=env,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                )
            except Exception:
                try:
                    pathlib.Path(output_path).unlink()
                except FileNotFoundError:
                    pass
                raise
            return _CodexAttempt(
                argv=list(argv),
                prompt=prompt,
                process=process,
                output_path=pathlib.Path(output_path),
                attempt_index=attempt_index,
                repair_index=repair_index,
            )

        def run_review(*, original_input, main_prompt, response, subcwd_path, review_config):
            review_options = _ResolvedOptions(
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
                ignore_global_context=options.ignore_global_context,
                allowed_skills=options.allowed_skills,
                allow_shell_commands=options.allow_shell_commands,
            )
            review_read_paths = []
            if subcwd_path is not None:
                review_read_paths = [subcwd_path]
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
                    subcwd_path=subcwd_path,
                    review_config=review_config,
                )
                review_prompt = _build_prompt(
                    input=review_input,
                    attachments=[],
                    read_paths=review_read_paths,
                    write_paths=[],
                    subcwd_files=[],
                    subcwd_path=None,
                    ignore_global_context=review_options.ignore_global_context,
                    output_schema_section=output_schema_section,
                )
                argv = self._build_argv(
                    output_path=str(output_path),
                    profile_name=review_profile_name,
                    options=review_options,
                    read_paths=review_read_paths,
                    write_paths=[],
                    output_schema_path=schema_path,
                )
                process = subprocess.Popen(
                    argv,
                    cwd=str(review_options.cwd),
                    env=env,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                )
                try:
                    stdout, stderr = process.communicate(
                        review_prompt,
                        timeout=review_config.timeout,
                    )
                except subprocess.TimeoutExpired:
                    _terminate_process_object(process)
                    stdout, stderr = process.communicate()
                    raise CodexExecutionError(
                        "codex review job timed out",
                        returncode=None,
                        stdout=stdout or "",
                        stderr=stderr or "",
                        argv=argv,
                    )
                if process.returncode != 0:
                    raise CodexExecutionError(
                        f"codex review job exited with status {process.returncode}",
                        returncode=process.returncode,
                        stdout=stdout or "",
                        stderr=stderr or "",
                        argv=argv,
                    )
                content = output_path.read_text(encoding="utf-8")
                parsed = _parse_review_output(
                    content,
                    stdout=stdout or "",
                    stderr=stderr or "",
                    argv=argv,
                )
                review_response = CodexResponse(
                    content=content,
                    returncode=process.returncode,
                    stdout=stdout or "",
                    stderr=stderr or "",
                    argv=tuple(argv),
                    usage=_extract_usage_from_stdout(stdout or ""),
                    files={},
                    activity_log=(),
                )
                return _ReviewResult(
                    passed=parsed["passed"],
                    repair_text=parsed["repair_text"],
                    summary=parsed["summary"],
                    response=review_response,
                    stdout=stdout or "",
                    stderr=stderr or "",
                    argv=list(argv),
                )
            finally:
                try:
                    output_path.unlink()
                except FileNotFoundError:
                    pass
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
            _cleanup_tree(subcwd_path)
            raise
        return CodexJob(
            attempt=attempt,
            spawn_attempt=spawn_attempt,
            original_input=input.strip(),
            attachments=attachments,
            inline_attachment_sections=inline_attachment_sections,
            output_schema_section=output_schema_section,
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
            repair_validation_failures=repair_validation_failures,
            max_repairs=max_repairs,
            repair_prompt_template=repair_prompt_template,
            validator_env=env,
            validator_cwd=options.cwd,
        )

    def run(self, **kwargs):
        timeout = kwargs.pop("timeout", None)
        job = self.start(timeout=timeout, **kwargs)
        return job.result(timeout=timeout)

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

    def _resolve_options(
        self,
        *,
        model,
        reasoning_effort,
        cwd,
        timeout,
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
            resolved_allowed_skills = _copy_optional_list(self.allowed_skills)
        else:
            resolved_allowed_skills = _copy_optional_list(allowed_skills)
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
            ignore_global_context=bool(resolved_ignore),
            allowed_skills=resolved_allowed_skills,
            allow_shell_commands=resolved_allow_shell_commands,
        )

    def _build_argv(
        self,
        *,
        output_path,
        profile_name,
        options,
        read_paths,
        write_paths,
        output_schema_path,
    ):
        argv = [
            self.codex_bin,
            "--ask-for-approval",
            "never",
            "exec",
            "--json",
            "--output-last-message",
            output_path,
            "--skip-git-repo-check",
            "--ephemeral",
            "-C",
            str(options.cwd),
        ]
        if options.model:
            argv.extend(["--model", options.model])
        if options.reasoning_effort:
            argv.extend(
                [
                    "-c",
                    f"model_reasoning_effort={_toml_string(options.reasoning_effort)}",
                ]
            )
        if output_schema_path is not None:
            argv.extend(["--output-schema", str(output_schema_path)])
        if options.ignore_global_context:
            argv.append("--ignore-user-config")
            argv.extend(["-c", "project_doc_max_bytes=0"])
        if not options.allow_shell_commands:
            argv.extend(["-c", "features.shell_tool=false"])
            argv.extend(["-c", "allow_login_shell=false"])

        extra_profile_read_paths = []
        if options.allow_shell_commands:
            extra_profile_read_paths = _codex_runtime_read_paths(self.codex_bin)
        filesystem_profile = _filesystem_inline_table(
            options.cwd,
            read_paths,
            write_paths,
            extra_read_paths=extra_profile_read_paths,
        )
        argv.extend(_skill_config_args(options.allowed_skills))
        argv.extend(
            [
                "-c",
                f"default_permissions={_toml_string(profile_name)}",
                "-c",
                (
                    f"permissions.{profile_name}.filesystem="
                    f"{filesystem_profile}"
                ),
            ]
        )
        argv.append("-")
        return argv


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
            + "\n\nThese files are pre-populated before the job starts. Read and "
            "edit them directly when needed."
        )
    return "\n\n".join(sections) + "\n"


def _build_review_input(*, original_input, main_prompt, response, subcwd_path, review_config):
    values = {
        "review_instructions": review_config.instructions,
        "original_input": original_input,
        "main_prompt": main_prompt,
        "response_content": response.content,
        "subcwd_path": "" if subcwd_path is None else str(subcwd_path),
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
    timeout = review.timeout
    if timeout is not None and not isinstance(timeout, (int, float)):
        raise ValueError("review.timeout must be numeric or None")
    return _ResolvedReviewConfig(
        instructions=review.instructions.strip(),
        prompt_template=prompt_template,
        timeout=timeout,
        model=review.model,
        reasoning_effort=_normalize_optional_reasoning_effort(
            review.reasoning_effort
        )
        if review.reasoning_effort is not None
        else None,
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


def _looks_like_path(value):
    return (
        value.startswith(".")
        or value.startswith("~")
        or os.path.isabs(value)
        or os.sep in value
        or (os.altsep is not None and os.altsep in value)
    )


def _normalize_write_paths(paths, cwd, subcwd_root, *, require_subcwd=False):
    subcwd_path = None
    if require_subcwd or _contains_subcwd_token(paths):
        subcwd_path = _create_subcwd(subcwd_root)
    try:
        normalized = []
        for raw in paths:
            text = _path_text(raw)
            if _SUBCWD_TOKEN in text and not text.startswith(_SUBCWD_TOKEN):
                raise ValueError(f"{_SUBCWD_TOKEN} must be the first path segment")
            if text.startswith(_SUBCWD_TOKEN):
                normalized.append(_expand_subcwd_write_path(raw, subcwd_path))
            else:
                normalized.append(_normalize_path(raw, cwd, must_exist=False))
        if require_subcwd and subcwd_path is not None and subcwd_path not in normalized:
            normalized.append(subcwd_path)
        return normalized, subcwd_path
    except Exception:
        _cleanup_tree(subcwd_path)
        raise


def _contains_subcwd_token(paths):
    return any(_SUBCWD_TOKEN in _path_text(path) for path in paths)


def _reject_subcwd_token(paths, field_name):
    if _contains_subcwd_token(paths):
        raise ValueError(f"{field_name} does not accept {_SUBCWD_TOKEN}")


def _expand_subcwd_write_path(raw, subcwd_path):
    if subcwd_path is None:
        raise ValueError(f"{_SUBCWD_TOKEN} requires a subcwd path")
    text = _path_text(raw)
    if text != _SUBCWD_TOKEN and not text.startswith(_SUBCWD_TOKEN + "/"):
        raise ValueError(f"{_SUBCWD_TOKEN} must be the first path segment")
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


def _default_repair_prompt(values):
    parts = [
        "The previous output failed validation.",
        "Do not broaden the task. Correct the previous output so it satisfies "
        "the validator error.",
        "",
        "Original request:",
        values["original_input"],
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
        "original request and the main job prompt.",
        "",
        "Review instructions:",
        values["review_instructions"],
        "",
        "Original request:",
        values["original_input"],
        "",
        "Main job prompt:",
        values["main_prompt"],
        "",
        "Main job final response:",
        values["response_content"],
    ]
    if values["subcwd_path"]:
        parts.extend(["", "Subcwd path:", values["subcwd_path"]])
    parts.extend(
        [
            "",
            "Return JSON with fields passed (bool), repair_text (string), and summary (string).",
            "When passed is true, repair_text may be empty.",
            "When passed is false, repair_text must explain the changes the main job should make.",
        ]
    )
    return "\n".join(parts)


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


def _workspace_relative_permission_path(path, cwd):
    if not _is_relative_to(path, cwd):
        raise ValueError(f"path must be inside cwd for permission profiles: {path}")
    relative = path.relative_to(cwd)
    if not relative.parts:
        return "."
    return relative.as_posix()


def _codex_runtime_read_paths(codex_bin):
    executable = _resolve_executable_path(codex_bin)
    if executable is None:
        return []
    if executable.parent.name == "bin":
        return [executable.parent.parent]
    return [executable.parent]


def _resolve_executable_path(executable):
    candidate = pathlib.Path(str(executable)).expanduser()
    if not candidate.is_absolute():
        resolved = shutil.which(str(executable))
        if resolved is None:
            return None
        candidate = pathlib.Path(resolved)
    try:
        return candidate.resolve(strict=True)
    except OSError:
        return None


def _skill_config_args(allowed_skills):
    if allowed_skills is None:
        return []
    if allowed_skills == []:
        return ["-c", "skills.include_instructions=false"]

    allowed = set(allowed_skills)
    discovered = _discover_skill_names()
    disabled = sorted(name for name in discovered if name not in allowed)
    args = ["-c", "skills.bundled.enabled=false"]
    if disabled:
        items = ",".join(
            "{name=" + _toml_string(name) + ",enabled=false}" for name in disabled
        )
        args.extend(["-c", f"skills.config=[{items}]"])
    return args


def _discover_skill_names():
    root = pathlib.Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser() / "skills"
    if not root.exists():
        return set()
    names = set()
    for skill_md in root.glob("**/SKILL.md"):
        name = _read_skill_name(skill_md)
        if name:
            names.add(name)
    return names


def _read_skill_name(skill_md):
    try:
        with skill_md.open("r", encoding="utf-8") as handle:
            for _ in range(30):
                line = handle.readline()
                if not line:
                    break
                if line.startswith("name:"):
                    return line.split(":", 1)[1].strip().strip("\"'")
    except OSError:
        return None
    return skill_md.parent.name


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


def _copy_optional_list(value):
    if value is None:
        return None
    return list(value)
