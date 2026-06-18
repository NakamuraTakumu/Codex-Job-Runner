"""Helper functions for the Codex job runner client implementation."""

from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import traceback
import uuid

from ._constants import (
    _API_PRICING_SOURCE_URL,
    _DEFAULT_MAX_INLINE_CONTEXT_BYTES,
    _MODEL_PRICING_USD_PER_MILLION,
    _SUBCWD_TOKEN,
)
from ._models import ReviewConfig, _ResolvedReviewConfig, _SubcwdFile, _ValidationResult
from .errors import CodexExecutionError, CodexValidationError
from .types import CodexActivity, CodexFile

def _build_prompt(
    *,
    input,
    context_paths,
    read_paths,
    write_paths,
    subcwd_files,
    subcwd_path,
    ignore_global_context,
    context_sections=None,
    output_schema_section=None,
    output_model_source_section=None,
    review_notice=None,
):
    sections = []
    if ignore_global_context:
        sections.append(
            "Global AGENTS.md and global skills should not be used for this job. "
            "Use only the request below and explicitly supplied context."
        )
    if context_sections:
        sections.append("Context:\n" + "\n\n".join(context_sections))
    if output_schema_section:
        sections.append("Output schema:\n" + output_schema_section)
    if output_model_source_section:
        sections.append("Output model source:\n" + output_model_source_section)
    if review_notice:
        sections.append(review_notice)
    if context_paths:
        sections.append(_format_path_section("Context paths", context_paths))
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
            + "\n\nThese files are pre-populated before the job starts. "
            "Only read or edit a staged file when the corresponding "
            f"{_SUBCWD_TOKEN}/... path is covered by this job's read_paths "
            "or write_paths."
        )
    sections.append(input.strip())
    return "\n\n".join(sections) + "\n"


def _build_review_notice():
    return (
        "Review mode:\n"
        "After this attempt completes, an internal reviewer will check the final "
        "response against the original request and review instructions. Produce "
        "a complete, defensible answer; incomplete or weak output may be sent "
        "back for repair."
    )


def _build_repair_turn_prompt(*, input):
    return input.strip() + "\n"


def _build_review_input(
    *,
    original_input,
    main_context,
    response,
    subcwd_path,
    review_config,
    review_read_paths,
):
    values = {
        "review_context": "\n\n".join(review_config.context),
        "original_input": original_input,
        "main_context": main_context if review_config.include_main_context else "",
        "response_content": response.content,
        "subcwd_path": "" if subcwd_path is None else str(subcwd_path),
        "review_read_paths": _format_review_read_paths_for_log(
            review_read_paths,
            subcwd_path=subcwd_path,
        )
        if review_read_paths
        else "",
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


def _format_review_read_paths_for_log(paths, *, subcwd_path):
    display_paths = []
    for path in paths:
        if subcwd_path is not None and (path == subcwd_path or _is_relative_to(path, subcwd_path)):
            relative = path.relative_to(subcwd_path).as_posix()
            display_paths.append(
                _SUBCWD_TOKEN if not relative or relative == "." else f"{_SUBCWD_TOKEN}/{relative}"
            )
            continue
        display_paths.append(str(path))
    return _format_path_section("Review readable paths", display_paths)


def _resolve_inline_context_sections(
    *,
    context_entries,
    inline_context,
    max_file_bytes,
    max_total_bytes,
    initial_total_bytes=0,
):
    should_inline = True if inline_context is None else bool(inline_context)
    if not isinstance(max_file_bytes, int) or max_file_bytes < 0:
        raise ValueError("max_inline_context_bytes must be a non-negative integer")
    if not isinstance(max_total_bytes, int) or max_total_bytes < 0:
        raise ValueError(
            "max_inline_context_total_bytes must be a non-negative integer"
        )

    sections = []
    if not isinstance(initial_total_bytes, int) or initial_total_bytes < 0:
        raise ValueError("initial_total_bytes must be a non-negative integer")

    total = initial_total_bytes
    for kind, value in context_entries:
        if kind == "text":
            sections.append(_format_inline_context_text(value))
            continue
        path = value
        if not should_inline:
            continue
        if path.is_dir():
            continue
        if path.is_symlink() or not path.is_file():
            continue
        size = path.stat().st_size
        if size > max_file_bytes:
            raise ValueError(
                f"context file exceeds max_inline_context_bytes: {path}"
            )
        if total + size > max_total_bytes:
            raise ValueError(
                "inline context exceeds max_inline_context_total_bytes"
            )
        data = path.read_bytes()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"context file is not valid UTF-8 text: {path}") from exc
        sections.append(_format_inline_context_file(path, text))
        total += len(data)
    return sections


def _normalize_context_items(context, cwd):
    if context is None:
        return []
    if isinstance(context, (str, os.PathLike)):
        raw_items = [context]
    else:
        raw_items = list(context)
    normalized_entries = []
    for item in raw_items:
        if isinstance(item, os.PathLike):
            normalized_entries.append(("path", _normalize_path(item, cwd, must_exist=True)))
            continue
        if not isinstance(item, str):
            raise ValueError("context entries must be strings or paths")
        if not item.strip():
            raise ValueError("context entries must be non-empty")
        candidate = pathlib.Path(item).expanduser()
        if not candidate.is_absolute():
            candidate = cwd / candidate
        if candidate.exists():
            normalized_entries.append(("path", candidate.resolve()))
            continue
        if _looks_like_context_path(item):
            raise FileNotFoundError(str(candidate.resolve()))
        normalized_entries.append(("text", item.strip()))
    return normalized_entries


def _context_paths_from_entries(context_entries):
    return [value for kind, value in context_entries if kind == "path"]


def _looks_like_context_path(value):
    text = str(value)
    if _looks_like_path(text):
        return True
    if "\n" in text or "\r" in text:
        return False
    if any(char.isspace() for char in text):
        return False
    suffix = pathlib.PurePosixPath(text).suffix
    return bool(suffix)


def _format_inline_context_text(text):
    return f"<ContextText>\n{text}\n</ContextText>"


def _resolve_inline_output_schema_section(
    *,
    output_schema_path,
    max_file_bytes,
):
    if output_schema_path is None:
        return None, 0
    if not isinstance(max_file_bytes, int) or max_file_bytes < 0:
        raise ValueError("max_inline_context_bytes must be a non-negative integer")
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
            f"output_schema_path exceeds max_inline_context_bytes: {output_schema_path}"
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


def _format_inline_context_file(path, text):
    return (
        f"<ContextFile path={_xml_attr(str(path))}>\n"
        f"{text}\n"
        "</ContextFile>"
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


def _copy_optional_pricing(pricing):
    if pricing is None:
        return None
    return dict(pricing)


def _resolve_model_pricing(model):
    if not isinstance(model, str) or not model.strip():
        return None
    rates = _MODEL_PRICING_USD_PER_MILLION.get(model.strip())
    if rates is None:
        return None
    pricing = {"model": model.strip(), "source_url": _API_PRICING_SOURCE_URL}
    pricing["input_usd_per_million"] = rates["input"]
    if rates["cached_input"] is not None:
        pricing["cached_input_usd_per_million"] = rates["cached_input"]
    pricing["output_usd_per_million"] = rates["output"]
    return pricing


def _estimate_cost_usd_for_usage(*, usage, pricing):
    if usage is None or pricing is None:
        return None
    input_tokens = usage.get("input_tokens", 0)
    cached_input_tokens = usage.get("cached_input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    reasoning_output_tokens = usage.get("reasoning_output_tokens", 0)
    output_billed_tokens = output_tokens + reasoning_output_tokens
    non_cached_input_tokens = max(input_tokens - cached_input_tokens, 0)
    input_cost = non_cached_input_tokens * float(pricing["input_usd_per_million"]) / 1_000_000
    cached_input_rate = float(pricing.get("cached_input_usd_per_million", 0.0))
    cached_input_cost = cached_input_tokens * cached_input_rate / 1_000_000
    output_cost = output_billed_tokens * float(pricing["output_usd_per_million"]) / 1_000_000
    return input_cost + cached_input_cost + output_cost


def _format_usage_summary(*, model, usage):
    parts = []
    if isinstance(model, str) and model.strip():
        parts.append(f"model={model.strip()}")
    for key in (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "total_tokens",
    ):
        value = usage.get(key)
        if isinstance(value, int):
            parts.append(f"{key}={value}")
    pricing = _resolve_model_pricing(model)
    if pricing is not None:
        parts.append(
            "usd_per_million="
            + "input:"
            + str(pricing["input_usd_per_million"])
            + ",cached_input:"
            + str(pricing.get("cached_input_usd_per_million", 0.0))
            + ",output:"
            + str(pricing["output_usd_per_million"])
        )
        estimated_cost_usd = _estimate_cost_usd_for_usage(usage=usage, pricing=pricing)
        if estimated_cost_usd is not None:
            parts.append(f"estimated_cost_usd={estimated_cost_usd:.6f}")
    return " ".join(parts)


def _response_model_name(response):
    if response is None:
        return None
    pricing = getattr(response, "pricing", None)
    if isinstance(pricing, dict):
        model = pricing.get("model")
        if isinstance(model, str) and model.strip():
            return model.strip()
    return _model_name_from_argv(getattr(response, "argv", ()))


def _model_name_from_argv(argv):
    for index, token in enumerate(argv):
        if token == "--model" and index + 1 < len(argv):
            model = argv[index + 1]
            if isinstance(model, str) and model.strip():
                return model.strip()
    return None


def _emit_activity_stdout(entry):
    sys.stdout.write(_format_activity_stdout(entry) + "\n")
    sys.stdout.flush()


def _stream_activity_from_line(line, *, detail=None, attempt=None, repair_index=None):
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(event, dict):
        return None
    event_type = event.get("type")
    if not isinstance(event_type, str):
        return None
    message = f"stream event observed: {event_type}"
    if event_type in {"item.completed", "item.delta"}:
        item_type = event.get("item_type")
        item_text = event.get("item_text") or event.get("delta_text")
        if isinstance(item_type, str) and isinstance(item_text, str) and item_text:
            message = f"stream event observed: {event_type} ({item_type}): {item_text}"
        elif isinstance(item_type, str):
            message = f"stream event observed: {event_type} ({item_type})"
    elif event_type == "plan.updated":
        summary = event.get("summary")
        if isinstance(summary, str) and summary:
            message = f"stream event observed: plan.updated: {summary}"
    return CodexActivity(
        phase="stream",
        event=event_type,
        message=message,
        detail=detail,
        attempt=attempt,
        repair_index=repair_index,
        stdout=line,
    )


def _stream_item_text(item):
    item_type = getattr(item, "type", None)
    if item_type == "agentMessage":
        return _summarize_agent_message_text(getattr(item, "text", ""))
    if item_type == "reasoning":
        summary = _extract_reasoning_text(getattr(item, "summary", None))
        if summary:
            return _truncate_activity_text(summary)
        content = _extract_reasoning_text(getattr(item, "content", None))
        if content:
            return _truncate_activity_text(content)
    if item_type == "webSearch":
        return _truncate_activity_text(_describe_web_search(item))
    if item_type == "mcpToolCall":
        return _truncate_activity_text(_describe_mcp_tool_call(item))
    if item_type == "fileChange":
        return _truncate_activity_text(_describe_file_change(item))
    return ""


def _extract_reasoning_text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            text = _extract_reasoning_text(item)
            if text:
                parts.append(text)
        return " ".join(parts)
    text = getattr(value, "text", None)
    if isinstance(text, str) and text.strip():
        return text
    summary_text = getattr(value, "summary_text", None)
    if isinstance(summary_text, str) and summary_text.strip():
        return summary_text
    if isinstance(value, dict):
        for key in ("text", "summary_text", "summaryText", "content"):
            text = _extract_reasoning_text(value.get(key))
            if text:
                return text
    return str(value) if isinstance(value, (int, float)) else ""


def _describe_web_search(item):
    action = getattr(item, "action", None)
    action_root = getattr(action, "root", action)
    action_type = getattr(action_root, "type", None)
    if action_type == "search":
        query = getattr(action_root, "query", None)
        if isinstance(query, str) and query.strip():
            return f"search {query}"
        queries = getattr(action_root, "queries", None) or []
        if queries:
            return "search " + ", ".join(str(part) for part in queries[:3] if part)
    if action_type == "openPage":
        url = getattr(action_root, "url", None)
        if isinstance(url, str) and url.strip():
            return f"open {url}"
    if action_type == "findInPage":
        pattern = getattr(action_root, "pattern", None)
        url = getattr(action_root, "url", None)
        if isinstance(pattern, str) and pattern.strip() and isinstance(url, str) and url.strip():
            return f"find {pattern} in {url}"
        if isinstance(pattern, str) and pattern.strip():
            return f"find {pattern}"
    query = getattr(item, "query", None)
    if isinstance(query, str) and query.strip():
        return f"query {query}"
    return ""


def _summarize_agent_message_text(text):
    if not isinstance(text, str):
        return ""
    stripped = text.strip()
    if not stripped:
        return ""
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return _truncate_activity_text(stripped)
    if isinstance(payload, dict):
        parts = []
        company_name = payload.get("company_name")
        if isinstance(company_name, str) and company_name.strip():
            parts.append(f"company_name={company_name.strip()}")
        verdict = payload.get("verdict")
        if isinstance(verdict, str) and verdict.strip():
            parts.append(f"verdict={verdict.strip()}")
        references = payload.get("references")
        if isinstance(references, list):
            parts.append(f"references={len(references)}")
        if not parts:
            keys = [str(key) for key in list(payload.keys())[:5]]
            if keys:
                parts.append("keys=" + ", ".join(keys))
        return _truncate_activity_text("JSON response " + ", ".join(parts))
    if isinstance(payload, list):
        return _truncate_activity_text(f"JSON response items={len(payload)}")
    return _truncate_activity_text(stripped)


def _describe_mcp_tool_call(item):
    server = getattr(item, "server", None)
    tool = getattr(item, "tool", None)
    status = getattr(item, "status", None)
    result = getattr(item, "result", None)
    details = []
    if isinstance(server, str) and server:
        details.append(server)
    if isinstance(tool, str) and tool:
        details.append(tool)
    if isinstance(status, str) and status:
        details.append(status)
    result_text = _extract_tool_result_text(result)
    if result_text:
        details.append(result_text)
    return " ".join(details)


def _extract_tool_result_text(result):
    if result is None:
        return ""
    content = getattr(result, "content", None)
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str) and item.strip():
                parts.append(item)
                continue
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text)
                    continue
                inner = item.get("content")
                text = _extract_reasoning_text(inner)
                if text:
                    parts.append(text)
        if parts:
            return " ".join(parts[:2])
    structured = getattr(result, "structured_content", None)
    if structured:
        return _summarize_mapping(structured)
    return ""


def _describe_file_change(item):
    changes = getattr(item, "changes", None) or []
    paths = []
    for change in changes:
        path = getattr(change, "path", None)
        kind = getattr(change, "kind", None)
        if isinstance(path, str) and path:
            if isinstance(kind, str) and kind:
                paths.append(f"{kind}:{path}")
            else:
                paths.append(path)
    status = getattr(item, "status", None)
    parts = []
    if isinstance(status, str) and status:
        parts.append(status)
    if paths:
        preview = ", ".join(paths[:3])
        if len(paths) > 3:
            preview += f" (+{len(paths) - 3})"
        parts.append(preview)
    return " ".join(parts)


def _summarize_mapping(value):
    if isinstance(value, dict):
        parts = []
        for key, item in list(value.items())[:3]:
            if isinstance(item, (str, int, float)) and not isinstance(item, bool):
                parts.append(f"{key}={item}")
            else:
                parts.append(str(key))
        return ", ".join(parts)
    return ""


def _truncate_activity_text(text, *, limit=240):
    if not isinstance(text, str):
        return ""
    normalized = " ".join(text.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3] + "..."


def _stdout_field_text(text):
    if not isinstance(text, str):
        return '""'
    normalized = " ".join(text.split())
    escaped = normalized.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _format_activity_stdout(entry):
    if entry.phase == "stream":
        return _format_stream_activity_stdout(entry)
    if entry.phase == "review" and entry.event == "reviewed_response":
        return f"[Reviewer][Reviewed Response] {_stdout_field_text(entry.message)}"

    actor = _activity_actor(entry.phase)
    kind = _non_stream_kind(entry)
    parts = [f"[{actor}][{kind}]"]
    _append_activity_context_parts(parts, entry)
    if entry.returncode is not None:
        parts.append(f"returncode={entry.returncode}")
    summary = _non_stream_summary(entry)
    if summary:
        parts.append(summary)
    if _needs_explicit_message(entry):
        parts.append(_stdout_field_text(entry.message))
    return " ".join(parts)


def _format_stream_activity_stdout(entry):
    try:
        event = json.loads(entry.stdout) if entry.stdout else {}
    except json.JSONDecodeError:
        event = {}
    event_type = event.get("type")
    actor = _stream_actor(entry)
    if event_type == "item.completed":
        item_type = event.get("item_type", "?")
        item_text = event.get("item_text")
        parts = [f"[{actor}][{_stream_item_label(item_type)}]"]
        _append_activity_context_parts(parts, entry)
        if isinstance(item_text, str) and item_text:
            parts.append(_stdout_field_text(item_text))
        return " ".join(parts)
    if event_type == "item.delta":
        item_type = event.get("item_type", "?")
        delta_text = event.get("delta_text")
        parts = [f"[{actor}][{_stream_item_label(item_type)}]"]
        _append_activity_context_parts(parts, entry)
        if isinstance(delta_text, str) and delta_text:
            parts.append(_stdout_field_text(delta_text))
        return " ".join(parts)
    if event_type == "plan.updated":
        summary = event.get("summary")
        parts = [f"[{actor}][Plan]"]
        _append_activity_context_parts(parts, entry)
        if isinstance(summary, str) and summary:
            parts.append(_stdout_field_text(summary))
        return " ".join(parts)
    if event_type == "thread.started":
        parts = [f"[{actor}][Start]"]
        _append_activity_context_parts(parts, entry)
        return " ".join(parts)
    if event_type == "turn.completed":
        parts = [f"[{actor}][Result]"]
        _append_activity_context_parts(parts, entry)
        return " ".join(parts)
    parts = [f"[{actor}][{_stream_event_kind(entry)}]"]
    _append_activity_context_parts(parts, entry)
    fallback = _stream_fallback_summary(entry)
    if fallback:
        parts.append(fallback)
    parts.append(_stdout_field_text(entry.message))
    return " ".join(parts)


def _append_activity_context_parts(parts, entry):
    if not _should_show_activity_context(entry):
        return
    if entry.attempt is not None:
        parts.append(f"attempt={entry.attempt}")
    if entry.repair_index is not None:
        parts.append(f"repair={entry.repair_index}")


def _should_show_activity_context(entry):
    if entry.phase == "stream":
        try:
            event = json.loads(entry.stdout) if entry.stdout else {}
        except json.JSONDecodeError:
            event = {}
        return event.get("type") in {"thread.started", "turn.completed"}
    return entry.event in {"start", "end"}


def _activity_actor(phase):
    return {
        "main_attempt": "Main Attempt",
        "validator": "Validator",
        "review": "Reviewer",
        "repair": "Repair",
        "file_collection": "File Collection",
        "cleanup": "Cleanup",
        "cancel": "Cancel",
        "timeout": "Timeout",
    }.get(phase, phase.replace("_", " ").title())


def _stream_actor(entry):
    origin = getattr(entry, "detail", None)
    if origin == "review":
        return "Reviewer"
    if origin == "validator":
        return "Validator"
    return "Main Attempt"


def _non_stream_kind(entry):
    if entry.event == "usage":
        return "Usage"
    if entry.event == "detail":
        return "Message"
    if entry.phase == "main_attempt":
        if entry.event == "error" or "exited with status" in entry.message:
            return "Error"
        return _event_kind(entry.event)
    if entry.phase == "repair" and entry.event == "prompt":
        return "Prompt"
    if entry.phase in {"validator", "review", "file_collection", "cleanup", "cancel", "timeout"}:
        return _event_kind(entry.event)
    return "Event"


def _non_stream_summary(entry):
    if entry.event == "usage":
        return entry.message
    if entry.phase == "cleanup":
        if "output file cleanup" in entry.message:
            return "output cleanup"
        if "subcwd cleanup" in entry.message:
            return "subcwd cleanup"
    if entry.phase == "main_attempt":
        if "exited with status" in entry.message:
            return ""
        return ""
    if entry.phase == "validator":
        return ""
    if entry.phase == "review":
        return ""
    if entry.phase == "file_collection":
        return ""
    return ""


def _event_kind(event):
    return {
        "start": "Start",
        "end": "Result",
        "error": "Error",
        "usage": "Usage",
        "input": "Input",
    }.get(event, "Event")


def _stream_item_label(item_type):
    return {
        "reasoning": "Thinking",
        "agentMessage": "Message",
        "webSearch": "Search",
        "mcpToolCall": "Tool",
        "fileChange": "Edit",
    }.get(item_type, "Item")


def _stream_event_kind(entry):
    return {
        "start": "Start",
        "end": "Result",
        "error": "Error",
        "thread.started": "Start",
        "turn.completed": "Result",
        "item.completed": "Item",
    }.get(entry.event, "Event")


def _stream_fallback_summary(entry):
    return {
        "start": "started",
        "end": "finished",
        "error": "failed",
        "thread.started": "thread started",
        "turn.completed": "turn completed",
        "item.completed": "item completed",
    }.get(entry.event, entry.event)


def _needs_explicit_message(entry):
    if entry.phase == "stream":
        return False
    if entry.event == "usage":
        return False
    if entry.phase == "cleanup":
        return entry.message not in {
            "output file cleanup started",
            "output file cleanup finished",
            "subcwd cleanup started",
            "subcwd cleanup finished",
        }
    if entry.phase == "main_attempt":
        return entry.message not in {
            "main attempt started",
            "repair main attempt started",
            "main attempt completed successfully",
        } and "main attempt exited with status" not in entry.message
    if entry.phase == "validator":
        return entry.message not in {"validator started", "validator finished"}
    if entry.phase == "review":
        return entry.message not in {"review started", "review finished"}
    if entry.phase == "file_collection":
        return entry.message not in {"file collection started", "file collection finished"}
    if entry.phase == "cancel":
        return entry.message != "job cancellation requested"
    if entry.phase == "timeout":
        return entry.message != "job timeout requested"
    return True


def _import_openai_codex():
    try:
        return importlib.import_module("openai_codex")
    except ImportError as exc:
        raise RuntimeError(
            "openai-codex is not installed in the current Python environment"
        ) from exc


def _codex_runtime_read_paths():
    try:
        spec = importlib.util.find_spec("openai_codex")
    except (ImportError, AttributeError, ValueError):
        return []
    if spec is None:
        return []

    candidate = None
    locations = getattr(spec, "submodule_search_locations", None)
    if locations:
        candidate = next(iter(locations), None)
    if candidate is None:
        origin = getattr(spec, "origin", None)
        if origin and origin != "built-in":
            candidate = pathlib.Path(origin).parent
    if candidate is None:
        return []

    try:
        path = pathlib.Path(candidate).expanduser().resolve()
    except OSError:
        return []
    if not path.exists():
        return []
    return [path]


def _normalize_sdk_usage(usage):
    if usage is None:
        return None
    breakdown = usage
    last_breakdown = getattr(usage, "last", None)
    if last_breakdown is not None:
        breakdown = last_breakdown
    elif getattr(usage, "total", None) is not None:
        breakdown = getattr(usage, "total")
    values = {}
    for key in (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "total_tokens",
    ):
        value = getattr(breakdown, key, None)
        if isinstance(value, int) and not isinstance(value, bool):
            values[key] = value
    return values or None


def _load_output_schema_json(output_schema_path):
    if output_schema_path is None:
        return None
    return json.loads(output_schema_path.read_text(encoding="utf-8"))


def _normalize_output_pydantic_model(output_pydantic_model):
    if output_pydantic_model is None:
        return None
    if not callable(getattr(output_pydantic_model, "model_json_schema", None)):
        raise ValueError(
            "output_pydantic_model must define model_json_schema()"
        )
    if not callable(getattr(output_pydantic_model, "model_validate_json", None)):
        raise ValueError(
            "output_pydantic_model must define model_validate_json()"
        )
    return output_pydantic_model


def _pydantic_model_label(model):
    name = getattr(model, "__name__", None)
    if isinstance(name, str) and name.strip():
        return name
    return str(model)


def _render_pydantic_output_schema_json(model):
    schema = model.model_json_schema(ref_template="#/$defs/{model}")
    schema = _strip_structured_output_schema_metadata(schema)
    return json.dumps(schema, ensure_ascii=False, indent=2)


def _strip_structured_output_schema_metadata(value):
    if isinstance(value, list):
        return [_strip_structured_output_schema_metadata(item) for item in value]
    if not isinstance(value, dict):
        return value
    if "$ref" in value:
        return {"$ref": value["$ref"]}
    stripped = {}
    for key, item in value.items():
        if key in {"description", "title", "examples", "default"}:
            continue
        if isinstance(key, str) and key.startswith("x-"):
            continue
        stripped[key] = _strip_structured_output_schema_metadata(item)
    return stripped


def _render_pydantic_model_source_context(model):
    try:
        source = inspect.getsource(model)
    except (OSError, TypeError):
        source = None
    if not source:
        try:
            module = inspect.getmodule(model)
            if module is not None:
                source = inspect.getsource(module)
        except (OSError, TypeError):
            source = None
    if not source:
        return f"<PydanticModel name=\"{_pydantic_model_label(model)}\" />"
    return (
        f"<PydanticModel name=\"{_pydantic_model_label(model)}\">\n"
        f"{source.rstrip()}\n"
        "</PydanticModel>"
    )


def _run_pydantic_validation(*, model, content):
    argv = ["pydantic", _pydantic_model_label(model)]
    try:
        model.model_validate_json(content)
    except Exception as exc:
        return _ValidationResult(
            returncode=1,
            stdout="",
            stderr=f"pydantic validation failed for {_pydantic_model_label(model)}:\n{exc}",
            argv=argv,
        )
    return _ValidationResult(
        returncode=0,
        stdout="",
        stderr=f"pydantic validation passed for {_pydantic_model_label(model)}",
        argv=argv,
    )


def _append_stream_event(stdout_lines, process, event):
    line = json.dumps(event)
    stdout_lines.append(line)
    emit = getattr(process, "emit_stdout_line", None)
    if emit is not None:
        emit(line)


def _consume_turn_via_stream(process, turn):
    stdout_lines = []
    items = []
    usage = None
    final_response_chunks = []
    completed_turn = None
    _append_stream_event(stdout_lines, process, {"type": "thread.started"})
    stream = turn.stream()
    try:
        for event in stream:
            method = getattr(event, "method", "")
            payload = getattr(event, "payload", None)
            if method in {"item/agentMessage/delta", "item.agentMessage.delta"}:
                delta = getattr(payload, "delta", None)
                if isinstance(delta, str) and delta:
                    final_response_chunks.append(delta)
                continue
            if method in {
                "item/reasoning/summaryTextDelta",
                "item/reasoning/textDelta",
                "item.reasoning.summaryTextDelta",
                "item.reasoning.textDelta",
            }:
                delta = getattr(payload, "delta", None)
                if isinstance(delta, str) and delta:
                    _append_stream_event(
                        stdout_lines,
                        process,
                        {"type": "item.delta", "item_type": "reasoning", "delta_text": delta},
                    )
                continue
            if method in {"turn/plan/updated", "turn.plan.updated"}:
                summary = _summarize_turn_plan(payload)
                event_payload = {"type": "plan.updated"}
                if summary:
                    event_payload["summary"] = summary
                _append_stream_event(stdout_lines, process, event_payload)
                continue
            if method in {"item/completed", "item.completed"}:
                item = getattr(payload, "item", None)
                if item is None:
                    continue
                items.append(item)
                item_payload = getattr(item, "root", item)
                item_type = getattr(item_payload, "type", None)
                if not isinstance(item_type, str):
                    item_type = type(item_payload).__name__
                item_text = _stream_item_text(item_payload)
                event_payload = {"type": "item.completed", "item_type": item_type}
                if item_text:
                    event_payload["item_text"] = item_text
                _append_stream_event(stdout_lines, process, event_payload)
                continue
            if method in {"thread/tokenUsage/updated", "thread.tokenUsage.updated"}:
                usage = _normalize_sdk_usage(
                    getattr(payload, "token_usage", getattr(payload, "tokenUsage", None))
                )
                continue
            if method in {"turn/completed", "turn.completed"}:
                completed_turn = getattr(payload, "turn", None)
                event_payload = {"type": "turn.completed"}
                if usage is not None:
                    event_payload["usage"] = usage
                _append_stream_event(stdout_lines, process, event_payload)
                continue
    finally:
        close = getattr(stream, "close", None)
        if close is not None:
            close()

    if completed_turn is None:
        raise RuntimeError("turn completed event not received")
    if usage is None:
        usage = _normalize_sdk_usage(getattr(completed_turn, "token_usage", None))
    if usage is None:
        usage = _normalize_sdk_usage(getattr(completed_turn, "tokenUsage", None))
    if usage is None:
        usage = _normalize_sdk_usage(getattr(completed_turn, "usage", None))
    _raise_for_failed_sdk_turn(completed_turn)
    final_response = _final_response_from_stream(items, final_response_chunks)
    return (
        "\n".join(stdout_lines) + ("\n" if stdout_lines else ""),
        final_response,
        usage,
        items,
    )


def _raise_for_failed_sdk_turn(turn):
    status = getattr(turn, "status", None)
    status_value = getattr(status, "value", status)
    if status_value != "failed":
        return
    error = getattr(turn, "error", None)
    message = getattr(error, "message", None)
    if isinstance(message, str) and message:
        raise RuntimeError(message)
    raise RuntimeError(f"turn failed with status {status_value}")


def _final_response_from_stream(items, delta_chunks):
    for item in reversed(items):
        item_payload = getattr(item, "root", item)
        if getattr(item_payload, "type", None) != "agentMessage":
            continue
        text = getattr(item_payload, "text", None)
        if isinstance(text, str):
            return text
    return "".join(delta_chunks)


def _summarize_turn_plan(payload):
    explanation = getattr(payload, "explanation", None)
    if isinstance(explanation, str) and explanation.strip():
        return _truncate_activity_text(explanation)
    plan = getattr(payload, "plan", None) or []
    steps = []
    for step in plan[:3]:
        step_text = getattr(step, "step", None)
        if isinstance(step_text, str) and step_text.strip():
            steps.append(step_text.strip())
    return _truncate_activity_text(" / ".join(steps))


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
    review_context = _normalize_review_context(review.context)
    prompt_path = _normalize_optional_path(
        review.prompt_path,
        cwd,
        must_exist=True,
    )
    prompt_template = None
    if prompt_path is not None:
        prompt_template = prompt_path.read_text(encoding="utf-8")
    normalized_read_paths = None
    if review.read_paths is not None:
        normalized_read_paths = tuple(_normalize_raw_path_list(review.read_paths, "review.read_paths"))
    timeout = review.timeout
    if timeout is not None and not isinstance(timeout, (int, float)):
        raise ValueError("review.timeout must be numeric or None")
    if not isinstance(review.reuse_session, bool):
        raise ValueError("review.reuse_session must be a bool")
    if not isinstance(review.include_main_context, bool):
        raise ValueError("review.include_main_context must be a bool")
    return _ResolvedReviewConfig(
        context=review_context,
        prompt_template=prompt_template,
        read_paths=normalized_read_paths,
        timeout=timeout,
        model=review.model,
        reasoning_effort=_normalize_optional_reasoning_effort(
            review.reasoning_effort
        )
        if review.reasoning_effort is not None
        else None,
        reuse_session=review.reuse_session,
        include_main_context=review.include_main_context,
    )


def _normalize_review_context(context):
    if isinstance(context, str):
        items = [context]
    else:
        try:
            items = list(context)
        except TypeError as exc:
            raise ValueError("review.context must be a non-empty string or sequence") from exc
    normalized = []
    for item in items:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("review.context entries must be non-empty strings")
        normalized.append(item.strip())
    if not normalized:
        raise ValueError("review.context must not be empty")
    return tuple(normalized)


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


def _normalize_raw_path_list(paths, field_name):
    if not isinstance(paths, (list, tuple)):
        raise ValueError(f"{field_name} must be a list or tuple of paths")
    normalized = []
    for item in paths:
        text = os.fspath(item)
        if not isinstance(text, str) or not text:
            raise ValueError(f"{field_name} entries must be non-empty strings")
        normalized.append(text)
    return normalized


def _looks_like_path(value):
    return (
        value.startswith(".")
        or value.startswith("~")
        or os.path.isabs(value)
        or os.sep in value
        or (os.altsep is not None and os.altsep in value)
    )


def _contains_subcwd_token(paths):
    return any(_SUBCWD_TOKEN in _path_text(path) for path in paths)


def _reject_subcwd_token(paths, field_name):
    if _contains_subcwd_token(paths):
        raise ValueError(f"{field_name} does not accept {_SUBCWD_TOKEN}")


def _normalize_subcwd_paths(paths, cwd, subcwd_path, *, must_exist, field_name):
    normalized = []
    for raw in paths:
        text = _path_text(raw)
        if _SUBCWD_TOKEN in text:
            normalized.append(
                _expand_subcwd_path(
                    raw,
                    subcwd_path,
                    field_name=field_name,
                    allow_root=False,
                )
            )
            if must_exist and not normalized[-1].exists():
                raise FileNotFoundError(normalized[-1])
            continue
        normalized.append(_normalize_path(raw, cwd, must_exist=must_exist))
    return normalized


def _expand_subcwd_path(raw, subcwd_path, *, field_name, allow_root):
    if subcwd_path is None:
        raise ValueError(f"{field_name} uses {_SUBCWD_TOKEN} but no subcwd was created")
    text = _path_text(raw)
    if text != _SUBCWD_TOKEN and not text.startswith(_SUBCWD_TOKEN + "/"):
        raise ValueError(f"{_SUBCWD_TOKEN} must be the first path segment")
    if text == _SUBCWD_TOKEN and not allow_root:
        raise ValueError(f"{field_name} does not accept bare {_SUBCWD_TOKEN}")
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


def _ensure_staged_subcwd_files_are_accessible(
    subcwd_path,
    subcwd_files,
    read_paths,
    write_paths,
):
    if subcwd_path is None or not subcwd_files:
        return
    allowed_paths = list(read_paths) + list(write_paths)
    for item in subcwd_files:
        path = (subcwd_path / item.relative_path).resolve()
        if any(path == allowed or _is_relative_to(path, allowed) for allowed in allowed_paths):
            continue
        raise ValueError(
            "subcwd_files path must be covered by read_paths or write_paths: "
            f"{_SUBCWD_TOKEN}/{item.relative_path}"
        )


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


def _review_failure_message(review):
    summary = review.summary.strip() if isinstance(review.summary, str) else ""
    if summary:
        return f"codex review reported issues: {summary}"
    return "codex review reported issues"


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
        prefix="codex_job_runner_",
        suffix=".txt",
        delete=False,
    )
    output_path = output_file.name
    output_file.close()
    return output_path


def _prepare_job_log_path(log_dir, job_id, *, log_path=None):
    if log_path is not None:
        path = pathlib.Path(log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        return str(path)
    if log_dir is None:
        return None
    directory = pathlib.Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)
    return str(directory / f"{job_id}.log")


def _format_review_response_for_log(content):
    if not isinstance(content, str):
        return ""
    return _summarize_agent_message_text(content)


def _format_prompt_summary_for_log(prompt, *, prefix):
    if not isinstance(prompt, str):
        prompt = ""
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    preview = _truncate_activity_text(prompt, limit=320)
    return f"{prefix}: bytes={len(prompt.encode('utf-8'))} sha256={digest} preview={preview}"


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


def _extract_checklist_feedback(validation):
    if validation is None:
        return ""
    stderr = validation.stderr or ""
    marker = "workflow checklist is incomplete:"
    if marker not in stderr:
        return ""
    return stderr.split(marker, 1)[1].strip()


def _default_repair_prompt(values):
    stage = values["failure_stage"]
    parts = [
        "The previous output failed validation or review.",
        "Do not broaden the task. Correct the previous output so it satisfies "
        "the reported failure feedback.",
        "",
        "Failure stage:",
        stage,
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
    if values["checklist_feedback"]:
        parts.extend(["", "Incomplete checklist items:", values["checklist_feedback"]])
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
        "Internal review mode: codex_job_runner",
        "Review the completed job output and determine whether it follows the "
        "original request and the review context.",
        "",
        "Review context:",
        values["review_context"],
        "",
        "Original request:",
        values["original_input"],
    ]
    if values["main_context"]:
        parts.extend(["", "Main job context:", values["main_context"]])
    if values["review_read_paths"]:
        parts.extend(["", values["review_read_paths"]])
    parts.extend(["", "Main job final response:", values["response_content"]])
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


def _build_review_session_developer_instructions(context):
    return (
        "You are the reviewer in a runner-reviewer conversation session. "
        "Continue the conversation as the reviewer, follow the review context, "
        "and answer only with the reviewer's next reply.\n\n"
        "Review context:\n"
        f"{context}"
    )


def _build_review_session_turn_input(*, history, message):
    if not history:
        return message
    lines = [
        "Conversation history:",
    ]
    for role, content in history:
        label = "Runner" if role == "runner" else "Reviewer"
        lines.extend([f"{label}:", content, ""])
    lines.extend(["Runner:", message])
    return "\n".join(lines)


def _build_review_session_prompt(
    *,
    context,
    history,
    message,
    ignore_global_context,
    output_schema_path,
):
    sections = []
    if ignore_global_context:
        sections.append(
            "Global AGENTS.md and global skills should not be used for this job. "
            "Use only the request below and explicitly attached paths as task context."
        )
    sections.extend(
        [
            "Internal review session mode: codex_job_runner",
            "Continue the conversation as the reviewer.",
            "",
            "Review context:",
            context,
            "",
            _build_review_session_turn_input(history=history, message=message),
        ]
    )
    if output_schema_path is not None:
        output_schema_section, _ = _resolve_inline_output_schema_section(
            output_schema_path=output_schema_path,
            max_file_bytes=_DEFAULT_MAX_INLINE_CONTEXT_BYTES,
        )
        sections.extend(["", "Output schema:", output_schema_section])
    sections.extend(["", "Return only the reviewer's next reply."])
    return "\n".join(sections).strip() + "\n"


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


def _sdk_sandbox_for_permissions(sdk, write_paths):
    # The SDK sandbox is coarse. Path-level boundaries are carried by the
    # wrapper-generated Permission Profile.
    return sdk.Sandbox.workspace_write if write_paths else sdk.Sandbox.read_only


def _workspace_relative_permission_path(path, cwd):
    if not _is_relative_to(path, cwd):
        raise ValueError(f"path must be inside cwd for permission profiles: {path}")
    relative = path.relative_to(cwd)
    if not relative.parts:
        return "."
    return relative.as_posix()


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


def _normalize_allowed_skills(value):
    if value is None:
        return None
    if not isinstance(value, (list, tuple)):
        raise ValueError("allowed_skills must be None or []")
    skills = list(value)
    if not skills:
        return []
    raise ValueError(
        "allowed_skills supports only None or [] in v1; use [] to disable "
        "automatic skill instructions"
    )


__all__ = [
    name
    for name in globals()
    if name.startswith("_") and not name.startswith("__")
]
