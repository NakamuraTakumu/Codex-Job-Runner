"""Exceptions raised by codex_job_runner."""


class CodexExecutionError(RuntimeError):
    """Raised when a Codex job attempt fails."""

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
        log_path=None,
    ):
        super().__init__(message)
        self.message = message
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.argv = list(argv or [])
        self.files = dict(files or {})
        self.activity_log = list(activity_log or [])
        self.log_path = log_path


class CodexTimeoutError(CodexExecutionError):
    """Raised when a job is stopped because result(timeout=...) expired."""


class CodexCancelledError(CodexExecutionError):
    """Raised when result() is called for a cancelled job."""


class CodexNotStartedError(CodexExecutionError):
    """Raised when result() is called for a pending job."""


class CodexAlreadyStartedError(CodexExecutionError):
    """Raised when a job is started more than once."""


class CodexSessionClosedError(CodexExecutionError):
    """Raised when an operation requires an open Codex session."""


class CodexValidationError(CodexExecutionError):
    """Raised when a successful Codex job result fails validation."""

    def __init__(
        self,
        message,
        *,
        response=None,
        validation_diagnostic="",
        attempts=1,
        repairs=0,
        files=None,
        failure_stage="validator",
        review_response=None,
        review_stdout="",
        review_stderr="",
        review_argv=None,
        activity_log=None,
        log_path=None,
    ):
        super().__init__(
            message,
            files=files,
            activity_log=activity_log,
            log_path=log_path,
        )
        self.response = response
        self.validation_diagnostic = validation_diagnostic
        self.attempts = attempts
        self.repairs = repairs
        self.failure_stage = failure_stage
        self.review_response = review_response
        self.review_stdout = review_stdout
        self.review_stderr = review_stderr
        self.review_argv = list(review_argv or [])


class CodexReviewProtocolError(CodexExecutionError):
    """Raised when a peer-review protocol artifact or ledger is invalid."""

    def __init__(self, message, *, failure_stage="review", round_index=None,
                 diagnostic="", review_artifact_paths=(), response=None,
                 log_path=None, **kwargs):
        super().__init__(message, log_path=log_path, **kwargs)
        self.failure_stage = failure_stage
        self.round_index = round_index
        self.diagnostic = diagnostic
        self.review_artifact_paths = tuple(review_artifact_paths)
        self.response = response


class CodexReviewerDecisionValidationError(CodexReviewProtocolError):
    """Raised when a completed reviewer turn returns an invalid decision."""
