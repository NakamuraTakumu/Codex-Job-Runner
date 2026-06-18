"""Internal data models for the Codex job runner."""

from __future__ import annotations

import pathlib
from dataclasses import dataclass
from typing import Any, Optional

from ._constants import _DEFAULT_REVIEW_TIMEOUT
from .types import CodexResponse


@dataclass(frozen=True)
class ReviewConfig:
    context: str | tuple[str, ...] | list[str]
    prompt_path: str | None = None
    read_paths: tuple[str, ...] | None = None
    timeout: int | float | None = _DEFAULT_REVIEW_TIMEOUT
    model: str | None = None
    reasoning_effort: str | None = None
    reuse_session: bool = False
    include_main_context: bool = False


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
    context: tuple[str, ...]
    prompt_template: str | None
    read_paths: tuple[str, ...] | None
    timeout: int | float | None
    model: str | None
    reasoning_effort: str | None
    reuse_session: bool
    include_main_context: bool


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
