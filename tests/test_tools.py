import concurrent.futures
import http.client
import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from types import SimpleNamespace
from typing import Literal, TypedDict

from codex_job_runner import Tool
from codex_job_runner.client import create_runtime as CodexClient
from codex_job_runner._client_helpers import _describe_mcp_tool_call
from codex_job_runner._tools import _ToolBridge


class CompletionResult(TypedDict):
    status: Literal["completed", "rejected"]
    unmet_conditions: list[str]


def complete_task(task_id: str, evidence_ids: list[str]) -> CompletionResult:
    return {"status": "completed", "unmet_conditions": []}


def make_tool(handler=complete_task, *, name=None):
    return Tool(
        handler,
        "Complete a task only after checking its evidence.",
        args={
            "task_id": "The task identifier.",
            "evidence_ids": "Evidence identifiers used for validation.",
        },
        returns="The completion status and any unmet conditions.",
        name=name,
    )


class ToolTests(unittest.TestCase):
    def test_builds_input_and_output_schema(self):
        tool = make_tool()

        self.assertEqual(tool.name, "complete_task")
        self.assertEqual(
            tool.input_schema["properties"]["task_id"]["description"],
            "The task identifier.",
        )
        self.assertFalse(tool.input_schema["additionalProperties"])
        self.assertEqual(tool.output_schema["type"], "object")
        self.assertEqual(
            tool.invoke({"task_id": "TASK-1", "evidence_ids": ["E-1"]}),
            {"status": "completed", "unmet_conditions": []},
        )

    def test_rejects_missing_and_unknown_argument_descriptions(self):
        with self.assertRaisesRegex(ValueError, "missing descriptions"):
            Tool(
                complete_task,
                "Complete a task.",
                args={"task_id": "Task."},
                returns="Result.",
            )
        with self.assertRaisesRegex(ValueError, "unknown arguments"):
            Tool(
                complete_task,
                "Complete a task.",
                args={
                    "task_id": "Task.",
                    "evidence_ids": "Evidence.",
                    "other": "Unknown.",
                },
                returns="Result.",
            )

    def test_rejects_unannotated_return(self):
        def unannotated(value: str):
            return value

        with self.assertRaisesRegex(TypeError, "return type annotation"):
            Tool(
                unannotated,
                "Return a value.",
                args={"value": "Value."},
                returns="The value.",
            )

    def test_rejects_duplicate_names(self):
        with self.assertRaisesRegex(ValueError, "duplicate tool names"):
            CodexClient(
                tools=[
                    make_tool(name="same"),
                    make_tool(name="same"),
                ]
            )

    def test_handler_error_does_not_expose_message(self):
        def failing(
            task_id: str,
            evidence_ids: list[str],
        ) -> CompletionResult:
            raise RuntimeError("secret diagnostic")

        bridge = _ToolBridge(
            (make_tool(failing),),
            max_concurrency=1,
            tool_timeout=1,
        )
        result = bridge._call_tool(
            {
                "name": "failing",
                "arguments": {"task_id": "TASK-1", "evidence_ids": []},
            }
        )
        bridge.close()

        self.assertTrue(result["isError"])
        self.assertIn("RuntimeError", result["content"][0]["text"])
        self.assertNotIn("secret diagnostic", result["content"][0]["text"])

    def test_internal_tool_activity_does_not_include_result(self):
        item = SimpleNamespace(
            server="codex_job_runner",
            tool="complete_task",
            status="completed",
            result=SimpleNamespace(
                content=[{"type": "text", "text": "secret result"}],
            ),
        )

        description = _describe_mcp_tool_call(item)

        self.assertEqual(
            description,
            "codex_job_runner complete_task completed",
        )


class ToolBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tool = make_tool()
        self.bridge = _ToolBridge(
            (self.tool,),
            max_concurrency=2,
            tool_timeout=2,
        )
        self.bridge.start()

    def tearDown(self):
        self.bridge.close()

    def post(self, payload, *, token=None):
        request = urllib.request.Request(
            self.bridge.url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token or self.bridge.token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.status, json.loads(response.read())

    def test_initialize_list_and_call(self):
        status, initialized = self.post(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            initialized["result"]["protocolVersion"],
            "2025-06-18",
        )

        _, listed = self.post(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        )
        self.assertEqual(listed["result"]["tools"][0]["name"], "complete_task")

        _, called = self.post(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "complete_task",
                    "arguments": {
                        "task_id": "TASK-1",
                        "evidence_ids": ["E-1"],
                    },
                },
            }
        )
        self.assertFalse(called["result"]["isError"])
        self.assertEqual(
            called["result"]["structuredContent"]["status"],
            "completed",
        )

    def test_rejects_wrong_bearer_token(self):
        with self.assertRaises(urllib.error.HTTPError) as captured:
            self.post(
                {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                token="wrong",
            )
        self.assertEqual(captured.exception.code, 401)

    def test_rejects_oversized_request_before_reading_body(self):
        endpoint = urlsplit(self.bridge.url)
        connection = http.client.HTTPConnection(
            endpoint.hostname,
            endpoint.port,
            timeout=2,
        )
        connection.request(
            "POST",
            endpoint.path,
            body=b"",
            headers={
                "Authorization": f"Bearer {self.bridge.token}",
                "Content-Length": str(1048577),
                "Content-Type": "application/json",
            },
        )
        response = connection.getresponse()
        response.read()
        connection.close()

        self.assertEqual(response.status, 413)

    def test_rejects_empty_json_rpc_batch(self):
        with self.assertRaises(urllib.error.HTTPError) as captured:
            self.post([])
        self.assertEqual(captured.exception.code, 400)

    def test_bounds_concurrent_handler_execution(self):
        active = 0
        maximum = 0
        lock = threading.Lock()

        def slow(task_id: str, evidence_ids: list[str]) -> CompletionResult:
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return {"status": "completed", "unmet_conditions": []}

        bridge = _ToolBridge(
            (make_tool(slow),),
            max_concurrency=2,
            tool_timeout=1,
        )
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            futures = [
                executor.submit(
                    bridge._call_tool,
                    {
                        "name": "slow",
                        "arguments": {
                            "task_id": str(index),
                            "evidence_ids": [],
                        },
                    },
                )
                for index in range(6)
            ]
            for future in futures:
                self.assertFalse(future.result()["isError"])
        bridge.close()
        self.assertEqual(maximum, 2)

    def test_close_does_not_wait_for_running_handler(self):
        started = threading.Event()
        release = threading.Event()

        def blocking(
            task_id: str,
            evidence_ids: list[str],
        ) -> CompletionResult:
            started.set()
            release.wait()
            return {"status": "completed", "unmet_conditions": []}

        bridge = _ToolBridge(
            (make_tool(blocking),),
            max_concurrency=1,
            tool_timeout=2,
        )
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                bridge._call_tool,
                {
                    "name": "blocking",
                    "arguments": {
                        "task_id": "TASK-1",
                        "evidence_ids": [],
                    },
                },
            )
            self.assertTrue(started.wait(1))
            before = time.monotonic()
            bridge.close()
            elapsed = time.monotonic() - before
            release.set()
            self.assertFalse(future.result(timeout=1)["isError"])

        self.assertLess(elapsed, 0.5)


class ClientToolConfigTests(unittest.TestCase):
    def setUp(self):
        self.options = SimpleNamespace(
            subagents_enabled=False,
            max_concurrent_subagents=2,
        )

    def test_main_config_uses_one_shared_bridge_without_exposing_token(self):
        client = CodexClient(tools=[make_tool()])
        first = client._runtime_config_overrides(
            self.options,
            include_tools=True,
        )
        second = client._runtime_config_overrides(
            self.options,
            include_tools=True,
        )
        try:
            first_mcp = next(
                value
                for value in first
                if value.startswith("mcp_servers.codex_job_runner=")
            )
            second_mcp = next(
                value
                for value in second
                if value.startswith("mcp_servers.codex_job_runner=")
            )
            self.assertEqual(first_mcp, second_mcp)
            self.assertNotIn(client._tool_bridge.token, first_mcp)
            self.assertEqual(
                client._main_sdk_env()[client._tool_env_name],
                client._tool_bridge.token,
            )
            self.assertNotIn("mcp_servers={}", first)
        finally:
            client.close()

    def test_review_config_keeps_mcp_disabled(self):
        client = CodexClient(tools=[make_tool()])
        try:
            overrides = client._runtime_config_overrides(self.options)
            self.assertIn("mcp_servers={}", overrides)
            self.assertIsNone(client._tool_bridge)
        finally:
            client.close()

    def test_close_is_idempotent_and_rejects_new_jobs(self):
        client = CodexClient(tools=[make_tool()])
        client._ensure_tool_bridge()
        client.close()
        client.close()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            client.start(input="should not start")

    def test_close_cancels_active_jobs(self):
        class FakeJob:
            def __init__(self):
                self.cancelled = 0

            def cancel(self):
                self.cancelled += 1

        client = CodexClient(tools=[make_tool()])
        job = FakeJob()
        client._register_job(job)
        client.close()

        self.assertEqual(job.cancelled, 1)


if __name__ == "__main__":
    unittest.main()
