import unittest

from codex_job_runner._native_usage import _NativeUsageCollector
from codex_job_runner._job import _exact_native_usage_cost
from codex_job_runner.types import CodexNativeUsage


class _Payload:
    def __init__(self, params, item=None):
        self.params = params
        self.item = item


class _Notification:

    def __init__(self, params, method="rawResponse/completed", item=None):
        self.method = method
        self.payload = _Payload(params, item=item)


class _Router:
    def __init__(self):
        self.seen = []

    def route_notification(self, notification):
        self.seen.append(notification)


class _RawClient:
    def __init__(self):
        self._router = _Router()


class _Codex:
    def __init__(self):
        self._client = _RawClient()


class NativeUsageTests(unittest.TestCase):
    def test_observer_preserves_router_and_deduplicates_response_id(self):
        codex = _Codex()
        collector = _NativeUsageCollector(
            model="main-model", default_subagent_model="child-model", role="main"
        )
        self.assertTrue(collector.attach(codex))
        self.assertTrue(collector.attach(codex, thread_id="parent"))
        codex._client._router.route_notification(
            _Notification({"threadId": "child", "turnId": "turn-1", "model": "luna"}, "model/rerouted")
        )
        notification = _Notification(
            {"responseId": "response-1", "threadId": "child", "turnId": "turn-1", "usage": {"input_tokens": 3}}
        )
        codex._client._router.route_notification(notification)
        codex._client._router.route_notification(notification)
        self.assertEqual(codex._client._router.seen[-2:], [notification, notification])
        record, = collector.snapshot()
        self.assertEqual(record.role, "native_subagent")
        self.assertEqual(record.model, "luna")
        self.assertTrue(record.exact)

    def test_thread_settings_model_attributes_parent_after_late_registration(self):
        codex = _Codex()
        collector = _NativeUsageCollector(model="requested", default_subagent_model=None, role="main")
        collector.attach(codex)
        collector.attach(codex, thread_id="parent")
        codex._client._router.route_notification(
            _Notification(
                {"threadId": "parent", "threadSettings": {"model": "terra"}},
                "thread/settings/updated",
            )
        )
        codex._client._router.route_notification(
            _Notification({"responseId": "response-parent", "threadId": "parent", "turnId": "turn-parent", "usage": {"inputTokens": 4}})
        )
        record, = collector.snapshot()
        self.assertEqual(record.role, "main")
        self.assertEqual(record.model, "terra")
        self.assertTrue(record.exact)

    def test_missing_model_or_usage_is_not_exact(self):
        collector = _NativeUsageCollector(model=None, default_subagent_model=None, role="reviewer")
        collector.observe(_Notification({"responseId": "response-2", "threadId": "review", "turnId": "turn-2", "usage": None}))
        record, = collector.snapshot()
        self.assertFalse(record.exact)
        self.assertIsNone(record.model)
        self.assertIsNone(record.usage)

    def test_subagent_activity_uses_configured_default_model(self):
        collector = _NativeUsageCollector(model="terra", default_subagent_model="luna", role="main")
        collector.observe(_Notification({}, "item/started", item={"type": "subAgentActivity", "agentThreadId": "child"}))
        collector.observe(_Notification({"responseId": "child-response", "threadId": "child", "turnId": "child-turn", "usage": {"outputTokens": 2}}))
        record, = collector.snapshot()
        self.assertEqual(record.model, "luna")
        self.assertTrue(record.exact)

    def test_collab_receiver_uses_explicit_requested_model(self):
        collector = _NativeUsageCollector(model="terra", default_subagent_model="luna", role="main")
        collector.observe(_Notification({}, "item/completed", item={"type": "collabAgentToolCall", "receiverThreadIds": ["child"], "model": "sol", "reasoningEffort": "medium"}))
        collector.observe(_Notification({"responseId": "child-response", "threadId": "child", "turnId": "child-turn", "usage": {"inputTokens": 2}}))
        record, = collector.snapshot()
        self.assertEqual(record.model, "sol")
        self.assertTrue(record.exact)

    def test_exact_cost_uses_each_attributed_model_without_parent_double_count(self):
        records = (
            CodexNativeUsage("parent", "parent", "p", "main", "gpt-5.6-terra", {"input_tokens": 1_000_000}, True),
            CodexNativeUsage("child", "child", "c", "native_subagent", "gpt-5.6-luna", {"output_tokens": 1_000_000}, True),
        )
        self.assertEqual(_exact_native_usage_cost(records), 3.2)

    def test_exact_cost_falls_back_when_a_raw_record_is_incomplete(self):
        records = (
            CodexNativeUsage("parent", "parent", "p", "main", "gpt-5.6-terra", {"input_tokens": 1}, True),
            CodexNativeUsage("child", "child", "c", "native_subagent", None, None, False),
        )
        self.assertIsNone(_exact_native_usage_cost(records))


if __name__ == "__main__":
    unittest.main()
