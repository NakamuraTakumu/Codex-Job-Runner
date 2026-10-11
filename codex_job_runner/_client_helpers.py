"""Helper functions for the Codex job runner client implementation."""

from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import os
import pathlib
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import traceback
import uuid

from ._constants import (
    _API_PRICING_SOURCE_URL,
    _DEFAULT_MAX_INLINE_CONTEXT_BYTES,
    _MODEL_PRICING_USD_PER_MILLION,
    _SUBCWD_TOKEN,
)
from ._models import (
    ReviewConfig,
    _ResolvedReviewConfig,
    _SubcwdFile,
    _ValidationResult,
)
from .errors import CodexExecutionError, CodexValidationError
from .types import CodexActivity, CodexFile


# Exact-file writes are enabled on Linux/WSL only after the selected Codex
# runtime has been verified end-to-end.  Keep this allow-list empty until such
# a runtime is recorded by a live capability test.
_EXACT_FILE_WRITE_SUPPORTED_LINUX_CODEX_VERSIONS = frozenset()
_SDK_SESSION_TMPFS_ROOT = pathlib.Path("/dev/shm")

def _build_prompt(
    *,
    context_paths,
    input_paths,
    read_paths,
    write_paths,
    subcwd_files,
    subcwd_path,
    ignore_global_context,
    context_sections=None,
    output_schema_section=None,
    output_model_source_section=None,
    review_notice=None,
    input_sections=None,
    subagent_context_policy=None,
):
    sections = [
        "Native collaboration tools are prohibited. Do not call Codex's "
        "built-in spawn_agent, send_input, wait, resume_agent, or close_agent "
        "tools. Runner-managed subagent tools served by the codex_job_runner "
        "MCP server are not native tools, even when they share a name such as "
        "spawn_agent; when they are available, use only the "
        "codex_job_runner MCP tools for subagents."
    ]
    if ignore_global_context:
        sections.append(
            "Global AGENTS.md and global skills should not be used for this job. "
            "Use only the request below and explicitly supplied context."
        )
    if subagent_context_policy is not None:
        fork_context = (
            "true" if subagent_context_policy == "all" else "false"
        )
        subagent_instruction = (
            "Native V1 subagents are ON.\n"
            "You may use the native spawn_agent tool for independent work. "
            "For every spawn_agent call, set "
            f"fork_context={fork_context}. Spawned descendants inherit this "
            "job's Permission Profile. "
        )
        if write_paths:
            subagent_instruction += (
                "Only the parent may use file-writing tools. In every subagent "
                "task, explicitly prohibit all file modifications and require "
                "the result in the subagent's final response. This restriction "
                "is instruction-level because descendants technically inherit "
                "the parent's write permissions. "
            )
        else:
            subagent_instruction += "This job's Permission Profile is read-only. "
        sections.append(
            subagent_instruction
            + "The wrapper requests this "
            "context policy through instructions; the model selects the "
            "spawn_agent arguments."
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
    if input_paths:
        sections.append(_format_path_section("Input paths", input_paths))
    if input_sections:
        sections.extend(input_sections)
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


def _build_review_delta_input(*, response):
    return (
        "Internal review continuation: codex_job_runner\n"
        "The main job revised its output after the preceding review findings. "
        "Re-review the revised result against the original request and review "
        "context already present in this reviewer thread. If the original "
        "request or review context identifies a canonical artifact, re-read that "
        "artifact through the previously granted filesystem path; do not treat "
        "the revised final response below as a substitute for the artifact.\n\n"
        "Revised main job final response:\n"
        f"{response.content}"
    )


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


def _normalize_context_items(
    context, cwd, *, field_name="context", string_paths=True
):
    if context is None:
        return []
    if isinstance(context, (str, os.PathLike)):
        raw_items = [context]
    else:
        raw_items = list(context)
    normalized_entries = []
    for index, item in enumerate(raw_items):
        item_field_name = f"{field_name}[{index}]"
        if isinstance(item, os.PathLike):
            normalized_entries.append(
                (
                    "path",
                    _normalize_path(
                        item,
                        cwd,
                        must_exist=True,
                        field_name=item_field_name,
                    ),
                )
            )
            continue
        if not isinstance(item, str):
            raise ValueError(f"{field_name} entries must be strings or paths")
        if not item.strip():
            raise ValueError(f"{field_name} entries must be non-empty")
        if not string_paths:
            normalized_entries.append(("text", item.strip()))
            continue
        candidate = pathlib.Path(item).expanduser()
        if not candidate.is_absolute():
            candidate = cwd / candidate
        if candidate.exists():
            normalized_entries.append(
                (
                    "path",
                    _normalize_path(
                        item,
                        cwd,
                        must_exist=True,
                        field_name=item_field_name,
                    ),
                )
            )
            continue
        if _looks_like_context_path(item):
            raise FileNotFoundError(str(candidate.resolve()))
        normalized_entries.append(("text", item.strip()))
    return normalized_entries


def _resolve_inline_input_sections(**kwargs):
    context_entries = kwargs["context_entries"]
    rendered = iter(_resolve_inline_context_sections(**kwargs))
    should_inline = (
        True
        if kwargs["inline_context"] is None
        else bool(kwargs["inline_context"])
    )
    sections = []
    for kind, value in context_entries:
        if kind == "text":
            section = next(rendered)
            sections.append(
                section[len("<ContextText>\n") : -len("\n</ContextText>")]
            )
            continue
        sections.append(_format_path_section("Input paths", [value]))
        if (
            should_inline
            and value.is_file()
            and not value.is_symlink()
        ):
            sections.append(
                next(rendered)
                .replace("<ContextFile ", "<InputFile ")
                .replace("</ContextFile>", "</InputFile>")
            )
    return sections


def _inline_context_file_bytes(context_entries, *, inline_context):
    if inline_context is not None and not bool(inline_context):
        return 0
    return sum(
        value.stat().st_size
        for kind, value in context_entries
        if kind == "path"
        and value.is_file()
        and not value.is_symlink()
    )


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
    non_cached_input_tokens = max(input_tokens - cached_input_tokens, 0)
    input_cost = non_cached_input_tokens * float(pricing["input_usd_per_million"]) / 1_000_000
    cached_input_rate = float(pricing.get("cached_input_usd_per_million", 0.0))
    cached_input_cost = cached_input_tokens * cached_input_rate / 1_000_000
    output_cost = output_tokens * float(pricing["output_usd_per_million"]) / 1_000_000
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
    if not _stream_event_has_display_content(event):
        return None
    message = f"stream event observed: {event_type}"
    if event_type in {"item.completed", "item.delta"}:
        item_type = event.get("item_type")
        item_text = event.get("item_text") or event.get("delta_text")
        if isinstance(item_type, str) and isinstance(item_text, str) and item_text:
            message = f"stream event observed: {event_type} ({item_type}): {item_text}"
        elif isinstance(item_type, str):
            message = f"stream event observed: {event_type} ({item_type})"
    elif event_type in {"error", "turn.failed"}:
        error = event.get("error")
        error_text = event.get("message") or (error.get("message") if isinstance(error, dict) else error)
        if error_text:
            message = f"stream event observed: {event_type}: {error_text}"
    elif event_type == "subagent.lifecycle":
        message = _describe_subagent_event(event)
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


def _stream_event_has_display_content(event):
    """Return whether an event should produce an activity display line."""
    event_type = event.get("type")
    if event_type == "item.completed":
        return _has_meaningful_activity_text(event.get("item_text"))
    if event_type == "item.delta":
        return _has_meaningful_activity_text(event.get("delta_text"))
    if event_type == "plan.updated":
        return _has_meaningful_activity_text(event.get("summary"))
    return True


def _has_meaningful_activity_text(value):
    return isinstance(value, str) and bool(value.strip())


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
    if item_type == "commandExecution":
        return _describe_command_execution(item)
    return ""


def _enum_text(value):
    enum_value = getattr(value, "value", value)
    return enum_value if isinstance(enum_value, str) else ""


def _subagent_stream_event(item, *, lifecycle):
    item_type = getattr(item, "type", None)
    if item_type == "collabAgentToolCall":
        agents_states = {}
        for thread_id, state in getattr(item, "agents_states", {}).items():
            status = _enum_text(getattr(state, "status", None))
            if status:
                agents_states[str(thread_id)] = status
        event = {
            "type": "subagent.lifecycle",
            "source": "collab_tool",
            "lifecycle": lifecycle,
            "tool": _enum_text(getattr(item, "tool", None)),
            "call_status": _enum_text(getattr(item, "status", None)),
            "sender_thread_id": getattr(item, "sender_thread_id", ""),
            "receiver_thread_ids": list(
                getattr(item, "receiver_thread_ids", ()) or ()
            ),
            "agent_states": agents_states,
        }
        model = getattr(item, "model", None)
        if isinstance(model, str) and model:
            event["model"] = model
        effort = _enum_text(getattr(item, "reasoning_effort", None))
        if effort:
            event["reasoning_effort"] = effort
        prompt = getattr(item, "prompt", None)
        if isinstance(prompt, str) and prompt:
            event["prompt"] = prompt
        return event
    if item_type == "subAgentActivity":
        return {
            "type": "subagent.lifecycle",
            "source": "activity",
            "lifecycle": lifecycle,
            "kind": _enum_text(getattr(item, "kind", None)),
            "agent_thread_id": getattr(item, "agent_thread_id", ""),
            "agent_path": getattr(item, "agent_path", ""),
        }
    return None


def _describe_subagent_event(event):
    source = event.get("source")
    lifecycle = event.get("lifecycle", "")
    if source == "activity":
        return (
            "subagent activity observed: "
            f"lifecycle={lifecycle} kind={event.get('kind', '')} "
            f"thread={event.get('agent_thread_id', '')}"
        ).strip()
    states = event.get("agent_states") or {}
    state_text = ",".join(
        f"{thread_id}:{status}"
        for thread_id, status in sorted(states.items())
    )
    return (
        "subagent collaboration observed: "
        f"lifecycle={lifecycle} tool={event.get('tool', '')} "
        f"call_status={event.get('call_status', '')} "
        f"receivers={','.join(event.get('receiver_thread_ids') or [])} "
        f"states={state_text}"
    ).strip()


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
            return "search " + ", ".join(str(part) for part in queries if part)
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
    # Never return empty: an unlogged web action would make the audit undercount.
    query = getattr(item, "query", None)
    query = query.strip() if isinstance(query, str) else ""
    return f"{action_type or 'unknown'} action {query}".strip()


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
    if server != "codex_job_runner":
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


def _describe_command_execution(item):
    # The full command is kept untruncated so the log can audit what was run.
    command = " ".join(str(getattr(item, "command", "") or "").split())
    exit_code = getattr(item, "exit_code", None)
    return f"$ {command} (exit={exit_code})" if command else ""


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
    if event_type == "subagent.lifecycle":
        source = event.get("source")
        if source == "activity":
            kind = event.get("kind") or "Activity"
            label = {
                "started": "Start",
                "interacted": "Activity",
                "interrupted": "Stop",
            }.get(kind, "Activity")
            parts = [f"[Subagent][{label}]"]
            thread_id = event.get("agent_thread_id")
            if thread_id:
                parts.append(f"thread={thread_id}")
            agent_path = event.get("agent_path")
            if agent_path:
                parts.append(f"path={agent_path}")
            parts.append(f"lifecycle={event.get('lifecycle', '')}")
            return " ".join(parts)
        tool = event.get("tool") or "collaboration"
        label = {
            "spawnAgent": "Spawn",
            "sendInput": "Input",
            "resumeAgent": "Resume",
            "wait": "Wait",
            "closeAgent": "Close",
        }.get(tool, "Lifecycle")
        parts = [f"[Subagent][{label}]"]
        parts.append(f"lifecycle={event.get('lifecycle', '')}")
        call_status = event.get("call_status")
        if call_status:
            parts.append(f"call_status={call_status}")
        receivers = event.get("receiver_thread_ids") or []
        if receivers:
            parts.append("receivers=" + ",".join(receivers))
        states = event.get("agent_states") or {}
        if states:
            parts.append(
                "states="
                + ",".join(
                    f"{thread_id}:{status}"
                    for thread_id, status in sorted(states.items())
                )
            )
        model = event.get("model")
        if model:
            parts.append(f"model={model}")
        effort = event.get("reasoning_effort")
        if effort:
            parts.append(f"effort={effort}")
        prompt = event.get("prompt")
        if isinstance(prompt, str) and prompt:
            parts.append(f"prompt={_stdout_field_text(prompt)}")
        return " ".join(parts)
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
        "finding_response": "Main Answer",
        "repair": "Repair",
        "file_collection": "File Collection",
        "cleanup": "Cleanup",
        "cancel": "Cancel",
        "timeout": "Timeout",
    }.get(phase, phase.replace("_", " ").title())


def _stream_actor(entry):
    origin = getattr(entry, "detail", None)
    if origin == "finding_response":
        return "Main Answer"
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
        "commandExecution": "Shell",
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


def _codex_runtime_read_paths(codex_bin=None):
    paths = []
    try:
        spec = importlib.util.find_spec("openai_codex")
    except (ImportError, AttributeError, ValueError):
        spec = None
    if spec is not None:
        candidate = None
        locations = getattr(spec, "submodule_search_locations", None)
        if locations:
            candidate = next(iter(locations), None)
        if candidate is None:
            origin = getattr(spec, "origin", None)
            if origin and origin != "built-in":
                candidate = pathlib.Path(origin).parent
        if candidate is not None:
            try:
                package_path = pathlib.Path(candidate).expanduser().resolve()
                if package_path.exists():
                    paths.append(package_path)
            except (OSError, RuntimeError):
                pass

    runtime_path = _selected_codex_runtime_path(codex_bin)
    if runtime_path is not None:
        host_name = "codex-code-mode-host.exe" if os.name == "nt" else "codex-code-mode-host"
        host_path = runtime_path.parent / host_name
        try:
            if host_path.is_file() and runtime_path.parent not in paths:
                paths.append(runtime_path.parent)
        except OSError:
            pass
    return paths


def _selected_codex_runtime_path(codex_bin):
    try:
        if codex_bin is None:
            runtime_package = importlib.import_module("codex_cli_bin")
            candidate = runtime_package.bundled_codex_path()
        else:
            candidate = codex_bin
        path = pathlib.Path(candidate).expanduser().resolve()
        return path if path.is_file() else None
    except (ImportError, AttributeError, OSError, RuntimeError, TypeError, ValueError):
        return None


def _codex_runtime_version(runtime_path):
    if runtime_path is None:
        return None, []
    argv = [str(runtime_path), "--version"]
    try:
        completed = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None, argv
    output = "\n".join((completed.stdout or "", completed.stderr or ""))
    match = re.search(r"\bcodex-cli\s+([^\s]+)", output)
    if completed.returncode != 0 or match is None:
        return None, argv
    return match.group(1), argv


def _enforce_write_path_runtime_capability(
    *,
    cwd,
    raw_write_paths,
    write_paths,
    subcwd_path,
    codex_bin,
):
    """Fail closed for unverified Linux exact-file write permission rules."""
    cwd = pathlib.Path(cwd).resolve()
    for path in write_paths:
        if not _is_relative_to(path, cwd):
            raise ValueError(f"path must be inside cwd for permission profiles: {path}")

    # Preserve the established Windows/macOS runtime behavior.  WSL reports a
    # Linux sys.platform and therefore follows the Linux capability gate.
    if not sys.platform.startswith("linux") or not write_paths:
        return

    offenders = []
    for raw, resolved in zip(raw_write_paths, write_paths):
        display, lexical_path = _write_path_gate_display_and_lexical_path(
            raw,
            cwd=cwd,
            subcwd_path=subcwd_path,
        )
        try:
            is_symlink = lexical_path.is_symlink()
            if is_symlink:
                if resolved.is_dir():
                    continue
                kind = "symlink-to-file" if resolved.is_file() else "unknown"
                offenders.append((display, kind))
                continue
            if resolved.is_dir():
                continue
            if resolved.exists() and resolved.is_file():
                offenders.append((display, "regular-file"))
            else:
                offenders.append((display, "unknown"))
        except OSError:
            offenders.append((display, "unknown"))

    if not offenders:
        return

    runtime_path = _selected_codex_runtime_path(codex_bin)
    if runtime_path is None:
        runtime_version, version_argv = None, []
    else:
        runtime_version, version_argv = _codex_runtime_version(runtime_path)
    only_verified_exact_files = all(kind == "regular-file" for _, kind in offenders)
    if (
        only_verified_exact_files
        and runtime_path is not None
        and runtime_version in _EXACT_FILE_WRITE_SUPPORTED_LINUX_CODEX_VERSIONS
    ):
        return

    platform_name = f"{sys.platform} ({platform.platform()})"
    runtime_display = str(runtime_path) if runtime_path is not None else "<unknown>"
    version_display = runtime_version if runtime_version is not None else "<unavailable>"
    offending_display = ", ".join(
        f"{path} [{kind}]" for path, kind in offenders
    )
    raise CodexExecutionError(
        "exact-file write capability is not verified for the selected Linux/WSL "
        "runtime; "
        f"platform={platform_name}; runtime_path={runtime_display}; "
        f"runtime_version={version_display}; "
        f"offending_write_paths={offending_display}. "
        "Use codex_bin to select a verified runtime or grant an existing directory "
        "write path.",
        argv=version_argv,
    )


def _write_path_gate_display_and_lexical_path(raw, *, cwd, subcwd_path):
    text = _path_text(raw)
    if text == _SUBCWD_TOKEN or text.startswith(_SUBCWD_TOKEN + "/"):
        suffix = text[len(_SUBCWD_TOKEN) :].lstrip("/")
        lexical = subcwd_path if not suffix else subcwd_path / suffix
        return text, lexical
    lexical = pathlib.Path(text).expanduser()
    if not lexical.is_absolute():
        lexical = cwd / lexical
    return text, lexical


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
    return _strip_structured_output_schema_metadata_in_scope(value, in_properties=False)


def _strip_structured_output_schema_metadata_in_scope(value, *, in_properties):
    if isinstance(value, list):
        return [
            _strip_structured_output_schema_metadata_in_scope(
                item,
                in_properties=False,
            )
            for item in value
        ]
    if not isinstance(value, dict):
        return value
    if "$ref" in value:
        return {"$ref": value["$ref"]}
    stripped = {}
    for key, item in value.items():
        if not in_properties and key in {"description", "title", "examples", "default"}:
            continue
        if isinstance(key, str) and key.startswith("x-"):
            continue
        stripped[key] = _strip_structured_output_schema_metadata_in_scope(
            item,
            in_properties=(key == "properties"),
        )
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
    try:
        model.model_validate_json(content)
    except Exception as exc:
        return _ValidationResult(
            success=False,
            diagnostic=(
                f"pydantic validation failed for {_pydantic_model_label(model)}:\n{exc}"
            ),
        )
    return _ValidationResult(success=True)


def _make_pydantic_output_validator(model):
    """Adapt local Pydantic validation to the standard validator contract."""

    def validate(response):
        result = _run_pydantic_validation(model=model, content=response.content)
        if not result.success:
            raise ValueError(result.diagnostic or "output validation failed")

    return validate


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
            if method in {"item/started", "item.started"}:
                item = getattr(payload, "item", None)
                if item is None:
                    continue
                item_payload = getattr(item, "root", item)
                _reject_native_collaboration(turn, item_payload)
                subagent_event = _subagent_stream_event(
                    item_payload,
                    lifecycle="started",
                )
                if subagent_event is not None:
                    _append_stream_event(
                        stdout_lines,
                        process,
                        subagent_event,
                    )
                continue
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
                _reject_native_collaboration(turn, item_payload)
                subagent_event = _subagent_stream_event(
                    item_payload,
                    lifecycle="completed",
                )
                if subagent_event is not None:
                    _append_stream_event(
                        stdout_lines,
                        process,
                        subagent_event,
                    )
                    continue
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
            if method in {"error", "turn/error", "turn.error"}:
                error = getattr(payload, "error", payload)
                message = getattr(error, "message", None) or str(error)
                _append_stream_event(stdout_lines, process,
                                     {"type": "error", "message": message})
                continue
            if method in {"turn/completed", "turn.completed"}:
                completed_turn = getattr(payload, "turn", None)
                error = getattr(completed_turn, "error", None)
                if error is not None:
                    _append_stream_event(stdout_lines, process,
                                         {"type": "error", "message": getattr(error, "message", str(error))})
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


def _reject_native_collaboration(turn, item):
    if getattr(item, "type", None) not in {
        "collabAgentToolCall",
        "subAgentActivity",
    }:
        return
    interrupt = getattr(turn, "interrupt", None)
    if interrupt is not None:
        try:
            interrupt()
        except Exception:
            pass
    raise RuntimeError(
        "native collaboration tools are prohibited by codex-job-runner; "
        "use only Runner-managed subagent tools"
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


def _normalize_paths(paths, cwd, *, must_exist, field_name="paths"):
    normalized = []
    for index, raw in enumerate(paths):
        normalized.append(
            _normalize_path(
                raw,
                cwd,
                must_exist=must_exist,
                field_name=f"{field_name}[{index}]",
            )
        )
    return normalized


def _normalize_optional_path(path, cwd, *, must_exist, field_name="path"):
    if path is None:
        return None
    return _normalize_path(path, cwd, must_exist=must_exist, field_name=field_name)


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
        field_name="review.prompt_path",
    )
    prompt_template = None
    if prompt_path is not None:
        prompt_template = prompt_path.read_text(encoding="utf-8")
    normalized_read_paths = (
        tuple(review.session.workspace.read_paths) or None
    )
    timeout = review.session.timeout
    if timeout is not None and not isinstance(timeout, (int, float)):
        raise ValueError("review.timeout must be numeric or None")
    if not isinstance(review.reuse_session, bool):
        raise ValueError("review.reuse_session must be a bool")
    if not isinstance(review.include_main_context, bool):
        raise ValueError("review.include_main_context must be a bool")
    if not isinstance(review.enable_peer_exchange, bool):
        raise ValueError("review.enable_peer_exchange must be a bool")
    return _ResolvedReviewConfig(
        session=review.session,
        context=review_context,
        prompt_template=prompt_template,
        read_paths=normalized_read_paths,
        timeout=timeout,
        model=review.session.model,
        reasoning_effort=review.session.reasoning_effort,
        reuse_session=review.reuse_session,
        include_main_context=review.include_main_context,
        enable_peer_exchange=review.enable_peer_exchange,
        subagents_enabled=False,
        subagents=review.subagents,
        artifact_paths=tuple(review.artifact_paths),
        max_repairs=review.max_repairs,
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
    for index, raw in enumerate(paths):
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
        normalized.append(
            _normalize_path(
                raw,
                cwd,
                must_exist=must_exist,
                field_name=f"{field_name}[{index}]",
            )
        )
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
    path = _normalize_path(raw, cwd, must_exist=False, field_name="subcwd_root")
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
    findings = parsed.get("findings")
    summary = parsed.get("summary")
    if not isinstance(passed, bool):
        raise CodexExecutionError(
            "codex review output requires boolean passed",
            returncode=None,
            stdout=stdout,
            stderr=stderr,
            argv=argv,
        )
    if not isinstance(findings, list) or any(
        not isinstance(finding, str) or not finding.strip()
        for finding in findings
    ):
        raise CodexExecutionError(
            "codex review output requires findings as an array of non-empty strings",
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
    if not passed and not findings:
        raise CodexExecutionError(
            "codex review output requires at least one finding when passed is false",
            returncode=None,
            stdout=stdout,
            stderr=stderr,
            argv=argv,
        )
    return {
        "passed": passed,
        "findings": tuple(findings),
        "summary": summary,
    }


def _review_failure_message(review):
    summary = review.summary.strip() if isinstance(review.summary, str) else ""
    if summary:
        return f"codex review reported issues: {summary}"
    return "codex review reported issues"


def _normalize_path(raw, cwd, *, must_exist, field_name):
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


def _archive_rollouts(home, destination):
    """Copy the SDK's complete conversation records out of an isolated home.

    The event stream omits some tool calls (e.g. Code Mode web actions), so the
    rollout files are the only full record of what a session did.
    """
    if home is None:
        return
    for path in sorted((pathlib.Path(home) / "sessions").rglob("*.jsonl")):
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination / path.name)


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
    diagnostic = validation.diagnostic or ""
    marker = "workflow checklist is incomplete:"
    if marker not in diagnostic:
        return ""
    return diagnostic.split(marker, 1)[1].strip()


def _default_repair_prompt(values):
    stage = values["failure_stage"]
    parts = [
        "The previous output failed validation or review.",
        "Treat the reported feedback as the minimum required correction, not "
        "necessarily an exhaustive list.",
        "Identify the underlying failure pattern, correct every explicitly "
        "reported instance, then inspect the current output and permitted work "
        "artifacts for other instances of the same pattern within the original "
        "task scope. Correct each analogous instance only after confirming that "
        "the same cause applies to its own local evidence.",
        "Do not expand into unrelated criteria, optional improvements, or new "
        "task goals. Do not bulk-replace items merely because they share text, "
        "a key, a type, or a path shape.",
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
        "Validation diagnostic:",
        values["validation_diagnostic"],
        "",
        "Previous final response:",
        values["previous_content"],
    ]
    if values["checklist_feedback"]:
        parts.extend(["", "Incomplete checklist items:", values["checklist_feedback"]])
    if values["review_summary"]:
        parts.extend(["", "Review summary:", values["review_summary"]])
    if values["review_findings"]:
        parts.extend(
            ["", "Review findings:", values["review_findings"]]
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
            "Return JSON with fields passed (bool), findings (array of strings), and summary (string).",
            "When passed is true, findings may be empty.",
            "When passed is false, findings must contain the changes the main job should make.",
            "Describe the underlying failure pattern for each finding clearly enough that the "
            "main job can check for confirmed analogous instances within the original task scope.",
        ]
    )
    return "\n".join(parts)


def _build_review_session_developer_instructions(
    context,
    *,
    subagent_context_policy=None,
):
    instructions = (
        "You are the reviewer in a runner-reviewer conversation session. "
        "Continue the conversation as the reviewer, follow the review context, "
        "and answer only with the reviewer's next reply.\n\n"
        "Native collaboration tools are prohibited. Do not call Codex's "
        "built-in spawn_agent, send_input, wait, resume_agent, or close_agent "
        "tools. Runner-managed subagent tools served by the codex_job_runner "
        "MCP server are not native tools, even when they share a name such as "
        "spawn_agent; when they are available, use only the "
        "codex_job_runner MCP tools for subagents."
    )
    if subagent_context_policy is not None:
        fork_context = (
            "true" if subagent_context_policy == "all" else "false"
        )
        instructions += (
            "\n\nNative V1 subagents are ON.\n"
            "You may use the native spawn_agent tool for independent read-only "
            "review work. For every spawn_agent call, set "
            f"fork_context={fork_context}. Spawned descendants inherit the "
            "reviewer's read-only Permission Profile and must return findings "
            "in their final response."
        )
    return (
        instructions
        + "\n\n"
        "Review context:\n"
        f"{context}"
    )


def _path_text(path):
    return os.fspath(path)


def _is_relative_to(path, parent):
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _filesystem_permission_map(cwd, read_paths, write_paths, extra_read_paths=None):
    cwd = pathlib.Path(cwd).resolve()
    cwd_modes = {}
    outside_modes = {}
    write_paths = list(write_paths)
    for path in read_paths:
        if not _is_relative_to(path, cwd):
            raise ValueError(f"path must be inside cwd for permission profiles: {path}")
        if any(
            path == write_path or _is_relative_to(path, write_path)
            for write_path in write_paths
        ):
            continue
        relative = path.relative_to(cwd).as_posix()
        cwd_modes["." if relative == "." else relative] = "read"
    for path in write_paths:
        if not _is_relative_to(path, cwd):
            raise ValueError(f"path must be inside cwd for permission profiles: {path}")
        relative = path.relative_to(cwd).as_posix()
        cwd_modes["." if relative == "." else relative] = "write"
    for path in extra_read_paths or []:
        resolved = pathlib.Path(path).expanduser().resolve()
        if _is_relative_to(resolved, cwd):
            relative = resolved.relative_to(cwd).as_posix()
            if relative == ".":
                continue
            if any(
                mode == "write"
                and (
                    relative == permitted
                    or pathlib.PurePosixPath(relative).is_relative_to(permitted)
                )
                for permitted, mode in cwd_modes.items()
            ):
                continue
            cwd_modes.setdefault(relative, "read")
            continue
        outside_modes.setdefault(str(resolved), "read")

    modes = {":minimal": "read"}
    if cwd_modes:
        modes[str(cwd)] = dict(sorted(cwd_modes.items()))
    modes.update(dict(sorted(outside_modes.items())))
    return modes


def _filesystem_inline_table(cwd, read_paths, write_paths, extra_read_paths=None):
    return _toml_inline_table(
        _filesystem_permission_map(
            cwd,
            read_paths,
            write_paths,
            extra_read_paths=extra_read_paths,
        )
    )


def _toml_inline_table(mapping):
    parts = []
    ordered = sorted(
        mapping.items(),
        key=lambda item: (item[0] != ":minimal", str(item[0])),
    )
    for key, value in ordered:
        rendered = (
            _toml_inline_table(value)
            if isinstance(value, dict)
            else _toml_string(value)
        )
        parts.append(f"{_toml_string(key)}={rendered}")
    return "{" + ",".join(parts) + "}"


def _permission_profile_sdk_env(env, *, existing_home=None):
    base_env = dict(env or {})
    if existing_home is not None:
        isolated_home = pathlib.Path(existing_home).expanduser().resolve()
        base_env["CODEX_HOME"] = str(isolated_home)
        base_env["CODEX_SQLITE_HOME"] = str(isolated_home / "sqlite")
        return base_env, isolated_home
    source_home = pathlib.Path(
        base_env.get("CODEX_HOME")
        or os.environ.get("CODEX_HOME")
        or pathlib.Path.home() / ".codex"
    ).expanduser().resolve()
    tmpfs_root = _SDK_SESSION_TMPFS_ROOT
    if not tmpfs_root.is_dir() or not os.access(
        tmpfs_root,
        os.W_OK | os.X_OK,
    ):
        raise RuntimeError(
            "SDK session isolation requires writable tmpfs at /dev/shm"
        )
    try:
        isolated_home = pathlib.Path(
            tempfile.mkdtemp(
                prefix="codex_job_runner_home_",
                dir=tmpfs_root,
            )
        ).resolve()
    except OSError as exc:
        raise RuntimeError(
            f"failed to create SDK session home in /dev/shm: {exc}"
        ) from exc
    isolated_home.chmod(0o700)
    sqlite_home = isolated_home / "sqlite"
    sqlite_home.mkdir(mode=0o700)
    source_auth = source_home / "auth.json"
    if source_auth.is_file():
        shutil.copy2(source_auth, isolated_home / "auth.json")
    base_env["CODEX_HOME"] = str(isolated_home)
    base_env["CODEX_SQLITE_HOME"] = str(sqlite_home)
    return base_env, isolated_home


def _without_legacy_sandbox_config(text):
    lines = text.splitlines(keepends=True)
    output = []
    table_segments = ()
    skip_table = False
    skipped_value = None
    for line in lines:
        if skipped_value is not None:
            skipped_value += line
            if _toml_value_is_complete(skipped_value):
                skipped_value = None
            continue

        table_key = _toml_table_key(line)
        if table_key is not None:
            table_segments = _toml_key_segments(table_key)
            skip_table = _is_legacy_sandbox_path(
                table_segments,
                key_name="sandbox_workspace_write",
            )
            if skip_table:
                continue
        if skip_table:
            continue

        assignment = _toml_assignment(line)
        if assignment is not None:
            key, value = assignment
            key_segments = table_segments + _toml_key_segments(key)
            if not (
                _is_legacy_sandbox_path(key_segments, key_name="sandbox_mode")
                or _is_legacy_sandbox_path(
                    key_segments,
                    key_name="sandbox_workspace_write",
                )
            ):
                output.append(line)
                continue
            if not _toml_value_is_complete(value):
                skipped_value = value
            continue
        output.append(line)
    return "".join(output)


def _toml_table_key(line):
    match = re.match(r"^\s*(\[\[?)(.*?)(\]\]?)\s*(?:#.*)?$", line.rstrip("\r\n"))
    if match is None or len(match.group(1)) != len(match.group(3)):
        return None
    return match.group(2).strip()


def _toml_assignment(line):
    quote = None
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
            continue
        if quote == '"' and char == "\\":
            escaped = True
            continue
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
            continue
        if char == "=":
            return line[:index].strip(), line[index + 1 :]
        if char == "#":
            return None
    return None


def _toml_key_segments(key):
    segments = []
    current = []
    quote = None
    escaped = False
    for char in key:
        if escaped:
            current.append(char)
            escaped = False
            continue
        if quote == '"' and char == "\\":
            escaped = True
            continue
        if quote is not None:
            if char == quote:
                quote = None
            else:
                current.append(char)
            continue
        if char in {'"', "'"}:
            quote = char
        elif char == ".":
            segments.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    segments.append("".join(current).strip())
    return tuple(segment for segment in segments if segment)


def _is_legacy_sandbox_path(segments, *, key_name):
    return bool(
        segments
        and (
            segments[0] == key_name
            or (
                len(segments) >= 3
                and segments[0] == "profiles"
                and segments[2] == key_name
            )
        )
    )


def _toml_value_is_complete(value):
    try:
        tomllib.loads("value = " + value)
    except tomllib.TOMLDecodeError:
        return False
    return True


def _toml_string(value):
    text = str(value)
    escaped = text.replace("\\", "\\\\").replace("\"", "\\\"")
    return f"\"{escaped}\""


def _project_untrusted_config_override(cwd):
    return f"projects.{_toml_string(str(cwd))}.trust_level=\"untrusted\""


def _normalize_optional_reasoning_effort(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        raise ValueError("reasoning_effort must be a non-empty string")
    return text


def _normalize_web_search(value):
    if value is None:
        return None
    if not isinstance(value, str) or value not in ("live", "cached", "disabled"):
        raise ValueError("web_search must be None, 'live', 'cached', or 'disabled'")
    return value


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
