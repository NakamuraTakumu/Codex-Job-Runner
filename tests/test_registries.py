import pathlib
import sys
import unittest
from unittest import mock

from pydantic import BaseModel


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import codex_job_runner._pool as pool_module  # noqa: E402
import codex_job_runner.job as job_module  # noqa: E402
from codex_job_runner import (  # noqa: E402
    CodexJob,
    CodexResponse,
    CodexWorkerPool,
    SessionConfig,
    Tool,
    ToolConfig,
    WorkspaceConfig,
)


def _tool(name):
    def handler(value: str) -> str:
        return value

    return Tool(
        handler,
        "Return the supplied value.",
        args={"value": "A value."},
        returns="The supplied value.",
        name=name,
    )


class _FakeExecution:
    def __init__(self):
        self.status = "running"
        self.error = None
        self.response = None

    def start_background(self):
        pass

    def done(self):
        return self.status != "running"

    def result(self, timeout=None):
        del timeout
        self.status = "completed"
        self.response = CodexResponse(content="ok", returncode=0, stdout="", stderr="")
        return self.response

    def cancel(self):
        self.status = "cancelled"


class _FakeRunner:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.start_kwargs = None
        self.closed = False
        self.__class__.instances.append(self)

    def start(self, **kwargs):
        self.start_kwargs = kwargs
        return _FakeExecution()

    def close(self):
        self.closed = True


def _fake_runtime(_owner, **kwargs):
    return _FakeRunner(**kwargs)


class _FakeBridge:
    instances = []

    def __init__(self, tools, *, max_concurrency, tool_timeout):
        self.tools = tuple(tools)
        self.max_concurrency = max_concurrency
        self.tool_timeout = tool_timeout
        self.started = False
        self.closed = False
        self.__class__.instances.append(self)

    def start(self):
        self.started = True

    def close(self):
        self.closed = True


class PendingRegistryTests(unittest.TestCase):
    def setUp(self):
        _FakeRunner.instances.clear()
        _FakeBridge.instances.clear()
        self.config = SessionConfig(workspace=WorkspaceConfig(cwd=ROOT))

    def test_config_tools_are_copied_per_job(self):
        configured = _tool("configured")
        config = self.config.with_overrides(tools=ToolConfig(tools=(configured,)))
        first = CodexJob(config=config, input="first")
        second = CodexJob(config=config, input="second")

        first.tools.append(_tool("first_only"))

        self.assertEqual(tuple(first.tools), (configured, first.tools[1]))
        self.assertEqual(tuple(second.tools), (configured,))
        self.assertEqual(config.tools.tools, (configured,))

    def test_pydantic_validator_precedes_legacy_and_appended_validator(self):
        events = []

        class Output(BaseModel):
            value: str

        class ValidatingJob(CodexJob):
            def validate_output(self, response):
                events.append(("legacy", response.content))

        job = ValidatingJob(
            config=self.config,
            input="inspect",
            output_pydantic_model=Output,
        )
        job.output_validators.append(
            lambda response: events.append(("appended", response.content))
        )

        with self.assertRaises(ValueError):
            job.output_validators[0](CodexResponse(content="{}", returncode=0, stdout="", stderr=""))
        self.assertEqual(events, [])
        response = CodexResponse(
            content='{"value": "ok"}', returncode=0, stdout="", stderr=""
        )
        for validator in job.output_validators:
            validator(response)
        self.assertEqual(events, [("legacy", response.content), ("appended", response.content)])

    def test_all_legacy_hook_overrides_register_once(self):
        events = []
        methods = {}
        for phase in job_module._HOOK_NAMES:
            def hook(self, phase=phase):
                events.append(phase)

            methods[phase] = hook
        AllHooksJob = type("AllHooksJob", (CodexJob,), methods)
        job = AllHooksJob(config=self.config, input="inspect")

        self.assertEqual(set(vars(job.hooks)), set(job_module._HOOK_NAMES))
        for phase in job_module._HOOK_NAMES:
            callbacks = getattr(job.hooks, phase)
            self.assertEqual(len(callbacks), 1)
            callbacks[0]()
        self.assertEqual(events, list(job_module._HOOK_NAMES))

    def test_appended_hooks_run_after_auto_registered_hook_in_fifo_order(self):
        events = []

        class HookedJob(CodexJob):
            def before_main_attempt(self):
                events.append("legacy")

        job = HookedJob(config=self.config, input="inspect")
        job.hooks.before_main_attempt.append(lambda: events.append("first"))
        job.hooks.before_main_attempt.append(lambda: events.append("second"))

        for callback in job._freeze_registries_locked()["hooks"]["before_main_attempt"]:
            callback()
        self.assertEqual(events, ["legacy", "first", "second"])

    def test_freeze_start_and_cancel_reject_registry_mutation(self):
        def assert_mutation_rejected(job):
            with self.assertRaisesRegex(RuntimeError, "cannot be changed"):
                job.output_validators.append(lambda response: None)
            with self.assertRaisesRegex(RuntimeError, "cannot be changed"):
                job.tools.append(_tool("late"))
            with self.assertRaisesRegex(RuntimeError, "cannot be changed"):
                job.hooks.before_cleanup.append(lambda: None)

        frozen = CodexJob(config=self.config, input="inspect")
        frozen._freeze_registries_locked()
        assert_mutation_rejected(frozen)

        with mock.patch.object(job_module, "_configure_runtime", _fake_runtime):
            started = CodexJob(config=self.config, input="inspect").start()
        assert_mutation_rejected(started)

        cancelled = CodexJob(config=self.config, input="inspect")
        cancelled.cancel()
        assert_mutation_rejected(cancelled)

    def test_duplicate_tool_names_fail_before_runner_or_bridge_creation(self):
        job = CodexJob(config=self.config, input="inspect")
        job.tools.extend((_tool("duplicate"), _tool("duplicate")))

        with mock.patch.object(job_module, "_configure_runtime", _fake_runtime):
            with self.assertRaisesRegex(ValueError, "duplicate tool names"):
                job.start()
        self.assertEqual(_FakeRunner.instances, [])
        self.assertEqual(job.status, "failed")
        with self.assertRaisesRegex(RuntimeError, "cannot be changed"):
            job.tools.clear()

    def test_terminal_hooks_continue_after_errors_and_fail_fast_hooks_stop(self):
        events = []
        activity = []
        execution = object.__new__(CodexJob)
        execution._hooks = {
            "before_cleanup": (
                lambda: (_ for _ in ()).throw(RuntimeError("cleanup failure")),
                lambda: events.append("cleanup continued"),
            ),
            "before_main_attempt": (
                lambda: (_ for _ in ()).throw(RuntimeError("main failure")),
                lambda: events.append("main continued"),
            ),
        }
        execution._log_activity = lambda *args, **kwargs: activity.append((args, kwargs))

        execution._invoke_hook("cleanup", "before", raise_errors=False)
        with self.assertRaisesRegex(RuntimeError, "main failure"):
            execution._invoke_hook("main_attempt", "before")

        self.assertEqual(events, ["cleanup continued"])
        self.assertEqual(len(activity), 1)
        self.assertEqual(activity[0][0][:2], ("cleanup", "error"))

    def test_pool_bridge_uses_tools_appended_to_pending_job(self):
        job = CodexJob(config=self.config, input="inspect")
        appended = _tool("appended")
        job.tools.append(appended)

        with (
            mock.patch.object(job_module, "_configure_runtime", _fake_runtime),
            mock.patch.object(pool_module, "_ToolBridge", _FakeBridge),
        ):
            result = CodexWorkerPool(max_workers=1).start_many([job]).result(timeout=1)

        self.assertEqual(result.responses[0].content, "ok")
        self.assertEqual(len(_FakeBridge.instances), 1)
        self.assertEqual(_FakeBridge.instances[0].tools, (appended,))
        self.assertIs(_FakeRunner.instances[0].start_kwargs["_borrowed_tool_bridge"], _FakeBridge.instances[0])


if __name__ == "__main__":
    unittest.main()
