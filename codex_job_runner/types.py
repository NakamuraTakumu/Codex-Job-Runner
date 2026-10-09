"""Lightweight response objects."""

import json
from dataclasses import dataclass, field


@dataclass(frozen=True)
class CodexFile:
    """File collected from a job-local output directory."""

    path: str
    bytes: bytes

    @property
    def text(self):
        return self.bytes.decode("utf-8")

    def json(self):
        return json.loads(self.text)

    def to_dict(self):
        return {
            "path": self.path,
            "size": len(self.bytes),
        }


@dataclass(frozen=True)
class CodexActivity:
    """Wrapper-side activity log entry."""

    phase: str
    event: str
    message: str
    detail: str | None = None
    attempt: int | None = None
    repair_index: int | None = None
    argv: tuple[str, ...] = ()
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""

    def to_dict(self):
        return {
            "phase": self.phase,
            "event": self.event,
            "message": self.message,
            "detail": self.detail,
            "attempt": self.attempt,
            "repair_index": self.repair_index,
            "argv": list(self.argv),
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }


@dataclass(frozen=True)
class CodexNativeUsage:
    """One app-server ``rawResponse/completed`` usage observation.

    ``exact`` is true only when the server supplied usage and the model could
    be attributed.  This is usage telemetry, not a billing invoice.
    """

    response_id: str
    thread_id: str | None
    turn_id: str | None
    role: str
    model: str | None
    usage: dict[str, int] | None
    exact: bool

    def to_dict(self):
        return {
            "response_id": self.response_id,
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "role": self.role,
            "model": self.model,
            "usage": None if self.usage is None else dict(self.usage),
            "exact": self.exact,
        }


@dataclass(frozen=True)
class CodexResponse:
    """Successful Codex job result."""

    content: str
    returncode: int
    stdout: str
    stderr: str
    argv: tuple[str, ...] = ()
    usage: dict[str, int] | None = None
    pricing: dict[str, float | str] | None = None
    estimated_cost_usd: float | None = None
    native_usage: tuple[CodexNativeUsage, ...] = ()
    usage_cost_source: str = "parent_stream_fallback"
    files: dict[str, CodexFile] = field(default_factory=dict)
    activity_log: tuple[CodexActivity, ...] = ()
    log_path: str | None = None

    def to_dict(self):
        return {
            "content": self.content,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "argv": list(self.argv),
            "usage": None if self.usage is None else dict(self.usage),
            "pricing": None if self.pricing is None else dict(self.pricing),
            "estimated_cost_usd": self.estimated_cost_usd,
            "native_usage": [record.to_dict() for record in self.native_usage],
            "usage_cost_source": self.usage_cost_source,
            "files": {path: file.to_dict() for path, file in self.files.items()},
            "activity_log": [entry.to_dict() for entry in self.activity_log],
            "log_path": self.log_path,
        }
