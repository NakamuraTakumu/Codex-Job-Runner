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
            "attempt": self.attempt,
            "repair_index": self.repair_index,
            "argv": list(self.argv),
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }


@dataclass(frozen=True)
class CodexResponse:
    """Successful ``codex exec`` result."""

    content: str
    returncode: int
    stdout: str
    stderr: str
    argv: tuple[str, ...] = ()
    usage: dict[str, int] | None = None
    files: dict[str, CodexFile] = field(default_factory=dict)
    activity_log: tuple[CodexActivity, ...] = ()

    def to_dict(self):
        return {
            "content": self.content,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "argv": list(self.argv),
            "usage": None if self.usage is None else dict(self.usage),
            "files": {path: file.to_dict() for path, file in self.files.items()},
            "activity_log": [entry.to_dict() for entry in self.activity_log],
        }
