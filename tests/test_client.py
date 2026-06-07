import contextlib
import io
import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import codex_api_wrapper.client as client_module  # noqa: E402
from codex_api_wrapper import (  # noqa: E402
    CodexCancelledError,
    CodexResponse,
    CodexClient,
    CodexExecutionError,
    CodexTimeoutError,
    CodexValidationError,
    ReviewConfig,
)
from jobs.demo_company_analysis import DemoCompanyAnalysisClient  # noqa: E402


class CodexClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = pathlib.Path(self.tmp.name)
        self.codex_bin = "openai-codex"
        self.project = self.tmp_path / "project"
        self.project.mkdir()
        (self.project / "README.md").write_text("hello\n", encoding="utf-8")
        (self.project / "out").mkdir()
        (self.project / "result.schema.json").write_text(
            '{"type":"object"}\n',
            encoding="utf-8",
        )
        self._previous_import = client_module._import_openai_codex
        self._install_fake_sdk()

    def tearDown(self):
        client_module._import_openai_codex = self._previous_import
        self.tmp.cleanup()

    def test_run_returns_response_content(self):
        client = CodexClient(cwd=str(self.project))

        response = client.run(
            input="README を要約して",
            attachments=["README.md"],
            read_paths=["README.md"],
            write_paths=["out"],
            timeout=3,
        )

        self.assertEqual(response.content, "fake final answer")
        self.assertEqual(response.returncode, 0)
        self.assertIn('"type": "thread.started"', response.stdout)
        self.assertIsNotNone(response.log_path)
        self.assertTrue(pathlib.Path(response.log_path).is_file())
        self.assertEqual(
            response.usage,
            {
                "input_tokens": 100,
                "cached_input_tokens": 25,
                "output_tokens": 10,
                "reasoning_output_tokens": 3,
            },
        )

    def test_activity_log_includes_stream_events_and_is_printed_to_stdout(self):
        client = CodexClient(cwd=str(self.project))

        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            response = client.run(input="README を要約して", timeout=3)

        self.assertIn("stream", [entry.phase for entry in response.activity_log])
        stream_events = [entry.event for entry in response.activity_log if entry.phase == "stream"]
        self.assertIn("thread.started", stream_events)
        self.assertIn("turn.completed", stream_events)
        output = captured.getvalue()
        self.assertIn("[Main Attempt][Start]", output)
        self.assertIn("[Main Attempt][Result]", output)

    def test_review_config_default_timeout_is_none(self):
        review = ReviewConfig(instructions="x")
        self.assertIsNone(review.timeout)

    def test_review_session_preserves_history_in_prompt(self):
        client = CodexClient(cwd=str(self.project))

        session = client.start_review_session(
            review=ReviewConfig(instructions="Be precise."),
            read_paths=["README.md"],
        )
        try:
            first = session.send("ECHO_PROMPT first reviewer turn")
            second = session.send("ECHO_PROMPT second reviewer turn")
        finally:
            session.close()

        self.assertEqual(first.content, "ECHO_PROMPT first reviewer turn")
        self.assertIn("Runner:\nECHO_PROMPT second reviewer turn", second.content)
        self.assertIn("Runner:\nECHO_PROMPT first reviewer turn", second.content)
        self.assertIn("Reviewer:\nECHO_PROMPT first reviewer turn", second.content)

    def test_review_session_sdk_backend_reuses_same_thread(self):
        captured = {
            "thread_start_calls": 0,
            "turn_prompts": [],
        }

        class FakeTurnResult:
            usage = None
            items = []

            def __init__(self, prompt):
                self.final_response = f"reply:{prompt}"

        class FakeTurn:
            def __init__(self, prompt):
                self.prompt = prompt

            def run(self):
                return FakeTurnResult(self.prompt)

        class FakeThread:
            def turn(self, prompt, **kwargs):
                captured["turn_prompts"].append(prompt)
                return FakeTurn(prompt)

        class FakeCodex:
            def __init__(self, config=None):
                self.config = config

            def thread_start(self, **kwargs):
                captured["thread_start_calls"] += 1
                return FakeThread()

            def close(self):
                captured["closed"] = True

        fake_sdk = SimpleNamespace(
            Codex=FakeCodex,
            CodexConfig=lambda **kwargs: SimpleNamespace(**kwargs),
            Sandbox=SimpleNamespace(read_only="read_only", workspace_write="workspace_write"),
        )

        previous_import = client_module._import_openai_codex
        client_module._import_openai_codex = lambda: fake_sdk
        try:
            client = CodexClient(cwd=str(self.project))
            session = client.start_review_session(
                review=ReviewConfig(instructions="Use conversation history."),
            )
            try:
                first = session.send("first question")
                second = session.send("second question")
            finally:
                session.close()
        finally:
            client_module._import_openai_codex = previous_import

        self.assertEqual(first.content, "reply:first question")
        self.assertIn("Conversation history:", second.content)
        self.assertEqual(captured["thread_start_calls"], 1)
        self.assertEqual(captured["turn_prompts"][0], "first question")
        self.assertIn("Runner:\nfirst question", captured["turn_prompts"][1])
        self.assertTrue(captured.get("closed"))

    def test_default_backend_uses_openai_codex_public_surface(self):
        captured = {}

        class FakeUsage:
            input_tokens = 11
            cached_input_tokens = 2
            output_tokens = 3
            reasoning_output_tokens = 1

        class FakeTurnResult:
            final_response = "sdk final answer"
            usage = FakeUsage()
            items = [
                SimpleNamespace(
                    root=SimpleNamespace(
                        type="reasoning",
                        summary=["reasoning summary text"],
                        content=[],
                    )
                ),
                SimpleNamespace(
                    root=SimpleNamespace(
                        type="agentMessage",
                        text="sdk final answer",
                    )
                ),
                SimpleNamespace(
                    root=SimpleNamespace(
                        type="webSearch",
                        query="Toyota employee count",
                        action=SimpleNamespace(
                            root=SimpleNamespace(
                                type="search",
                                query="Toyota employee count",
                            )
                        ),
                    )
                ),
                SimpleNamespace(
                    root=SimpleNamespace(
                        type="mcpToolCall",
                        server="fetch",
                        tool="open_url",
                        status="completed",
                        result=SimpleNamespace(
                            content=[{"text": "Opened investor relations page"}],
                            structured_content=None,
                        ),
                    )
                ),
                SimpleNamespace(
                    root=SimpleNamespace(
                        type="fileChange",
                        status="applied",
                        changes=[
                            SimpleNamespace(kind="update", path="workflow_checklist.json"),
                        ],
                    )
                ),
            ]

        class FakeTurn:
            def __init__(self, prompt):
                self.prompt = prompt

            def run(self):
                return FakeTurnResult()

            def interrupt(self):
                captured["interrupted"] = True

        class FakeThread:
            def turn(self, prompt, **kwargs):
                captured["turn_prompt"] = prompt
                captured["turn_kwargs"] = kwargs
                return FakeTurn(prompt)

        class FakeCodex:
            def __init__(self, config=None):
                captured["sdk_config"] = config

            def thread_start(self, **kwargs):
                captured["thread_start_kwargs"] = kwargs
                return FakeThread()

            def close(self):
                captured["closed"] = True

        fake_sdk = SimpleNamespace(
            Codex=FakeCodex,
            CodexConfig=lambda **kwargs: SimpleNamespace(**kwargs),
            Sandbox=SimpleNamespace(read_only="read_only", workspace_write="workspace_write"),
        )

        previous_import = client_module._import_openai_codex
        client_module._import_openai_codex = lambda: fake_sdk
        try:
            client = CodexClient(cwd=str(self.project), ignore_global_context=True)
            response = client.run(
                input="README を要約して",
                read_paths=["README.md"],
                write_paths=["out"],
                output_schema_path="result.schema.json",
                timeout=3,
            )
        finally:
            client_module._import_openai_codex = previous_import

        self.assertEqual(response.content, "sdk final answer")
        self.assertEqual(
            response.usage,
            {
                "input_tokens": 11,
                "cached_input_tokens": 2,
                "output_tokens": 3,
                "reasoning_output_tokens": 1,
            },
        )
        self.assertEqual(
            response.pricing,
            {
                "model": "gpt-5.4-mini",
                "source_url": "https://developers.openai.com/api/docs/pricing",
                "input_usd_per_million": 0.75,
                "cached_input_usd_per_million": 0.075,
                "output_usd_per_million": 4.5,
            },
        )
        self.assertAlmostEqual(response.estimated_cost_usd, 0.0000264)
        self.assertEqual(captured["thread_start_kwargs"]["sandbox"], "workspace_write")
        self.assertTrue(captured["thread_start_kwargs"]["ephemeral"])
        self.assertEqual(captured["turn_kwargs"]["sandbox"], "workspace_write")
        self.assertEqual(captured["turn_kwargs"]["output_schema"], {"type": "object"})
        self.assertIn("project_doc_max_bytes=0", captured["sdk_config"].config_overrides)
        self.assertIn("skills.include_instructions=false", captured["sdk_config"].config_overrides)
        self.assertTrue(captured.get("closed"))
        stream_messages = [entry.message for entry in response.activity_log if entry.phase == "stream"]
        self.assertTrue(
            any(
                "item.completed (reasoning): reasoning summary text" in message
                for message in stream_messages
            )
        )
        self.assertTrue(
            any(
                "item.completed (agentMessage): sdk final answer" in message
                for message in stream_messages
            )
        )
        self.assertTrue(
            any(
                "item.completed (webSearch): search Toyota employee count" in message
                for message in stream_messages
            )
        )
        self.assertTrue(
            any(
                "item.completed (mcpToolCall): fetch open_url completed Opened investor relations page" in message
                for message in stream_messages
            )
        )
        self.assertTrue(
            any(
                "item.completed (fileChange): applied update:workflow_checklist.json" in message
                for message in stream_messages
            )
        )
        printed = response.stdout
        self.assertIn('"type": "item.completed", "item_type": "agentMessage"', printed)

    def test_usage_and_pricing_are_logged_to_stdout(self):
        client = CodexClient(cwd=str(self.project))

        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            response = client.run(input="README を要約して", timeout=3)

        self.assertEqual(response.pricing["model"], "gpt-5.4-mini")
        output = captured.getvalue()
        self.assertIn("[Main Attempt][Usage]", output)
        self.assertIn("input_tokens=100", output)
        self.assertIn("cached_input_tokens=25", output)
        self.assertIn("estimated_cost_usd=", output)

    def test_sdk_backend_passes_ephemeral_to_main_and_review_threads(self):
        captured = {"thread_start_kwargs": []}

        class FakeTurnResult:
            def __init__(self, prompt):
                if "Internal review mode: codex_api_wrapper" in prompt:
                    self.final_response = '{"passed": true, "repair_text": "", "summary": "ok"}'
                else:
                    self.final_response = "sdk final answer"
                self.usage = None
                self.items = []

        class FakeTurn:
            def __init__(self, prompt):
                self.prompt = prompt

            def run(self):
                return FakeTurnResult(self.prompt)

            def interrupt(self):
                return None

        class FakeThread:
            def turn(self, prompt, **kwargs):
                return FakeTurn(prompt)

        class FakeCodex:
            def __init__(self, config=None):
                self.config = config

            def thread_start(self, **kwargs):
                captured["thread_start_kwargs"].append(kwargs)
                return FakeThread()

            def close(self):
                return None

        fake_sdk = SimpleNamespace(
            Codex=FakeCodex,
            CodexConfig=lambda **kwargs: SimpleNamespace(**kwargs),
            Sandbox=SimpleNamespace(read_only="read_only", workspace_write="workspace_write"),
        )

        previous_import = client_module._import_openai_codex
        client_module._import_openai_codex = lambda: fake_sdk
        try:
            client = CodexClient(cwd=str(self.project), ephemeral=True)
            response = client.run(
                input="x",
                review=ReviewConfig(instructions="REVIEW_SHOULD_PASS"),
                timeout=3,
            )
        finally:
            client_module._import_openai_codex = previous_import

        self.assertEqual(response.content, "sdk final answer")
        self.assertEqual(len(captured["thread_start_kwargs"]), 2)
        self.assertTrue(all(kwargs["ephemeral"] is True for kwargs in captured["thread_start_kwargs"]))

    def test_sdk_backend_review_uses_same_executor_path(self):
        captured = {"prompts": []}

        class FakeTurnResult:
            def __init__(self, prompt):
                if "Internal review mode: codex_api_wrapper" in prompt:
                    self.final_response = '{"passed": true, "repair_text": "", "summary": "ok"}'
                else:
                    self.final_response = "sdk final answer"
                self.usage = None
                self.items = []

        class FakeTurn:
            def __init__(self, prompt):
                self.prompt = prompt

            def run(self):
                captured["prompts"].append(self.prompt)
                return FakeTurnResult(self.prompt)

            def interrupt(self):
                return None

        class FakeThread:
            def turn(self, prompt, **kwargs):
                return FakeTurn(prompt)

        class FakeCodex:
            def __init__(self, config=None):
                self.config = config

            def thread_start(self, **kwargs):
                return FakeThread()

            def close(self):
                return None

        fake_sdk = SimpleNamespace(
            Codex=FakeCodex,
            CodexConfig=lambda **kwargs: SimpleNamespace(**kwargs),
            Sandbox=SimpleNamespace(read_only="read_only", workspace_write="workspace_write"),
        )

        previous_import = client_module._import_openai_codex
        client_module._import_openai_codex = lambda: fake_sdk
        try:
            client = CodexClient(cwd=str(self.project))
            response = client.run(
                input="x",
                review=ReviewConfig(instructions="REVIEW_SHOULD_PASS"),
                timeout=3,
            )
        finally:
            client_module._import_openai_codex = previous_import

        self.assertEqual(response.content, "sdk final answer")
        self.assertEqual(len(captured["prompts"]), 2)
        self.assertTrue(any("Internal review mode: codex_api_wrapper" in prompt for prompt in captured["prompts"]))

    def test_review_stream_events_are_logged_with_reviewer_actor(self):
        captured_stdout = io.StringIO()

        class FakeUsage:
            input_tokens = 4
            cached_input_tokens = 0
            output_tokens = 2
            reasoning_output_tokens = 1

        class FakeStream:
            def __init__(self, events):
                self._events = events

            def __iter__(self):
                return iter(self._events)

            def close(self):
                return None

        class FakeTurn:
            def __init__(self, prompt):
                self.prompt = prompt

            def stream(self):
                if "Main job final response:" in self.prompt:
                    reasoning_item = SimpleNamespace(root=SimpleNamespace(type="reasoning", summary=["review reasoning"], content=[]))
                    message_item = SimpleNamespace(
                        root=SimpleNamespace(
                            type="agentMessage",
                            text='{"passed": true, "repair_text": "", "summary": "ok"}',
                        )
                    )
                    completed = SimpleNamespace(status="completed")
                    return FakeStream(
                        [
                            SimpleNamespace(method="item.completed", payload=SimpleNamespace(item=reasoning_item)),
                            SimpleNamespace(method="item.completed", payload=SimpleNamespace(item=message_item)),
                            SimpleNamespace(method="turn.completed", payload=SimpleNamespace(turn=completed)),
                        ]
                    )
                item = SimpleNamespace(root=SimpleNamespace(type="agentMessage", text="sdk final answer"))
                completed = SimpleNamespace(status="completed")
                return FakeStream(
                    [
                        SimpleNamespace(method="item.completed", payload=SimpleNamespace(item=item)),
                        SimpleNamespace(
                            method="thread.tokenUsage.updated",
                            payload=SimpleNamespace(token_usage=FakeUsage()),
                        ),
                        SimpleNamespace(method="turn.completed", payload=SimpleNamespace(turn=completed)),
                    ]
                )

            def interrupt(self):
                return None

        class FakeThread:
            def turn(self, prompt, **kwargs):
                return FakeTurn(prompt)

        class FakeCodex:
            def __init__(self, config=None):
                self.config = config

            def thread_start(self, **kwargs):
                return FakeThread()

            def close(self):
                return None

        fake_sdk = SimpleNamespace(
            Codex=FakeCodex,
            CodexConfig=lambda **kwargs: SimpleNamespace(**kwargs),
            Sandbox=SimpleNamespace(read_only="read_only", workspace_write="workspace_write"),
        )

        previous_import = client_module._import_openai_codex
        client_module._import_openai_codex = lambda: fake_sdk
        try:
            client = CodexClient(cwd=str(self.project))
            with contextlib.redirect_stdout(captured_stdout):
                response = client.run(
                    input="x",
                    review=ReviewConfig(instructions="REVIEW_SHOULD_PASS"),
                    timeout=3,
                )
        finally:
            client_module._import_openai_codex = previous_import

        self.assertEqual(response.content, "sdk final answer")
        self.assertTrue(
            any(
                entry.phase == "stream"
                and entry.detail == "review"
                and entry.event == "item.completed"
                and "review reasoning" in entry.message
                for entry in response.activity_log
            )
        )
        self.assertIn("[Reviewer][Thinking]", captured_stdout.getvalue())

    def test_demo_company_analysis_job_wires_schema_validator_review_and_repair(self):
        client = DemoCompanyAnalysisClient(cwd=str(self.project))

        job = client.start(company_name="OpenAI", extra_input="Focus on compensation.")

        self.assertIn("Company name:\nOpenAI", job._attempt.prompt)
        self.assertIn("Additional input:\nFocus on compensation.", job._attempt.prompt)
        self.assertTrue(str(job._output_schema_path).endswith("demo_company_analysis/output.schema.json"))
        self.assertEqual(job._output_validator[0], "python3")
        self.assertTrue(str(job._output_validator[1]).endswith("demo_company_analysis/validate_output.py"))
        self.assertTrue(job._repair_validation_failures)
        self.assertEqual(job._max_repairs, 5)
        self.assertIsNotNone(job._review_config)
        self.assertIn("references in the JSON output", job._review_config.instructions)
        self.assertIsNone(job._review_config.timeout)
        self.assertIsNone(client.timeout)
        self.assertEqual(job._subcwd_files, ["${SUBCWD}/workflow_checklist.json"])
        job.result(timeout=3)

    def test_demo_company_analysis_validator_requires_completed_checklist(self):
        validator = "/home/nakamura/.codex/skills/codex-job-runner/jobs/demo_company_analysis/validate_output.py"
        sample = {
            "company_name": "OpenAI",
            "summary": "AI company with strong model and platform position.",
            "employee_count": {"value": 1000, "scope": "unknown", "as_of": "2026-01", "source_note": "Public estimate from cited source."},
            "salary": {"value_jpy": 15000000, "label": "estimated", "as_of": "2026-01", "source_note": "Converted from cited compensation discussion."},
            "references": [
                {"title": "Example Source", "url": "https://example.com/company", "note": "Supports summary and employee estimate."}
            ],
        }
        subcwd = self.tmp_path / "validator_subcwd"
        subcwd.mkdir()
        (subcwd / "workflow_checklist.json").write_text(
            json.dumps(
                {
                    "checked_company_summary": True,
                    "checked_employee_count": False,
                }
            ),
            encoding="utf-8",
        )
        proc = subprocess.run(
            ["python3", validator],
            input=json.dumps(sample),
            text=True,
            capture_output=True,
            env={**os.environ, "CODEX_WRAPPER_SUBCWD": str(subcwd)},
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("workflow checklist is incomplete", proc.stderr)

    def test_repair_prompt_includes_failure_stage_and_checklist_feedback(self):
        client = CodexClient(cwd=str(self.project))
        job = client.start(input="x", subcwd_files={"workflow_checklist.json": {"text": "{}\n"}})
        try:
            response = CodexResponse(
                content='{"ok": false}',
                returncode=0,
                stdout="",
                stderr="",
                argv=(),
                usage=None,
                files={},
                activity_log=(),
            )
            validation = SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="workflow checklist is incomplete: checked_salary, checked_references",
            )
            prompt = job._build_repair_prompt(
                response=response,
                validation=validation,
                review=None,
                attempt_index=1,
                repair_index=1,
            )
            self.assertIn("Failure stage:\nvalidator", prompt)
            self.assertIn("Incomplete checklist items:\nchecked_salary, checked_references", prompt)
        finally:
            job.cancel()

    def test_json_mode_is_enabled_and_usage_is_aggregated_across_repairs(self):
        validator = self._make_validator(
            "validate_json_only.py",
            """
            import json
            import sys

            json.loads(sys.stdin.read())
            sys.exit(0)
            """,
        )
        client = CodexClient(cwd=str(self.project))

        response = client.run(
            input="NEEDS_REPAIR",
            output_validator=["python3", str(validator)],
            repair_validation_failures=True,
            max_repairs=1,
            timeout=3,
        )

        self.assertEqual(response.content, '{"ok": true}')
        self.assertIn('"type": "turn.completed"', response.stdout)
        self.assertEqual(
            response.usage,
            {
                "input_tokens": 250,
                "cached_input_tokens": 60,
                "output_tokens": 25,
                "reasoning_output_tokens": 8,
            },
        )

    def test_argv_contains_sdk_context_controls_and_permissions(self):
        client = CodexClient(
                        cwd=str(self.project),
            model="gpt-test",
            ignore_global_context=True,
            allow_shell_commands=False,
        )

        job = client.start(
            input="調べて",
            read_paths=["README.md"],
            write_paths=["out"],
        )
        response = job.result(timeout=3)

        self.assertEqual(response.content, "fake final answer")
        argv = job.argv
        self.assertEqual(argv[:2], ["openai-codex", "thread.run"])
        self.assertIn("--cwd", argv)
        self.assertIn("--model", argv)
        self.assertIn("gpt-test", argv)
        self.assertIn("--effort", argv)
        self.assertIn("medium", argv)
        self.assertIn("project_doc_max_bytes=0", argv)
        self.assertIn("features.shell_tool=false", argv)
        self.assertIn("allow_login_shell=false", argv)
        self.assertIn("skills.include_instructions=false", argv)
        filesystem_args = [item for item in argv if item.startswith("permissions.")]
        self.assertEqual(len(filesystem_args), 1)
        self.assertIn('":minimal"="read"', filesystem_args[0])
        self.assertIn('":workspace_roots"', filesystem_args[0])
        self.assertIn('"README.md"="read"', filesystem_args[0])
        self.assertIn('"out"="write"', filesystem_args[0])

    def test_allow_shell_commands_default_allows_shell(self):
        client = CodexClient(
                        cwd=str(self.project),
        )

        job = client.start(input="x")
        job.result(timeout=3)

        self.assertNotIn("features.shell_tool=false", job.argv)
        self.assertNotIn("allow_login_shell=false", job.argv)
        filesystem_args = [item for item in job.argv if item.startswith("permissions.")]
        self.assertIn('":minimal"="read"', filesystem_args[0])

    def test_default_model_and_reasoning_effort_are_passed_to_sdk_argv(self):
        client = CodexClient(
                        cwd=str(self.project),
        )

        job = client.start(input="x")
        job.result(timeout=3)

        self.assertIn("--model", job.argv)
        self.assertEqual(
            job.argv[job.argv.index("--model") + 1],
            "gpt-5.4-mini",
        )
        self.assertIn("--effort", job.argv)
        self.assertEqual(job.argv[job.argv.index("--effort") + 1], "medium")

    def test_explicit_none_client_defaults_delegate_to_sdk_defaults(self):
        client = CodexClient(
                        cwd=str(self.project),
            model=None,
            reasoning_effort=None,
        )

        job = client.start(input="x")
        job.result(timeout=3)

        self.assertNotIn("--model", job.argv)
        self.assertNotIn("--effort", job.argv)

    def test_reasoning_effort_adds_cli_config(self):
        client = CodexClient(
                        cwd=str(self.project),
            reasoning_effort="low",
        )

        job = client.start(input="x")
        job.result(timeout=3)

        self.assertIn("--effort", job.argv)
        self.assertEqual(job.argv[job.argv.index("--effort") + 1], "low")

    def test_reasoning_effort_per_call_overrides_client_default(self):
        client = CodexClient(
                        cwd=str(self.project),
            reasoning_effort="high",
        )

        job = client.start(input="x", reasoning_effort="low")
        job.result(timeout=3)

        self.assertEqual(job.argv[job.argv.index("--effort") + 1], "low")
        self.assertNotIn("high", job.argv)

    def test_empty_reasoning_effort_errors_before_start(self):
        client = CodexClient(cwd=str(self.project))

        with self.assertRaises(ValueError):
            client.start(input="x", reasoning_effort="")

    def test_paths_outside_cwd_are_rejected_for_permissions(self):
        outside = self.tmp_path / "outside.txt"
        outside.write_text("secret\n", encoding="utf-8")
        client = CodexClient(cwd=str(self.project))

        with self.assertRaises(ValueError):
            client.start(input="x", read_paths=[str(outside)])

    def test_file_attachments_are_inlined_by_default(self):
        client = CodexClient(cwd=str(self.project))

        response = client.run(input="ECHO_PROMPT", attachments=["README.md"])

        self.assertIn("Attachments:", response.content)
        self.assertIn("Attached file contents:", response.content)
        self.assertIn("<AttachedFile path=", response.content)
        self.assertIn("hello\n", response.content)

    def test_inline_attachments_none_uses_default_inline(self):
        client = CodexClient(cwd=str(self.project))

        response = client.run(
            input="ECHO_PROMPT",
            attachments=["README.md"],
            inline_attachments=None,
        )

        self.assertIn("Attached file contents:", response.content)
        self.assertIn("hello\n", response.content)

    def test_inline_attachments_false_lists_paths_without_contents(self):
        client = CodexClient(
                        cwd=str(self.project),
        )

        response = client.run(
            input="ECHO_PROMPT",
            attachments=["README.md"],
            inline_attachments=False,
        )

        self.assertIn("Attachments:", response.content)
        self.assertNotIn("Attached file contents:", response.content)
        self.assertNotIn("<AttachedFile path=", response.content)

    def test_inline_attachment_size_limit_errors(self):
        client = CodexClient(cwd=str(self.project))

        with self.assertRaises(ValueError):
            client.start(
                input="x",
                attachments=["README.md"],
                max_inline_attachment_bytes=1,
            )

    def test_allowed_skills_empty_disables_skill_instructions(self):
        client = CodexClient(cwd=str(self.project))

        job = client.start(input="x", allowed_skills=[])
        job.result(timeout=3)

        self.assertIn("skills.include_instructions=false", job.argv)

    def test_output_schema_path_adds_cli_option(self):
        client = CodexClient(cwd=str(self.project))

        job = client.start(
            input="ECHO_PROMPT",
            output_schema_path="result.schema.json",
        )
        response = job.result(timeout=3)

        argv = job.argv
        schema_index = argv.index("--output-schema")
        self.assertEqual(
            argv[schema_index + 1],
            str(self.project / "result.schema.json"),
        )
        self.assertIn("Output schema:", response.content)
        self.assertIn("<OutputSchema path=", response.content)
        self.assertIn('{"type":"object"}', response.content)

    def test_output_pydantic_model_adds_cli_option_and_runs_validation(self):
        client = CodexClient(cwd=str(self.project))

        class FakePydanticModel:
            @classmethod
            def model_json_schema(cls, **kwargs):
                return {"type": "object", "additionalProperties": False}

            @classmethod
            def model_validate_json(cls, content):
                if content != "fake final answer":
                    raise ValueError("unexpected content")
                return {"ok": True}

        job = client.start(
            input="x",
            output_pydantic_model=FakePydanticModel,
        )
        schema_index = job.argv.index("--output-schema")
        temp_schema_path = pathlib.Path(job.argv[schema_index + 1])

        response = job.result(timeout=3)

        self.assertEqual(response.content, "fake final answer")
        self.assertIn("validator", [entry.phase for entry in response.activity_log])
        self.assertFalse(temp_schema_path.exists())

    def test_output_pydantic_model_failure_raises_validation_error_before_output_validator(self):
        validator = self._make_validator(
            "validate_should_not_run.py",
            """
            import sys
            print("external validator should not run", file=sys.stderr)
            sys.exit(7)
            """,
        )
        client = CodexClient(cwd=str(self.project))

        class FakePydanticModel:
            @classmethod
            def model_json_schema(cls, **kwargs):
                return {"type": "object"}

            @classmethod
            def model_validate_json(cls, content):
                raise ValueError("pydantic rejected output")

        with self.assertRaises(CodexValidationError) as caught:
            client.run(
                input="x",
                output_pydantic_model=FakePydanticModel,
                output_validator=["python3", str(validator)],
            )

        self.assertEqual(caught.exception.validator_returncode, 1)
        self.assertIn("pydantic validation failed", caught.exception.validator_stderr)
        self.assertIn("pydantic rejected output", caught.exception.validator_stderr)

    def test_output_schema_path_and_output_pydantic_model_are_mutually_exclusive(self):
        client = CodexClient(cwd=str(self.project))

        class FakePydanticModel:
            @classmethod
            def model_json_schema(cls, **kwargs):
                return {"type": "object"}

            @classmethod
            def model_validate_json(cls, content):
                return {"ok": True}

        with self.assertRaises(ValueError):
            client.start(
                input="x",
                output_schema_path="result.schema.json",
                output_pydantic_model=FakePydanticModel,
            )

    def test_output_schema_inline_size_limit_errors(self):
        client = CodexClient(cwd=str(self.project))

        with self.assertRaises(ValueError):
            client.start(
                input="x",
                output_schema_path="result.schema.json",
                max_inline_attachment_bytes=1,
            )

    def test_missing_output_schema_path_errors_before_start(self):
        client = CodexClient(cwd=str(self.project))

        with self.assertRaises(FileNotFoundError):
            client.start(input="x", output_schema_path="missing.schema.json")

    def test_output_validator_success(self):
        validator = self._make_validator(
            "validate_success.py",
            """
            import os
            import sys

            content = sys.stdin.read()
            if content != "fake final answer":
                print("unexpected content", file=sys.stderr)
                sys.exit(2)
            if os.environ["CODEX_WRAPPER_ATTEMPT"] != "0":
                print("unexpected attempt", file=sys.stderr)
                sys.exit(3)
            sys.exit(0)
            """,
        )
        client = CodexClient(cwd=str(self.project))

        response = client.run(input="x", output_validator=["python3", str(validator)])

        self.assertEqual(response.content, "fake final answer")
        self.assertEqual(response.argv[0], str(self.codex_bin))
        self.assertIn("validator", [entry.phase for entry in response.activity_log])

    def test_output_validator_failure_raises_validation_error(self):
        validator = self._make_validator(
            "validate_failure.py",
            """
            import sys

            sys.stdin.read()
            print("not valid", file=sys.stderr)
            sys.exit(9)
            """,
        )
        client = CodexClient(cwd=str(self.project))

        with self.assertRaises(CodexValidationError) as caught:
            client.run(input="x", output_validator=["python3", str(validator)])

        self.assertEqual(caught.exception.validator_returncode, 9)
        self.assertIn("not valid", caught.exception.validator_stderr)
        self.assertEqual(caught.exception.attempts, 1)
        self.assertEqual(caught.exception.repairs, 0)
        self.assertIn("validator", [entry.phase for entry in caught.exception.activity_log])
        validator_end = [
            entry for entry in caught.exception.activity_log
            if entry.phase == "validator" and entry.event == "end"
        ]
        self.assertEqual(len(validator_end), 1)
        self.assertEqual(validator_end[0].argv[0], "python3")
        self.assertIn("not valid", validator_end[0].stderr)
        validator_details = [
            entry.message for entry in caught.exception.activity_log
            if entry.phase == "validator" and entry.event == "detail"
        ]
        self.assertTrue(
            any("validator stderr:\nnot valid" in message for message in validator_details)
        )
        self.assertIn(
            'validator stderr: not valid',
            pathlib.Path(caught.exception.log_path).read_text(encoding="utf-8"),
        )

    def test_review_passes_after_validator_success(self):
        client = CodexClient(cwd=str(self.project))

        response = client.run(
            input="x",
            review=ReviewConfig(instructions="REVIEW_SHOULD_PASS"),
            timeout=3,
        )

        self.assertEqual(response.content, "fake final answer")

    def test_review_failure_triggers_repair_and_succeeds(self):
        client = CodexClient(cwd=str(self.project))

        response = client.run(
            input="REVIEW_TRIGGER_REPAIR",
            review=ReviewConfig(instructions="REVIEW_SHOULD_FAIL_ON_BAD_RESPONSE"),
            repair_validation_failures=True,
            max_repairs=1,
            timeout=3,
        )

        self.assertEqual(response.content, "review repaired answer")
        self.assertTrue(
            any(
                entry.phase == "main_attempt" and entry.repair_index == 1
                for entry in response.activity_log
            )
        )

    def test_review_failure_exposes_summary_and_repair_text(self):
        client = CodexClient(cwd=str(self.project))

        with self.assertRaises(CodexValidationError) as caught:
            client.run(
                input="REVIEW_TRIGGER_REPAIR",
                review=ReviewConfig(instructions="REVIEW_SHOULD_FAIL_ON_BAD_RESPONSE"),
                repair_validation_failures=True,
                max_repairs=0,
                timeout=3,
            )

        self.assertIn("response does not satisfy review", caught.exception.message)
        self.assertIsNotNone(caught.exception.log_path)
        self.assertTrue(pathlib.Path(caught.exception.log_path).is_file())
        review_details = [
            entry.message for entry in caught.exception.activity_log
            if entry.phase == "review" and entry.event == "detail"
        ]
        self.assertTrue(
            any("review summary: response does not satisfy review" in message for message in review_details)
        )
        self.assertTrue(
            any("review repair request: Please change the final response to review repaired answer." in message for message in review_details)
        )

    def test_review_logs_pretty_printed_input_json(self):
        client = CodexClient(cwd=str(self.project))

        response = client.run(
            input="企業について短い構造化分析を行い、JSON だけを返してください。\n\nCompany name:\n日本アイ・ビー・エム株式会社",
            review=ReviewConfig(instructions="REVIEW_SHOULD_PASS"),
            timeout=3,
        )

        review_input_entries = [
            entry for entry in response.activity_log
            if entry.phase == "review" and entry.event == "input_json"
        ]
        self.assertEqual(len(review_input_entries), 1)
        self.assertIn('"company_name": "日本アイ・ビー・エム株式会社"', review_input_entries[0].message)
        self.assertIn('\n  "summary": ', review_input_entries[0].message)

        log_text = pathlib.Path(response.log_path).read_text(encoding="utf-8")
        self.assertIn("[Reviewer][Input JSON]", log_text)
        self.assertIn('"company_name": "日本アイ・ビー・エム株式会社"', log_text)

    def test_review_stream_events_are_logged_with_reviewer_actor(self):
        captured = {}

        class FakeMainTurn:
            def __init__(self, prompt):
                self.prompt = prompt

            def run(self):
                return SimpleNamespace(
                    final_response="needs review fix",
                    usage=None,
                    items=[],
                )

        class FakeReviewTurn:
            def __init__(self, prompt):
                self.prompt = prompt

            def stream(self):
                def event(method, payload):
                    return SimpleNamespace(method=method, payload=payload)

                return iter(
                    [
                        event(
                            "item/reasoning/textDelta",
                            SimpleNamespace(delta="reviewing the response"),
                        ),
                        event(
                            "item/completed",
                            SimpleNamespace(
                                item=SimpleNamespace(
                                    root=SimpleNamespace(
                                        type="reasoning",
                                        summary=["reviewing the response"],
                                        content=[],
                                    )
                                )
                            ),
                        ),
                        event(
                            "item/completed",
                            SimpleNamespace(
                                item=SimpleNamespace(
                                    root=SimpleNamespace(
                                        type="webSearch",
                                        query="review source",
                                    )
                                )
                            ),
                        ),
                        event(
                            "item/completed",
                            SimpleNamespace(
                                item=SimpleNamespace(
                                    root=SimpleNamespace(
                                        type="agentMessage",
                                        text='{"passed": true, "repair_text": "", "summary": "ok"}',
                                    )
                                )
                            ),
                        ),
                        event(
                            "turn/completed",
                            SimpleNamespace(turn=SimpleNamespace(status="completed")),
                        ),
                    ]
                )

        class FakeThread:
            def turn(self, prompt, **kwargs):
                captured["turn_prompts"].append(prompt)
                if "Internal review mode: codex_api_wrapper" in prompt:
                    return FakeReviewTurn(prompt)
                return FakeMainTurn(prompt)

        class FakeCodex:
            def __init__(self, config=None):
                self.config = config

            def thread_start(self, **kwargs):
                captured["thread_start_kwargs"] = kwargs
                captured.setdefault("turn_prompts", [])
                return FakeThread()

            def close(self):
                captured["closed"] = True

        fake_sdk = SimpleNamespace(
            Codex=FakeCodex,
            CodexConfig=lambda **kwargs: SimpleNamespace(**kwargs),
            Sandbox=SimpleNamespace(read_only="read_only", workspace_write="workspace_write"),
        )

        previous_import = client_module._import_openai_codex
        client_module._import_openai_codex = lambda: fake_sdk
        try:
            client = CodexClient(cwd=str(self.project))
            captured_stdout = io.StringIO()
            with contextlib.redirect_stdout(captured_stdout):
                response = client.run(
                    input="REVIEW_STREAM_LOGGING",
                    review=ReviewConfig(instructions="REVIEW_STREAM_LOGGING"),
                    timeout=3,
                )
        finally:
            client_module._import_openai_codex = previous_import

        self.assertEqual(response.content, "needs review fix")
        self.assertTrue(captured.get("closed"))
        self.assertTrue(
            any(
                entry.phase == "stream" and entry.detail == "review"
                for entry in response.activity_log
            )
        )
        printed = captured_stdout.getvalue()
        self.assertIn("[Reviewer][Thinking]", printed)
        self.assertIn("[Reviewer][Search]", printed)
        self.assertIn("[Reviewer][Message]", printed)

    def test_validator_failure_skips_review(self):
        validator = self._make_validator(
            "validate_failure_skip_review.py",
            """
            import sys
            sys.stdin.read()
            print("validator failed", file=sys.stderr)
            sys.exit(5)
            """,
        )
        client = CodexClient(cwd=str(self.project))

        with self.assertRaises(CodexValidationError) as caught:
            client.run(
                input="x",
                output_validator=["python3", str(validator)],
                review=ReviewConfig(instructions="REVIEW_FORCE_BAD_OUTPUT"),
                timeout=3,
            )

        self.assertEqual(caught.exception.failure_stage, "validator")
        self.assertEqual(caught.exception.validator_returncode, 5)

    def test_review_can_see_original_input_prompt_and_subcwd(self):
        client = CodexClient(cwd=str(self.project))

        response = client.run(
            input="REVIEW_CONTEXT_MARKER",
            subcwd_files={"state.json": {"text": '{"ok": true}\n'}},
            review=ReviewConfig(instructions="CHECK_REVIEW_CONTEXT_AND_SUBCWD"),
            collect_files=True,
            timeout=3,
        )

        self.assertEqual(response.content, "fake final answer")

    def test_review_timeout_is_execution_error(self):
        client = CodexClient(cwd=str(self.project))

        with self.assertRaises(CodexExecutionError) as caught:
            client.run(
                input="x",
                review=ReviewConfig(
                    instructions="FORCE_REVIEW_TIMEOUT",
                    timeout=0.1,
                ),
                timeout=3,
            )

        self.assertIn("timed out", caught.exception.message)

    def test_review_bad_output_is_execution_error(self):
        client = CodexClient(cwd=str(self.project))

        with self.assertRaises(CodexExecutionError) as caught:
            client.run(
                input="x",
                review=ReviewConfig(instructions="REVIEW_FORCE_BAD_OUTPUT"),
                timeout=3,
            )

        self.assertIn("review output", caught.exception.message)
        self.assertNotIn("communication failed", caught.exception.message)
        self.assertIn('"type": "thread.started"', caught.exception.stdout)
        self.assertEqual(caught.exception.argv[0], str(self.codex_bin))
        self.assertIn("review", [entry.phase for entry in caught.exception.activity_log])
        review_errors = [
            entry for entry in caught.exception.activity_log
            if entry.phase == "review" and entry.event == "error"
        ]
        self.assertEqual(len(review_errors), 1)
        self.assertEqual(review_errors[0].stderr, "")

    def test_subcwd_files_stage_text_and_collect_edited_result(self):
        client = CodexClient(cwd=str(self.project))

        response = client.run(
            input="EDIT_STAGED_TEXT_FILE",
            subcwd_files={
                "inputs/neighborhood_tree.json": {
                    "text": '{"status": "draft"}\n',
                }
            },
            collect_files=True,
            timeout=3,
        )

        self.assertEqual(
            response.files["inputs/neighborhood_tree.json"].text,
            '{"status": "edited"}\n',
        )
        self.assertFalse(self._subcwd_dirs())

    def test_subcwd_files_stage_bytes(self):
        client = CodexClient(cwd=str(self.project))

        response = client.run(
            input="SUM_STAGED_BINARY_FILE",
            subcwd_files={
                "inputs/blob.bin": {
                    "bytes": b"\x00\x01\x02\x03",
                }
            },
            collect_files=True,
            timeout=3,
        )

        self.assertEqual(response.content, "binary-bytes=4")
        self.assertEqual(
            response.files["outputs/report.txt"].text,
            "binary-bytes=4\n",
        )

    def test_validation_repair_reuses_subcwd_and_collects_final_files(self):
        validator = self._make_validator(
            "validate_json_and_subcwd.py",
            """
            import json
            import os
            import pathlib
            import sys

            data = json.loads(sys.stdin.read())
            subcwd = pathlib.Path(os.environ["CODEX_WRAPPER_SUBCWD"])
            file_data = json.loads((subcwd / "data.json").read_text())
            if data != {"ok": True} or file_data != {"ok": True}:
                print("needs repair", file=sys.stderr)
                sys.exit(4)
            sys.exit(0)
            """,
        )
        client = CodexClient(cwd=str(self.project))

        response = client.run(
            input="NEEDS_REPAIR MAKE_REPAIR_SUBCWD_FILE",
            write_paths=["${SUBCWD}"],
            collect_files=True,
            output_validator=["python3", str(validator)],
            repair_validation_failures=True,
            max_repairs=1,
            timeout=3,
        )

        self.assertEqual(response.content, '{"ok": true}')
        self.assertEqual(response.files["data.json"].json(), {"ok": True})
        self.assertFalse(self._subcwd_dirs())

    def test_validation_repair_reuses_staged_subcwd_files_without_restaging(self):
        validator = self._make_validator(
            "validate_repair_staged_state.py",
            """
            import json
            import os
            import pathlib
            import sys

            data = json.loads(sys.stdin.read())
            subcwd = pathlib.Path(os.environ["CODEX_WRAPPER_SUBCWD"])
            state = (subcwd / "state.json").read_text()
            if data != {"ok": True} or state != '{"state": "mutated"}\\n':
                print("needs repair", file=sys.stderr)
                sys.exit(6)
            sys.exit(0)
            """,
        )
        client = CodexClient(cwd=str(self.project))

        response = client.run(
            input="NEEDS_REPAIR_CHECK_STAGED_PERSISTENCE",
            subcwd_files={
                "state.json": {
                    "text": '{"state": "initial"}\n',
                }
            },
            collect_files=True,
            output_validator=["python3", str(validator)],
            repair_validation_failures=True,
            max_repairs=1,
            timeout=3,
        )

        self.assertEqual(response.content, '{"ok": true}')
        self.assertEqual(
            response.files["state.json"].text,
            '{"state": "mutated"}\n',
        )

    def test_missing_output_validator_path_errors_before_start(self):
        client = CodexClient(cwd=str(self.project))

        with self.assertRaises(FileNotFoundError):
            client.start(input="x", output_validator="./missing-validator")

    def test_collect_files_reads_subcwd_into_response_and_cleans_up(self):
        client = CodexClient(cwd=str(self.project))

        job = client.start(
            input="MAKE_SUBCWD_FILES",
            write_paths=["${SUBCWD}"],
            collect_files=True,
        )
        response = job.result(timeout=3)

        self.assertEqual(response.files["report.md"].text, "hello from subcwd\n")
        self.assertEqual(response.files["data.json"].json(), {"ok": True})
        self.assertEqual(
            response.to_dict()["files"]["report.md"]["size"],
            len("hello from subcwd\n"),
        )
        self.assertFalse(self._subcwd_dirs())
        filesystem_args = [item for item in job.argv if item.startswith("permissions.")]
        self.assertNotIn("${SUBCWD}", filesystem_args[0])
        self.assertIn('"write"', filesystem_args[0])

    def test_subcwd_root_relative_path_creates_subcwd_under_custom_parent(self):
        client = CodexClient(
                        cwd=str(self.project),
            subcwd_root="tmp/jobs",
        )

        response = client.run(
            input="MAKE_SUBCWD_FILES",
            write_paths=["${SUBCWD}"],
            collect_files=True,
            timeout=3,
        )

        self.assertEqual(response.files["report.md"].text, "hello from subcwd\n")
        custom_root = self.project / "tmp" / "jobs"
        self.assertTrue(custom_root.is_dir())
        self.assertEqual(list(custom_root.iterdir()), [])

    def test_subcwd_root_is_created_when_missing(self):
        client = CodexClient(
                        cwd=str(self.project),
            subcwd_root="tmp/missing/jobs",
        )

        client.run(
            input="MAKE_SUBCWD_FILES",
            write_paths=["${SUBCWD}"],
            timeout=3,
        )

        self.assertTrue((self.project / "tmp" / "missing" / "jobs").is_dir())

    def test_subcwd_root_accepts_absolute_path_inside_cwd(self):
        custom_root = self.project / "tmp" / "absolute"
        client = CodexClient(
                        cwd=str(self.project),
            subcwd_root=str(custom_root),
        )

        client.run(
            input="MAKE_SUBCWD_FILES",
            write_paths=["${SUBCWD}"],
            timeout=3,
        )

        self.assertTrue(custom_root.is_dir())
        self.assertEqual(list(custom_root.iterdir()), [])

    def test_subcwd_root_outside_cwd_is_rejected(self):
        client = CodexClient(
                        cwd=str(self.project),
            subcwd_root=str(self.tmp_path / "outside"),
        )

        with self.assertRaises(ValueError):
            client.start(input="x", write_paths=["${SUBCWD}"])

    def test_subcwd_root_rejects_subcwd_token(self):
        client = CodexClient(
                        cwd=str(self.project),
            subcwd_root="tmp/${SUBCWD}",
        )

        with self.assertRaises(ValueError):
            client.start(input="x", write_paths=["${SUBCWD}"])

    def test_subcwd_without_collect_files_cleans_up(self):
        client = CodexClient(cwd=str(self.project))

        response = client.run(
            input="MAKE_SUBCWD_FILES",
            write_paths=["${SUBCWD}"],
            timeout=3,
        )

        self.assertEqual(response.files, {})
        self.assertFalse(self._subcwd_dirs())

    def test_subcwd_files_add_subcwd_write_permission_without_explicit_write_paths(self):
        client = CodexClient(cwd=str(self.project))

        job = client.start(
            input="EDIT_STAGED_TEXT_FILE",
            subcwd_files={
                "inputs/neighborhood_tree.json": {
                    "text": '{"status": "draft"}\n',
                }
            },
            timeout=3,
        )
        job.result(timeout=3)

        filesystem_args = [item for item in job.argv if item.startswith("permissions.")]
        self.assertIn('"write"', filesystem_args[0])

    def test_collect_files_limit_error_cleans_up(self):
        client = CodexClient(cwd=str(self.project))

        job = client.start(
            input="MAKE_SUBCWD_FILES",
            write_paths=["${SUBCWD}"],
            collect_files=True,
            max_file_bytes=1,
        )

        with self.assertRaises(CodexExecutionError) as caught:
            job.result(timeout=3)
        self.assertIn("file collection failed", caught.exception.message)
        self.assertFalse(self._subcwd_dirs())

    def test_subcwd_is_rejected_for_read_paths(self):
        client = CodexClient(cwd=str(self.project))

        with self.assertRaises(ValueError):
            client.start(input="x", read_paths=["${SUBCWD}"])

    def test_subcwd_files_rejects_absolute_path(self):
        client = CodexClient(cwd=str(self.project))

        with self.assertRaises(ValueError):
            client.start(
                input="x",
                subcwd_files={"/abs.txt": {"text": "x"}},
            )

    def test_subcwd_files_rejects_escape_path(self):
        client = CodexClient(cwd=str(self.project))

        with self.assertRaises(ValueError):
            client.start(
                input="x",
                subcwd_files={"../escape.txt": {"text": "x"}},
            )

    def test_subcwd_files_rejects_empty_path(self):
        client = CodexClient(cwd=str(self.project))

        with self.assertRaises(ValueError):
            client.start(
                input="x",
                subcwd_files={"": {"text": "x"}},
            )

    def test_subcwd_files_rejects_invalid_value_shape(self):
        client = CodexClient(cwd=str(self.project))

        with self.assertRaises(ValueError):
            client.start(
                input="x",
                subcwd_files={"a.txt": {"text": "x", "bytes": b"y"}},
            )

    def test_subcwd_files_rejects_missing_text_or_bytes(self):
        client = CodexClient(cwd=str(self.project))

        with self.assertRaises(ValueError):
            client.start(
                input="x",
                subcwd_files={"a.txt": {"encoding": "utf-8"}},
            )

    def test_subcwd_files_rejects_invalid_encoding_usage(self):
        client = CodexClient(cwd=str(self.project))

        with self.assertRaises(ValueError):
            client.start(
                input="x",
                subcwd_files={"a.bin": {"bytes": b"x", "encoding": "utf-8"}},
            )

    def test_subcwd_escape_is_rejected_and_cleaned_up(self):
        client = CodexClient(cwd=str(self.project))

        with self.assertRaises(ValueError):
            client.start(input="x", write_paths=["${SUBCWD}/../escape"])
        self.assertFalse(self._subcwd_dirs())

    def test_nonzero_exit_raises_execution_error(self):
        client = CodexClient(cwd=str(self.project))

        job = client.start(input="FORCE_FAIL")

        with self.assertRaises(CodexExecutionError) as caught:
            job.result(timeout=3)
        self.assertEqual(caught.exception.returncode, 1)
        self.assertIn("FAKE_STDERR", caught.exception.stderr)

    def test_result_timeout_raises_timeout_error(self):
        client = CodexClient(cwd=str(self.project))

        job = client.start(input="sleep")

        with self.assertRaises(CodexTimeoutError):
            job.result(timeout=0.1)
        self.assertEqual(job.status, "timeout")

    def test_job_uses_client_default_timeout(self):
        client = CodexClient(
                        cwd=str(self.project),
            timeout=0.1,
        )

        job = client.start(input="sleep")

        with self.assertRaises(CodexTimeoutError):
            job.result()

    def test_cancelled_job_raises_cancelled_error(self):
        client = CodexClient(cwd=str(self.project))

        job = client.start(input="sleep")
        time.sleep(0.1)
        job.cancel()

        with self.assertRaises(CodexCancelledError):
            job.result(timeout=1)

    def test_wait_any_returns_completed_job(self):
        client = CodexClient(cwd=str(self.project))

        slow = client.start(input="sleep")
        fast = client.start(input="quick")
        done = client.wait_any({slow, fast}, timeout=3)

        self.assertIs(done, fast)
        slow.cancel()

    def test_missing_attachment_errors_before_start(self):
        client = CodexClient(cwd=str(self.project))

        with self.assertRaises(FileNotFoundError):
            client.start(input="x", attachments=["missing.txt"])

    def _subcwd_dirs(self):
        return [
            path
            for path in self.project.iterdir()
            if path.is_dir() and path.name not in {"out", ".codex-job-runner-logs"}
        ]

    def _install_fake_sdk(self):
        test_case = self

        class FakeUsage:
            def __init__(self, is_repair):
                self.input_tokens = 150 if is_repair else 100
                self.cached_input_tokens = 35 if is_repair else 25
                self.output_tokens = 15 if is_repair else 10
                self.reasoning_output_tokens = 5 if is_repair else 3

        class FakeTurnResult:
            def __init__(self, prompt):
                self.final_response = test_case._fake_final_response(prompt)
                self.usage = FakeUsage(test_case._is_repair_prompt(prompt))
                self.items = []

        class FakeTurn:
            def __init__(self, prompt):
                self.prompt = prompt
                self._interrupted = False

            def run(self):
                test_case._apply_fake_side_effects(self.prompt)
                if "sleep" in self.prompt or "FORCE_REVIEW_TIMEOUT" in self.prompt:
                    for _ in range(100):
                        if self._interrupted:
                            return FakeTurnResult(self.prompt)
                        time.sleep(0.1)
                if "FORCE_FAIL" in self.prompt:
                    raise RuntimeError("FAKE_STDERR")
                return FakeTurnResult(self.prompt)

            def interrupt(self):
                self._interrupted = True

        class FakeThread:
            def turn(self, prompt, **kwargs):
                return FakeTurn(prompt)

        class FakeCodex:
            def __init__(self, config=None):
                self.config = config

            def thread_start(self, **kwargs):
                return FakeThread()

            def close(self):
                return None

        fake_sdk = SimpleNamespace(
            Codex=FakeCodex,
            CodexConfig=lambda **kwargs: SimpleNamespace(**kwargs),
            Sandbox=SimpleNamespace(read_only="read_only", workspace_write="workspace_write"),
        )
        client_module._import_openai_codex = lambda: fake_sdk

    def _is_repair_prompt(self, prompt):
        return (
            "The previous output failed validation." in prompt
            or "The previous output failed validation or review." in prompt
        )

    def _extract_subcwd(self, prompt):
        for line in prompt.splitlines():
            if line.startswith("${SUBCWD} = "):
                return pathlib.Path(line.split(" = ", 1)[1])
        return None

    def _extract_review_main_response(self, prompt):
        marker = "Main job final response:\n"
        if marker not in prompt:
            return prompt
        response = prompt.split(marker, 1)[1]
        for terminator in ("\n\nSubcwd path:\n", "\n\nReview instructions:\n"):
            if terminator in response:
                response = response.split(terminator, 1)[0]
        return response

    def _fake_final_response(self, prompt):
        is_repair = self._is_repair_prompt(prompt)
        final_answer = "fake final answer"
        if "企業について短い構造化分析を行い、JSON だけを返してください。" in prompt:
            company_name = "Unknown Company"
            marker = "Company name:\n"
            if marker in prompt:
                company_name = prompt.split(marker, 1)[1].splitlines()[0].strip() or company_name
            salary_reference = {
                "title": "Annual Securities Report 2025",
                "url": "https://example.com/annual-securities-report-2025",
                "note": "Supports the average annual salary figure.",
            } if is_repair else {
                "title": "Unverified Salary Blog",
                "url": "https://example.com/unverified-salary-blog",
                "note": "Mentions a salary figure without primary evidence.",
            }
            payload = {
                "company_name": company_name,
                "summary": f"{company_name} is analyzed as a data-driven technology company with durable platform demand.",
                "employee_count": {
                    "value": 1234,
                    "scope": "consolidated",
                    "as_of": "2025-03",
                    "source_note": "Representative headcount taken from a cited company disclosure.",
                },
                "salary": {
                    "value_jpy": 15000000,
                    "label": "average_annual_salary",
                    "as_of": "2025-03",
                    "source_note": (
                        "Supported by a cited annual securities report."
                        if is_repair
                        else "Derived from a weak salary reference that still needs verification."
                    ),
                },
                "references": [
                    {
                        "title": "Company Factsheet",
                        "url": "https://example.com/company-factsheet",
                        "note": "Supports the company summary and headcount figure.",
                    },
                    salary_reference,
                ],
            }
            final_answer = json.dumps(payload, ensure_ascii=False)
        if "NEEDS_REPAIR" in prompt:
            final_answer = '{"ok": true}' if is_repair else 'not json'
        if "REVIEW_TRIGGER_REPAIR" in prompt:
            if is_repair and "Please change the final response to review repaired answer." in prompt:
                final_answer = "review repaired answer"
            else:
                final_answer = "needs review fix"
        if "ECHO_PROMPT" in prompt:
            final_answer = prompt
        if "Internal review mode: codex_api_wrapper" in prompt:
            if "REVIEW_FORCE_BAD_OUTPUT" in prompt:
                final_answer = "not json"
            elif "CHECK_REVIEW_CONTEXT_AND_SUBCWD" in prompt:
                if "Original request:\nREVIEW_CONTEXT_MARKER" not in prompt:
                    final_answer = '{"passed": false, "repair_text": "missing original input", "summary": "missing original input"}'
                elif "Main job prompt:" not in prompt:
                    final_answer = '{"passed": false, "repair_text": "missing main prompt", "summary": "missing main prompt"}'
                else:
                    subcwd = None
                    for index, line in enumerate(prompt.splitlines()):
                        if line == "Subcwd path:":
                            subcwd = pathlib.Path(prompt.splitlines()[index + 1])
                            break
                    if subcwd is None or (subcwd / "state.json").read_text() != '{"ok": true}\n':
                        final_answer = '{"passed": false, "repair_text": "missing subcwd state", "summary": "missing subcwd state"}'
                    else:
                        final_answer = '{"passed": true, "repair_text": "", "summary": "ok"}'
            elif "REVIEW_SHOULD_FAIL_ON_BAD_RESPONSE" in prompt:
                if "Main job final response:\nneeds review fix" in prompt:
                    final_answer = '{"passed": false, "repair_text": "Please change the final response to review repaired answer.", "summary": "response does not satisfy review"}'
                else:
                    final_answer = '{"passed": true, "repair_text": "", "summary": "ok"}'
            elif "Use the references in the JSON output as the starting point for verification." in prompt:
                reviewed_response = self._extract_review_main_response(prompt)
                if "Unverified Salary Blog" in reviewed_response:
                    final_answer = '{"passed": false, "repair_text": "Replace the salary reference with a primary or otherwise reliable salary source.", "summary": "salary reference is not reliable enough"}'
                else:
                    final_answer = '{"passed": true, "repair_text": "", "summary": "ok"}'
            else:
                final_answer = '{"passed": true, "repair_text": "", "summary": "ok"}'
        if "SUM_STAGED_BINARY_FILE" in prompt:
            subcwd = self._extract_subcwd(prompt)
            size = len((subcwd / "inputs" / "blob.bin").read_bytes())
            final_answer = f"binary-bytes={size}"
        if "NEEDS_REPAIR_CHECK_STAGED_PERSISTENCE" in prompt and not is_repair:
            final_answer = "not json"
        return final_answer

    def _apply_fake_side_effects(self, prompt):
        is_repair = self._is_repair_prompt(prompt)
        subcwd = self._extract_subcwd(prompt)
        if "企業について短い構造化分析を行い、JSON だけを返してください。" in prompt and subcwd is not None:
            checklist_path = subcwd / "workflow_checklist.json"
            if checklist_path.is_file():
                checklist = json.loads(checklist_path.read_text(encoding="utf-8"))
                for key in checklist:
                    checklist[key] = True
                checklist_path.write_text(
                    json.dumps(checklist, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
        if "EDIT_STAGED_TEXT_FILE" in prompt and subcwd is not None:
            path = subcwd / "inputs" / "neighborhood_tree.json"
            path.write_text('{"status": "edited"}\n', encoding="utf-8")
        if "SUM_STAGED_BINARY_FILE" in prompt and subcwd is not None:
            size = len((subcwd / "inputs" / "blob.bin").read_bytes())
            (subcwd / "outputs" / "report.txt").parent.mkdir(parents=True, exist_ok=True)
            (subcwd / "outputs" / "report.txt").write_text(
                f"binary-bytes={size}\n",
                encoding="utf-8",
            )
        if "MAKE_SUBCWD_FILES" in prompt and subcwd is not None:
            (subcwd / "report.md").write_text("hello from subcwd\n", encoding="utf-8")
            (subcwd / "data.json").write_text('{"ok": true}\n', encoding="utf-8")
        if "MAKE_REPAIR_SUBCWD_FILE" in prompt and subcwd is not None:
            content = '{"ok": true}\n' if is_repair else '{"ok": false}\n'
            (subcwd / "data.json").write_text(content, encoding="utf-8")
        if "NEEDS_REPAIR_CHECK_STAGED_PERSISTENCE" in prompt and subcwd is not None and not is_repair:
            (subcwd / "state.json").write_text('{"state": "mutated"}\n', encoding="utf-8")

    def _make_validator(self, name, source):
        path = self.tmp_path / name
        path.write_text(
            "#!/usr/bin/env python3\n" + textwrap.dedent(source).lstrip(),
            encoding="utf-8",
        )
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path


if __name__ == "__main__":
    unittest.main()
