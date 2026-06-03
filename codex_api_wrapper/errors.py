"""Exceptions raised by codex_api_wrapper."""


class CodexExecutionError(RuntimeError):
    """Raised when ``codex exec`` exits with a non-zero status."""

    def __init__(
        self,
        message,
        *,
        returncode=None,
        stdout="",
        stderr="",
        argv=None,
        files=None,
        activity_log=None,
    ):
        super().__init__(message)
        self.message = message
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.argv = list(argv or [])
        self.files = dict(files or {})
        self.activity_log = list(activity_log or [])


class CodexTimeoutError(CodexExecutionError):
    """Raised when a job is stopped because result(timeout=...) expired."""


class CodexCancelledError(CodexExecutionError):
    """Raised when result() is called for a cancelled job."""


class CodexValidationError(CodexExecutionError):
    """Raised when a successful ``codex exec`` result fails validation."""

    def __init__(
        self,
        message,
        *,
        response=None,
        validator_returncode=None,
        validator_stdout="",
        validator_stderr="",
        validator_argv=None,
        attempts=1,
        repairs=0,
        files=None,
        failure_stage="validator",
        review_response=None,
        review_stdout="",
        review_stderr="",
        review_argv=None,
        activity_log=None,
    ):
        super().__init__(
            message,
            returncode=validator_returncode,
            stdout=validator_stdout,
            stderr=validator_stderr,
            argv=validator_argv,
            files=files,
            activity_log=activity_log,
        )
        self.response = response
        self.validator_returncode = validator_returncode
        self.validator_stdout = validator_stdout
        self.validator_stderr = validator_stderr
        self.validator_argv = list(validator_argv or [])
        self.attempts = attempts
        self.repairs = repairs
        self.failure_stage = failure_stage
        self.review_response = review_response
        self.review_stdout = review_stdout
        self.review_stderr = review_stderr
        self.review_argv = list(review_argv or [])
