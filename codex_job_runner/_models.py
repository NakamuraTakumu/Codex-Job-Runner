"""Internal data models for the Codex job runner."""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, field
from typing import Any, Optional

from ._constants import _DEFAULT_REVIEW_TIMEOUT
from .types import CodexResponse


def _default_review_session_config():
    from .config import SessionConfig

    return SessionConfig(
        timeout=_DEFAULT_REVIEW_TIMEOUT,
        allow_shell_commands=False,
    )


@dataclass(frozen=True)
class ReviewConfig:
    context: str | tuple[str, ...] | list[str]
    artifact_paths: Any = ()
    session: Any = field(default_factory=_default_review_session_config)
    prompt_path: str | None = None
    reuse_session: bool = True
    include_main_context: bool = False
    enable_peer_exchange: bool = False
    max_repairs: int | None = None
    subagents: Any = None

    def __post_init__(self):
        from .config import SessionConfig, SubagentConfig

        if not isinstance(self.session, SessionConfig):
            raise TypeError("session must be a SessionConfig")
        if self.session.tools is not None:
            raise ValueError("review session must not expose tools")
        if self.session.workspace.write_paths:
            raise ValueError("review session must be read-only")
        if self.reuse_session is not True:
            raise ValueError("peer review requires reuse_session=True")
        if isinstance(self.artifact_paths, (str, pathlib.Path)):
            paths = (self.artifact_paths,)
        else:
            try:
                paths = tuple(self.artifact_paths)
            except TypeError as exc:
                raise ValueError("artifact_paths must be an iterable of paths") from exc
        for path in paths:
            if not isinstance(path, (str, pathlib.Path)) or not str(path):
                raise ValueError("artifact_paths entries must be non-empty paths")
        object.__setattr__(self, "artifact_paths", paths)
        if self.subagents is not None and not isinstance(
            self.subagents,
            SubagentConfig,
        ):
            raise TypeError("subagents must be a SubagentConfig or None")
        if self.max_repairs is not None and (
            not isinstance(self.max_repairs, int)
            or isinstance(self.max_repairs, bool)
            or self.max_repairs < 0
        ):
            raise ValueError("max_repairs must be a non-negative integer or None")


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
    subagents_enabled: bool
    max_concurrent_subagents: int
    subagent_context_policy: str
    default_subagent_model: Optional[str]
    default_subagent_reasoning_effort: Optional[str]


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
    success: bool
    diagnostic: str = ""


@dataclass(frozen=True)
class _ResolvedReviewConfig:
    session: Any
    context: tuple[str, ...]
    prompt_template: str | None
    read_paths: tuple[str, ...] | None
    timeout: int | float | None
    model: str | None
    reasoning_effort: str | None
    reuse_session: bool
    include_main_context: bool
    enable_peer_exchange: bool
    subagents_enabled: bool | None
    subagents: Any = None
    artifact_paths: tuple[Any, ...] = ()
    max_repairs: int | None = None


@dataclass(frozen=True)
class _SubcwdFile:
    relative_path: str
    data: bytes


@dataclass(frozen=True)
class _ReviewResult:
    passed: bool
    findings: tuple[str, ...]
    summary: str
    response: CodexResponse
    stdout: str
    stderr: str
    argv: list[str]
    input_summary: str = ""
    read_paths: tuple[pathlib.Path, ...] = ()
    stream_emitted: bool = False
    decision: Any = None
    decision_validation_failures: tuple[Any, ...] = ()
