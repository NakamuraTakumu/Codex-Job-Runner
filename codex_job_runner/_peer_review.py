"""Deterministic, runner-owned data for the peer-review protocol."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import pathlib
from dataclasses import dataclass
from typing import Any

from .errors import (
    CodexReviewProtocolError,
    CodexReviewerDecisionValidationError,
)

SCHEMA_VERSION = 2
# Each protocol output gets the first answer plus at most this many corrections.
MAX_CORRECTIONS = 2
# A finding judged unresolved this many re-reviews in a row stops the job as non-convergent.
MAX_CONSECUTIVE_UNRESOLVED = 2
CONTENT_PATH = "__codex_job_response_content__"


def _id_schema(values: list[str] | tuple[str, ...]) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "string"}
    if values:
        schema["enum"] = list(values)
    return schema


def reviewer_decision_schema(*, open_ids: tuple[str, ...], initial: bool) -> dict[str, Any]:
    """Return the SDK schema for a Reviewer protocol decision turn."""
    decisions: dict[str, Any] = {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "finding_id": _id_schema(open_ids),
                "resolution": {
                    "type": "string",
                    "enum": ["resolved", "withdrawn", "unresolved"],
                },
                "comment": {"type": "string"},
            },
            "required": ["finding_id", "resolution", "comment"],
            "additionalProperties": False,
        },
        "minItems": 0 if initial else len(open_ids),
        "maxItems": 0 if initial else len(open_ids),
    }
    return {
        "type": "object",
        "properties": {
            "decisions": decisions,
            "new_findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"comment": {"type": "string", "minLength": 1}},
                    "required": ["comment"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["decisions", "new_findings"],
        "additionalProperties": False,
    }


def main_finding_response_schema(*, open_ids: tuple[str, ...]) -> dict[str, Any]:
    """Return the SDK schema for the Main finding-response turn."""
    return {
        "type": "object",
        "properties": {
            "finding_responses": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "finding_id": _id_schema(open_ids),
                        "disposition": {
                            "type": "string",
                            "enum": ["addressed", "not_addressed", "disputed"],
                        },
                        "response": {"type": "string", "minLength": 1},
                    },
                    "required": ["finding_id", "disposition", "response"],
                    "additionalProperties": False,
                },
                "minItems": len(open_ids),
                "maxItems": len(open_ids),
            },
        },
        "required": ["finding_responses"],
        "additionalProperties": False,
    }


def _object(text: str, *, name: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise CodexReviewProtocolError(f"{name} must be a JSON object", diagnostic=str(exc)) from exc
    if not isinstance(value, dict):
        raise CodexReviewProtocolError(f"{name} must be a JSON object")
    return value


def _keys(value: dict[str, Any], expected: set[str], *, name: str) -> None:
    if set(value) != expected:
        raise CodexReviewProtocolError(
            f"{name} fields must be exactly {sorted(expected)!r}; got {sorted(value)!r}"
        )


def parse_reviewer_decision(text: str, *, open_ids: tuple[str, ...], initial: bool) -> dict[str, Any]:
    try:
        return _parse_reviewer_decision(text, open_ids=open_ids, initial=initial)
    except CodexReviewerDecisionValidationError:
        raise
    except CodexReviewProtocolError as exc:
        raise CodexReviewerDecisionValidationError(
            exc.message,
            diagnostic=exc.diagnostic or exc.message,
        ) from exc


def _parse_reviewer_decision(text: str, *, open_ids: tuple[str, ...], initial: bool) -> dict[str, Any]:
    value = _object(text, name="reviewer decision")
    _keys(value, {"decisions", "new_findings"}, name="reviewer decision")
    decisions, new = value["decisions"], value["new_findings"]
    if not isinstance(decisions, list) or not isinstance(new, list):
        raise CodexReviewProtocolError("reviewer decision arrays are required")
    if initial and decisions:
        raise CodexReviewProtocolError("initial review must not decide existing findings")
    seen = set()
    for item in decisions:
        if not isinstance(item, dict):
            raise CodexReviewProtocolError("reviewer decision entries must be objects")
        _keys(item, {"finding_id", "resolution", "comment"}, name="reviewer decision entry")
        fid, resolution, comment = item["finding_id"], item["resolution"], item["comment"]
        if not isinstance(fid, str) or fid in seen or fid not in open_ids:
            raise CodexReviewProtocolError("reviewer decisions must cover each open finding once")
        if resolution not in {"resolved", "withdrawn", "unresolved"} or not isinstance(comment, str):
            raise CodexReviewProtocolError("invalid reviewer decision")
        seen.add(fid)
    if set(open_ids) != seen:
        raise CodexReviewProtocolError("reviewer decision omits an open finding")
    for item in new:
        if not isinstance(item, dict):
            raise CodexReviewProtocolError("new findings must be objects")
        _keys(item, {"comment"}, name="new finding")
        if not isinstance(item["comment"], str) or not item["comment"].strip():
            raise CodexReviewProtocolError("new finding comment must be non-empty")
    return value


def parse_main_response(text: str, *, open_ids: tuple[str, ...]) -> list[dict[str, Any]]:
    """Validate a Main finding response; Main answers findings but never owns changes."""
    value = _object(text, name="main finding response")
    _keys(value, {"finding_responses"}, name="main finding response")
    responses = value["finding_responses"]
    if not isinstance(responses, list):
        raise CodexReviewProtocolError("finding_responses must be an array")
    by_finding = {}
    for item in responses:
        if not isinstance(item, dict):
            raise CodexReviewProtocolError("finding response must be an object")
        _keys(item, {"finding_id", "disposition", "response"}, name="finding response")
        fid = item["finding_id"]
        if not isinstance(fid, str) or fid in by_finding or fid not in open_ids:
            raise CodexReviewProtocolError("finding responses must answer each open finding once")
        if item["disposition"] not in {"addressed", "not_addressed", "disputed"}:
            raise CodexReviewProtocolError("invalid finding disposition")
        if not isinstance(item["response"], str) or not item["response"].strip():
            raise CodexReviewProtocolError("finding response text must be non-empty")
        by_finding[fid] = item
    if set(by_finding) != set(open_ids):
        raise CodexReviewProtocolError("finding responses omit an open finding")
    return [by_finding[fid] for fid in open_ids]


@dataclass(frozen=True)
class Artifacts:
    content: str
    files: dict[str, str]
    manifest: list[dict[str, Any]]


def _identity(path: str) -> dict[str, Any]:
    if path == CONTENT_PATH:
        return {"kind": "response_content", "path": None}
    return {"kind": "file", "path": path}


def _ordered_paths(paths) -> list[str]:
    files = sorted(path for path in paths if path != CONTENT_PATH)
    return [CONTENT_PATH, *files]


def capture_artifacts(*, cwd: pathlib.Path, paths: tuple[Any, ...], content: str, initial: bool) -> Artifacts:
    files: dict[str, str] = {}
    for raw in paths:
        path = (cwd / os.fspath(raw)).resolve()
        try: path.relative_to(cwd.resolve())
        except ValueError as exc: raise CodexReviewProtocolError("artifact path must be inside workspace") from exc
        if not path.exists():
            if initial: raise CodexReviewProtocolError(f"artifact path does not exist: {raw}")
            continue
        candidates = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())
        for file in candidates:
            try: files[file.relative_to(cwd).as_posix()] = file.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc: raise CodexReviewProtocolError(f"artifact is not readable text: {file}", diagnostic=str(exc)) from exc
    all_values = {CONTENT_PATH: content, **files}
    manifest = [
        {
            "artifact": _identity(path),
            "size": len(all_values[path].encode("utf-8")),
            "content_hash": hashlib.sha256(all_values[path].encode("utf-8")).hexdigest(),
        }
        for path in _ordered_paths(all_values)
    ]
    return Artifacts(content, files, manifest)


def changes(before: Artifacts, after: Artifacts) -> list[dict[str, Any]]:
    """Return runner-owned changes: one zero-context unified hunk per changed line block."""
    result = []
    for path in _ordered_paths(set(before.files) | set(after.files)):
        old = before.content if path == CONTENT_PATH else before.files.get(path, "")
        new = after.content if path == CONTENT_PATH else after.files.get(path, "")
        if old == new:
            continue
        old_lines, new_lines = old.splitlines(), new.splitlines()
        matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            header = f"@@ -{i1 + 1},{i2 - i1} +{j1 + 1},{j2 - j1} @@\n"
            body = "".join("-" + line + "\n" for line in old_lines[i1:i2])
            body += "".join("+" + line + "\n" for line in new_lines[j1:j2])
            result.append({"artifact": _identity(path), "diff": header + body})
    return [
        {"change_id": f"C{index:03d}", **item}
        for index, item in enumerate(result, 1)
    ]


def dump(path: pathlib.Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, str): path.write_text(value, encoding="utf-8")
    else: path.write_text(json.dumps({"schema_version": SCHEMA_VERSION, "payload": value}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def dump_package(path: pathlib.Path, package: dict[str, Any]) -> None:
    """The review package carries schema_version itself instead of an envelope."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(package, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
