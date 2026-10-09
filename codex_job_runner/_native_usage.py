"""Best-effort observer for app-server raw completion usage notifications."""

from __future__ import annotations

import threading

from ._client_helpers import _normalize_sdk_usage
from .types import CodexNativeUsage


class _NativeUsageCollector:
    """Observe, but never consume or reroute, SDK MessageRouter notifications."""

    def __init__(self, *, model, default_subagent_model, role):
        self._lock = threading.RLock()
        self._records = []
        self._response_ids = set()
        self._requested_model = model if isinstance(model, str) and model.strip() else None
        self._default_subagent_model = (
            default_subagent_model
            if isinstance(default_subagent_model, str) and default_subagent_model.strip()
            else None
        )
        self._role = role
        self._parent_thread_ids = set()
        self._thread_models = {}
        self._turn_models = {}

    def attach(self, codex, *, thread_id=None):
        """Install a per-router observer; SDK routing remains the owner of events."""
        if isinstance(thread_id, str) and thread_id:
            with self._lock:
                self._parent_thread_ids.add(thread_id)
                if self._requested_model is not None:
                    self._thread_models.setdefault(thread_id, self._requested_model)
        raw_client = getattr(codex, "_client", None)
        router = getattr(raw_client, "_router", None)
        route = getattr(router, "route_notification", None)
        if not callable(route):
            return False
        # Registration above is intentionally independent of installation: the
        # SDK thread id is only available after the first attach call.
        if getattr(router, "_codex_job_runner_usage_observer", False):
            return True

        def observed(notification):
            self.observe(notification)
            return route(notification)

        try:
            router.route_notification = observed
            router._codex_job_runner_usage_observer = True
            return True
        except Exception:
            return False

    def observe(self, notification):
        method = getattr(notification, "method", None)
        if method not in {
            "rawResponse/completed",
            "model/rerouted",
            "thread/settings/updated",
            "item/started",
            "item/complete",
            "item/completed",
        }:
            return
        payload = getattr(notification, "payload", None)
        params = getattr(payload, "params", None)
        if method.startswith("item/"):
            self._register_subagent_item(_as_mapping(getattr(payload, "item", None)))
            return
        if not isinstance(params, dict):
            return
        if method != "rawResponse/completed":
            self._record_effective_model(params)
            return
        response_id = params.get("responseId")
        if not isinstance(response_id, str) or not response_id:
            return
        with self._lock:
            if response_id in self._response_ids:
                return
            self._response_ids.add(response_id)
        raw_usage = params.get("usage")
        usage = _normalize_raw_usage(raw_usage)
        record_thread_id = params.get("threadId")
        turn_id = params.get("turnId")
        with self._lock:
            model = _model_from_params(params) or self._turn_models.get(turn_id) or self._thread_models.get(record_thread_id)
            role = self._role if record_thread_id in self._parent_thread_ids else "native_subagent"
        record = CodexNativeUsage(
            response_id=response_id,
            thread_id=record_thread_id if isinstance(record_thread_id, str) else None,
            turn_id=turn_id if isinstance(turn_id, str) else None,
            role=role,
            model=model,
            usage=usage,
            exact=usage is not None and model is not None,
        )
        with self._lock:
            self._records.append(record)

    def snapshot(self):
        with self._lock:
            return tuple(self._records)

    def _record_effective_model(self, params):
        model = _model_from_params(params)
        if model is None:
            return
        thread_id = params.get("threadId")
        turn_id = params.get("turnId")
        with self._lock:
            if isinstance(thread_id, str):
                self._thread_models[thread_id] = model
            if isinstance(turn_id, str):
                self._turn_models[turn_id] = model

    def _register_subagent_item(self, item):
        if not item:
            return
        item_type = item.get("type")
        if item_type == "subAgentActivity":
            thread_ids = (item.get("agentThreadId"),)
            model = _model_from_params(item) or self._default_subagent_model
        elif item_type == "collabAgentToolCall":
            receiver_ids = item.get("receiverThreadIds")
            thread_ids = receiver_ids if isinstance(receiver_ids, list) else ()
            model = _model_from_params(item) or self._default_subagent_model
        else:
            return
        if model is None:
            return
        with self._lock:
            for thread_id in thread_ids:
                if isinstance(thread_id, str) and thread_id:
                    # A later reroute/settings event overwrites this request
                    # attribution with the actual runtime model.
                    self._thread_models.setdefault(thread_id, model)


def _model_from_params(params):
    response = params.get("response") if isinstance(params.get("response"), dict) else {}
    settings = params.get("settings") if isinstance(params.get("settings"), dict) else {}
    thread_settings = (
        params.get("threadSettings")
        if isinstance(params.get("threadSettings"), dict)
        else {}
    )
    for value in (
        params.get("model"),
        params.get("toModel"),
        response.get("model"),
        settings.get("model"),
        thread_settings.get("model"),
    ):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _as_mapping(value):
    value = getattr(value, "root", value)
    if isinstance(value, dict):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            result = dump(by_alias=True, exclude_none=True)
        except TypeError:
            result = dump()
        if isinstance(result, dict):
            return result
    return {}


def _normalize_raw_usage(usage):
    normalized = _normalize_sdk_usage(usage)
    if normalized is not None or not isinstance(usage, dict):
        return normalized
    values = {}
    for key in (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "total_tokens",
    ):
        camel_key = "".join(
            word.title() if index else word
            for index, word in enumerate(key.split("_"))
        )
        value = usage.get(key, usage.get(camel_key))
        if isinstance(value, int) and not isinstance(value, bool):
            values[key] = value
    return values or None
