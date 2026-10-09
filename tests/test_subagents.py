import json
import pathlib
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from codex_job_runner import (  # noqa: E402
    CodexJob,
    SessionConfig,
    SubagentConfig,
    Tool,
    ToolConfig,
    WorkspaceConfig,
)
from codex_job_runner._subagents import (  # noqa: E402
    _DirectChildManager,
    _validate_child_permissions,
)


def echo(value: str) -> str:
    return value


def make_tool():
    return Tool(
        echo,
        "Echo a value.",
        args={"value": "Value to echo."},
        returns="The echoed value.",
    )


class _FakeChildJob:
    instances = []

    def __init__(
        self,
        *,
        config,
        input,
        _conversation_state=None,
        _activity_actor=None,
        _rollout_dir=None,
    ):
        self.config = config
        self.input = input
        self.rollout_dir = _rollout_dir
        self._conversation_state = (
            _conversation_state
            if _conversation_state is not None
            else SimpleNamespace(thread_id=f"thread-{len(self.instances)}")
        )
        self.status = "pending"
        self.log_path = None
        self.cancelled = False
        self.activity_actor = _activity_actor
        self.instances.append(self)

    def start(self):
        self.status = "running"
        return self

    def result(self):
        for _ in range(100):
            if self.cancelled:
                self.status = "cancelled"
                raise RuntimeError("cancelled")
            break
        self.status = "completed"
        return SimpleNamespace(
            content=f"done:{self.input}",
            usage={"input_tokens": 1},
            estimated_cost_usd=0.01,
        )

    def cancel(self):
        self.cancelled = True
        self.status = "cancelled"


class _LoggingFakeChildJob(_FakeChildJob):
    def start(self):
        self.status = "running"
        self.log_path = pathlib.Path(self.config.workspace.cwd) / "child.log"
        self.log_path.write_text(
            f"[{self.activity_actor}][Start] attempt=0 repair=0\n",
            encoding="utf-8",
        )
        return self


class _SlowLoggingFakeChildJob(_LoggingFakeChildJob):
    def result(self):
        time.sleep(0.05)
        return super().result()


class _StuckChildJob(_FakeChildJob):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.forced = False
        self._released = threading.Event()

    def result(self):
        self._released.wait()
        self.status = "cancelled"
        raise RuntimeError("cancelled")

    def cancel(self):
        self.cancelled = True

    def _terminate_process(self, _phase):
        self.forced = True
        self._released.set()


class _AskingFakeChildJob(_FakeChildJob):
    def result(self):
        ask_parent = next(
            tool
            for tool in self.config.tools.tools
            if tool.name == "ask_parent"
        )
        answer = ask_parent.invoke({"message": "Which value should I use?"})
        self.status = "completed"
        return SimpleNamespace(
            content=f"parent answered:{answer}",
            usage={"input_tokens": 1},
            estimated_cost_usd=0.01,
        )


class DirectChildPermissionTests(unittest.TestCase):
    def test_child_permissions_may_only_narrow_parent(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = pathlib.Path(tempdir)
            tool = make_tool()
            parent = SessionConfig(
                env={"SHARED": "yes"},
                allowed_skills=None,
                workspace=WorkspaceConfig(
                    cwd=root,
                    read_paths=("src",),
                    write_paths=("out",),
                ),
                tools=ToolConfig(tools=(tool,)),
            )
            child = SessionConfig(
                allow_shell_commands=False,
                env={"SHARED": "yes"},
                allowed_skills=(),
                workspace=WorkspaceConfig(
                    cwd=root,
                    read_paths=("src/pkg",),
                    write_paths=("out/report",),
                ),
                tools=ToolConfig(tools=(tool,)),
            )

            result = _validate_child_permissions(
                parent,
                child,
                parent_tools=(tool,),
            )

            self.assertEqual(result.workspace.write_paths, ("out/report",))

    def test_child_timeout_is_capped_by_parent(self):
        parent = SessionConfig(timeout=30)
        child = SessionConfig(timeout=90)

        result = _validate_child_permissions(
            parent,
            child,
            parent_tools=(),
        )

        self.assertEqual(result.timeout, 30.0)

    def test_child_write_shell_skill_tool_and_environment_escalation_fail(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = pathlib.Path(tempdir)
            parent_tool = make_tool()
            other_tool = make_tool()
            parent = SessionConfig(
                allow_shell_commands=False,
                env={"SHARED": "yes"},
                allowed_skills=(),
                workspace=WorkspaceConfig(
                    cwd=root,
                    read_paths=("src",),
                    write_paths=("out",),
                ),
                tools=ToolConfig(tools=(parent_tool,)),
            )
            cases = (
                SessionConfig(
                    allow_shell_commands=False,
                    workspace=WorkspaceConfig(cwd=root, write_paths=("src",)),
                ),
                SessionConfig(
                    allow_shell_commands=True,
                    workspace=WorkspaceConfig(cwd=root),
                ),
                SessionConfig(
                    allow_shell_commands=False,
                    allowed_skills=None,
                    workspace=WorkspaceConfig(cwd=root),
                ),
                SessionConfig(
                    allow_shell_commands=False,
                    tools=ToolConfig(tools=(other_tool,)),
                    workspace=WorkspaceConfig(cwd=root),
                ),
                SessionConfig(
                    allow_shell_commands=False,
                    env={"NEW": "value"},
                    workspace=WorkspaceConfig(cwd=root),
                ),
            )

            for child in cases:
                with self.subTest(child=child):
                    with self.assertRaises(ValueError):
                        _validate_child_permissions(
                            parent,
                            child,
                            parent_tools=(parent_tool,),
                        )


class DirectChildManagerTests(unittest.TestCase):
    def setUp(self):
        _FakeChildJob.instances.clear()
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        root = pathlib.Path(self.tempdir.name)
        self.parent = SessionConfig(
            workspace=WorkspaceConfig(cwd=root),
        )
        child = SessionConfig(
            model="gpt-5.6-luna",
            reasoning_effort="medium",
            allow_shell_commands=False,
            workspace=WorkspaceConfig(cwd=root),
        )
        self.config = SubagentConfig(
            session=child,
            allowed_models={
                "gpt-5.6-luna": ("low", "medium"),
                "gpt-5.6-terra": ("low",),
            },
            max_concurrent=2,
        )
        self.events = []
        self.manager = _DirectChildManager(
            parent_config=self.parent,
            config=self.config,
            activity_callback=lambda event, message: self.events.append(
                (event, message)
            ),
        )
        self.manager.validate_permissions(parent_tools=())
        self.addCleanup(self.manager.close)

    def _wait_completed(self, target):
        for _ in range(100):
            snapshot = next(
                item
                for item in self.manager.list_agents()
                if item["target"] == target
            )
            if snapshot["status"] == "completed":
                return snapshot
            time.sleep(0.001)
        self.fail("child did not complete")

    def test_tools_match_v2_names(self):
        self.assertEqual(
            tuple(tool.name for tool in self.manager.tools()),
            (
                "spawn_agent",
                "send_message",
                "followup_task",
                "interrupt_agent",
                "list_agents",
                "wait_agent",
            ),
        )

    def test_child_rollouts_are_kept_under_parent_job(self):
        self.manager._rollout_dir = lambda: pathlib.Path("/logs/parent/rollouts/subagents")
        with mock.patch("codex_job_runner.job.CodexJob", _FakeChildJob):
            snapshot = self.manager.spawn_agent(message="research", task_name="research_1")
            self._wait_completed(snapshot["target"])
        self.assertEqual(
            _FakeChildJob.instances[0].rollout_dir,
            pathlib.Path("/logs/parent/rollouts/subagents/research_1"),
        )

    def test_spawn_uses_allowed_model_without_parent_context(self):
        with mock.patch("codex_job_runner.job.CodexJob", _FakeChildJob):
            snapshot = self.manager.spawn_agent(
                message="research",
                task_name="research_1",
                model="gpt-5.6-terra",
                reasoning_effort="low",
            )
            completed = self._wait_completed(snapshot["target"])

        child = _FakeChildJob.instances[0]
        self.assertEqual(child.config.model, "gpt-5.6-terra")
        self.assertEqual(child.input, "research")
        self.assertEqual(completed["latest"]["status"], "completed")
        self.assertEqual(completed["latest"]["usage"], {"input_tokens": 1})
        self.assertEqual(self.manager.estimated_cost_usd(), 0.01)
        self.assertTrue(self.manager.cost_complete())
        self.assertTrue(any(event == "spawn" for event, _ in self.events))

    def test_child_ask_parent_resumes_same_turn_after_send_message_answer(self):
        with mock.patch("codex_job_runner.job.CodexJob", _AskingFakeChildJob):
            spawned = self.manager.spawn_agent(
                message="ask before continuing",
                task_name="asker",
            )
            waiting = self.manager.wait_agent(timeout_ms=1000)
            delivered = self.manager.send_message(
                spawned["target"],
                "Use 42.",
            )
            completed = self._wait_completed(spawned["target"])

        child = _AskingFakeChildJob.instances[0]
        self.assertEqual(
            tuple(tool.name for tool in child.config.tools.tools),
            ("ask_parent",),
        )
        self.assertEqual(
            waiting[0]["pending_question"],
            {"message": "Which value should I use?"},
        )
        self.assertEqual(
            delivered["delivery"],
            "answered_parent_question",
        )
        self.assertEqual(completed["turns"], 1)
        self.assertEqual(
            completed["latest"]["content"],
            "parent answered:Use 42.",
        )
        self.assertEqual(completed["queued_messages"], 0)
        self.assertTrue(any(event == "question" for event, _ in self.events))
        self.assertTrue(any(event == "answer" for event, _ in self.events))

    def test_child_timeout_uses_parent_remaining_deadline(self):
        self.manager.activate(timeout=1.0)
        with mock.patch("codex_job_runner.job.CodexJob", _FakeChildJob):
            first = self.manager.spawn_agent(
                message="research",
                task_name="deadline",
            )
            self._wait_completed(first["target"])

        timeout = _FakeChildJob.instances[0].config.timeout
        self.assertGreater(timeout, 0)
        self.assertLessEqual(timeout, 1.0)

    def test_spawn_rejects_unallowed_pair_and_unknown_target(self):
        with self.assertRaisesRegex(ValueError, "not allowed"):
            self.manager.spawn_agent(
                message="research",
                task_name="research_1",
                model="gpt-5.6-terra",
                reasoning_effort="medium",
            )
        with self.assertRaisesRegex(ValueError, "not found"):
            self.manager.send_message("/root/other", "message")

    def test_followup_reuses_conversation_and_drains_fifo_messages(self):
        with mock.patch("codex_job_runner.job.CodexJob", _FakeChildJob):
            first = self.manager.spawn_agent(
                message="first",
                task_name="worker",
            )
            self._wait_completed(first["target"])
            state = _FakeChildJob.instances[0]._conversation_state
            self.manager.send_message(first["target"], "queued one")
            self.manager.send_message(first["target"], "queued two")
            self.manager.followup_task(first["target"], "follow up")
            self._wait_completed(first["target"])

        second = _FakeChildJob.instances[1]
        self.assertIs(second._conversation_state, state)
        self.assertEqual(
            second.input,
            "queued one\n\nqueued two\n\nfollow up",
        )
        self.assertEqual(len(self.manager.results()), 2)

    def test_only_concurrent_children_are_limited(self):
        with mock.patch("codex_job_runner.job.CodexJob", _FakeChildJob):
            for index in range(7):
                child = self.manager.spawn_agent(
                    message=f"task {index}",
                    task_name=f"worker_{index}",
                )
                self._wait_completed(child["target"])

            target = "/root/worker_0"
            for index in range(3):
                self.manager.followup_task(target, f"follow up {index}")
                self._wait_completed(target)

        self.assertEqual(len(self.manager.results()), 10)

    def test_delegation_log_retains_exact_multiline_spawn_and_followup_messages(self):
        parent_job = CodexJob(config=self.parent, input="parent task")
        parent_job._done = threading.Event()
        parent_job._activity_log = []
        parent_job.__dict__["log_path"] = pathlib.Path(self.tempdir.name) / "parent.log"
        self.manager._delegation_callback = parent_job._subagent_delegation
        spawn_message = "first line\n  exact indented line\nlast line"
        followup_message = "next line\n\nfinal exact line"

        with mock.patch("codex_job_runner.job.CodexJob", _FakeChildJob):
            first = self.manager.spawn_agent(
                message=spawn_message,
                task_name="worker",
            )
            self._wait_completed(first["target"])
            self.manager.followup_task(first["target"], followup_message)
            self._wait_completed(first["target"])

        records = [
            json.loads(line)
        for line in parent_job.log_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            [(record["operation"], record["message"]) for record in records],
            [
                ("spawn_agent", spawn_message),
                ("followup_task", followup_message),
            ],
        )
        self.assertEqual(
            [record["child_turn_index"] for record in records],
            [0, 1],
        )
        self.assertTrue(all(record["type"] == "subagent.delegation" for record in records))
        self.assertTrue(all(record["parent_job_id"] == parent_job.id for record in records))
        self.assertTrue(all(record["child_task_path"] == "/root/worker" for record in records))
        self.assertEqual(
            [entry.detail for entry in parent_job._activity_log],
            [json.dumps(record, ensure_ascii=False, sort_keys=True) for record in records],
        )
        self.assertTrue(any(event == "spawn" for event, _ in self.events))
        self.assertTrue(any(event == "followup" for event, _ in self.events))

    def test_message_queue_is_recorded_in_parent_activity(self):
        with mock.patch("codex_job_runner.job.CodexJob", _FakeChildJob):
            first = self.manager.spawn_agent(
                message="first",
                task_name="worker",
            )
            self.manager.send_message(first["target"], "queued")

        self.assertIn(
            ("message", "queued_for_next_turn target=/root/worker"),
            self.events,
        )

    def test_cleanup_timeout_forces_child_process_and_records_target(self):
        with mock.patch("codex_job_runner.job.CodexJob", _StuckChildJob):
            self.manager.spawn_agent(message="first", task_name="stuck")
            self.manager.close(timeout=0)
            for _ in range(100):
                if _FakeChildJob.instances[0].forced:
                    break
                time.sleep(0.001)

        self.assertTrue(_FakeChildJob.instances[0].forced)
        self.assertTrue(any(
            event == "cleanup_timeout" and "target=/root/stuck" in message
            for event, message in self.events
        ))

    def test_wait_returns_changes_once(self):
        with mock.patch("codex_job_runner.job.CodexJob", _FakeChildJob):
            self.manager.spawn_agent(message="first", task_name="worker")
            self._wait_completed("/root/worker")
            first = self.manager.wait_agent(timeout_ms=0)
            second = self.manager.wait_agent(timeout_ms=0)

        self.assertEqual(first[0]["target"], "/root/worker")
        self.assertEqual(second, [])

    def test_wait_returns_exact_unread_child_log_once(self):
        with mock.patch(
            "codex_job_runner.job.CodexJob",
            _LoggingFakeChildJob,
        ):
            self.manager.spawn_agent(message="first", task_name="logger")
            first = self.manager.wait_agent(timeout_ms=0)
            self._wait_completed("/root/logger")
            second = self.manager.wait_agent(timeout_ms=0)
            third = self.manager.wait_agent(timeout_ms=0)

        combined = "".join(
            item["log_delta"]
            for batch in (first, second)
            for item in batch
            if item["target"] == "/root/logger"
        )
        self.assertEqual(
            combined,
            "[Subagent /root/logger][Start] attempt=0 repair=0\n",
        )
        self.assertEqual(third, [])

    def test_wait_does_not_return_early_for_log_output(self):
        with mock.patch(
            "codex_job_runner.job.CodexJob",
            _SlowLoggingFakeChildJob,
        ):
            self.manager.spawn_agent(message="first", task_name="slow_logger")
            started = time.monotonic()
            result = self.manager.wait_agent(timeout_ms=10)
            elapsed = time.monotonic() - started

        self.assertGreaterEqual(elapsed, 0.008)
        self.assertEqual(result[0]["status"], "running")
        self.assertEqual(
            result[0]["log_delta"],
            "[Subagent /root/slow_logger][Start] attempt=0 repair=0\n",
        )


class DirectChildJobIntegrationTests(unittest.TestCase):
    def test_parent_bound_tools_prevent_worker_pool_bridge_sharing(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = pathlib.Path(tempdir)
            parent = SessionConfig(workspace=WorkspaceConfig(cwd=root))
            child = SessionConfig(
                workspace=WorkspaceConfig(cwd=root),
                allow_shell_commands=False,
            )
            subagents = SubagentConfig(
                session=child,
                allowed_models={
                    child.model: (child.reasoning_effort,),
                },
            )
            first = CodexJob(
                config=parent,
                input="first",
                subagents=subagents,
            )
            second = CodexJob(
                config=parent,
                input="second",
                subagents=subagents,
            )

            first_tools = first._tool_bridge_spec()[0]
            second_tools = second._tool_bridge_spec()[0]

        self.assertEqual(
            tuple(tool.name for tool in first_tools),
            (
                "spawn_agent",
                "send_message",
                "followup_task",
                "interrupt_agent",
                "list_agents",
                "wait_agent",
            ),
        )
        self.assertNotEqual(first_tools, second_tools)

    def test_permission_escalation_fails_before_runner_creation(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = pathlib.Path(tempdir)
            parent = SessionConfig(
                allow_shell_commands=False,
                workspace=WorkspaceConfig(cwd=root),
            )
            child = SessionConfig(
                allow_shell_commands=True,
                workspace=WorkspaceConfig(cwd=root),
            )
            subagents = SubagentConfig(
                session=child,
                allowed_models={
                    child.model: (child.reasoning_effort,),
                },
            )
            job = CodexJob(
                config=parent,
                input="parent",
                subagents=subagents,
            )

            with self.assertRaisesRegex(ValueError, "shell permission"):
                job.start()

        self.assertEqual(job.status, "failed")


class AggregateSubagentCostTests(unittest.TestCase):
    @staticmethod
    def _manager(
        *,
        cost,
        complete=True,
        reasons=(),
        results=(),
        agents=(),
    ):
        return SimpleNamespace(
            estimated_cost_usd=lambda: cost,
            cost_complete=lambda: complete,
            usage_incomplete_reasons=lambda: reasons,
            results=lambda: tuple(results),
            list_agents=lambda: list(agents),
        )

    def test_recursive_usage_includes_parent_and_both_child_groups_once(self):
        job = CodexJob(
            config=SessionConfig(),
            input="parent",
            review=SimpleNamespace(),
        )
        job._response = SimpleNamespace(
                usage={"input_tokens": 3},
                estimated_cost_usd=3.0,
            )
        job._last_review_result = SimpleNamespace(response=SimpleNamespace(
                usage={"input_tokens": 2},
                estimated_cost_usd=2.0,
            ))
        job._usage_by_model = {
                "main-model": {"input_tokens": 1},
                "review-model": {"input_tokens": 2},
            }
        job._estimated_cost_usd_by_model = {
                "main-model": 1.0,
                "review-model": 2.0,
            }
        job._subagent_manager = self._manager(
            cost=0.1,
            results=(
                {
                    "target": "/root/main_child",
                    "turn_index": 0,
                    "status": "completed",
                    "model": "main-child-model",
                    "usage": {"input_tokens": 3},
                    "estimated_cost_usd": 0.1,
                },
            ),
        )
        with mock.patch.object(
            job,
            "_review_subagent_manager",
            return_value=self._manager(
                cost=0.2,
                results=(
                    {
                        "target": "/root/reviewer_child",
                        "turn_index": 0,
                        "status": "completed",
                        "model": "review-child-model",
                        "usage": {"input_tokens": 4},
                        "estimated_cost_usd": 0.2,
                    },
                ),
            ),
        ):
            self.assertTrue(job.total_cost_complete)
            self.assertEqual(job.total_usage_incomplete_reasons, ())
            self.assertAlmostEqual(job.total_estimated_cost_usd, 3.3)
            self.assertEqual(job.total_usage, {"input_tokens": 10})
            self.assertEqual(
                job.total_usage_by_model,
                {
                    "main-model": {"input_tokens": 1},
                    "review-model": {"input_tokens": 2},
                    "main-child-model": {"input_tokens": 3},
                    "review-child-model": {"input_tokens": 4},
                },
            )
            self.assertEqual(
                job.total_estimated_cost_usd_by_model,
                {
                    "main-model": 1.0,
                    "review-model": 2.0,
                    "main-child-model": 0.1,
                    "review-child-model": 0.2,
                },
            )

    def test_total_cost_reasons_expose_main_reviewer_and_child_gaps(self):
        job = CodexJob(
            config=SessionConfig(),
            input="parent",
            review=SimpleNamespace(),
        )
        job._response = SimpleNamespace(usage=None, estimated_cost_usd=None)
        job._last_review_result = None
        job._subagent_manager = self._manager(
            cost=None,
            complete=False,
            reasons=("/root/main_child turn usage unavailable",),
            results=(
                {
                    "target": "/root/main_child",
                    "turn_index": 0,
                    "status": "cancelled",
                    "model": "child-model",
                    "usage": None,
                    "estimated_cost_usd": None,
                },
            ),
        )
        with mock.patch.object(
            job,
            "_review_subagent_manager",
            return_value=self._manager(
                cost=None,
                complete=False,
                reasons=("/root/reviewer_child turn pricing unavailable",),
                agents=(
                    {
                        "target": "/root/reviewer_child",
                        "status": "failed",
                        "turns": 0,
                    },
                ),
            ),
        ):
            self.assertFalse(job.total_cost_complete)
            self.assertIsNone(job.total_estimated_cost_usd)
            self.assertEqual(
                job.total_usage_incomplete_reasons,
                (
                    "main response usage unavailable",
                    "main response pricing unavailable",
                    "reviewer response unavailable",
                    "/root/main_child turn usage unavailable",
                    "/root/main_child turn[0] status is cancelled",
                    "/root/reviewer_child turn pricing unavailable",
                    "/root/reviewer_child has no usage-bearing turn and status is failed",
                    "main subagent cost is incomplete",
                    "reviewer subagent cost is incomplete",
                ),
            )


if __name__ == "__main__":
    unittest.main()
