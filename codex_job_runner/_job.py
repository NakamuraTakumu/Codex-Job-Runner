"""Non-blocking Codex job handle implementation."""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys
import threading
import uuid
from dataclasses import replace

from ._client_helpers import *  # noqa: F401,F403
from ._constants import (
    _CANCELLED,
    _COMPLETED,
    _DEFAULT_MAX_FILE_BYTES,
    _DEFAULT_MAX_FILES,
    _DEFAULT_MAX_TOTAL_FILE_BYTES,
    _DEFAULT_MODEL,
    _FAILED,
    _RUNNING,
    _SUBCWD_TOKEN,
    _TIMEOUT,
)
from ._models import _ValidationResult
from .errors import (
    CodexCancelledError,
    CodexExecutionError,
    CodexTimeoutError,
    CodexValidationError,
)
from .types import CodexActivity, CodexResponse


class CodexJob:
    """A non-blocking Codex job handle."""

    def __init__(
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
        main_session_cleanup=None,
    ):
        self.id = "codex_job_" + uuid.uuid4().hex
        self.log_path = _prepare_job_log_path(log_dir, self.id, log_path=log_path)
        self.argv = list(attempt.argv)
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
        self._main_session_cleanup = main_session_cleanup
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
                    main_context=self._initial_main_context,
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

    def _run_review(self, *, response, main_context, attempt_index, repair_index):
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
                main_context=main_context,
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
        self._cleanup_main_session()
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
