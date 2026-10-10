import contextlib
import enum
import gc
import io
import inspect
import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from types import ModuleType, SimpleNamespace
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import codex_job_runner.client as client_module  # noqa: E402
import codex_job_runner._client_helpers as helpers_module  # noqa: E402
import codex_job_runner._job as job_module  # noqa: E402
import codex_job_runner.job as public_job_module  # noqa: E402
from codex_job_runner import (  # noqa: E402
    CodexCancelledError,
    CodexResponse,
    CodexSessionClosedError,
    CodexWorkerPool,
    CodexExecutionError,
    CodexTimeoutError,
    CodexValidationError,
    ReviewConfig,
)
from codex_job_runner.client import create_runtime as CodexClient  # noqa: E402
from codex_job_runner._subagents import _DirectChildManager  # noqa: E402
from codex_job_runner import (  # noqa: E402
    CodexJob,
    CodexSession,
    SessionConfig,
    SubagentConfig,
    Tool,
    ToolConfig,
    ValidationConfig,
    WorkspaceConfig,
)
from jobs.demo_company_analysis import DemoCompanyAnalysisJob  # noqa: E402


_FAKE_APPROVAL_MODE = SimpleNamespace(deny_all="deny_all", auto_review="auto_review")


class CodexClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = pathlib.Path(self.tmp.name)
        self._preexisting_sdk_homes = set(self._sdk_home_dirs())
        self.codex_bin = "openai-codex"
        self.sdk_configs = []
        self.thread_start_calls = []
        self.thread_prompts = []
        self.turn_calls = []
        self.sdk_threads = {}
        self.fake_turn_callback = None
        self.project = self.tmp_path / "project"
        self.project.mkdir()
        self.runtime_bin = self.project / "codex"
        self.runtime_bin.write_text("fake runtime\n", encoding="utf-8")
        (self.project / "README.md").write_text("hello\n", encoding="utf-8")
        (self.project / "out").mkdir()
        (self.project / "result.schema.json").write_text(
            '{"type":"object"}\n',
            encoding="utf-8",
        )
        self._previous_import = client_module._import_openai_codex
        self._previous_runtime_version = helpers_module._codex_runtime_version
        self._previous_supported_versions = (
            helpers_module._EXACT_FILE_WRITE_SUPPORTED_LINUX_CODEX_VERSIONS
        )
        helpers_module._codex_runtime_version = lambda runtime_path: (
            "test-supported",
            [str(runtime_path), "--version"],
        )
        helpers_module._EXACT_FILE_WRITE_SUPPORTED_LINUX_CODEX_VERSIONS = frozenset(
            {"test-supported"}
        )
        self._install_fake_sdk()

    def tearDown(self):
        client_module._import_openai_codex = self._previous_import
        helpers_module._codex_runtime_version = self._previous_runtime_version
        helpers_module._EXACT_FILE_WRITE_SUPPORTED_LINUX_CODEX_VERSIONS = (
            self._previous_supported_versions
        )
        for path in set(self._sdk_home_dirs()) - self._preexisting_sdk_homes:
            shutil.rmtree(path, ignore_errors=True)
        self.tmp.cleanup()

    @staticmethod
    def _require_repaired_json(response):
        if response.content != '{"ok": true}':
            raise ValueError("return the repaired JSON")

    def test_run_returns_response_content(self):
        client = CodexClient(cwd=str(self.project), context=["README.md"])

        response = client.run(
            input="README を要約して",
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

    def test_subagents_are_default_off_without_changing_runtime_overrides(self):
        client = CodexClient(cwd=str(self.project))

        job = client.start(input="x")
        job.result(timeout=3)

        argv_overrides = [
            job.argv[index + 1]
            for index, item in enumerate(job.argv[:-1])
            if item == "-c"
        ]
        self.assertEqual(
            argv_overrides[:len(client_module._RUNNER_RUNTIME_CONFIG_OVERRIDES)],
            list(client_module._RUNNER_RUNTIME_CONFIG_OVERRIDES),
        )
        self.assertIn(
            "features.multi_agent=false",
            self.sdk_configs[0].config_overrides,
        )
        self.assertIn(
            "features.multi_agent_v2=false",
            self.sdk_configs[0].config_overrides,
        )
        self.assertFalse(self.thread_start_calls[0]["ephemeral"])

    def test_subagents_enable_native_v1_with_bounded_concurrency(self):
        client = CodexClient(
            cwd=str(self.project),
            subagents_enabled=True,
            max_concurrent_subagents=3,
            subagent_context_policy="all",
        )

        response = client.run(
            input="ECHO_PROMPT",
            timeout=3,
        )

        overrides = self.sdk_configs[0].config_overrides
        self.assertIn("features.multi_agent=true", overrides)
        self.assertIn("features.multi_agent_v2=false", overrides)
        self.assertNotIn("features.multi_agent=false", overrides)
        self.assertIn("max_concurrent_threads_per_session=3", overrides)
        self.assertFalse(self.thread_start_calls[0]["ephemeral"])
        self.assertIn("fork_context=true", response.content)
        self.assertIn("model selects the spawn_agent arguments", response.content)

    def test_native_subagent_session_fields_are_removed(self):
        with self.assertRaises(TypeError):
            SessionConfig(subagents_enabled=True)
        with self.assertRaises(TypeError):
            SessionConfig(default_subagent_model="gpt-5.6-luna")

    def test_subagent_context_policy_none_is_put_in_main_prompt(self):
        client = CodexClient(
            cwd=str(self.project),
            subagents_enabled=True,
            subagent_context_policy="none",
        )

        response = client.run(
            input="ECHO_PROMPT",
            timeout=3,
        )

        self.assertIn("fork_context=false", response.content)

    def test_main_prompt_prohibits_native_collaboration(self):
        client = CodexClient(cwd=str(self.project))

        response = client.run(
            input="ECHO_PROMPT",
            timeout=3,
        )

        self.assertIn("Native collaboration tools are prohibited", response.content)
        self.assertIn("codex_job_runner MCP tools", response.content)

    def test_reviewer_prompt_prohibits_native_collaboration(self):
        instructions = (
            helpers_module._build_review_session_developer_instructions(
                "Review carefully.",
            )
        )

        self.assertIn("Native collaboration tools are prohibited", instructions)
        self.assertIn("codex_job_runner MCP tools", instructions)
        self.assertIn("Review context:", instructions)

    def test_native_collaboration_interrupts_and_fails_closed(self):
        item = SimpleNamespace(type="collabAgentToolCall")
        event = SimpleNamespace(
            method="item/started",
            payload=SimpleNamespace(item=item),
        )

        class Turn:
            interrupted = False

            def stream(self):
                return iter((event,))

            def interrupt(self):
                self.interrupted = True

        turn = Turn()
        with self.assertRaisesRegex(
            RuntimeError,
            "native collaboration tools are prohibited",
        ):
            helpers_module._consume_turn_via_stream(
                SimpleNamespace(),
                turn,
            )
        self.assertTrue(turn.interrupted)

    def test_subagent_collaboration_item_becomes_structured_lifecycle_event(self):
        item = SimpleNamespace(
            type="collabAgentToolCall",
            tool=SimpleNamespace(value="spawnAgent"),
            status=SimpleNamespace(value="completed"),
            sender_thread_id="parent-thread",
            receiver_thread_ids=["child-thread"],
            agents_states={
                "child-thread": SimpleNamespace(
                    status=SimpleNamespace(value="running"),
                    message=None,
                ),
            },
            model="gpt-test",
            reasoning_effort=SimpleNamespace(value="high"),
            prompt="inspect the target and report findings",
        )

        event = helpers_module._subagent_stream_event(
            item,
            lifecycle="completed",
        )

        self.assertEqual(event["type"], "subagent.lifecycle")
        self.assertEqual(event["tool"], "spawnAgent")
        self.assertEqual(event["receiver_thread_ids"], ["child-thread"])
        self.assertEqual(event["agent_states"], {"child-thread": "running"})
        self.assertEqual(event["model"], "gpt-test")
        self.assertEqual(event["reasoning_effort"], "high")
        self.assertEqual(
            event["prompt"],
            "inspect the target and report findings",
        )
        activity = helpers_module._stream_activity_from_line(
            json.dumps(event),
            detail="main",
        )
        rendered = helpers_module._format_activity_stdout(activity)
        self.assertIn("[Subagent][Spawn]", rendered)
        self.assertIn("receivers=child-thread", rendered)
        self.assertIn("states=child-thread:running", rendered)
        self.assertIn("model=gpt-test", rendered)
        self.assertIn("effort=high", rendered)
        self.assertIn(
            'prompt="inspect the target and report findings"',
            rendered,
        )

    def test_subagent_input_logs_prompt_when_sdk_provides_it(self):
        item = SimpleNamespace(
            type="collabAgentToolCall",
            tool=SimpleNamespace(value="sendInput"),
            status=SimpleNamespace(value="completed"),
            sender_thread_id="parent-thread",
            receiver_thread_ids=["child-thread"],
            agents_states={},
            model=None,
            reasoning_effort=None,
            prompt='follow up with "exact" evidence',
        )

        event = helpers_module._subagent_stream_event(
            item,
            lifecycle="completed",
        )
        activity = helpers_module._stream_activity_from_line(
            json.dumps(event),
            detail="review",
        )
        rendered = helpers_module._format_activity_stdout(activity)

        self.assertEqual(
            event["prompt"],
            'follow up with "exact" evidence',
        )
        self.assertIn("[Subagent][Input]", rendered)
        self.assertIn(
            'prompt="follow up with \\"exact\\" evidence"',
            rendered,
        )

    def test_subagent_activity_item_logs_start_and_interrupt(self):
        started = SimpleNamespace(
            type="subAgentActivity",
            kind=SimpleNamespace(value="started"),
            agent_thread_id="child-thread",
            agent_path="slot-1",
        )
        interrupted = SimpleNamespace(
            type="subAgentActivity",
            kind=SimpleNamespace(value="interrupted"),
            agent_thread_id="child-thread",
            agent_path="slot-1",
        )

        started_event = helpers_module._subagent_stream_event(
            started,
            lifecycle="completed",
        )
        interrupted_event = helpers_module._subagent_stream_event(
            interrupted,
            lifecycle="completed",
        )

        started_line = helpers_module._format_activity_stdout(
            helpers_module._stream_activity_from_line(
                json.dumps(started_event),
                detail="main",
            )
        )
        interrupted_line = helpers_module._format_activity_stdout(
            helpers_module._stream_activity_from_line(
                json.dumps(interrupted_event),
                detail="main",
            )
        )
        self.assertIn("[Subagent][Start]", started_line)
        self.assertIn("thread=child-thread", started_line)
        self.assertIn("[Subagent][Stop]", interrupted_line)

    def test_subagent_enabled_write_job_warns_that_child_write_ban_is_instruction_only(self):
        client = CodexClient(
            cwd=str(self.project),
            subagents_enabled=True,
        )

        response = client.run(
            input="ECHO_PROMPT",
            write_paths=["out"],
            timeout=3,
        )

        self.assertIn("Only the parent may use file-writing tools", response.content)
        self.assertIn("instruction-level", response.content)

    def test_subagent_client_settings_are_validated(self):
        with self.assertRaisesRegex(ValueError, "positive integer"):
            CodexClient(
                cwd=str(self.project),
                max_concurrent_subagents=0,
            )

    def test_public_session_runs_one_turn_and_reuses_thread_for_protected_turn(self):
        config = SessionConfig(
            workspace=WorkspaceConfig(cwd=self.project),
        )

        with CodexSession(
            config=config,
            input="ECHO_PROMPT first",
        ) as session:
            first = session.run()
            second = session._send_turn("ECHO_PROMPT second")

        self.assertEqual(first.content, "ECHO_PROMPT first")
        self.assertEqual(second.content, "ECHO_PROMPT second")
        self.assertEqual(len(self.thread_start_calls), 1)

    def test_public_session_resume_and_fork_reuse_one_conversation_store(self):
        config = SessionConfig(
            workspace=WorkspaceConfig(cwd=self.project),
        )
        source = CodexSession(config=config, input="ECHO_PROMPT first")
        first = source.run()
        source.close()

        resumed = source.resume(input="ECHO_PROMPT continued")
        branched = source.fork(input="ECHO_PROMPT branched")
        continued = resumed.run()
        branch_response = branched.run()

        self.assertEqual(first.content, "ECHO_PROMPT first")
        self.assertEqual(continued.content, "ECHO_PROMPT continued")
        self.assertEqual(branch_response.content, "ECHO_PROMPT branched")
        self.assertEqual(len(self.thread_start_calls), 1)
        self.assertEqual(len(self.sdk_threads), 2)
        self.assertEqual(
            {
                config.env["CODEX_HOME"]
                for config in self.sdk_configs
            },
            {self.sdk_configs[0].env["CODEX_HOME"]},
        )

    def test_fork_captures_history_at_method_call_time(self):
        config = SessionConfig(
            workspace=WorkspaceConfig(cwd=self.project),
        )
        source = CodexSession(config=config, input="ECHO_PROMPT first")
        source.run()

        branched = source.fork(input="ECHO_PROMPT branch")
        source._send_turn("ECHO_PROMPT after fork")
        branched.run()

        branch_thread = self.sdk_threads["thread-2"]
        self.assertNotIn("ECHO_PROMPT after fork", branch_thread.prompts)
        self.assertIn("ECHO_PROMPT branch", branch_thread.prompts)

    def test_finished_job_deletes_its_store_so_resume_and_fork_are_refused(self):
        config = SessionConfig(
            workspace=WorkspaceConfig(cwd=self.project),
        )
        job = CodexJob(config=config, input="ECHO_PROMPT job")

        response = job.run()

        self.assertIn("ECHO_PROMPT job", response.content)
        with self.assertRaises(CodexSessionClosedError):
            job.resume(input="ECHO_PROMPT continued")
        with self.assertRaises(CodexSessionClosedError):
            job.fork(input="ECHO_PROMPT branched")

    def _assert_no_new_homes(self, before):
        self.assertEqual(set(self._sdk_home_dirs()), before)

    def test_finished_job_removes_home_while_job_is_still_referenced(self):
        before = set(self._sdk_home_dirs())
        gc.disable()
        try:
            job = CodexJob(
                config=SessionConfig(workspace=WorkspaceConfig(cwd=self.project)),
                input="ECHO_PROMPT job",
                review=ReviewConfig(context="REVIEW_SHOULD_PASS"),
            )
            response = job.run()
            self._assert_no_new_homes(before)
        finally:
            gc.enable()
        self.assertTrue(response.content)

    def test_job_removes_homes_with_peer_exchange_and_reused_reviewer(self):
        before = set(self._sdk_home_dirs())
        job = CodexJob(
            config=SessionConfig(workspace=WorkspaceConfig(cwd=self.project)),
            input="REVIEW_TRIGGER_REPAIR",
            review=ReviewConfig(
                context="REVIEW_SHOULD_FAIL_ON_BAD_RESPONSE",
                enable_peer_exchange=True,
                reuse_session=True,
            ),
            validation=ValidationConfig(repair=True),
        )
        gc.disable()
        try:
            job.run()
            self._assert_no_new_homes(before)
        finally:
            gc.enable()

    def test_timed_out_and_cancelled_jobs_remove_home_while_referenced(self):
        before = set(self._sdk_home_dirs())
        config = SessionConfig(workspace=WorkspaceConfig(cwd=self.project))
        timed_out = CodexJob(config=config, input="sleep").start()
        with self.assertRaises(CodexTimeoutError):
            timed_out.result(timeout=0.1)
        timed_out._thread.join(timeout=2)
        cancelled = CodexJob(config=config, input="sleep").start()
        time.sleep(0.1)
        cancelled.cancel()
        with self.assertRaises(CodexCancelledError):
            cancelled.result(timeout=2)
        cancelled._thread.join(timeout=2)
        self._assert_no_new_homes(before)

    def test_pool_batch_removes_every_home_while_results_are_referenced(self):
        before = set(self._sdk_home_dirs())
        config = SessionConfig(workspace=WorkspaceConfig(cwd=self.project))
        jobs = [
            CodexJob(
                config=config,
                input=f"ECHO_PROMPT job {index}",
                review=ReviewConfig(context="REVIEW_SHOULD_PASS"),
            )
            for index in range(4)
        ]
        gc.disable()
        try:
            result = CodexWorkerPool(max_workers=2, warmup_jobs=0).start_many(
                jobs
            ).result(timeout=10)
            self._assert_no_new_homes(before)
        finally:
            gc.enable()
        self.assertEqual(len(result.responses), 4)

    def test_session_resume_keeps_store_until_explicitly_deleted(self):
        config = SessionConfig(workspace=WorkspaceConfig(cwd=self.project))
        source = CodexSession(config=config, input="ECHO_PROMPT first")
        source.run()
        source.close()
        gc.disable()
        try:
            source.resume(input="ECHO_PROMPT again").run()
            self.assertTrue(self._sdk_home_dirs())
            source._conversation_state.store.delete()
        finally:
            gc.enable()

    def test_subagent_enabled_job_removes_isolated_home_after_completion(self):
        before = set(self._sdk_home_dirs())
        client = CodexClient(
            cwd=str(self.project),
            subagents_enabled=True,
        )

        client.run(input="x", timeout=3)
        gc.collect()

        self.assertEqual(set(self._sdk_home_dirs()), before)

    def test_subagent_enabled_timeout_interrupts_and_removes_isolated_home(self):
        before = set(self._sdk_home_dirs())
        client = CodexClient(
            cwd=str(self.project),
            subagents_enabled=True,
        )
        job = client.start(input="sleep")

        with self.assertRaises(CodexTimeoutError):
            job.result(timeout=0.1)

        job._thread.join(timeout=1)
        self.assertFalse(job._thread.is_alive())
        del job
        gc.collect()
        self.assertEqual(set(self._sdk_home_dirs()), before)

    def test_subagent_enabled_cancel_interrupts_and_removes_isolated_home(self):
        before = set(self._sdk_home_dirs())
        client = CodexClient(
            cwd=str(self.project),
            subagents_enabled=True,
        )
        job = client.start(input="sleep")
        time.sleep(0.1)

        job.cancel()

        with self.assertRaises(CodexCancelledError):
            job.result(timeout=1)
        job._thread.join(timeout=1)
        self.assertFalse(job._thread.is_alive())
        del job
        gc.collect()
        self.assertEqual(set(self._sdk_home_dirs()), before)

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

    def test_empty_stream_activity_items_are_suppressed_but_lifecycle_events_remain(self):
        empty_events = (
            {"type": "item.completed", "item_type": "reasoning"},
            {"type": "item.completed", "item_type": "reasoning", "item_text": None},
            {"type": "item.delta", "item_type": "agentMessage", "delta_text": ""},
            {"type": "item.completed", "item_type": "webSearch", "item_text": " \t\n "},
            {"type": "plan.updated", "summary": " "},
        )

        for event in empty_events:
            with self.subTest(event=event):
                self.assertIsNone(
                    helpers_module._stream_activity_from_line(json.dumps(event))
                )

        nonempty_events = (
            ({"type": "item.completed", "item_type": "reasoning", "item_text": "consider options"}, "[Main Attempt][Thinking]"),
            ({"type": "item.completed", "item_type": "agentMessage", "item_text": "answer"}, "[Main Attempt][Message]"),
            ({"type": "item.completed", "item_type": "webSearch", "item_text": "search docs"}, "[Main Attempt][Search]"),
            ({"type": "item.completed", "item_type": "mcpToolCall", "item_text": "run tool"}, "[Main Attempt][Tool]"),
            ({"type": "item.completed", "item_type": "fileChange", "item_text": "update file"}, "[Main Attempt][Edit]"),
            ({"type": "item.completed", "item_type": "commandExecution", "item_text": "$ ls (exit=0)"}, "[Main Attempt][Shell]"),
            ({"type": "thread.started"}, "[Main Attempt][Start]"),
            ({"type": "turn.completed"}, "[Main Attempt][Result]"),
            ({"type": "subagent.lifecycle", "source": "activity", "lifecycle": "completed"}, "[Subagent][Activity]"),
        )
        for event, expected_label in nonempty_events:
            with self.subTest(event=event):
                entry = helpers_module._stream_activity_from_line(json.dumps(event))
                self.assertIsNotNone(entry)
                self.assertIn(expected_label, helpers_module._format_activity_stdout(entry))

    def test_rollouts_are_archived_from_isolated_home(self):
        home = self.project / "home"
        rollout = home / "sessions" / "2026" / "10" / "08" / "rollout-x.jsonl"
        rollout.parent.mkdir(parents=True)
        rollout.write_text('{"type":"response_item"}\n', encoding="utf-8")
        destination = self.project / "logs" / "job" / "rollouts" / "reviewer"
        helpers_module._archive_rollouts(home, destination)
        self.assertEqual((destination / "rollout-x.jsonl").read_text(encoding="utf-8"), '{"type":"response_item"}\n')

    def test_web_search_without_action_details_is_still_logged(self):
        for action in (None, SimpleNamespace(root=SimpleNamespace(type="openPage", url=None)),
                       SimpleNamespace(root=SimpleNamespace(type="other"))):
            with self.subTest(action=action):
                item = SimpleNamespace(type="webSearch", query="", action=action)
                self.assertTrue(helpers_module._stream_item_text(item))

    def test_client_hooks_receive_job_and_read_phase_state(self):
        events = []

        client = CodexClient(cwd=str(self.project))
        client.before_main_attempt = lambda job: events.append(
            ("before_main_attempt", job.id, job.status, job.cwd,
             job.subcwd_path.is_dir(), job.current_attempt, job.repair_index)
        )
        client.after_main_attempt = lambda job: events.append(
            ("after_main_attempt", job.returncode, job.stdout, job.response.content)
        )

        response = client.run(
            input="README を要約して",
            read_paths=["${SUBCWD}/state.json"],
            subcwd_files={"state.json": {"text": "{}\n"}},
            timeout=3,
        )

        self.assertEqual(response.content, "fake final answer")
        event_names = [event[0] for event in events]
        self.assertEqual(
            event_names,
            [
                "before_main_attempt",
                "after_main_attempt",
            ],
        )
        self.assertTrue(events[0][1].startswith("codex_job_"))
        self.assertEqual(events[0][2], "running")
        self.assertEqual(events[0][3], self.project)
        self.assertTrue(events[0][4])
        self.assertEqual(events[0][5], 0)
        self.assertEqual(events[0][6], 0)
        self.assertEqual(events[1][1], 0)
        self.assertIn('"type": "thread.started"', events[1][2])
        self.assertEqual(events[1][3], "fake final answer")

    def test_review_hooks_wrap_decision_correction_turns_once(self):
        events = []

        client = CodexClient(cwd=str(self.project))
        client.before_review = lambda job: events.append("before_review")
        client.after_review = lambda job: events.append("after_review")

        response = client.run(
            input="x",
            review=ReviewConfig(context="REVIEW_FORCE_BAD_OUTPUT"),
            timeout=3,
        )

        self.assertEqual(events, ["before_review", "after_review"])
        self.assertTrue(
            any(
                entry.phase == "review" and entry.event == "invalid"
                for entry in response.activity_log
            )
        )

    def test_after_review_runs_when_review_raises_unexpected_exception(self):
        events = []

        client = CodexClient(cwd=str(self.project))
        client.before_review = lambda job: events.append("before_review")
        client.after_review = lambda job: events.append("after_review")
        def fail_review_session(**_kwargs):
            raise RuntimeError("review runner boom")
        client._create_review_session = fail_review_session

        with self.assertRaises(CodexExecutionError) as caught:
            client.run(
                input="x",
                review=ReviewConfig(context="REVIEW_SHOULD_PASS"),
                timeout=3,
            )

        self.assertEqual(events, ["before_review", "after_review"])
        self.assertIn("review runner boom", caught.exception.message)
        self.assertTrue(
            any(
                entry.phase == "review" and entry.event == "error"
                for entry in caught.exception.activity_log
            )
        )

    def test_regular_phase_hook_exception_fails_job(self):
        self._make_validator(
            "hook_failure_validator.py",
            """
            import sys

            sys.stdin.read()
            sys.exit(0)
            """,
        )

        client = CodexClient(cwd=str(self.project))
        def fail_before_validator(job):
            raise RuntimeError("validator hook boom")
        client.before_validator = fail_before_validator

        with self.assertRaises(CodexExecutionError) as caught:
            client.run(
                input="x",
                validate_output=lambda response: None,
                timeout=3,
            )

        self.assertIn("validator hook boom", caught.exception.message)
        self.assertTrue(
            any(
                entry.phase == "failure"
                and entry.event == "error"
                and "validator hook boom" in entry.message
                for entry in caught.exception.activity_log
            )
        )

    def test_cleanup_hook_exception_is_best_effort(self):
        client = CodexClient(cwd=str(self.project))
        def fail_after_cleanup(job):
            raise RuntimeError("cleanup hook boom")
        client.after_cleanup = fail_after_cleanup

        response = client.run(input="x", timeout=3)

        self.assertEqual(response.content, "fake final answer")
        self.assertTrue(
            any(
                entry.phase == "cleanup"
                and entry.event == "error"
                and "after_cleanup hook failed: cleanup hook boom" in entry.message
                for entry in response.activity_log
            )
        )

    def test_review_config_owns_an_independent_session_config(self):
        review = ReviewConfig(context="x")
        self.assertIsNone(review.session.timeout)
        self.assertEqual(review.session.workspace.read_paths, ())
        self.assertFalse(hasattr(review.session, "subagents_enabled"))
        self.assertTrue(review.reuse_session)
        self.assertFalse(review.include_main_context)
        self.assertFalse(review.enable_peer_exchange)

    def test_peer_exchange_is_not_added_when_disabled(self):
        client = CodexClient(cwd=str(self.project))

        response = client.run(
            input="x",
            review=ReviewConfig(context="REVIEW_SHOULD_PASS"),
            timeout=3,
        )

        self.assertEqual(response.content, "fake final answer")
        self.assertFalse(
            any(
                "codex_job_runner_peer_exchange" in override
                for config in self.sdk_configs
                for override in config.config_overrides
            )
        )

    def test_peer_exchange_flag_must_be_boolean(self):
        client = CodexClient(cwd=str(self.project))

        with self.assertRaisesRegex(
            ValueError,
            "review.enable_peer_exchange must be a bool",
        ):
            client.run(
                input="x",
                review=ReviewConfig(
                    context="REVIEW_SHOULD_PASS",
                    enable_peer_exchange="yes",
                ),
                timeout=3,
            )

    def test_peer_exchange_forks_each_parent_without_mutating_it(self):
        captured = {}
        real_bridge = client_module._ToolBridge

        class CapturingBridge(real_bridge):
            def __init__(self, tools, **kwargs):
                names = tuple(tool.name for tool in tools)
                if names == ("ask_peer",):
                    captured["peer_tool"] = tuple(tools)[0]
                super().__init__(tools, **kwargs)

        def user_tool_handler(value: str) -> str:
            return value

        user_tool = Tool(
            user_tool_handler,
            "Echo one value.",
            args={"value": "The value to echo."},
            returns="The echoed value.",
            name="user_echo",
        )
        config = SessionConfig(
            tools=ToolConfig(tools=(user_tool,)),
            workspace=WorkspaceConfig(cwd=self.project),
        )
        exchanges = []
        invoked = {"initial_main": False, "reviewer": False, "main": False}

        def invoke_peer(caller, question):
            tool = captured["peer_tool"]
            before = {
                thread_id: list(thread.prompts)
                for thread_id, thread in self.sdk_threads.items()
            }
            result = tool.invoke({"message": question})
            after_ids = set(self.sdk_threads) - set(before)
            self.assertEqual(len(after_ids), 1)
            fork_id = after_ids.pop()
            for thread_id, prompts in before.items():
                self.assertEqual(self.sdk_threads[thread_id].prompts, prompts)
            self.assertEqual(self.sdk_threads[fork_id].prompts[-1], question)
            exchanges.append((caller, result, fork_id))

        def on_fake_turn(prompt, context_prompt):
            if (
                "REVIEW_TRIGGER_REPAIR" in context_prompt
                and "Internal review mode: codex_job_runner" not in context_prompt
                and not self._is_repair_prompt(context_prompt)
                and not invoked["initial_main"]
            ):
                invoked["initial_main"] = True
                thread_count = len(self.sdk_threads)
                with self.assertRaisesRegex(
                    Exception,
                    "RuntimeError",
                ):
                    captured["peer_tool"].invoke(
                        {"message": "reviewer available yet?"}
                    )
                self.assertEqual(len(self.sdk_threads), thread_count)
            if (
                "You are a peer reviewer. Return only a strict JSON object" in context_prompt
                and not invoked["reviewer"]
            ):
                invoked["reviewer"] = True
                invoke_peer("reviewer", "question from reviewer")
            if self._is_repair_prompt(context_prompt) and not invoked["main"]:
                invoked["main"] = True
                invoke_peer("main", "question from main")

        self.fake_turn_callback = on_fake_turn
        with mock.patch.object(client_module, "_ToolBridge", CapturingBridge):
            response = CodexJob(
                config=config,
                input="REVIEW_TRIGGER_REPAIR",
                review=ReviewConfig(
                    context="REVIEW_SHOULD_FAIL_ON_BAD_RESPONSE",
                    enable_peer_exchange=True,
                    reuse_session=True,
                ),
                validation=ValidationConfig(repair=True),
            ).run()

        self.assertEqual(response.content, "review repaired answer")
        self.assertEqual(
            captured["peer_tool"].prompt,
            "Ask the other role one question using its latest completed "
            "conversation. The other role answers once on a temporary "
            "restricted fork without configured write paths. The exchange "
            "is not recorded in the other role's original conversation, "
            "so do not use it to establish agreement or obtain approval.",
        )
        self.assertEqual([caller for caller, _result, _fork in exchanges], [
            "reviewer",
            "main",
        ])
        self.assertTrue(all(isinstance(result, str) for _, result, _ in exchanges))
        peer_configs = [
            config
            for config in self.sdk_configs
            if any(
                "mcp_servers.codex_job_runner_peer_exchange=" in override
                for override in config.config_overrides
            )
        ]
        fork_configs = [
            config
            for config in self.sdk_configs
            if config not in peer_configs
        ]
        self.assertEqual(len(peer_configs), 2)
        self.assertEqual(
            sum(
                any(
                    "mcp_servers.codex_job_runner={" in override
                    for override in config.config_overrides
                )
                for config in peer_configs
            ),
            1,
        )
        self.assertEqual(len(fork_configs), 2)
        for fork_config in fork_configs:
            self.assertIn("mcp_servers={}", fork_config.config_overrides)
            self.assertIn("features.hooks=false", fork_config.config_overrides)
            self.assertIn(
                "features.multi_agent=false",
                fork_config.config_overrides,
            )
        reviewer_to_main_fork, main_to_reviewer_fork = fork_configs
        self.assertNotIn(
            "features.shell_tool=false",
            reviewer_to_main_fork.config_overrides,
        )
        self.assertNotIn(
            "allow_login_shell=false",
            reviewer_to_main_fork.config_overrides,
        )
        self.assertIn(
            "features.shell_tool=false",
            main_to_reviewer_fork.config_overrides,
        )
        self.assertIn(
            "allow_login_shell=false",
            main_to_reviewer_fork.config_overrides,
        )
        self.assertTrue(
            any(entry.phase == "peer_exchange" for entry in response.activity_log)
        )
        exchange_events = [
            entry.message
            for entry in response.activity_log
            if entry.phase == "peer_exchange"
            and entry.event in {"start", "end"}
        ]
        self.assertEqual(len(exchange_events), 4)
        self.assertTrue(
            all(
                "source_thread=thread-" in message
                and "fork_thread=thread-" in message
                for message in exchange_events
            )
        )
        self.assertEqual(response.usage["input_tokens"], 800)
        self.assertEqual(response.usage["output_tokens"], 80)

    def test_peer_exchange_uses_reused_reviewer_without_mutating_it(self):
        captured = {}
        real_bridge = client_module._ToolBridge

        class CapturingBridge(real_bridge):
            def __init__(self, tools, **kwargs):
                names = tuple(tool.name for tool in tools)
                if names == ("ask_peer",):
                    captured["peer_tool"] = tuple(tools)[0]
                super().__init__(tools, **kwargs)

        invoked = False
        fork_id = None

        def on_fake_turn(prompt, context_prompt):
            nonlocal invoked, fork_id
            if not self._is_repair_prompt(context_prompt) or invoked:
                return
            invoked = True
            before = {
                thread_id: list(thread.prompts)
                for thread_id, thread in self.sdk_threads.items()
            }
            result = captured["peer_tool"].invoke(
                {"message": "question for reused reviewer"}
            )
            self.assertIsInstance(result, str)
            after_ids = set(self.sdk_threads) - set(before)
            self.assertEqual(len(after_ids), 1)
            fork_id = after_ids.pop()
            for thread_id, prompts in before.items():
                self.assertEqual(self.sdk_threads[thread_id].prompts, prompts)

        self.fake_turn_callback = on_fake_turn
        with mock.patch.object(client_module, "_ToolBridge", CapturingBridge):
            response = CodexJob(
                config=SessionConfig(
                    workspace=WorkspaceConfig(cwd=self.project),
                ),
                input="REVIEW_TRIGGER_REPAIR",
                review=ReviewConfig(
                    context="REVIEW_SHOULD_FAIL_ON_BAD_RESPONSE",
                    enable_peer_exchange=True,
                    reuse_session=True,
                ),
                validation=ValidationConfig(repair=True),
            ).run()

        self.assertEqual(response.content, "review repaired answer")
        self.assertTrue(invoked)
        self.assertIsNotNone(fork_id)
        self.assertEqual(
            self.sdk_threads[fork_id].prompts[-1],
            "question for reused reviewer",
        )
        self.assertEqual(response.usage["input_tokens"], 700)
        self.assertEqual(response.usage["output_tokens"], 70)

    def test_review_session_uses_codex_session_without_replaying_history(self):
        client = CodexClient(cwd=str(self.project))

        session = client.start_review_session(
            review=ReviewConfig(context="Be precise."),
            read_paths=["README.md"],
        )
        try:
            first = session.send("ECHO_PROMPT first reviewer turn")
            second = session.send("ECHO_PROMPT second reviewer turn")
        finally:
            session.close()

        self.assertEqual(first.content, "ECHO_PROMPT first reviewer turn")
        self.assertEqual(second.content, "ECHO_PROMPT second reviewer turn")
        self.assertIsInstance(session, CodexSession)
        self.assertEqual(
            session._conversation_state.completed_turns(),
            (
                ("ECHO_PROMPT first reviewer turn", "ECHO_PROMPT first reviewer turn"),
                ("ECHO_PROMPT second reviewer turn", "ECHO_PROMPT second reviewer turn"),
            ),
        )

    def test_review_session_uses_codex_session_send_signature(self):
        parameters = inspect.signature(CodexSession.send).parameters

        self.assertIn("input", parameters)
        self.assertIn("output_schema_path", parameters)
        self.assertNotIn("activity_callback", parameters)
        self.assertNotIn("_activity_callback", parameters)

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
                captured["sdk_config"] = config
                self.config = config

            def thread_start(self, **kwargs):
                captured["thread_start_calls"] += 1
                captured["thread_start_kwargs"] = kwargs
                return FakeThread()

            def close(self):
                captured["closed"] = True

        fake_sdk = SimpleNamespace(
            Codex=FakeCodex,
            CodexConfig=lambda **kwargs: SimpleNamespace(**kwargs),
            Sandbox=SimpleNamespace(read_only="read_only", workspace_write="workspace_write"),
            ApprovalMode=_FAKE_APPROVAL_MODE,
        )

        previous_import = client_module._import_openai_codex
        client_module._import_openai_codex = lambda: fake_sdk
        try:
            client = CodexClient(cwd=str(self.project))
            session = client.start_review_session(
                review=ReviewConfig(context="Use conversation history."),
            )
            try:
                first = session.send("first question")
                second = session.send("second question")
            finally:
                session.close()
        finally:
            client_module._import_openai_codex = previous_import

        self.assertEqual(first.content, "reply:first question")
        self.assertEqual(second.content, "reply:second question")
        self.assertEqual(captured["thread_start_calls"], 1)
        self.assertEqual(captured["thread_start_kwargs"]["approval_mode"], "deny_all")
        developer_instructions = captured["thread_start_kwargs"]["developer_instructions"]
        self.assertEqual(developer_instructions.count("Use conversation history."), 1)
        self.assertFalse(
            any(
                "Use conversation history." in prompt
                for prompt in captured["turn_prompts"]
            )
        )
        for override in client_module._RUNNER_ALWAYS_OFF_CONFIG_OVERRIDES:
            self.assertIn(override, captured["sdk_config"].config_overrides)
        self.assertFalse(
            any(
                item.startswith("features.code_mode")
                for item in captured["sdk_config"].config_overrides
            )
        )
        self.assertEqual(captured["turn_prompts"][0], "first question")
        self.assertEqual(captured["turn_prompts"][1], "second question")
        self.assertTrue(captured.get("closed"))

    def test_review_session_send_normalizes_cwd_relative_output_schema_path(self):
        captured = {}

        class FakeTurnResult:
            final_response = '{"ok": true}'
            usage = None
            items = []

        class FakeTurn:
            def run(self):
                return FakeTurnResult()

        class FakeThread:
            def turn(self, prompt, **kwargs):
                captured["output_schema"] = kwargs["output_schema"]
                return FakeTurn()

        class FakeCodex:
            def __init__(self, config=None):
                self.config = config

            def thread_start(self, **kwargs):
                return FakeThread()

            def close(self):
                pass

        fake_sdk = SimpleNamespace(
            Codex=FakeCodex,
            CodexConfig=lambda **kwargs: SimpleNamespace(**kwargs),
            Sandbox=SimpleNamespace(read_only="read_only", workspace_write="workspace_write"),
            ApprovalMode=_FAKE_APPROVAL_MODE,
        )

        previous_import = client_module._import_openai_codex
        client_module._import_openai_codex = lambda: fake_sdk
        try:
            client = CodexClient(cwd=str(self.project))
            session = client.start_review_session(
                review=ReviewConfig(context="Use schema."),
            )
            try:
                response = session.send(
                    "return json",
                    output_schema_path="result.schema.json",
                )
            finally:
                session.close()
        finally:
            client_module._import_openai_codex = previous_import

        self.assertEqual(response.content, '{"ok": true}')
        self.assertEqual(captured["output_schema"], {"type": "object"})

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
                SimpleNamespace(
                    root=SimpleNamespace(
                        type="commandExecution",
                        command="curl -sL https://example.com/recruit",
                        exit_code=0,
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
            ApprovalMode=_FAKE_APPROVAL_MODE,
        )

        previous_import = client_module._import_openai_codex
        client_module._import_openai_codex = lambda: fake_sdk
        try:
            client = CodexClient(
                cwd=str(self.project),
                ignore_global_context=True,
                model="gpt-5.4-mini",
            )
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
        self.assertAlmostEqual(response.estimated_cost_usd, 0.0000204)
        self.assertNotIn("sandbox", captured["thread_start_kwargs"])
        self.assertFalse(captured["thread_start_kwargs"]["ephemeral"])
        self.assertEqual(captured["thread_start_kwargs"]["approval_mode"], "deny_all")
        self.assertNotIn("sandbox", captured["turn_kwargs"])
        self.assertEqual(captured["turn_kwargs"]["output_schema"], {"type": "object"})
        for override in client_module._RUNNER_ALWAYS_OFF_CONFIG_OVERRIDES:
            self.assertIn(override, captured["sdk_config"].config_overrides)
        self.assertFalse(
            any(
                item.startswith("features.code_mode")
                for item in captured["sdk_config"].config_overrides
            )
        )
        default_permission_overrides = [
            override
            for override in captured["sdk_config"].config_overrides
            if override.startswith("default_permissions=")
        ]
        self.assertEqual(len(default_permission_overrides), 1)
        filesystem_overrides = [
            override
            for override in captured["sdk_config"].config_overrides
            if override.startswith("permissions.")
        ]
        self.assertEqual(len(filesystem_overrides), 1)
        self.assertEqual(pathlib.Path(captured["sdk_config"].cwd), self.project)
        self.assertIn(f'"{self.project}"={{', filesystem_overrides[0])
        self.assertIn('"out"="write"', filesystem_overrides[0])
        self.assertIn("project_doc_max_bytes=0", captured["sdk_config"].config_overrides)
        self.assertIn("features.hooks=false", captured["sdk_config"].config_overrides)
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
        self.assertTrue(
            any(
                "item.completed (commandExecution): $ curl -sL https://example.com/recruit (exit=0)" in message
                for message in stream_messages
            )
        )
        printed = response.stdout
        self.assertIn('"type": "item.completed", "item_type": "agentMessage"', printed)

    def test_permission_profile_uses_cwd_root_with_exact_descendant_rule(self):
        section = self.project / "section1.tex"
        section.write_text("text\n", encoding="utf-8")

        profile = client_module._filesystem_permission_map(
            self.project, [], [section]
        )

        self.assertEqual(profile[str(self.project)], {"section1.tex": "write"})
        self.assertNotIn(":workspace_roots", profile)
        self.assertNotIn(".", profile[str(self.project)])
        self.assertNotIn(str(section), profile)

    def test_runtime_version_parses_explicit_codex_bin_with_argv(self):
        captured = []
        previous_run = helpers_module.subprocess.run
        helpers_module.subprocess.run = lambda argv, **kwargs: (
            captured.append((argv, kwargs))
            or SimpleNamespace(
                returncode=0,
                stdout="codex-cli 0.144.0\n",
                stderr="",
            )
        )
        try:
            version, argv = self._previous_runtime_version(self.runtime_bin)
        finally:
            helpers_module.subprocess.run = previous_run

        self.assertEqual(version, "0.144.0")
        self.assertEqual(argv, [str(self.runtime_bin), "--version"])
        self.assertEqual(captured[0][0], argv)
        self.assertFalse(captured[0][1]["check"])

    def test_bundled_runtime_path_is_used_for_version_probe(self):
        runtime_path = helpers_module._selected_codex_runtime_path(None)
        self.assertIsNotNone(runtime_path)
        captured = []
        previous_run = helpers_module.subprocess.run
        helpers_module.subprocess.run = lambda argv, **kwargs: (
            captured.append(argv)
            or SimpleNamespace(
                returncode=0,
                stdout="codex-cli 0.137.0-alpha.4\n",
                stderr="",
            )
        )
        try:
            version, argv = self._previous_runtime_version(runtime_path)
        finally:
            helpers_module.subprocess.run = previous_run

        self.assertEqual(version, "0.137.0-alpha.4")
        self.assertEqual(argv, [str(runtime_path), "--version"])
        self.assertEqual(captured, [argv])

    def test_linux_unverified_0_144_exact_file_fails_synchronously(self):
        allowed = self.project / "allowed.txt"
        allowed.write_text("before\n", encoding="utf-8")
        helpers_module._codex_runtime_version = lambda runtime_path: (
            "0.144.0",
            [str(runtime_path), "--version"],
        )
        helpers_module._EXACT_FILE_WRITE_SUPPORTED_LINUX_CODEX_VERSIONS = frozenset()
        client = CodexClient(cwd=str(self.project), codex_bin=str(self.runtime_bin))

        with self.assertRaises(CodexExecutionError) as caught:
            client.start(input="x", write_paths=["allowed.txt"])

        self.assertEqual(self.sdk_configs, [])
        self.assertEqual(allowed.read_text(encoding="utf-8"), "before\n")
        self.assertIn("runtime_version=0.144.0", caught.exception.message)

    def test_staged_subcwd_exact_file_gate_failure_cleans_up(self):
        helpers_module._EXACT_FILE_WRITE_SUPPORTED_LINUX_CODEX_VERSIONS = frozenset()
        client = CodexClient(cwd=str(self.project))

        with self.assertRaises(CodexExecutionError):
            client.start(
                input="x",
                subcwd_files={"allowed.txt": {"text": "before\n"}},
                write_paths=["${SUBCWD}/allowed.txt"],
            )

        self.assertFalse(self._subcwd_dirs())
        self.assertEqual(self.sdk_configs, [])

    def test_linux_nonexistent_write_path_fails_closed(self):
        client = CodexClient(cwd=str(self.project))

        with self.assertRaises(CodexExecutionError) as caught:
            client.start(input="x", write_paths=["missing-output"])

        self.assertIn("missing-output [unknown]", caught.exception.message)
        self.assertEqual(self.sdk_configs, [])

    def test_existing_directory_write_bypasses_exact_file_gate(self):
        helpers_module._codex_runtime_version = lambda runtime_path: self.fail(
            "directory-only writes must not probe the runtime version"
        )
        client = CodexClient(cwd=str(self.project))

        response = client.run(
            input="x",
            write_paths=["out"],
            timeout=3,
        )

        self.assertEqual(response.content, "fake final answer")

    def test_mixed_directory_and_exact_file_fails_before_sdk_start(self):
        allowed = self.project / "allowed.txt"
        allowed.write_text("before\n", encoding="utf-8")
        helpers_module._EXACT_FILE_WRITE_SUPPORTED_LINUX_CODEX_VERSIONS = frozenset()
        client = CodexClient(cwd=str(self.project))

        with self.assertRaises(CodexExecutionError):
            client.start(
                input="EDIT_ALLOWED_FILE",
                write_paths=["out", "allowed.txt"],
            )

        self.assertEqual(self.sdk_configs, [])
        self.assertEqual(allowed.read_text(encoding="utf-8"), "before\n")

    def test_read_only_job_is_outside_exact_file_gate(self):
        helpers_module._codex_runtime_version = lambda runtime_path: self.fail(
            "read-only jobs must not probe the runtime version"
        )
        client = CodexClient(cwd=str(self.project))

        response = client.run(
            input="x",
            read_paths=["README.md"],
            timeout=3,
        )

        self.assertEqual(response.content, "fake final answer")

    def test_symlink_write_path_classification_and_boundary(self):
        target_file = self.project / "target.txt"
        target_file.write_text("text\n", encoding="utf-8")
        file_link = self.project / "file-link"
        file_link.symlink_to(target_file)
        directory_link = self.project / "directory-link"
        directory_link.symlink_to(self.project / "out", target_is_directory=True)
        broken_link = self.project / "broken-link"
        broken_link.symlink_to(self.project / "missing-target")
        outside_target = self.tmp_path / "outside.txt"
        outside_target.write_text("outside\n", encoding="utf-8")
        outside_link = self.project / "outside-link"
        outside_link.symlink_to(outside_target)
        client = CodexClient(cwd=str(self.project))

        for path, kind in ((file_link, "symlink-to-file"), (broken_link, "unknown")):
            with self.subTest(path=path):
                with self.assertRaises(CodexExecutionError) as caught:
                    client.start(input="x", write_paths=[path.name])
                self.assertIn(kind, caught.exception.message)

        response = client.run(
            input="x",
            write_paths=[directory_link.name],
            timeout=3,
        )
        self.assertEqual(response.content, "fake final answer")
        with self.assertRaises(ValueError):
            client.start(input="x", write_paths=[outside_link.name])

    def test_gate_failure_does_not_start_sdk_or_later_phases(self):
        marker = self.project / "validator-ran"
        self._make_validator(
            "gate_validator.py",
            f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n",
        )
        allowed = self.project / "allowed.txt"
        allowed.write_text("before\n", encoding="utf-8")
        helpers_module._EXACT_FILE_WRITE_SUPPORTED_LINUX_CODEX_VERSIONS = frozenset()
        client = CodexClient(cwd=str(self.project))

        with self.assertRaises(CodexExecutionError):
            client.start(
                input="x",
                write_paths=["allowed.txt"],
                validate_output=lambda response: None,
                review=ReviewConfig(context="review"),
                collect_files=True,
            )

        self.assertEqual(self.sdk_configs, [])
        self.assertEqual(self.thread_start_calls, [])
        self.assertEqual(self.turn_calls, [])
        self.assertFalse(marker.exists())

    def test_gate_diagnostic_includes_runtime_platform_path_and_offenders(self):
        allowed = self.project / "allowed.txt"
        allowed.write_text("before\n", encoding="utf-8")
        helpers_module._codex_runtime_version = lambda runtime_path: (
            None,
            [str(runtime_path), "--version"],
        )
        client = CodexClient(cwd=str(self.project), codex_bin=str(self.runtime_bin))

        with self.assertRaises(CodexExecutionError) as caught:
            client.start(input="x", write_paths=["allowed.txt"])

        message = caught.exception.message
        self.assertIn("platform=linux", message)
        self.assertIn(f"runtime_path={self.runtime_bin}", message)
        self.assertIn("runtime_version=<unavailable>", message)
        self.assertIn("allowed.txt [regular-file]", message)
        self.assertEqual(caught.exception.argv, [str(self.runtime_bin), "--version"])

    def test_unknown_runtime_path_fails_closed(self):
        allowed = self.project / "allowed.txt"
        allowed.write_text("before\n", encoding="utf-8")
        previous_selected = helpers_module._selected_codex_runtime_path
        helpers_module._selected_codex_runtime_path = lambda codex_bin: None
        try:
            with self.assertRaises(CodexExecutionError) as caught:
                CodexClient(cwd=str(self.project)).start(
                    input="x",
                    write_paths=["allowed.txt"],
                )
        finally:
            helpers_module._selected_codex_runtime_path = previous_selected

        self.assertIn("runtime_path=<unknown>", caught.exception.message)
        self.assertIn("runtime_version=<unavailable>", caught.exception.message)
        self.assertEqual(self.sdk_configs, [])

    def test_non_linux_preserves_existing_exact_file_behavior(self):
        allowed = self.project / "allowed.txt"
        allowed.write_text("before\n", encoding="utf-8")
        previous_platform = helpers_module.sys.platform
        helpers_module.sys.platform = "darwin"
        helpers_module._codex_runtime_version = lambda runtime_path: self.fail(
            "non-Linux runtimes are not part of this gate"
        )
        try:
            response = CodexClient(cwd=str(self.project)).run(
                input="EDIT_ALLOWED_FILE",
                write_paths=["allowed.txt"],
                timeout=3,
            )
        finally:
            helpers_module.sys.platform = previous_platform

        self.assertEqual(response.content, "fake final answer")
        self.assertEqual(allowed.read_text(encoding="utf-8"), "updated\n")

    def test_supported_runtime_keeps_nested_profile_argv_and_response(self):
        allowed = self.project / "allowed.txt"
        allowed.write_text("before\n", encoding="utf-8")
        client = CodexClient(cwd=str(self.project))

        response = client.run(
            input="EDIT_ALLOWED_FILE",
            write_paths=["allowed.txt"],
            timeout=3,
        )

        filesystem_arg = next(
            item for item in response.argv if item.startswith("permissions.")
        )
        parsed = tomllib.loads(
            "filesystem = " + filesystem_arg.split("=", 1)[1]
        )["filesystem"]
        self.assertEqual(parsed[str(self.project)], {"allowed.txt": "write"})
        self.assertEqual(response.content, "fake final answer")
        self.assertEqual(allowed.read_text(encoding="utf-8"), "updated\n")

    def test_permission_profile_exact_file_does_not_grant_sibling(self):
        allowed = self.project / "section1.tex"
        sibling = self.project / "section2.tex"
        allowed.write_text("one\n", encoding="utf-8")
        sibling.write_text("two\n", encoding="utf-8")

        profile = client_module._filesystem_permission_map(
            self.project, [], [allowed]
        )

        self.assertNotIn("section2.tex", profile[str(self.project)])

    def test_permission_profile_directory_rules_cover_subtrees(self):
        read_dir = self.project / "read-dir"
        write_dir = self.project / "write-dir"
        read_dir.mkdir()
        write_dir.mkdir()

        profile = client_module._filesystem_permission_map(
            self.project, [read_dir], [write_dir]
        )

        self.assertEqual(
            profile[str(self.project)],
            {"read-dir": "read", "write-dir": "write"},
        )

    def test_permission_profile_supports_nonexistent_descendants(self):
        profile = client_module._filesystem_permission_map(
            self.project,
            [self.project / "missing-input.txt"],
            [self.project / "missing" / "output.txt"],
        )

        self.assertEqual(
            profile[str(self.project)],
            {
                "missing-input.txt": "read",
                "missing/output.txt": "write",
            },
        )

    def test_permission_profile_only_adds_dot_for_explicit_cwd(self):
        implicit = client_module._filesystem_permission_map(
            self.project,
            [self.project / "README.md"],
            [],
        )
        explicit_read = client_module._filesystem_permission_map(
            self.project,
            [self.project],
            [],
        )
        explicit_write = client_module._filesystem_permission_map(
            self.project,
            [],
            [self.project],
        )
        runtime_only = client_module._filesystem_permission_map(
            self.project,
            [],
            [],
            extra_read_paths=[self.project],
        )

        self.assertNotIn(".", implicit[str(self.project)])
        self.assertEqual(explicit_read[str(self.project)]["."], "read")
        self.assertEqual(explicit_write[str(self.project)]["."], "write")
        self.assertNotIn(str(self.project), runtime_only)

    def test_permission_profile_keeps_read_parent_and_write_child(self):
        read_dir = self.project / "tree"
        write_file = read_dir / "output.txt"

        profile = client_module._filesystem_permission_map(
            self.project,
            [read_dir],
            [write_file],
        )

        self.assertEqual(
            profile[str(self.project)],
            {"tree": "read", "tree/output.txt": "write"},
        )

    def test_failed_job_can_leave_direct_write(self):
        allowed = self.project / "allowed.txt"
        allowed.write_text("original\n", encoding="utf-8")
        client = CodexClient(cwd=str(self.project))

        job = client.start(
            input="EDIT_ALLOWED_FILE FORCE_FAIL",
            write_paths=["allowed.txt"],
        )
        with self.assertRaises(CodexExecutionError):
            job.result(timeout=3)

        self.assertEqual(allowed.read_text(encoding="utf-8"), "updated\n")

    def test_collection_failure_can_leave_direct_write(self):
        allowed = self.project / "allowed.txt"
        allowed.write_text("original\n", encoding="utf-8")
        client = CodexClient(cwd=str(self.project))

        job = client.start(
            input="EDIT_ALLOWED_FILE MAKE_SUBCWD_FILES",
            subcwd_files={
                "report.md": {"text": ""},
                "data.json": {"text": "{}\n"},
            },
            write_paths=[
                "allowed.txt",
                "${SUBCWD}/report.md",
                "${SUBCWD}/data.json",
            ],
            collect_files=True,
            max_file_bytes=1,
        )
        with self.assertRaises(CodexExecutionError):
            job.result(timeout=3)

        self.assertEqual(allowed.read_text(encoding="utf-8"), "updated\n")

    def test_write_parent_is_not_downgraded_by_read_child(self):
        write_dir = self.project / "write-dir"
        write_dir.mkdir()
        child = write_dir / "child.txt"
        child.write_text("text\n", encoding="utf-8")

        profile = client_module._filesystem_permission_map(
            self.project, [child], [write_dir]
        )

        self.assertEqual(profile[str(self.project)]["write-dir"], "write")
        self.assertNotIn("write-dir/child.txt", profile[str(self.project)])

    def test_runtime_reads_merge_inside_cwd_and_remain_flat_outside(self):
        write_dir = self.project / "write-dir"
        runtime_inside = write_dir / "runtime"
        runtime_sibling = self.project / "runtime-sibling"
        runtime_outside = self.tmp_path / "runtime-outside"

        profile = client_module._filesystem_permission_map(
            self.project,
            [],
            [write_dir],
            extra_read_paths=[runtime_inside, runtime_sibling, runtime_outside],
        )

        self.assertEqual(
            profile[str(self.project)],
            {"runtime-sibling": "read", "write-dir": "write"},
        )
        self.assertEqual(profile[str(runtime_outside)], "read")

    def test_nested_inline_toml_is_recursive_quoted_and_parseable(self):
        section = self.project / 'section "one".tex'
        rendered = client_module._filesystem_inline_table(
            self.project,
            [self.project / "inputs"],
            [section],
        )

        parsed = tomllib.loads("filesystem = " + rendered)["filesystem"]

        self.assertEqual(
            parsed,
            {
                ":minimal": "read",
                str(self.project): {
                    "inputs": "read",
                    'section "one".tex': "write",
                },
            },
        )

    def test_nested_inline_toml_sort_is_deterministic(self):
        first = client_module._filesystem_inline_table(
            self.project,
            [self.project / "z", self.project / "a"],
            [self.project / "m"],
        )
        second = client_module._filesystem_inline_table(
            self.project,
            [self.project / "a", self.project / "z"],
            [self.project / "m"],
        )

        self.assertEqual(first, second)
        self.assertLess(first.index('"a"="read"'), first.index('"z"="read"'))

    def test_sdk_home_copies_only_authentication_from_source_home(self):
        source_home = self.tmp_path / "source-codex-home"
        source_home.mkdir()
        (source_home / "auth.json").write_text('{"token":"sentinel"}\n', encoding="utf-8")
        (source_home / "config.toml").write_text(
            'model = "gpt-test"\n'
            'sandbox_mode = "danger-full-access"\n'
            '[sandbox_workspace_write]\n'
            'writable_roots = ["/tmp"]\n'
            '[features]\n'
            'hooks = true\n',
            encoding="utf-8",
        )
        (source_home / "AGENTS.md").write_text(
            "GLOBAL_AGENT_SENTINEL\n",
            encoding="utf-8",
        )
        (source_home / "installation_id").write_text(
            "installation-sentinel\n",
            encoding="utf-8",
        )
        (source_home / "models_cache.json").write_text("{}\n", encoding="utf-8")
        (source_home / "skills").mkdir()

        env, isolated_home = client_module._permission_profile_sdk_env(
            {"CODEX_HOME": str(source_home)}
        )
        try:
            isolated_entries = sorted(path.name for path in isolated_home.iterdir())
            copied_auth = (pathlib.Path(env["CODEX_HOME"]) / "auth.json").read_text(
                encoding="utf-8"
            )
        finally:
            client_module._cleanup_tree(isolated_home)

        self.assertEqual(isolated_entries, ["auth.json", "sqlite"])
        self.assertEqual(copied_auth, '{"token":"sentinel"}\n')

    def test_sdk_home_and_sqlite_state_are_isolated_in_tmpfs(self):
        source_home = self.tmp_path / "source-codex-home"
        source_home.mkdir()
        (source_home / "auth.json").write_text("{}\n", encoding="utf-8")

        first_env, first_home = client_module._permission_profile_sdk_env(
            {"CODEX_HOME": str(source_home)}
        )
        second_env, second_home = client_module._permission_profile_sdk_env(
            {"CODEX_HOME": str(source_home)}
        )
        try:
            first_sqlite_home = pathlib.Path(first_env["CODEX_SQLITE_HOME"])
            second_sqlite_home = pathlib.Path(second_env["CODEX_SQLITE_HOME"])
            self.assertEqual(first_home.parent, pathlib.Path("/dev/shm"))
            self.assertEqual(second_home.parent, pathlib.Path("/dev/shm"))
            self.assertNotEqual(first_home, second_home)
            self.assertEqual(first_sqlite_home, first_home / "sqlite")
            self.assertEqual(second_sqlite_home, second_home / "sqlite")
            self.assertNotEqual(first_sqlite_home, second_sqlite_home)
            self.assertTrue(first_sqlite_home.is_dir())
            self.assertTrue(second_sqlite_home.is_dir())
            self.assertEqual(stat.S_IMODE(first_home.stat().st_mode), 0o700)
            self.assertEqual(
                stat.S_IMODE(first_sqlite_home.stat().st_mode),
                0o700,
            )
            self.assertTrue((first_home / "auth.json").is_file())
            self.assertFalse((first_home / "auth.json").is_symlink())
        finally:
            client_module._cleanup_tree(first_home)
            client_module._cleanup_tree(second_home)

        self.assertFalse(first_home.exists())
        self.assertFalse(second_home.exists())

    def test_sdk_home_fails_closed_when_tmpfs_is_unavailable(self):
        previous_root = helpers_module._SDK_SESSION_TMPFS_ROOT
        helpers_module._SDK_SESSION_TMPFS_ROOT = self.tmp_path / "missing-tmpfs"
        try:
            with self.assertRaisesRegex(
                RuntimeError,
                "requires writable tmpfs",
            ):
                client_module._permission_profile_sdk_env({})
        finally:
            helpers_module._SDK_SESSION_TMPFS_ROOT = previous_root

    def test_dotted_and_multiline_legacy_sandbox_values_are_removed(self):
        rendered = client_module._without_legacy_sandbox_config(
            'profiles.strict.sandbox_mode = "danger-full-access"\n'
            'profiles.strict.sandbox_workspace_write = { writable_roots = [\n'
            '  "/tmp",\n'
            '] }\n'
            '[profiles.strict]\n'
            'model = "gpt-profile"\n'
            '[features]\n'
            'hooks = true\n'
        )

        parsed = tomllib.loads(rendered)

        self.assertNotIn("sandbox_mode", rendered)
        self.assertNotIn("sandbox_workspace_write", rendered)
        self.assertEqual(parsed["profiles"]["strict"]["model"], "gpt-profile")
        self.assertTrue(parsed["features"]["hooks"])

    def test_permission_profile_names_and_paths_are_preserved_by_sandbox_scrub(self):
        source = (
            '[permissions.sandbox_workspace_write.filesystem]\n'
            '"/work/sandbox_mode" = "read"\n'
            '"sandbox_workspace_write" = "write"\n'
        )

        rendered = client_module._without_legacy_sandbox_config(source)

        self.assertEqual(rendered, source)
        self.assertEqual(
            tomllib.loads(rendered)["permissions"]["sandbox_workspace_write"]
            ["filesystem"]["sandbox_workspace_write"],
            "write",
        )

    def test_main_and_reviewer_are_profile_only(self):
        source_home = self.tmp_path / "source-codex-home"
        source_home.mkdir()
        (source_home / "config.toml").write_text(
            'sqlite_home = "/shared/sqlite"\n',
            encoding="utf-8",
        )
        client = CodexClient(
            cwd=str(self.project),
            env={"CODEX_HOME": str(source_home)},
        )

        client.run(
            input="x",
            review=ReviewConfig(context="REVIEW_SHOULD_PASS"),
            timeout=3,
        )
        gc.collect()

        self.assertGreaterEqual(len(self.sdk_configs), 2)
        for config in self.sdk_configs:
            overrides = config.config_overrides
            self.assertTrue(any(x.startswith("default_permissions=") for x in overrides))
            self.assertTrue(any(x.startswith("permissions.") for x in overrides))
            self.assertFalse(any(x.startswith("sandbox_mode=") for x in overrides))
            self.assertFalse(any(x.startswith("sandbox_workspace_write") for x in overrides))
            sqlite_home = config.env["CODEX_SQLITE_HOME"]
            self.assertEqual(
                overrides[-1],
                f"sqlite_home={client_module._toml_string(sqlite_home)}",
            )
            self.assertEqual(
                pathlib.Path(sqlite_home),
                pathlib.Path(config.env["CODEX_HOME"]) / "sqlite",
            )
            self.assertFalse(pathlib.Path(config.env["CODEX_HOME"]).exists())
        self.assertTrue(self.thread_start_calls)
        self.assertTrue(self.turn_calls)
        self.assertTrue(all("sandbox" not in call for call in self.thread_start_calls))
        self.assertTrue(all("sandbox" not in call for call in self.turn_calls))

    def test_runtime_read_paths_resolve_explicit_codex_symlink_host(self):
        runtime_bin = self.tmp_path / "runtime" / "bin"
        runtime_bin.mkdir(parents=True)
        codex = runtime_bin / "codex"
        codex.write_text("codex\n", encoding="utf-8")
        (runtime_bin / "codex-code-mode-host").write_text(
            "host\n",
            encoding="utf-8",
        )
        codex_link = self.tmp_path / "codex-link"
        codex_link.symlink_to(codex)

        paths = client_module._codex_runtime_read_paths(str(codex_link))

        self.assertIn(runtime_bin.resolve(), paths)
        self.assertTrue(any(path.name == "openai_codex" for path in paths))

    def test_runtime_read_paths_use_sdk_bundled_codex_host_by_default(self):
        runtime_bin = self.tmp_path / "bundled" / "bin"
        runtime_bin.mkdir(parents=True)
        codex = runtime_bin / "codex"
        codex.write_text("codex\n", encoding="utf-8")
        (runtime_bin / "codex-code-mode-host").write_text(
            "host\n",
            encoding="utf-8",
        )
        previous_module = sys.modules.get("codex_cli_bin")
        sys.modules["codex_cli_bin"] = SimpleNamespace(
            bundled_codex_path=lambda: codex,
        )
        try:
            paths = client_module._codex_runtime_read_paths(None)
        finally:
            if previous_module is None:
                sys.modules.pop("codex_cli_bin", None)
            else:
                sys.modules["codex_cli_bin"] = previous_module

        self.assertIn(runtime_bin.resolve(), paths)

    def test_runtime_read_paths_skip_runtime_directory_when_host_is_missing(self):
        runtime_bin = self.tmp_path / "runtime-without-host" / "bin"
        runtime_bin.mkdir(parents=True)
        codex = runtime_bin / "codex"
        codex.write_text("codex\n", encoding="utf-8")

        paths = client_module._codex_runtime_read_paths(str(codex))

        self.assertNotIn(runtime_bin.resolve(), paths)
        self.assertTrue(any(path.name == "openai_codex" for path in paths))

    def test_sdk_config_is_profile_only_without_write_paths(self):
        captured = {}

        class FakeTurnResult:
            final_response = "ok"
            usage = None
            items = []

        class FakeTurn:
            def run(self):
                return FakeTurnResult()

        class FakeThread:
            def turn(self, prompt, **kwargs):
                captured["turn_kwargs"] = kwargs
                return FakeTurn()

        class FakeCodex:
            def __init__(self, config=None):
                captured["sdk_config"] = config

            def thread_start(self, **kwargs):
                captured["thread_start_kwargs"] = kwargs
                return FakeThread()

            def close(self):
                pass

        fake_sdk = SimpleNamespace(
            Codex=FakeCodex,
            CodexConfig=lambda **kwargs: SimpleNamespace(**kwargs),
            Sandbox=SimpleNamespace(read_only="read_only", workspace_write="workspace_write"),
            ApprovalMode=_FAKE_APPROVAL_MODE,
        )
        previous_import = client_module._import_openai_codex
        client_module._import_openai_codex = lambda: fake_sdk
        try:
            client = CodexClient(cwd=str(self.project))
            client.run(input="x", write_paths=[], timeout=3)
        finally:
            client_module._import_openai_codex = previous_import

        self.assertNotIn("sandbox", captured["thread_start_kwargs"])
        self.assertNotIn("sandbox", captured["turn_kwargs"])
        filesystem_overrides = [
            override
            for override in captured["sdk_config"].config_overrides
            if override.startswith("permissions.")
        ]
        self.assertEqual(len(filesystem_overrides), 1)
        self.assertIn('":minimal"="read"', filesystem_overrides[0])
        self.assertNotIn('":workspace_roots"', filesystem_overrides[0])

    def test_usage_and_pricing_are_logged_to_stdout(self):
        client = CodexClient(cwd=str(self.project), model="gpt-5.6-sol")

        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            response = client.run(input="README を要約して", timeout=3)

        self.assertEqual(
            response.pricing,
            {
                "model": "gpt-5.6-sol",
                "source_url": "https://developers.openai.com/api/docs/pricing",
                "input_usd_per_million": 5.0,
                "cached_input_usd_per_million": 0.5,
                "output_usd_per_million": 30.0,
            },
        )
        self.assertAlmostEqual(response.estimated_cost_usd, 0.0006875)
        output = captured.getvalue()
        self.assertIn("[Main Attempt][Usage]", output)
        self.assertIn("model=gpt-5.6-sol", output)
        self.assertIn("usd_per_million=input:5.0,cached_input:0.5,output:30.0", output)
        self.assertIn("estimated_cost_usd=0.000687", output)

    def test_gpt_5_6_variants_resolve_to_official_pricing(self):
        expected_rates = {
            "gpt-5.6-sol": (5.0, 0.5, 30.0),
            "gpt-5.6-terra": (2.0, 0.2, 12.0),
            "gpt-5.6-luna": (0.2, 0.02, 1.2),
        }

        for model, (input_rate, cached_input_rate, output_rate) in expected_rates.items():
            with self.subTest(model=model):
                pricing = client_module._resolve_model_pricing(model)
                self.assertEqual(pricing["model"], model)
                self.assertEqual(pricing["input_usd_per_million"], input_rate)
                self.assertEqual(
                    pricing["cached_input_usd_per_million"],
                    cached_input_rate,
                )
                self.assertEqual(pricing["output_usd_per_million"], output_rate)

    def test_gpt_5_6_sol_usage_has_pricing_cost_and_unchanged_log_format(self):
        usage = {
            "input_tokens": 11,
            "cached_input_tokens": 2,
            "output_tokens": 3,
            "reasoning_output_tokens": 1,
        }
        pricing = client_module._resolve_model_pricing("gpt-5.6-sol")

        estimated = client_module._estimate_cost_usd_for_usage(
            usage=usage,
            pricing=pricing,
        )
        summary = client_module._format_usage_summary(
            model="gpt-5.6-sol",
            usage=usage,
        )

        self.assertAlmostEqual(estimated, 0.000136)
        self.assertIn("model=gpt-5.6-sol", summary)
        self.assertIn("usd_per_million=input:5.0,cached_input:0.5,output:30.0", summary)
        self.assertIn("estimated_cost_usd=0.000136", summary)

    def test_estimate_cost_counts_output_without_readding_reasoning_subset(self):
        estimated = client_module._estimate_cost_usd_for_usage(
            usage={
                "input_tokens": 11,
                "cached_input_tokens": 2,
                "output_tokens": 3,
                "reasoning_output_tokens": 1,
            },
            pricing={
                "input_usd_per_million": 0.75,
                "cached_input_usd_per_million": 0.075,
                "output_usd_per_million": 4.5,
            },
        )

        self.assertAlmostEqual(estimated, 0.0000204)

    def test_estimate_cost_clamps_non_cached_input_when_cached_exceeds_input(self):
        estimated = client_module._estimate_cost_usd_for_usage(
            usage={
                "input_tokens": 10,
                "cached_input_tokens": 12,
                "output_tokens": 0,
                "reasoning_output_tokens": 0,
            },
            pricing={
                "input_usd_per_million": 0.75,
                "cached_input_usd_per_million": 0.075,
                "output_usd_per_million": 4.5,
            },
        )

        self.assertAlmostEqual(estimated, 0.0000009)

    def test_normalize_sdk_usage_supports_nested_thread_token_usage(self):
        breakdown = SimpleNamespace(
            input_tokens=11,
            cached_input_tokens=2,
            output_tokens=3,
            reasoning_output_tokens=1,
            total_tokens=17,
        )
        usage = SimpleNamespace(last=breakdown, total=SimpleNamespace())

        normalized = client_module._normalize_sdk_usage(usage)

        self.assertEqual(
            normalized,
            {
                "input_tokens": 11,
                "cached_input_tokens": 2,
                "output_tokens": 3,
                "reasoning_output_tokens": 1,
                "total_tokens": 17,
            },
        )

    def test_sdk_backend_persists_main_and_review_threads(self):
        captured = {"thread_start_kwargs": []}

        class FakeTurnResult:
            def __init__(self, prompt):
                if "You are a peer reviewer. Return only a strict JSON object" in prompt:
                    self.final_response = '{"decisions": [], "new_findings": []}'
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
            ApprovalMode=_FAKE_APPROVAL_MODE,
        )

        previous_import = client_module._import_openai_codex
        client_module._import_openai_codex = lambda: fake_sdk
        try:
            client = CodexClient(cwd=str(self.project))
            response = client.run(
                input="x",
                review=ReviewConfig(context="REVIEW_SHOULD_PASS"),
                timeout=3,
            )
        finally:
            client_module._import_openai_codex = previous_import

        self.assertEqual(response.content, "sdk final answer")
        self.assertEqual(len(captured["thread_start_kwargs"]), 2)
        self.assertTrue(
            all(
                kwargs["ephemeral"] is False
                for kwargs in captured["thread_start_kwargs"]
            )
        )

    def test_sdk_backend_review_uses_same_executor_path(self):
        captured = {"prompts": []}

        class FakeTurnResult:
            def __init__(self, prompt):
                if "You are a peer reviewer. Return only a strict JSON object" in prompt:
                    self.final_response = '{"decisions": [], "new_findings": []}'
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
            ApprovalMode=_FAKE_APPROVAL_MODE,
        )

        previous_import = client_module._import_openai_codex
        client_module._import_openai_codex = lambda: fake_sdk
        try:
            client = CodexClient(cwd=str(self.project))
            response = client.run(
                input="x",
                review=ReviewConfig(context="REVIEW_SHOULD_PASS"),
                timeout=3,
            )
        finally:
            client_module._import_openai_codex = previous_import

        self.assertEqual(response.content, "sdk final answer")
        self.assertEqual(len(captured["prompts"]), 2)
        self.assertTrue(any("You are a peer reviewer. Return only a strict JSON object" in prompt for prompt in captured["prompts"]))

    def test_review_stream_usage_is_logged_with_reviewer_actor(self):
        captured_stdout = io.StringIO()

        class FakeUsage:
            input_tokens = 4
            cached_input_tokens = 0
            output_tokens = 2
            reasoning_output_tokens = 1
            total_tokens = 7

        class FakeThreadUsage:
            last = FakeUsage()
            total = FakeUsage()

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
                if "You are a peer reviewer. Return only a strict JSON object" in self.prompt:
                    reasoning_item = SimpleNamespace(root=SimpleNamespace(type="reasoning", summary=["review reasoning"], content=[]))
                    message_item = SimpleNamespace(
                        root=SimpleNamespace(
                            type="agentMessage",
                            text='{"decisions": [], "new_findings": []}',
                        )
                    )
                    completed = SimpleNamespace(status="completed", token_usage=FakeThreadUsage())
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
            ApprovalMode=_FAKE_APPROVAL_MODE,
        )

        previous_import = client_module._import_openai_codex
        client_module._import_openai_codex = lambda: fake_sdk
        try:
            client = CodexClient(cwd=str(self.project))
            with contextlib.redirect_stdout(captured_stdout):
                response = client.run(
                    input="x",
                    review=ReviewConfig(context="REVIEW_SHOULD_PASS"),
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
        self.assertTrue(
            any(entry.phase == "review" and entry.event == "usage" for entry in response.activity_log)
        )

    def test_demo_company_analysis_job_wires_schema_validator_review_and_repair(self):
        job = DemoCompanyAnalysisJob(
            config=SessionConfig(
                workspace=WorkspaceConfig(cwd=str(ROOT)),
            ),
            company_name="OpenAI",
            extra_input="Focus on compensation.",
        ).start()

        self.assertIn("Company name:\nOpenAI", job._attempt.prompt)
        self.assertIn("Additional input:\nFocus on compensation.", job._attempt.prompt)
        self.assertTrue(str(job._output_schema_path).endswith("demo_company_analysis/output.schema.json"))
        self.assertIsNotNone(job._validate_output)
        self.assertTrue(job._repair_validation_failures)
        self.assertIsNotNone(job._review_config)
        self.assertIn("references in the JSON output", "\n\n".join(job._review_config.context))
        self.assertIsNone(job._review_config.timeout)
        self.assertIsNone(job.config.timeout)
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
        job = DemoCompanyAnalysisJob(
            config=SessionConfig(workspace=WorkspaceConfig(cwd=self.project)),
            company_name="OpenAI",
        )
        with mock.patch.object(
            CodexJob,
            "subcwd_path",
            new_callable=mock.PropertyMock,
            return_value=subcwd,
        ):
            with self.assertRaisesRegex(ValueError, "workflow checklist is incomplete"):
                job.validate_output(SimpleNamespace(content=json.dumps(sample)))

    def test_repair_prompt_includes_failure_stage_and_checklist_feedback(self):
        client = CodexClient(cwd=str(self.project))
        job = client.start(
            input="x",
            subcwd_files={"workflow_checklist.json": {"text": "{}\n"}},
            read_paths=["${SUBCWD}/workflow_checklist.json"],
            write_paths=["${SUBCWD}/workflow_checklist.json"],
        )
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
                diagnostic="workflow checklist is incomplete: checked_salary, checked_references",
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
            self.assertNotIn("Original request:", prompt)
            self.assertNotIn("Readable paths:", prompt)
            self.assertNotIn("Writable paths:", prompt)
            self.assertNotIn("Workspace files staged under ${SUBCWD}:", prompt)
        finally:
            job.cancel()

    def test_repair_prompt_does_not_reinject_static_main_context(self):
        schema = self.project / "schema.json"
        schema.write_text('{"type":"object"}\n', encoding="utf-8")
        context_file = self.project / "notes.txt"
        context_file.write_text("context_file text\n", encoding="utf-8")
        client = CodexClient(
            cwd=str(self.project),
            context=["notes.txt"],
            ignore_global_context=True,
        )
        job = client.start(
            input="x",
            read_paths=["notes.txt"],
            write_paths=["out"],
            output_schema_path=str(schema),
        )
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
            validation = SimpleNamespace(diagnostic="wrong")
            prompt = job._build_repair_prompt(
                response=response,
                validation=validation,
                review=None,
                attempt_index=1,
                repair_index=1,
            )

            self.assertIn("The previous output failed validation or review.", prompt)
            self.assertIn("minimum required correction", prompt)
            self.assertIn("underlying failure pattern", prompt)
            self.assertIn("other instances of the same pattern", prompt)
            self.assertIn("its own local evidence", prompt)
            self.assertIn("Do not bulk-replace", prompt)
            self.assertNotIn("Do not broaden the task", prompt)
            self.assertIn("Validation diagnostic:\nwrong", prompt)
            self.assertNotIn("Global AGENTS.md", prompt)
            self.assertNotIn("Output schema:", prompt)
            self.assertNotIn("Context paths:", prompt)
            self.assertNotIn("Context:", prompt)
            self.assertNotIn("Readable paths:", prompt)
            self.assertNotIn("Writable paths:", prompt)
        finally:
            job.cancel()

    def test_json_mode_is_enabled_and_usage_is_aggregated_across_repairs(self):
        self._make_validator(
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
            validate_output=self._require_repaired_json,
            repair_validation_failures=True,
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
        repair_stream_entries = [
            entry for entry in response.activity_log
            if entry.phase == "stream" and entry.repair_index == 1
        ]
        self.assertTrue(repair_stream_entries)
        self.assertTrue(all(entry.attempt == 1 for entry in repair_stream_entries))
        repair_prompt_entries = [
            entry for entry in response.activity_log
            if entry.phase == "repair" and entry.event == "prompt"
        ]
        self.assertEqual(len(repair_prompt_entries), 1)
        self.assertEqual(repair_prompt_entries[0].attempt, 1)
        self.assertEqual(repair_prompt_entries[0].repair_index, 1)
        self.assertIn("source=validator", repair_prompt_entries[0].message)
        self.assertIn("sha256=", repair_prompt_entries[0].message)
        log_text = pathlib.Path(response.log_path).read_text(encoding="utf-8")
        self.assertIn("[Repair][Prompt]", log_text)
        self.assertNotIn("[Repair][Prompt] attempt=1 repair=1", log_text)
        self.assertIn("[Main Attempt][Start] attempt=1 repair=1", log_text)

    def test_main_repair_attempt_reuses_sdk_thread(self):
        self._make_validator(
            "validate_json_only.py",
            """
            import json
            import sys

            json.loads(sys.stdin.read())
            sys.exit(0)
            """,
        )
        captured = {"thread_start_calls": 0, "turn_prompts": [], "closed": 0}

        class FakeUsage:
            input_tokens = 1
            cached_input_tokens = 0
            output_tokens = 1
            reasoning_output_tokens = 0

        class FakeTurnResult:
            usage = FakeUsage()
            items = []

            def __init__(self, prompt):
                if "The previous output failed validation or review." in prompt:
                    self.final_response = '{"ok": true}'
                else:
                    self.final_response = "not json"

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
                captured["closed"] += 1

        fake_sdk = SimpleNamespace(
            Codex=FakeCodex,
            CodexConfig=lambda **kwargs: SimpleNamespace(**kwargs),
            Sandbox=SimpleNamespace(read_only="read_only", workspace_write="workspace_write"),
            ApprovalMode=_FAKE_APPROVAL_MODE,
        )
        client_module._import_openai_codex = lambda: fake_sdk
        client = CodexClient(cwd=str(self.project))

        response = client.run(
            input="NEEDS_REPAIR",
            validate_output=self._require_repaired_json,
            repair_validation_failures=True,
            timeout=3,
        )

        self.assertEqual(response.content, '{"ok": true}')
        self.assertEqual(captured["thread_start_calls"], 1)
        self.assertEqual(len(captured["turn_prompts"]), 2)
        self.assertIn("NEEDS_REPAIR", captured["turn_prompts"][0])
        self.assertIn("The previous output failed validation or review.", captured["turn_prompts"][1])
        self.assertEqual(captured["closed"], 1)

    def test_review_after_validation_repair_does_not_receive_diagnostic(self):
        self._make_validator(
            "validate_json_only.py",
            """
            import json
            import sys

            json.loads(sys.stdin.read())
            sys.exit(0)
            """,
        )
        captured = {}
        client = CodexClient(cwd=str(self.project))

        class _FakeReviewSession:
            def _send(self, message, *, output_schema_path=None, activity_callback=None):
                captured["message"] = message
                return SimpleNamespace(
                    content='{"decisions": [], "new_findings": []}',
                    stdout="",
                    stderr="",
                    argv=["review"],
                    returncode=0,
                    usage=None,
                )

            def close(self):
                return None

        client._create_review_session = lambda **kwargs: _FakeReviewSession()

        response = client.run(
            input="NEEDS_REPAIR",
            validate_output=self._require_repaired_json,
            repair_validation_failures=True,
            review=ReviewConfig(context="REVIEW_SHOULD_PASS"),
            timeout=3,
        )

        self.assertEqual(response.content, '{"ok": true}')
        self.assertNotIn("Validation diagnostic:", captured["message"])
        self.assertNotIn("JSONDecodeError", captured["message"])
        self.assertNotIn("Main job context:", captured["message"])

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
        self.assertIn("features.hooks=false", argv)
        for override in client_module._RUNNER_ALWAYS_OFF_CONFIG_OVERRIDES:
            self.assertIn(override, argv)
        self.assertFalse(any(item.startswith("features.code_mode") for item in argv))
        self.assertIn("features.shell_tool=false", argv)
        self.assertIn("allow_login_shell=false", argv)
        self.assertIn("skills.include_instructions=false", argv)
        filesystem_args = [item for item in argv if item.startswith("permissions.")]
        self.assertEqual(len(filesystem_args), 1)
        self.assertIn('":minimal"="read"', filesystem_args[0])
        self.assertNotIn('":workspace_roots"', filesystem_args[0])
        self.assertEqual(pathlib.Path(argv[argv.index("--cwd") + 1]), self.project)
        self.assertIn(f'"{self.project}"={{', filesystem_args[0])
        self.assertIn('"README.md"="read"', filesystem_args[0])
        self.assertIn('"out"="write"', filesystem_args[0])
        self.assertNotIn('"."=', filesystem_args[0])
        sdk_filesystem_args = [
            item
            for item in self.sdk_configs[0].config_overrides
            if item.startswith("permissions.")
        ]
        self.assertEqual(filesystem_args, sdk_filesystem_args)

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

    def test_allow_shell_commands_adds_detected_runtime_read_path(self):
        runtime_dir = self.tmp_path / "openai_codex_runtime"
        runtime_dir.mkdir()
        captured_codex_bins = []
        previous_runtime_paths = client_module._codex_runtime_read_paths
        client_module._codex_runtime_read_paths = lambda codex_bin: (
            captured_codex_bins.append(codex_bin) or [runtime_dir]
        )
        try:
            client = CodexClient(cwd=str(self.project), allow_shell_commands=True)

            job = client.start(input="x")
            job.result(timeout=3)
        finally:
            client_module._codex_runtime_read_paths = previous_runtime_paths

        filesystem_args = [item for item in job.argv if item.startswith("permissions.")]
        self.assertIn(f'"{runtime_dir}"="read"', filesystem_args[0])
        self.assertEqual(captured_codex_bins, [None, None])

    def test_default_model_and_reasoning_effort_are_passed_to_sdk_argv(self):
        client = CodexClient(
                        cwd=str(self.project),
        )

        job = client.start(input="x")
        job.result(timeout=3)

        self.assertIn("--model", job.argv)
        self.assertEqual(
            job.argv[job.argv.index("--model") + 1],
            "gpt-5.6-luna",
        )
        self.assertIn("--effort", job.argv)
        self.assertEqual(job.argv[job.argv.index("--effort") + 1], "medium")

    def test_sdk_turn_effort_falls_back_only_for_unknown_sdk_enum_value(self):
        class Effort(enum.Enum):
            high = "high"
            xhigh = "xhigh"

        sdk = SimpleNamespace(ReasoningEffort=Effort)
        self.assertEqual(client_module._sdk_turn_reasoning_effort(sdk, "high"), "high")
        self.assertIsNone(client_module._sdk_turn_reasoning_effort(sdk, "max"))
        self.assertIsNone(client_module._sdk_turn_reasoning_effort(sdk, None))

    def test_sdk_turn_effort_preserves_value_when_sdk_has_no_enum(self):
        self.assertEqual(
            client_module._sdk_turn_reasoning_effort(SimpleNamespace(), "max"),
            "max",
        )

    def test_sdk_turn_effort_discovers_enum_from_sdk_types_module(self):
        class Effort(enum.Enum):
            high = "high"
            xhigh = "xhigh"

        sdk = SimpleNamespace(types=SimpleNamespace(ReasoningEffort=Effort))
        self.assertEqual(client_module._sdk_turn_reasoning_effort(sdk, "xhigh"), "xhigh")
        self.assertIsNone(client_module._sdk_turn_reasoning_effort(sdk, "max"))

    def test_sdk_thread_start_widens_unknown_response_effort_for_one_client(self):
        from pydantic import BaseModel, Field

        package_name = "fake_codex_sdk_reasoning_compat"
        sdk = ModuleType(package_name)
        sdk_types = ModuleType(f"{package_name}.types")
        sdk_client = ModuleType(f"{package_name}.client")

        class Effort(enum.Enum):
            high = "high"
            xhigh = "xhigh"

        class ThreadStartResponse(BaseModel):
            reasoning_effort: Effort | None = Field(
                default=None,
                alias="reasoningEffort",
            )

        sdk_types.ReasoningEffort = Effort
        sdk_client.ThreadStartResponse = ThreadStartResponse
        captured = {}

        class RawClient:
            def thread_start(self, params=None):
                raise AssertionError("original strict thread_start should be replaced")

            def request(self, method, payload, *, response_model):
                captured["method"] = method
                captured["payload"] = payload
                captured["response_model"] = response_model
                return response_model.model_validate({"reasoningEffort": "max"})

        class FakeCodex:
            def __init__(self):
                self._client = RawClient()

            def thread_start(self, **kwargs):
                return self._client.thread_start(kwargs)

        previous_modules = {
            name: sys.modules.get(name)
            for name in (package_name, f"{package_name}.types", f"{package_name}.client")
        }
        sys.modules[package_name] = sdk
        sys.modules[f"{package_name}.types"] = sdk_types
        sys.modules[f"{package_name}.client"] = sdk_client
        codex = FakeCodex()
        original_thread_start = codex._client.thread_start
        try:
            response = client_module._sdk_thread_start(
                codex,
                sdk,
                reasoning_effort="max",
                model="gpt-5.6-luna",
            )
        finally:
            for name, previous in previous_modules.items():
                if previous is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = previous

        self.assertEqual(response.reasoning_effort, "max")
        self.assertEqual(captured["method"], "thread/start")
        self.assertEqual(captured["payload"], {"model": "gpt-5.6-luna"})
        self.assertEqual(codex._client.thread_start, original_thread_start)

    def test_codex_bin_defaults_to_sdk_bundled_runtime(self):
        client = CodexClient(cwd=str(self.project))

        client.run(input="x", timeout=3)

        self.assertIsNone(self.sdk_configs[0].codex_bin)

    def test_explicit_codex_bin_is_passed_to_sdk_config(self):
        client = CodexClient(
            cwd=str(self.project),
            codex_bin="/opt/codex/bin/codex",
        )

        client.run(input="x", timeout=3)

        self.assertEqual(
            self.sdk_configs[0].codex_bin,
            "/opt/codex/bin/codex",
        )

    def test_selected_codex_bin_reaches_all_runtime_read_path_builders(self):
        selected_codex_bin = "/opt/codex/bin/codex"
        captured = []
        previous_runtime_read_paths = client_module._codex_runtime_read_paths
        client_module._codex_runtime_read_paths = lambda codex_bin: (
            captured.append(codex_bin) or []
        )
        try:
            client = CodexClient(
                cwd=str(self.project),
                codex_bin=selected_codex_bin,
            )
            client.run(
                input="x",
                review=ReviewConfig(
                    context="REVIEW_SHOULD_PASS",
                    session=SessionConfig(
                        codex_bin=selected_codex_bin,
                        allow_shell_commands=True,
                    ),
                ),
                timeout=3,
            )
            session = client.start_review_session(
                review=ReviewConfig(
                    context="review",
                    session=SessionConfig(
                        codex_bin=selected_codex_bin,
                        allow_shell_commands=True,
                    ),
                ),
            )
            try:
                session.send("x")
            finally:
                session.close()
        finally:
            client_module._codex_runtime_read_paths = previous_runtime_read_paths

        self.assertEqual(captured, [selected_codex_bin] * 6)

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

        cases = (
            {"read_paths": [str(outside)]},
            {"write_paths": [str(self.tmp_path / "outside-output")]},
        )
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    client.start(input="x", **kwargs)

    def test_file_context_are_inlined_by_default(self):
        client = CodexClient(cwd=str(self.project), context=["README.md"])

        response = client.run(input="ECHO_PROMPT")

        self.assertIn("Context paths:", response.content)
        self.assertIn("Context:", response.content)
        self.assertIn("<ContextFile path=", response.content)
        self.assertIn("hello\n", response.content)

    def test_inline_context_none_uses_default_inline(self):
        client = CodexClient(cwd=str(self.project), context=["README.md"])

        response = client.run(
            input="ECHO_PROMPT",
            inline_context=None,
        )

        self.assertIn("Context:", response.content)
        self.assertIn("hello\n", response.content)

    def test_inline_context_false_lists_paths_without_contents(self):
        client = CodexClient(
            cwd=str(self.project),
            context=["README.md"],
        )

        response = client.run(
            input="ECHO_PROMPT",
            inline_context=False,
        )

        self.assertIn("Context paths:", response.content)
        self.assertNotIn("Context:", response.content)
        self.assertNotIn("<ContextFile path=", response.content)

    def test_string_context_without_path_shape_is_inline_text(self):
        client = CodexClient(
            cwd=str(self.project), context=["Use concise wording"]
        )

        response = client.run(input="ECHO_PROMPT")

        self.assertIn("<ContextText>", response.content)
        self.assertIn("Use concise wording", response.content)
        self.assertNotIn("Context paths:", response.content)

    def test_context_preserves_caller_order_before_input(self):
        client = CodexClient(
            cwd=str(self.project),
            context=["first stable text", "README.md", "last volatile text"],
        )

        response = client.run(
            input="ECHO_PROMPT",
        )

        prompt = response.content
        self.assertLess(prompt.index("first stable text"), prompt.index("<ContextFile path="))
        self.assertLess(prompt.index("<ContextFile path="), prompt.index("last volatile text"))
        self.assertLess(prompt.index("last volatile text"), prompt.index("ECHO_PROMPT"))

    def test_inline_context_size_limit_errors(self):
        client = CodexClient(cwd=str(self.project), context=["README.md"])

        with self.assertRaises(ValueError):
            client.start(
                input="x",
                max_inline_context_bytes=1,
            )

    def test_start_context_is_rejected_with_migration_error(self):
        client = CodexClient(cwd=str(self.project))

        with self.assertRaisesRegex(
            ValueError, "pass fixed context to SessionConfig"
        ):
            client.start(input="x", context=None)

    def test_input_accepts_text_and_path_items_after_client_context(self):
        client = CodexClient(
            cwd=str(self.project), context=["stable instructions"]
        )

        response = client.run(
            input=["ECHO_PROMPT", pathlib.Path("README.md")]
        )

        prompt = response.content
        self.assertLess(prompt.index("stable instructions"), prompt.index("ECHO_PROMPT"))
        self.assertLess(prompt.index("ECHO_PROMPT"), prompt.index("Input paths:"))
        self.assertIn("Input paths:", prompt)
        self.assertIn("<InputFile path=", prompt)

    def test_client_context_is_reused_across_starts(self):
        client = CodexClient(
            cwd=str(self.project), context=(item for item in ["stable instructions"])
        )

        first = client.run(input="ECHO_PROMPT")
        second = client.run(input="ECHO_PROMPT")

        self.assertIn("stable instructions", first.content)
        self.assertIn("stable instructions", second.content)

    def test_allowed_skills_empty_disables_skill_instructions(self):
        client = CodexClient(cwd=str(self.project))

        job = client.start(input="x", allowed_skills=[])
        job.result(timeout=3)

        self.assertIn("skills.include_instructions=false", job.argv)

    def test_allowed_skills_non_empty_is_not_supported(self):
        with self.assertRaisesRegex(ValueError, "None or \\[\\]"):
            CodexClient(cwd=str(self.project), allowed_skills=["paper-review"])

        client = CodexClient(cwd=str(self.project))
        with self.assertRaisesRegex(ValueError, "None or \\[\\]"):
            client.start(input="x", allowed_skills=["paper-review"])

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
                return {
                    "type": "object",
                    "description": "top level description",
                    "additionalProperties": False,
                    "properties": {
                        "item": {
                            "$ref": "#/$defs/Foo",
                            "description": "field description",
                            "x-do_not": "field rule",
                        }
                    },
                    "$defs": {
                        "Foo": {
                            "type": "object",
                            "description": "definition description",
                            "x-do_not": "definition rule",
                            "additionalProperties": False,
                        }
                    },
                }

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
        rendered_schema = json.loads(temp_schema_path.read_text(encoding="utf-8"))

        response = job.result(timeout=3)

        self.assertEqual(response.content, "fake final answer")
        self.assertIn("validator", [entry.phase for entry in response.activity_log])
        self.assertNotIn("description", json.dumps(rendered_schema, ensure_ascii=False))
        self.assertNotIn("x-do_not", json.dumps(rendered_schema, ensure_ascii=False))
        self.assertEqual(rendered_schema["properties"]["item"], {"$ref": "#/$defs/Foo"})
        self.assertFalse(temp_schema_path.exists())
        self.assertIsNotNone(job._output_model_source_section)
        self.assertIn('<PydanticModel name="FakePydanticModel">', job._output_model_source_section)
        self.assertIn("class FakePydanticModel", job._output_model_source_section)
        self.assertIn("Output model source:", job._attempt.prompt)
        self.assertNotIn("Output schema:", job._attempt.prompt)

    def test_output_pydantic_model_preserves_description_property_name(self):
        client = CodexClient(cwd=str(self.project))

        class FakePydanticModel:
            @classmethod
            def model_json_schema(cls, **kwargs):
                return {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "description": {
                            "type": "string",
                            "description": "field metadata",
                            "title": "Description",
                        }
                    },
                    "required": ["description"],
                }

            @classmethod
            def model_validate_json(cls, content):
                return {"ok": True}

        job = client.start(
            input="x",
            output_pydantic_model=FakePydanticModel,
        )
        try:
            schema_index = job.argv.index("--output-schema")
            temp_schema_path = pathlib.Path(job.argv[schema_index + 1])
            rendered_schema = json.loads(temp_schema_path.read_text(encoding="utf-8"))

            self.assertEqual(
                rendered_schema["properties"]["description"],
                {"type": "string"},
            )
            self.assertEqual(rendered_schema["required"], ["description"])
        finally:
            job.cancel()

    def test_pydantic_failure_skips_validate_output(self):
        calls = []

        class FakePydanticModel:
            @classmethod
            def model_json_schema(cls, **kwargs):
                return {"type": "object"}

            @classmethod
            def model_validate_json(cls, content):
                raise ValueError("pydantic rejected output")

        class PydanticJob(CodexJob):
            def validate_output(self, response):
                calls.append(response)

        with self.assertRaises(CodexValidationError) as caught:
            PydanticJob(
                config=SessionConfig(workspace=WorkspaceConfig(cwd=self.project)),
                input="x",
                output_pydantic_model=FakePydanticModel,
            ).run()

        self.assertIn("pydantic validation failed", caught.exception.validation_diagnostic)
        self.assertIn("pydantic rejected output", caught.exception.validation_diagnostic)
        self.assertEqual(calls, [])

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
                max_inline_context_bytes=1,
            )

    def test_missing_output_schema_path_errors_before_start(self):
        client = CodexClient(cwd=str(self.project))

        with self.assertRaises(FileNotFoundError):
            client.start(input="x", output_schema_path="missing.schema.json")

    def test_host_paths_outside_cwd_are_allowed(self):
        outside_dir = self.tmp_path / "outside"
        outside_dir.mkdir()
        outside_file = outside_dir / "outside.txt"
        outside_file.write_text("outside\n", encoding="utf-8")
        outside_schema = outside_dir / "schema.json"
        outside_schema.write_text('{"type":"object"}\n', encoding="utf-8")
        outside_validator = outside_dir / "validate.py"
        outside_validator.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
        outside_log = outside_dir / "job.log"
        client = CodexClient(cwd=str(self.project), context=[outside_file])

        job = client.start(
            input="x",
            output_schema_path=outside_schema,
            repair_prompt_path=outside_file,
            log_path=outside_log,
            review=ReviewConfig(context="review", prompt_path=str(outside_file)),
            validate_output=lambda response: None,
        )
        try:
            self.assertIn(outside_file.resolve(), job._context_paths)
            self.assertEqual(job._output_schema_path, outside_schema.resolve())
            self.assertTrue(callable(job._validate_output))
            self.assertEqual(job.log_path, str(outside_log.resolve()))
        finally:
            job.cancel()

    def test_review_session_output_schema_path_outside_cwd_is_allowed(self):
        outside_schema = self.tmp_path / "outside.schema.json"
        outside_schema.write_text('{"type":"object"}\n', encoding="utf-8")
        client = CodexClient(cwd=str(self.project))
        session = client.start_review_session(review=ReviewConfig(context="review"))
        try:
            response = session.send("x", output_schema_path=str(outside_schema))
        finally:
            session.close()

        self.assertEqual(response.content, "fake final answer")

    def test_validate_output_success_and_value_error_diagnostic(self):
        config = SessionConfig(workspace=WorkspaceConfig(cwd=self.project))

        class FailingJob(CodexJob):
            def validate_output(self, response):
                raise ValueError("not valid")

        with self.assertRaises(CodexValidationError) as caught:
            FailingJob(config=config, input="x").run()

        self.assertEqual(caught.exception.validation_diagnostic, "not valid")
        self.assertEqual(caught.exception.attempts, 1)
        self.assertEqual(caught.exception.repairs, 0)
        self.assertIn("validator", [entry.phase for entry in caught.exception.activity_log])
        self.assertTrue(any("not valid" in entry.message for entry in caught.exception.activity_log))

    def test_validate_output_non_value_error_fails_the_job(self):
        config = SessionConfig(workspace=WorkspaceConfig(cwd=self.project))

        class BrokenJob(CodexJob):
            def validate_output(self, response):
                raise RuntimeError("validator crashed")

        with self.assertRaises(CodexExecutionError) as caught:
            BrokenJob(config=config, input="x").run()

        self.assertIn("validator crashed", caught.exception.message)

    def test_validate_output_repair_limits(self):
        config = SessionConfig(workspace=WorkspaceConfig(cwd=self.project))

        class JsonJob(CodexJob):
            def validate_output(self, response):
                if response.content != '{"ok": true}':
                    raise ValueError("return the repaired JSON")

        repaired = JsonJob(
            config=config,
            input="NEEDS_REPAIR",
            validation=ValidationConfig(repair=True),
        ).run()
        self.assertEqual(repaired.content, '{"ok": true}')
        self.assertTrue(
            any(
                entry.phase == "main_attempt" and entry.repair_index == 1
                for entry in repaired.activity_log
            )
        )

        with self.assertRaises(TypeError):
            ValidationConfig(max_repairs=0)

    def test_output_schema_path_alone_skips_python_validation(self):
        response = CodexJob(
            config=SessionConfig(workspace=WorkspaceConfig(cwd=self.project)),
            input="x",
            output_schema_path="result.schema.json",
        ).run()

        self.assertNotIn("validator", [entry.phase for entry in response.activity_log])

    def test_review_passes_after_validator_success(self):
        client = CodexClient(cwd=str(self.project))

        response = client.run(
            input="x",
            review=ReviewConfig(context="REVIEW_SHOULD_PASS"),
            timeout=3,
        )

        self.assertEqual(response.content, "fake final answer")

    def test_review_mode_notice_is_included_in_initial_main_context(self):
        client = CodexClient(cwd=str(self.project))

        response = client.run(
            input="ECHO_PROMPT",
            review=ReviewConfig(context="REVIEW_SHOULD_PASS"),
            timeout=3,
        )

        self.assertIn("Review mode:", response.content)
        self.assertIn("internal reviewer will check the final response", response.content)
        self.assertIn("may be sent back for repair", response.content)

    def test_review_failure_triggers_repair_and_succeeds(self):
        client = CodexClient(cwd=str(self.project))

        response = client.run(
            input="REVIEW_TRIGGER_REPAIR",
            repair_validation_failures=True,
            review=ReviewConfig(context="REVIEW_SHOULD_FAIL_ON_BAD_RESPONSE", max_repairs=1),
            timeout=3,
        )

        self.assertEqual(response.content, "review repaired answer")
        self.assertTrue(
            any(
                entry.phase == "main_attempt" and entry.repair_index == 1
                for entry in response.activity_log
            )
        )
        self.assertTrue(
            any(
                entry.phase == "repair"
                and entry.event == "prompt"
                and entry.attempt == 1
                and entry.repair_index == 1
                and "source=review" in entry.message
                for entry in response.activity_log
            )
        )

    def test_review_max_repairs_caps_review_repairs(self):
        self._make_validator(
            "total_repair_cap_validator.py",
            """
            import sys

            content = sys.stdin.read()
            if content == '{"ok": true}':
                sys.exit(0)
            print("return the repaired JSON", file=sys.stderr)
            sys.exit(9)
            """,
        )
        client = CodexClient(cwd=str(self.project))

        class _AlwaysFailReviewSession:
            def _send(
                self,
                message,
                *,
                output_schema_path=None,
                activity_callback=None,
            ):
                content = (
                    '{"decisions": [{"finding_id": "F001", '
                    '"resolution": "unresolved", "comment": "still fails"}], '
                    '"new_findings": []}'
                    if '"open_findings": []' not in message else
                    '{"decisions": [], "new_findings": '
                    '[{"comment": "review still fails"}]}'
                )
                return SimpleNamespace(
                    content=content,
                    stdout="",
                    stderr="",
                    argv=["review"],
                    returncode=0,
                    usage=None,
                )

            def close(self):
                pass

        client._create_review_session = lambda **kwargs: _AlwaysFailReviewSession()

        with self.assertRaises(CodexValidationError) as caught:
            client.run(
                input="NEEDS_REPAIR",
                validate_output=lambda response: None,
                review=ReviewConfig(context="always fail", max_repairs=1),
                repair_validation_failures=True,
                timeout=3,
            )

        self.assertEqual(caught.exception.failure_stage, "review")
        self.assertEqual(caught.exception.attempts, 2)
        self.assertEqual(caught.exception.repairs, 1)
        repair_starts = [
            entry
            for entry in caught.exception.activity_log
            if entry.phase == "main_attempt"
            and entry.event == "start"
            and entry.repair_index
        ]
        self.assertEqual([entry.repair_index for entry in repair_starts], [1])

    def test_review_failure_without_repair_flag_raises_validation_error(self):
        client = CodexClient(cwd=str(self.project))

        with self.assertRaises(CodexValidationError) as caught:
            client.run(
                input="REVIEW_TRIGGER_REPAIR",
                review=ReviewConfig(context="REVIEW_SHOULD_FAIL_ON_BAD_RESPONSE", max_repairs=0),
                timeout=3,
            )

        self.assertEqual(caught.exception.failure_stage, "review")
        self.assertEqual(caught.exception.repairs, 0)
        self.assertIn("open peer-review findings", caught.exception.message)
        self.assertFalse(
            any(
                entry.phase == "main_attempt" and entry.repair_index == 1
                for entry in caught.exception.activity_log
            )
        )

    def test_review_failure_exposes_summary_and_findings(self):
        client = CodexClient(cwd=str(self.project))

        with self.assertRaises(CodexValidationError) as caught:
            client.run(
                input="REVIEW_TRIGGER_REPAIR",
                repair_validation_failures=True,
                # A zero review cap rejects an initial open finding.
                review=ReviewConfig(context="REVIEW_SHOULD_FAIL_ON_BAD_RESPONSE", max_repairs=0),
                timeout=3,
            )

        self.assertIn("open peer-review findings", caught.exception.message)
        self.assertIsNotNone(caught.exception.log_path)
        self.assertTrue(pathlib.Path(caught.exception.log_path).is_file())
        review_details = [
            entry.message for entry in caught.exception.activity_log
            if entry.phase == "review" and entry.event == "detail"
        ]
        self.assertTrue(
            any("review summary: peer review has open findings" in message for message in review_details)
        )
        self.assertTrue(
            any(
                "Please change the final response to review repaired answer."
                in message
                for message in review_details
            )
        )

    def test_removed_validation_repair_limit_is_rejected_by_constructor(self):
        with self.assertRaises(TypeError):
            ValidationConfig(max_repairs=0)

    def test_auto_review_can_reuse_session_across_review_repairs(self):
        client = CodexClient(cwd=str(self.project))
        captured = {"sessions": 0, "messages": [], "closed": 0}

        class _FakeReviewSession:
            def _send(self, message, *, output_schema_path=None, activity_callback=None):
                captured["messages"].append(message)
                if '"open_findings": []' in message:
                    content = '{"decisions": [], "new_findings": [{"comment": "Please change the final response to review repaired answer."}]}'
                else:
                    content = '{"decisions": [{"finding_id": "F001", "resolution": "resolved", "comment": "ok"}], "new_findings": []}'
                return SimpleNamespace(
                    content=content,
                    stdout="",
                    stderr="",
                    argv=["review"],
                    returncode=0,
                    usage=None,
                )

            def close(self):
                captured["closed"] += 1

        def fake_create_review_session(*, review_config, options, read_paths, **_ignored):
            captured["sessions"] += 1
            return _FakeReviewSession()

        client._create_review_session = fake_create_review_session

        response = client.run(
            input="REVIEW_TRIGGER_REPAIR",
            review=ReviewConfig(
                context="REVIEW_SHOULD_FAIL_ON_BAD_RESPONSE",
                reuse_session=True,
            ),
            repair_validation_failures=True,
            timeout=3,
        )

        self.assertEqual(response.content, "review repaired answer")
        self.assertEqual(captured["sessions"], 1)
        self.assertEqual(len(captured["messages"]), 2)
        self.assertEqual(captured["closed"], 1)
        first_message, second_message = captured["messages"]
        self.assertIn('"open_findings": []', first_message)
        self.assertNotIn('"open_findings": []', second_message)
        self.assertIn('"finding_responses": [{"finding_id": "F001"', second_message)
        self.assertIn('"change_id": "C001"', second_message)
        self.assertIn('"diff": "@@', second_message)

    def test_custom_review_context_placeholder_coexists_with_developer_instructions(self):
        template = self.project / "custom_review_prompt.md"
        template.write_text(
            (
                "Internal review mode: codex_job_runner\n"
                "Custom review context:\n{review_context}\n\n"
                "Original request:\n{original_input}\n\n"
                "Main job final response:\n{response_content}\n"
            ),
            encoding="utf-8",
        )

        for reuse_session in (True,):
            with self.subTest(reuse_session=reuse_session):
                client = CodexClient(cwd=str(self.project))
                before_thread_count = len(self.thread_start_calls)
                before_prompt_count = len(self.thread_prompts)
                response = client.run(
                    input="REVIEW_TRIGGER_REPAIR",
                    review=ReviewConfig(
                        context="REVIEW_SHOULD_FAIL_ON_BAD_RESPONSE",
                        prompt_path=template,
                        reuse_session=reuse_session,
                    ),
                    repair_validation_failures=True,
                    timeout=3,
                )

                self.assertEqual(response.content, "review repaired answer")
                review_thread_calls = [
                    call
                    for call in self.thread_start_calls[before_thread_count:]
                    if call.get("developer_instructions")
                    and "runner-reviewer conversation session"
                    in call["developer_instructions"]
                ]
                expected_threads = 1 if reuse_session else 2
                self.assertEqual(len(review_thread_calls), expected_threads)
                for call in review_thread_calls:
                    self.assertEqual(
                        call["developer_instructions"].count(
                            "REVIEW_SHOULD_FAIL_ON_BAD_RESPONSE"
                        ),
                        1,
                    )
                review_prompts = [
                    prompt
                    for prompt in self.thread_prompts[before_prompt_count:]
                    if "You are a peer reviewer. Return only a strict JSON object" in prompt
                ]
                self.assertIn(
                    "You are a peer reviewer. Return only a strict JSON object",
                    review_prompts[0],
                )
                self.assertIn(
                    "Original job request:\nREVIEW_TRIGGER_REPAIR",
                    review_prompts[0],
                )
                if reuse_session:
                    self.assertEqual(len(review_prompts), 2)
                    self.assertIn('"round": 1', review_prompts[1])
                    self.assertNotIn("Original job request:", review_prompts[1])

    def test_review_logs_reviewed_response_summary(self):
        client = CodexClient(cwd=str(self.project))

        response = client.run(
            input="企業について短い構造化分析を行い、JSON だけを返してください。\n\nCompany name:\n日本アイ・ビー・エム株式会社",
            review=ReviewConfig(context="REVIEW_SHOULD_PASS"),
            timeout=3,
        )

        review_input_entries = [
            entry for entry in response.activity_log
            if entry.phase == "review" and entry.event == "reviewed_response"
        ]
        self.assertGreaterEqual(len(review_input_entries), 1)
        self.assertIn("company_name=日本アイ・ビー・エム株式会社", review_input_entries[0].message)
        self.assertIn("references=2", review_input_entries[0].message)
        self.assertNotIn('"summary":', review_input_entries[0].message)

        log_text = pathlib.Path(response.log_path).read_text(encoding="utf-8")
        self.assertIn("[Reviewer][Reviewed Response]", log_text)
        self.assertIn("company_name=日本アイ・ビー・エム株式会社", log_text)
        self.assertNotIn('"summary":', log_text)

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
                                        text='{"decisions": [], "new_findings": []}',
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
                if "You are a peer reviewer. Return only a strict JSON object" in prompt:
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
            ApprovalMode=_FAKE_APPROVAL_MODE,
        )

        previous_import = client_module._import_openai_codex
        client_module._import_openai_codex = lambda: fake_sdk
        try:
            client = CodexClient(cwd=str(self.project))
            captured_stdout = io.StringIO()
            with contextlib.redirect_stdout(captured_stdout):
                response = client.run(
                    input="REVIEW_STREAM_LOGGING",
                    review=ReviewConfig(context="REVIEW_STREAM_LOGGING"),
                    timeout=3,
                )
        finally:
            client_module._import_openai_codex = previous_import

        self.assertEqual(response.content, "needs review fix")
        self.assertTrue(captured.get("closed"))
        self.assertTrue(
            any(
                entry.phase == "stream" and entry.detail == "review"
                and entry.attempt == 0
                and entry.repair_index == 0
                for entry in response.activity_log
            )
        )
        printed = captured_stdout.getvalue()
        self.assertIn("[Reviewer][Thinking]", printed)
        self.assertNotIn("[Reviewer][Thinking] attempt=0 repair=0", printed)
        self.assertIn("[Reviewer][Search]", printed)
        self.assertIn("[Reviewer][Message]", printed)

    def test_validation_failure_skips_review(self):
        client = CodexClient(cwd=str(self.project))

        with self.assertRaises(CodexValidationError) as caught:
            client.run(
                input="x",
                validate_output=lambda response: (_ for _ in ()).throw(
                    ValueError("validator failed")
                ),
                review=ReviewConfig(context="REVIEW_FORCE_BAD_OUTPUT"),
                timeout=3,
            )

        self.assertEqual(caught.exception.failure_stage, "validator")

    def test_review_can_see_original_input_without_main_context_by_default(self):
        client = CodexClient(cwd=str(self.project))

        response = client.run(
            input="REVIEW_CONTEXT_MARKER",
            subcwd_files={"state.json": {"text": '{"ok": true}\n'}},
            read_paths=["${SUBCWD}/state.json"],
            review=ReviewConfig(context="CHECK_REVIEW_CONTEXT"),
            collect_files=True,
            timeout=3,
        )

        self.assertEqual(response.content, "fake final answer")

    def test_reviewer_subagents_reuse_materialized_main_artifact_paths(self):
        artifact = self.project / "recruitment_case.json"
        artifact.write_text('{"company_id": "1"}\n', encoding="utf-8")
        reviewer_session = SessionConfig(
            allow_shell_commands=False,
            workspace=WorkspaceConfig(
                cwd=self.project,
                read_paths=("${SUBCWD}/.",),
                write_paths=(),
            ),
        )
        child_session = SessionConfig(
            allow_shell_commands=False,
            workspace=WorkspaceConfig(
                cwd=self.project,
                read_paths=("${SUBCWD}/.",),
                write_paths=(),
            ),
        )
        review = ReviewConfig(
            context="CHECK_REVIEW_CONTEXT",
            session=reviewer_session,
            subagents=SubagentConfig(
                session=child_session,
                allowed_models={child_session.model: (child_session.reasoning_effort,)},
                max_concurrent=1,
            ),
        )
        parent_config, child_config = (
            client_module._materialize_reviewer_subagent_configs(
                review,
                (artifact,),
            )
        )

        self.assertEqual(parent_config.workspace.read_paths, (artifact,))
        self.assertEqual(child_config.session.workspace.read_paths, (artifact,))
        self.assertEqual(child_config.session.workspace.write_paths, ())

        class _FakeChildJob:
            configs = []

            def __init__(self, *, config, input, _conversation_state=None, **_kwargs):
                self.config = config
                self._conversation_state = object()
                self.log_path = None
                type(self).configs.append(config)

            def start(self):
                return self

            def result(self):
                return SimpleNamespace(
                    content="ok",
                    usage={"input_tokens": 1},
                    estimated_cost_usd=0.01,
                )

        manager = _DirectChildManager(
            parent_config=parent_config,
            config=child_config,
        )
        manager.validate_permissions(parent_tools=())
        manager.activate(timeout=None)
        with mock.patch.object(public_job_module, "CodexJob", _FakeChildJob):
            target = manager.spawn_agent("initial audit", "artifact_audit")["target"]
            manager.wait_agent(timeout_ms=1000)
            manager.followup_task(target, "re-read the artifact")
            manager.wait_agent(timeout_ms=1000)
        manager.close()

        self.assertEqual(len(_FakeChildJob.configs), 2)
        for config in _FakeChildJob.configs:
            self.assertEqual(config.workspace.read_paths, (artifact,))
            self.assertEqual(config.workspace.write_paths, ())

    def test_review_can_opt_in_to_main_context(self):
        client = CodexClient(cwd=str(self.project))

        response = client.run(
            input="REVIEW_CONTEXT_MARKER REQUIRE_MAIN_CONTEXT",
            review=ReviewConfig(
                context="CHECK_REVIEW_CONTEXT",
                include_main_context=True,
            ),
            timeout=3,
        )

        self.assertEqual(response.content, "fake final answer")

    def test_review_reuses_main_read_paths(self):
        captured = {}
        client = CodexClient(cwd=str(self.project))

        class _FakeReviewSession:
            def _send(self, message, *, output_schema_path=None, activity_callback=None):
                captured["message"] = message
                captured["output_schema_path"] = output_schema_path
                captured["output_schema"] = json.loads(
                    pathlib.Path(output_schema_path).read_text(encoding="utf-8")
                )
                return SimpleNamespace(
                    content='{"decisions": [], "new_findings": []}',
                    stdout="",
                    stderr="",
                    argv=["review"],
                    returncode=0,
                    usage=None,
                )

            def close(self):
                return None

        def fake_create_review_session(*, review_config, options, read_paths, **_ignored):
            captured["read_paths"] = list(read_paths)
            captured["artifact_paths"] = tuple(review_config.artifact_paths)
            return _FakeReviewSession()

        client._create_review_session = fake_create_review_session

        response = client.run(
            input="REVIEW_CONTEXT_MARKER",
            read_paths=["README.md", "${SUBCWD}/state.json"],
            subcwd_files={"state.json": {"text": '{"ok": true}\n'}},
            review=ReviewConfig(
                context="CHECK_REVIEW_CONTEXT",
                artifact_paths=("${SUBCWD}/state.json",),
            ),
            collect_files=True,
            timeout=3,
        )

        self.assertEqual(response.content, "fake final answer")
        self.assertEqual(captured["read_paths"][0], self.project / "README.md")
        self.assertEqual(captured["read_paths"][1].name, "state.json")
        self.assertEqual(captured["artifact_paths"], (captured["read_paths"][1],))
        self.assertIn('"open_findings": []', captured["message"])
        self.assertIn("state.json", captured["message"])
        self.assertNotIn('\\"ok\\": true', captured["message"])
        self.assertEqual(
            captured["output_schema"]["required"], ["decisions", "new_findings"]
        )
        self.assertEqual(
            captured["output_schema"]["properties"]["decisions"]["maxItems"], 0
        )
        review_detail_entries = [
            entry for entry in response.activity_log
            if entry.phase == "review" and entry.event == "detail"
        ]
        self.assertTrue(
            any("Review readable paths:" in entry.message for entry in review_detail_entries)
        )
        self.assertTrue(
            any(
                entry.event == "input"
                and "review input prepared" in entry.message
                and "sha256=" in entry.message
                for entry in response.activity_log
                if entry.phase == "review"
            )
        )
        self.assertTrue(
            any("- " + str(self.project / "README.md") in entry.message for entry in review_detail_entries)
        )
        self.assertTrue(
            any("- ${SUBCWD}/state.json" in entry.message for entry in review_detail_entries)
        )

    def test_review_can_override_main_read_paths(self):
        captured = {}
        client = CodexClient(cwd=str(self.project))

        class _FakeReviewSession:
            def _send(self, message, *, output_schema_path=None, activity_callback=None):
                captured["message"] = message
                return SimpleNamespace(
                    content='{"decisions": [], "new_findings": []}',
                    stdout="",
                    stderr="",
                    argv=["review"],
                    returncode=0,
                    usage=None,
                )

            def close(self):
                return None

        def fake_create_review_session(*, review_config, options, read_paths, **_ignored):
            captured["read_paths"] = list(read_paths)
            return _FakeReviewSession()

        client._create_review_session = fake_create_review_session

        response = client.run(
            input="REVIEW_CONTEXT_MARKER",
            read_paths=["README.md", "${SUBCWD}/state.json"],
            subcwd_files={"state.json": {"text": '{"ok": true}\n'}},
            review=ReviewConfig(
                context="CHECK_REVIEW_CONTEXT",
                session=SessionConfig(
                    workspace=WorkspaceConfig(
                        read_paths=["${SUBCWD}/state.json"],
                    ),
                ),
            ),
            collect_files=True,
            timeout=3,
        )

        self.assertEqual(response.content, "fake final answer")
        self.assertEqual(len(captured["read_paths"]), 1)
        self.assertEqual(captured["read_paths"][0].name, "state.json")
        self.assertIn("review-package.json:", captured["message"])
        review_detail_entries = [
            entry for entry in response.activity_log
            if entry.phase == "review" and entry.event == "detail"
        ]
        self.assertTrue(
            any("Review readable paths:" in entry.message for entry in review_detail_entries)
        )
        self.assertTrue(
            any("- ${SUBCWD}/state.json" in entry.message for entry in review_detail_entries)
        )
        self.assertFalse(
            any("- " + str(self.project / "README.md") in entry.message for entry in review_detail_entries)
        )

    def test_review_timeout_is_execution_error(self):
        client = CodexClient(cwd=str(self.project))

        with self.assertRaises(CodexExecutionError) as caught:
            client.run(
                input="x",
                review=ReviewConfig(
                    context="FORCE_REVIEW_TIMEOUT",
                    session=SessionConfig(timeout=0.1),
                ),
                timeout=3,
            )

        self.assertIn("timed out", caught.exception.message)

    def test_review_bad_output_is_corrected_on_the_same_session(self):
        client = CodexClient(cwd=str(self.project))

        response = client.run(
            input="x",
            review=ReviewConfig(context="REVIEW_FORCE_BAD_OUTPUT"),
            timeout=3,
        )

        self.assertEqual(response.content, "fake final answer")
        review_events = [
            entry.event for entry in response.activity_log if entry.phase == "review"
        ]
        self.assertIn("invalid", review_events)
        self.assertIn("regenerate", review_events)

    def test_subcwd_files_stage_text_and_collect_edited_result(self):
        client = CodexClient(cwd=str(self.project))

        response = client.run(
            input="EDIT_STAGED_TEXT_FILE",
            subcwd_files={
                "inputs/neighborhood_tree.json": {
                    "text": '{"status": "draft"}\n',
                }
            },
            read_paths=["${SUBCWD}/inputs/neighborhood_tree.json"],
            write_paths=["${SUBCWD}/inputs/neighborhood_tree.json"],
            collect_files=True,
            timeout=3,
        )

        self.assertEqual(
            response.files["inputs/neighborhood_tree.json"].text,
            '{"status": "edited"}\n',
        )
        self.assertFalse(self._subcwd_dirs())

    def test_subcwd_files_prompt_keeps_permissions_explicit(self):
        client = CodexClient(cwd=str(self.project))

        response = client.run(
            input="ECHO_PROMPT",
            subcwd_files={"inputs/neighborhood_tree.json": {"text": "{}\n"}},
            read_paths=["${SUBCWD}/inputs/neighborhood_tree.json"],
            timeout=3,
        )

        self.assertIn("Workspace files staged under ${SUBCWD}:", response.content)
        self.assertIn("- ${SUBCWD}/inputs/neighborhood_tree.json", response.content)
        self.assertIn(
            "Only read or edit a staged file when the corresponding ${SUBCWD}/... "
            "path is covered by this job's read_paths or write_paths.",
            response.content,
        )
        self.assertFalse(self._subcwd_dirs())

    def test_subcwd_cleanup_failure_is_reflected_in_response_stderr(self):
        client = CodexClient(cwd=str(self.project))
        previous_rmtree = job_module.shutil.rmtree
        previous_sdk_homes = set(self._sdk_home_dirs())

        def fail_rmtree(path):
            raise OSError("cleanup denied")

        job_module.shutil.rmtree = fail_rmtree
        try:
            response = client.run(
                input="x",
                subcwd_files={"report.md": {"text": ""}},
                write_paths=["${SUBCWD}/report.md"],
                collect_files=True,
                timeout=3,
            )
        finally:
            job_module.shutil.rmtree = previous_rmtree
            for path in self._subcwd_dirs():
                previous_rmtree(path)
            for path in set(self._sdk_home_dirs()) - previous_sdk_homes:
                previous_rmtree(path)

        self.assertIn("subcwd cleanup failed: cleanup denied", response.stderr)
        self.assertTrue(
            any(
                entry.phase == "cleanup"
                and entry.event == "error"
                and "subcwd cleanup failed" in entry.message
                for entry in response.activity_log
            )
        )

    def test_subcwd_files_stage_bytes(self):
        client = CodexClient(cwd=str(self.project))

        response = client.run(
            input="SUM_STAGED_BINARY_FILE",
            subcwd_files={
                "inputs/blob.bin": {
                    "bytes": b"\x00\x01\x02\x03",
                },
                "outputs/report.txt": {"text": ""},
            },
            read_paths=["${SUBCWD}/inputs/blob.bin"],
            write_paths=["${SUBCWD}/outputs/report.txt"],
            collect_files=True,
            timeout=3,
        )

        self.assertEqual(response.content, "binary-bytes=4")
        self.assertEqual(
            response.files["outputs/report.txt"].text,
            "binary-bytes=4\n",
        )

    def test_validation_repair_reuses_subcwd_and_collects_final_files(self):
        client = CodexClient(cwd=str(self.project))

        response = client.run(
            input="NEEDS_REPAIR MAKE_REPAIR_SUBCWD_FILE",
            subcwd_files={"data.json": {"text": "{}\n"}},
            write_paths=["${SUBCWD}/data.json"],
            collect_files=True,
            validate_output=self._require_repaired_json,
            repair_validation_failures=True,
            timeout=3,
        )

        self.assertEqual(response.content, '{"ok": true}')
        self.assertEqual(response.files["data.json"].json(), {"ok": True})
        self.assertFalse(self._subcwd_dirs())

    def test_validation_repair_reuses_staged_subcwd_files_without_restaging(self):
        client = CodexClient(cwd=str(self.project))

        response = client.run(
            input="NEEDS_REPAIR_CHECK_STAGED_PERSISTENCE",
            subcwd_files={
                "state.json": {
                    "text": '{"state": "initial"}\n',
                }
            },
            read_paths=["${SUBCWD}/state.json"],
            write_paths=["${SUBCWD}/state.json"],
            collect_files=True,
            validate_output=self._require_repaired_json,
            repair_validation_failures=True,
            timeout=3,
        )

        self.assertEqual(response.content, '{"ok": true}')
        self.assertEqual(
            response.files["state.json"].text,
            '{"state": "mutated"}\n',
        )

    def test_non_callable_validate_output_errors_before_start(self):
        client = CodexClient(cwd=str(self.project))

        with self.assertRaises(TypeError):
            client.start(
                input="x",
                validate_output="./missing-validator",
            )

    def test_collect_files_reads_subcwd_into_response_and_cleans_up(self):
        client = CodexClient(cwd=str(self.project))

        job = client.start(
            input="MAKE_SUBCWD_FILES",
            subcwd_files={
                "report.md": {"text": ""},
                "data.json": {"text": "{}\n"},
            },
            write_paths=["${SUBCWD}/report.md", "${SUBCWD}/data.json"],
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
        rendered = filesystem_args[0].split("=", 1)[1]
        parsed = tomllib.loads("filesystem = " + rendered)["filesystem"]
        self.assertNotIn(".", parsed[str(self.project)])
        relative_rules = parsed[str(self.project)]
        self.assertEqual(len(relative_rules), 2)
        self.assertEqual(set(relative_rules.values()), {"write"})
        self.assertEqual(
            {pathlib.PurePosixPath(path).name for path in relative_rules},
            {"report.md", "data.json"},
        )

    def test_subcwd_root_relative_path_creates_subcwd_under_custom_parent(self):
        client = CodexClient(
                        cwd=str(self.project),
            subcwd_root="tmp/jobs",
        )

        response = client.run(
            input="MAKE_SUBCWD_FILES",
            subcwd_files={
                "report.md": {"text": ""},
                "data.json": {"text": "{}\n"},
            },
            write_paths=["${SUBCWD}/report.md", "${SUBCWD}/data.json"],
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
            subcwd_files={
                "report.md": {"text": ""},
                "data.json": {"text": "{}\n"},
            },
            write_paths=["${SUBCWD}/report.md", "${SUBCWD}/data.json"],
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
            subcwd_files={
                "report.md": {"text": ""},
                "data.json": {"text": "{}\n"},
            },
            write_paths=["${SUBCWD}/report.md", "${SUBCWD}/data.json"],
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
            client.start(input="x", write_paths=["${SUBCWD}/x"])

    def test_subcwd_root_rejects_subcwd_token(self):
        client = CodexClient(
                        cwd=str(self.project),
            subcwd_root="tmp/${SUBCWD}",
        )

        with self.assertRaises(ValueError):
            client.start(input="x", write_paths=["${SUBCWD}/x"])

    def test_subcwd_without_collect_files_cleans_up(self):
        client = CodexClient(cwd=str(self.project))

        response = client.run(
            input="MAKE_SUBCWD_FILES",
            subcwd_files={
                "report.md": {"text": ""},
                "data.json": {"text": "{}\n"},
            },
            write_paths=["${SUBCWD}/report.md", "${SUBCWD}/data.json"],
            timeout=3,
        )

        self.assertEqual(response.files, {})
        self.assertFalse(self._subcwd_dirs())

    def test_subcwd_files_require_explicit_read_or_write_paths(self):
        client = CodexClient(cwd=str(self.project))

        with self.assertRaises(ValueError):
            client.start(
                input="EDIT_STAGED_TEXT_FILE",
                subcwd_files={
                    "inputs/neighborhood_tree.json": {
                        "text": '{"status": "draft"}\n',
                    }
                },
                timeout=3,
            )

    def test_collect_files_limit_error_cleans_up(self):
        client = CodexClient(cwd=str(self.project))

        job = client.start(
            input="MAKE_SUBCWD_FILES",
            subcwd_files={
                "report.md": {"text": ""},
                "data.json": {"text": "{}\n"},
            },
            write_paths=["${SUBCWD}/report.md", "${SUBCWD}/data.json"],
            collect_files=True,
            max_file_bytes=1,
        )

        with self.assertRaises(CodexExecutionError) as caught:
            job.result(timeout=3)
        self.assertIn("file collection failed", caught.exception.message)
        self.assertFalse(self._subcwd_dirs())

    def test_bare_subcwd_is_rejected_for_read_paths(self):
        client = CodexClient(cwd=str(self.project))

        with self.assertRaises(ValueError):
            client.start(input="x", read_paths=["${SUBCWD}"])

    def test_bare_subcwd_is_rejected_for_write_paths(self):
        client = CodexClient(cwd=str(self.project))

        with self.assertRaises(ValueError):
            client.start(input="x", write_paths=["${SUBCWD}"])

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
        fast._thread.join(timeout=1)
        self.assertFalse(fast._thread.is_alive())
        slow.cancel()
        with self.assertRaises(CodexCancelledError):
            slow.result(timeout=1)
        slow._thread.join(timeout=1)
        self.assertFalse(slow._thread.is_alive())

    def test_missing_context_errors_before_start(self):
        client = CodexClient(cwd=str(self.project), context=["missing.txt"])

        with self.assertRaises(FileNotFoundError):
            client.start(input="x")

    def _subcwd_dirs(self):
        return [
            path
            for path in self.project.iterdir()
            if path.is_dir() and path.name not in {"out", ".codex-job-runner-logs"}
        ]

    def _sdk_home_dirs(self):
        return list(pathlib.Path("/dev/shm").glob("codex_job_runner_home_*"))

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
            def __init__(self, prompt, context_prompt=None):
                self.prompt = prompt
                self.context_prompt = prompt if context_prompt is None else context_prompt
                self._interrupted = False

            def run(self):
                if test_case.fake_turn_callback is not None:
                    test_case.fake_turn_callback(
                        self.prompt,
                        self.context_prompt,
                    )
                test_case._apply_fake_side_effects(self.context_prompt)
                if "sleep" in self.context_prompt or "FORCE_REVIEW_TIMEOUT" in self.context_prompt:
                    for _ in range(100):
                        if self._interrupted:
                            return FakeTurnResult(self.context_prompt)
                        time.sleep(0.1)
                if "FORCE_FAIL" in self.context_prompt:
                    raise RuntimeError("FAKE_STDERR")
                result_prompt = (
                    self.prompt
                    if "ECHO_PROMPT" in self.prompt
                    else self.context_prompt
                )
                return FakeTurnResult(result_prompt)

            def interrupt(self):
                self._interrupted = True

        class FakeThread:
            def __init__(self, thread_id, developer_instructions=None):
                self.id = thread_id
                self.prompts = []
                self.developer_instructions = developer_instructions

            def turn(self, prompt, **kwargs):
                self.prompts.append(prompt)
                test_case.thread_prompts.append(prompt)
                test_case.turn_calls.append(kwargs)
                context_parts = []
                if self.developer_instructions:
                    context_parts.append(self.developer_instructions)
                context_parts.extend(self.prompts)
                return FakeTurn(prompt, "\n\n".join(context_parts))

        class FakeCodex:
            def __init__(self, config=None):
                self.config = config
                test_case.sdk_configs.append(config)

            def thread_start(self, **kwargs):
                test_case.thread_start_calls.append(kwargs)
                thread_id = f"thread-{len(test_case.sdk_threads) + 1}"
                thread = FakeThread(
                    thread_id,
                    kwargs.get("developer_instructions"),
                )
                test_case.sdk_threads[thread_id] = thread
                return thread

            def thread_resume(self, thread_id, **kwargs):
                del kwargs
                return test_case.sdk_threads[thread_id]

            def thread_fork(self, thread_id, **kwargs):
                source = test_case.sdk_threads[thread_id]
                fork_id = f"thread-{len(test_case.sdk_threads) + 1}"
                thread = FakeThread(
                    fork_id,
                    kwargs.get(
                        "developer_instructions",
                        source.developer_instructions,
                    ),
                )
                thread.prompts = list(source.prompts)
                test_case.sdk_threads[fork_id] = thread
                return thread

            def close(self):
                return None

        fake_sdk = SimpleNamespace(
            Codex=FakeCodex,
            CodexConfig=lambda **kwargs: SimpleNamespace(**kwargs),
            Sandbox=SimpleNamespace(read_only="read_only", workspace_write="workspace_write"),
            ApprovalMode=_FAKE_APPROVAL_MODE,
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
        for terminator in ("\n\nSubcwd path:\n", "\n\nReview context:\n"):
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
        if "NEEDS_REPAIR" in prompt and "You are a peer reviewer. Return only a strict JSON object" not in prompt:
            final_answer = '{"ok": true}' if is_repair else 'not json'
        if "You are a peer reviewer. Return only a strict JSON object" in prompt:
            if "REVIEW_FORCE_BAD_OUTPUT" in prompt:
                final_answer = "not json"
            elif '"open_findings": []' not in prompt.rsplit("review-package.json:", 1)[-1]:
                final_answer = '{"decisions":[{"finding_id":"F001","resolution":"resolved","comment":"ok"}],"new_findings":[]}'
            elif ("REVIEW_SHOULD_FAIL_ON_BAD_RESPONSE" in prompt or "Unverified Salary Blog" in prompt):
                final_answer = '{"decisions":[],"new_findings":[{"comment":"Please change the final response to review repaired answer."}]}'
            else:
                final_answer = '{"decisions":[],"new_findings":[]}'
        if "Your immediately preceding reviewer decision is invalid" in prompt:
            final_answer = '{"decisions":[],"new_findings":[]}'
        if "REVIEW_TRIGGER_REPAIR" in prompt and "You are a peer reviewer. Return only a strict JSON object" not in prompt:
            if is_repair and "Please change the final response to review repaired answer." in prompt:
                final_answer = "review repaired answer"
            else:
                final_answer = "needs review fix"
        if "ECHO_PROMPT" in prompt and "You are a peer reviewer. Return only a strict JSON object" not in prompt:
            final_answer = prompt
        if "Internal review mode: codex_job_runner" in prompt and "You are a peer reviewer. Return only a strict JSON object" not in prompt:
            if "REVIEW_FORCE_BAD_OUTPUT" in prompt:
                final_answer = "not json"
            elif "CHECK_REVIEW_CONTEXT" in prompt:
                if "Original request:\nREVIEW_CONTEXT_MARKER" not in prompt:
                    final_answer = '{"decisions":[],"new_findings":[{"comment":"missing original input"}]}'
                elif "REQUIRE_MAIN_CONTEXT" in prompt and "Main job context:" not in prompt:
                    final_answer = '{"decisions":[],"new_findings":[{"comment":"missing main context"}]}'
                else:
                    final_answer = '{"decisions":[],"new_findings":[]}'
            elif "REVIEW_SHOULD_FAIL_ON_BAD_RESPONSE" in prompt:
                if (
                    "Revised main job final response:\nreview repaired answer"
                    in prompt
                ):
                    final_answer = '{"decisions":[{"finding_id":"F001","resolution":"resolved","comment":"ok"}],"new_findings":[]}'
                elif "Main job final response:\nneeds review fix" in prompt:
                    final_answer = '{"decisions":[],"new_findings":[{"comment":"Please change the final response to review repaired answer."}]}'
                else:
                    final_answer = '{"decisions":[],"new_findings":[]}'
            elif "Use the references in the JSON output as the starting point for verification." in prompt:
                reviewed_response = self._extract_review_main_response(prompt)
                if "Unverified Salary Blog" in reviewed_response:
                    final_answer = '{"decisions":[],"new_findings":[{"comment":"Replace the salary reference with a primary or otherwise reliable salary source."}]}'
                else:
                    final_answer = '{"decisions":[],"new_findings":[]}'
            else:
                final_answer = '{"decisions":[],"new_findings":[]}'
        if "SUM_STAGED_BINARY_FILE" in prompt:
            subcwd = self._extract_subcwd(prompt)
            size = len((subcwd / "inputs" / "blob.bin").read_bytes())
            final_answer = f"binary-bytes={size}"
        if "NEEDS_REPAIR_CHECK_STAGED_PERSISTENCE" in prompt and not is_repair:
            final_answer = "not json"
        if "Return only a JSON object with finding_responses." in prompt:
            final_answer = (
                '{"finding_responses":[{"finding_id":"F001",'
                '"disposition":"addressed","response":"fixed"}]}'
            )
        return final_answer

    def _apply_fake_side_effects(self, prompt):
        is_repair = self._is_repair_prompt(prompt)
        subcwd = self._extract_subcwd(prompt)
        if "EDIT_ALLOWED_FILE" in prompt:
            for line in prompt.splitlines():
                if line.startswith("- ") and line[2:].endswith("/allowed.txt"):
                    pathlib.Path(line[2:]).write_text("updated\n", encoding="utf-8")
                    break
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
        del name, source
        return None


if __name__ == "__main__":
    unittest.main()
