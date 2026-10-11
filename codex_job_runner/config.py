"""Immutable, feature-oriented configuration for Codex sessions and jobs."""

from __future__ import annotations

import copy
import os
import pathlib
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Mapping

from ._client_helpers import (
    _normalize_allowed_skills,
    _normalize_optional_reasoning_effort,
    _normalize_web_search,
)
from ._constants import (
    _DEFAULT_MAX_FILE_BYTES,
    _DEFAULT_MAX_FILES,
    _DEFAULT_MAX_INLINE_CONTEXT_BYTES,
    _DEFAULT_MAX_INLINE_CONTEXT_TOTAL_BYTES,
    _DEFAULT_MAX_TOTAL_FILE_BYTES,
    _DEFAULT_MODEL,
    _DEFAULT_REASONING_EFFORT,
)
from ._tools import _normalize_tools


class _Config:
    def with_overrides(self, **changes: Any):
        """Return a newly validated config with selected fields replaced."""
        return replace(self, **changes)


def _optional_path(value: Any) -> pathlib.Path | None:
    return None if value is None else pathlib.Path(value).expanduser()


def _path_tuple(value: Any, *, field_name: str) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, os.PathLike)):
        raise ValueError(f"{field_name} must be an iterable of paths")
    try:
        result = tuple(value)
    except TypeError as exc:
        raise ValueError(f"{field_name} must be an iterable of paths") from exc
    for item in result:
        try:
            text = os.fspath(item)
        except TypeError as exc:
            raise ValueError(
                f"{field_name} entries must be strings or path-like objects"
            ) from exc
        if not isinstance(text, str) or not text:
            raise ValueError(f"{field_name} entries must be non-empty paths")
    return result


def _non_negative_integer(value: Any, *, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


def _positive_integer(value: Any, *, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _positive_number(value: Any, *, field_name: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or value <= 0
    ):
        raise ValueError(f"{field_name} must be a positive number")
    return float(value)


def _immutable_subcwd_files(value: Any) -> Mapping[str, Mapping[str, Any]]:
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise ValueError("subcwd_files must be a mapping")
    result: dict[str, Mapping[str, Any]] = {}
    for path, content in value.items():
        if not isinstance(path, str) or not path:
            raise ValueError("subcwd_files keys must be non-empty strings")
        if not isinstance(content, Mapping):
            raise ValueError("subcwd_files values must be mappings")
        result[path] = MappingProxyType(copy.deepcopy(dict(content)))
    return MappingProxyType(result)


def _immutable_allowed_models(value: Any) -> Mapping[str, tuple[str, ...]]:
    if not isinstance(value, Mapping) or not value:
        raise ValueError("allowed_models must be a non-empty mapping")
    result: dict[str, tuple[str, ...]] = {}
    for model, efforts in value.items():
        normalized_model = _normalize_optional_reasoning_effort(model)
        if normalized_model is None:
            raise ValueError("allowed_models keys must be non-empty model names")
        if isinstance(efforts, str):
            raise ValueError(
                "allowed_models values must be iterables of reasoning efforts"
            )
        try:
            normalized_efforts = tuple(
                _normalize_optional_reasoning_effort(effort)
                for effort in efforts
            )
        except TypeError as exc:
            raise ValueError(
                "allowed_models values must be iterables of reasoning efforts"
            ) from exc
        if not normalized_efforts or any(
            effort is None for effort in normalized_efforts
        ):
            raise ValueError(
                "allowed_models values must contain non-empty reasoning efforts"
            )
        if len(set(normalized_efforts)) != len(normalized_efforts):
            raise ValueError(
                f"allowed_models contains duplicate efforts for {normalized_model}"
            )
        result[normalized_model] = normalized_efforts
    return MappingProxyType(result)


@dataclass(frozen=True)
class WorkspaceConfig(_Config):
    """Workspace, permission, SUBCWD, and inline-context settings."""

    cwd: Any = None
    subcwd_root: Any = None
    read_paths: Any = ()
    write_paths: Any = ()
    subcwd_files: Any = None
    inline_context: Any = True
    max_inline_context_bytes: Any = _DEFAULT_MAX_INLINE_CONTEXT_BYTES
    max_inline_context_total_bytes: Any = _DEFAULT_MAX_INLINE_CONTEXT_TOTAL_BYTES

    def __post_init__(self) -> None:
        object.__setattr__(self, "cwd", _optional_path(self.cwd))
        object.__setattr__(self, "subcwd_root", _optional_path(self.subcwd_root))
        object.__setattr__(
            self,
            "read_paths",
            _path_tuple(self.read_paths, field_name="read_paths"),
        )
        object.__setattr__(
            self,
            "write_paths",
            _path_tuple(self.write_paths, field_name="write_paths"),
        )
        object.__setattr__(
            self, "subcwd_files", _immutable_subcwd_files(self.subcwd_files)
        )
        object.__setattr__(self, "inline_context", bool(self.inline_context))
        object.__setattr__(
            self,
            "max_inline_context_bytes",
            _non_negative_integer(
                self.max_inline_context_bytes,
                field_name="max_inline_context_bytes",
            ),
        )
        object.__setattr__(
            self,
            "max_inline_context_total_bytes",
            _non_negative_integer(
                self.max_inline_context_total_bytes,
                field_name="max_inline_context_total_bytes",
            ),
        )


@dataclass(frozen=True)
class ToolConfig(_Config):
    """Function Tool bridge settings."""

    tools: Any = ()
    max_concurrent_calls: Any = 8
    timeout: Any = 60

    def __post_init__(self) -> None:
        object.__setattr__(self, "tools", _normalize_tools(self.tools))
        object.__setattr__(
            self,
            "max_concurrent_calls",
            _positive_integer(
                self.max_concurrent_calls, field_name="max_concurrent_calls"
            ),
        )
        object.__setattr__(
            self,
            "timeout",
            _positive_number(self.timeout, field_name="timeout"),
        )


@dataclass(frozen=True)
class SessionConfig(_Config):
    """Settings sufficient to create and run a Codex session."""

    model: Any = _DEFAULT_MODEL
    reasoning_effort: Any = _DEFAULT_REASONING_EFFORT
    codex_bin: Any = None
    timeout: Any = None
    env: Any = None
    context: Any = ()
    ignore_global_context: Any = False
    allowed_skills: Any = None
    allow_shell_commands: Any = True
    workspace: WorkspaceConfig = field(default_factory=WorkspaceConfig)
    tools: ToolConfig | None = None
    web_search: Any = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "reasoning_effort",
            _normalize_optional_reasoning_effort(self.reasoning_effort),
        )
        object.__setattr__(self, "web_search", _normalize_web_search(self.web_search))
        object.__setattr__(
            self,
            "codex_bin",
            os.fspath(self.codex_bin) if self.codex_bin is not None else None,
        )
        if self.timeout is not None:
            object.__setattr__(
                self,
                "timeout",
                _positive_number(self.timeout, field_name="timeout"),
            )
        object.__setattr__(
            self, "env", MappingProxyType(dict(self.env or {}))
        )
        if self.context is None:
            context = ()
        elif isinstance(self.context, (str, os.PathLike)):
            context = (self.context,)
        else:
            context = tuple(self.context)
        object.__setattr__(self, "context", context)
        object.__setattr__(
            self, "ignore_global_context", bool(self.ignore_global_context)
        )
        normalized_skills = _normalize_allowed_skills(self.allowed_skills)
        object.__setattr__(
            self,
            "allowed_skills",
            None if normalized_skills is None else tuple(normalized_skills),
        )
        object.__setattr__(
            self, "allow_shell_commands", bool(self.allow_shell_commands)
        )
        if not isinstance(self.workspace, WorkspaceConfig):
            raise TypeError("workspace must be a WorkspaceConfig")
        if self.tools is not None and not isinstance(self.tools, ToolConfig):
            raise TypeError("tools must be a ToolConfig or None")

    def _runner_kwargs(self) -> dict[str, Any]:
        """Return transitional arguments for the current private runner."""
        tool_config = self.tools
        return {
            "context": self.context,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "web_search": self.web_search,
            "codex_bin": self.codex_bin,
            "cwd": self.workspace.cwd,
            "subcwd_root": self.workspace.subcwd_root,
            "timeout": self.timeout,
            "ignore_global_context": self.ignore_global_context,
            "allowed_skills": self.allowed_skills,
            "allow_shell_commands": self.allow_shell_commands,
            "subagents_enabled": False,
            "max_concurrent_subagents": 2,
            "subagent_context_policy": "none",
            "default_subagent_model": None,
            "default_subagent_reasoning_effort": None,
            "env": dict(self.env),
            "log_dir": None,
            "tools": () if tool_config is None else tool_config.tools,
            "max_concurrent_tool_calls": (
                8 if tool_config is None else tool_config.max_concurrent_calls
            ),
            "tool_timeout": 60.0 if tool_config is None else tool_config.timeout,
        }


@dataclass(frozen=True)
class SubagentConfig(_Config):
    """Direct child-job settings for one parent CodexJob."""

    session: SessionConfig
    allowed_models: Any
    max_concurrent: Any = 2

    def __post_init__(self) -> None:
        if not isinstance(self.session, SessionConfig):
            raise TypeError("session must be a SessionConfig")
        allowed_models = _immutable_allowed_models(self.allowed_models)
        object.__setattr__(self, "allowed_models", allowed_models)
        object.__setattr__(
            self,
            "max_concurrent",
            _positive_integer(
                self.max_concurrent,
                field_name="max_concurrent",
            ),
        )
        allowed_efforts = allowed_models.get(self.session.model)
        if (
            allowed_efforts is None
            or self.session.reasoning_effort not in allowed_efforts
        ):
            raise ValueError(
                "session model and reasoning_effort must be present in "
                "allowed_models"
            )


@dataclass(frozen=True)
class ValidationConfig(_Config):
    """Output-validation repair settings."""

    repair: Any = False
    repair_prompt_path: Any = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "repair", bool(self.repair))
        object.__setattr__(
            self,
            "repair_prompt_path",
            _optional_path(self.repair_prompt_path),
        )


@dataclass(frozen=True)
class FileCollectionConfig(_Config):
    """Limits for collecting files from a completed job."""

    enabled: Any = False
    max_files: Any = _DEFAULT_MAX_FILES
    max_file_bytes: Any = _DEFAULT_MAX_FILE_BYTES
    max_total_file_bytes: Any = _DEFAULT_MAX_TOTAL_FILE_BYTES

    def __post_init__(self) -> None:
        object.__setattr__(self, "enabled", bool(self.enabled))
        for name in ("max_files", "max_file_bytes", "max_total_file_bytes"):
            object.__setattr__(
                self,
                name,
                _non_negative_integer(getattr(self, name), field_name=name),
            )


@dataclass(frozen=True)
class LoggingConfig(_Config):
    """Optional event-log directory and explicit log path."""

    directory: Any = None
    path: Any = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "directory", _optional_path(self.directory))
        object.__setattr__(self, "path", _optional_path(self.path))

    @property
    def log_dir(self) -> pathlib.Path | None:
        """Compatibility name for the former flat config field."""
        return self.directory


__all__ = [
    "FileCollectionConfig",
    "LoggingConfig",
    "SessionConfig",
    "SubagentConfig",
    "ToolConfig",
    "ValidationConfig",
    "WorkspaceConfig",
]
