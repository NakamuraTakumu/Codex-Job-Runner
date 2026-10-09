import importlib.util
import pathlib
import sys
import tempfile
import threading
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import codex_job_runner  # noqa: E402
import codex_job_runner.job as job_module  # noqa: E402
from codex_job_runner._models import _CodexAttempt, _ReviewResult  # noqa: E402
from codex_job_runner.types import CodexResponse  # noqa: E402
from codex_job_runner import (  # noqa: E402
    CodexAlreadyStartedError,
    CodexCancelledError,
    CodexJob,
    CodexNotStartedError,
    CodexValidationError,
    SessionConfig,
    WorkspaceConfig,
)


class _FakeExecution:
    def __init__(self, hook_owner, response="response"):
        self.hook_owner = hook_owner
        self._response_value = response
        self.status = "running"
        self.error = None
        self.response = None
        self.background_started = False
        self.cancel_calls = 0
        self.argv = ["fake-codex"]

    def start_background(self):
        self.background_started = True
        self.hook_owner.before_main_attempt()
        self.hook_owner.after_main_attempt()

    def done(self):
        return self.status in {
            "completed",
            "failed",
            "timeout",
            "cancelled",
        }

    def result(self, timeout=None):
        del timeout
        self.status = "completed"
        self.response = self._response_value
        return self.response

    def cancel(self):
        self.cancel_calls += 1
        self.hook_owner.before_cancel()
        self.status = "cancelled"
        self.hook_owner.after_cancel()


class _FakeRunner:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.start_kwargs = None
        self.execution = None
        self.closed = False
        self.__class__.instances.append(self)

    def start(self, **kwargs):
        self.start_kwargs = kwargs
        owner = kwargs["_job_owner"]
        self.execution = _FakeExecution(kwargs["_hook_owner"])
        owner._done = threading.Event()
        owner._response = None
        owner._last_response = None
        owner._thread = type("_Thread", (), {
            "start": lambda _thread: self._complete(owner),
        })()
        return owner

    def _complete(self, owner):
        self.execution.start_background()
        owner._response = self.execution._response_value
        owner._status = "completed"
        owner._done.set()

    def close(self):
        self.closed = True


def _fake_runtime(_owner, **kwargs):
    return _FakeRunner(**kwargs)


class _CompletedProcess:
    returncode = 0

    def communicate(self, prompt):
        del prompt
        return "", ""

    def poll(self):
        return self.returncode

    def terminate(self):
        pass


class CodexJobTests(unittest.TestCase):
    def setUp(self):
        _FakeRunner.instances.clear()
        self.config = SessionConfig(workspace=WorkspaceConfig(cwd=ROOT))

    def test_constructing_pending_job_allocates_no_runner_resources(self):
        with mock.patch.object(job_module, "_configure_runtime", _fake_runtime):
            job = CodexJob(config=self.config, input="inspect")

        self.assertEqual(job.status, "pending")
        self.assertFalse(job.done())
        self.assertIsNone(job.error)
        self.assertIsNone(job.response)
        self.assertEqual(_FakeRunner.instances, [])
        with self.assertRaises(CodexNotStartedError):
            job.result()

    def test_start_returns_same_job_and_result_is_owned_by_job(self):
        with mock.patch.object(job_module, "_configure_runtime", _fake_runtime):
            config = self.config.with_overrides(
                workspace=self.config.workspace.with_overrides(
                    read_paths=["README.md"]
                )
            )
            job = CodexJob(config=config, input="inspect")
            returned = job.start()

        self.assertIs(returned, job)
        self.assertEqual(job.status, "completed")
        runner = _FakeRunner.instances[0]
        self.assertTrue(runner.execution.background_started)
        self.assertIs(runner.start_kwargs["_hook_owner"], job)
        self.assertTrue(runner.start_kwargs["_defer_background"])
        self.assertEqual(runner.start_kwargs["input"], "inspect")
        self.assertEqual(runner.start_kwargs["read_paths"], ("README.md",))
        self.assertEqual(job.result(), "response")
        self.assertEqual(job.status, "completed")
        self.assertTrue(job.done())

    def test_run_is_start_then_result_shortcut(self):
        with mock.patch.object(job_module, "_configure_runtime", _fake_runtime):
            job = CodexJob(config=self.config, input="inspect")
            response = job.run()

        self.assertEqual(response, "response")
        self.assertTrue(job.done())

    def test_pending_cancel_is_terminal_without_allocating_runner(self):
        with mock.patch.object(job_module, "_configure_runtime", _fake_runtime):
            job = CodexJob(config=self.config, input="inspect")
            job.cancel()

        self.assertEqual(job.status, "cancelled")
        self.assertTrue(job.done())
        self.assertEqual(_FakeRunner.instances, [])
        with self.assertRaises(CodexCancelledError):
            job.result()
        with self.assertRaises(CodexAlreadyStartedError):
            job.start()

    def test_started_cancel_is_idempotent_after_completion(self):
        with mock.patch.object(job_module, "_configure_runtime", _fake_runtime):
            job = CodexJob(config=self.config, input="inspect").start()
            execution = _FakeRunner.instances[0].execution
            job.cancel()

        self.assertEqual(execution.cancel_calls, 0)
        self.assertEqual(job.status, "completed")

    def test_start_can_only_be_called_once(self):
        with mock.patch.object(job_module, "_configure_runtime", _fake_runtime):
            job = CodexJob(config=self.config, input="inspect")
            job.start()
            with self.assertRaises(CodexAlreadyStartedError):
                job.start()

        self.assertEqual(len(_FakeRunner.instances), 1)

    def test_preparation_failure_is_saved_closed_and_rethrown(self):
        failure = ValueError("invalid preparation")

        class FailingRunner(_FakeRunner):
            def start(self, **kwargs):
                self.start_kwargs = kwargs
                raise failure

        with mock.patch.object(
            job_module,
            "_configure_runtime",
            lambda _owner, **kwargs: FailingRunner(**kwargs),
        ):
            job = CodexJob(config=self.config, input="inspect")
            with self.assertRaises(ValueError) as first:
                job.start()
            with self.assertRaises(ValueError) as second:
                job.result()
            with self.assertRaises(CodexAlreadyStartedError):
                job.start()

        self.assertIs(first.exception, failure)
        self.assertIs(second.exception, failure)
        self.assertEqual(job.status, "failed")
        self.assertIs(job.error, failure)
        self.assertTrue(job.done())
        self.assertTrue(FailingRunner.instances[-1].closed)

    def test_subclass_fields_exist_before_no_argument_hooks_run(self):
        events = []

        class AnalysisJob(CodexJob):
            def __init__(self, *, config, company_name):
                self.company_name = company_name
                super().__init__(
                    config=config,
                    input=f"analyze {company_name}",
                )

            def before_main_attempt(self):
                events.append(("before", self.company_name))

            def after_main_attempt(self):
                events.append(("after", self.company_name))

        with mock.patch.object(job_module, "_configure_runtime", _fake_runtime):
            job = AnalysisJob(config=self.config, company_name="A").start()

        self.assertEqual(job.company_name, "A")
        self.assertEqual(events, [("before", "A"), ("after", "A")])

    def test_base_validate_output_is_a_noop_and_subclass_method_is_registered(self):
        response = object()
        base = CodexJob(config=self.config, input="inspect")
        self.assertIsNone(base.validate_output(response))

        seen = []

        class ValidatingJob(CodexJob):
            def validate_output(self, candidate):
                seen.append(candidate)

        job = ValidatingJob(config=self.config, input="inspect")
        self.assertEqual(tuple(job.output_validators), (job.validate_output,))
        job.output_validators[0](response)

        self.assertEqual(seen, [response])

    @unittest.skip("replaced by peer-review protocol tests")
    def test_reviewer_failure_uses_the_shared_repair_scheduler(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = pathlib.Path(tempdir)
            initial_output = root / "initial.txt"
            initial_output.write_text("needs review repair", encoding="utf-8")
            prompts = []
            reviewed = []

            def spawn_attempt(*, prompt, attempt_index, repair_index):
                prompts.append(prompt)
                output_path = root / f"repair-{repair_index}.txt"
                output_path.write_text("repaired", encoding="utf-8")
                return _CodexAttempt(
                    argv=["codex"],
                    prompt=prompt,
                    process=_CompletedProcess(),
                    output_path=output_path,
                    attempt_index=attempt_index,
                    repair_index=repair_index,
                )

            def run_review(**kwargs):
                content = kwargs["response"].content
                reviewed.append(content)
                passed = content == "repaired"
                response = CodexResponse(
                    content='{"passed": true}' if passed else '{"passed": false}',
                    returncode=0,
                    stdout="",
                    stderr="",
                    argv=("review",),
                )
                return _ReviewResult(
                    passed=passed,
                    findings=() if passed else ("repair the response",),
                    summary="accepted" if passed else "needs repair",
                    response=response,
                    stdout="",
                    stderr="",
                    argv=["review"],
                    stream_emitted=True,
                )

            attempt = _CodexAttempt(
                argv=["codex"],
                prompt="main input",
                process=_CompletedProcess(),
                output_path=initial_output,
                attempt_index=0,
                repair_index=0,
            )
            execution = object.__new__(CodexJob)
            execution._initialize_runtime(
                attempt=attempt,
                spawn_attempt=spawn_attempt,
                original_input="main input",
                context_paths=(),
                context_sections=(),
                output_schema_section=None,
                output_model_source_section=None,
                read_paths=(),
                write_paths=(),
                subcwd_files=(),
                cwd=root,
                review_config=object(),
                review_runner=run_review,
                repair_validation_failures=True,
                max_repairs=1,
                max_review_repairs=1,
                log_dir=root,
            )
            execution.start_background()
            response = execution.result(timeout=2)

        self.assertEqual(response.content, "repaired")
        self.assertEqual(reviewed, ["needs review repair", "repaired"])
        self.assertEqual(len(prompts), 1)
        self.assertIn("source=review", next(
            entry.message for entry in execution.activity_log
            if entry.phase == "repair" and entry.event == "prompt"
        ))

    @unittest.skip("total repair cap was removed by the peer-review protocol")
    def test_total_repair_cap_preserves_review_failure_after_validator_repair(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = pathlib.Path(tempdir)
            initial_output = root / "initial.txt"
            initial_output.write_text("invalid", encoding="utf-8")
            spawned = []

            def spawn_attempt(*, prompt, attempt_index, repair_index):
                spawned.append((prompt, attempt_index, repair_index))
                output_path = root / f"repair-{repair_index}.txt"
                output_path.write_text("review fails", encoding="utf-8")
                return _CodexAttempt(
                    argv=["codex"],
                    prompt=prompt,
                    process=_CompletedProcess(),
                    output_path=output_path,
                    attempt_index=attempt_index,
                    repair_index=repair_index,
                )

            def validate(response):
                if response.content == "invalid":
                    raise ValueError("validator requires correction")

            def run_review(**kwargs):
                del kwargs
                review_response = CodexResponse(
                    content='{"passed": false}',
                    returncode=0,
                    stdout="review stdout",
                    stderr="",
                    argv=("review",),
                )
                return _ReviewResult(
                    passed=False,
                    findings=("review requires correction",),
                    summary="review failed",
                    response=review_response,
                    stdout="review stdout",
                    stderr="",
                    argv=["review"],
                    stream_emitted=True,
                )

            execution = object.__new__(CodexJob)
            execution._initialize_runtime(
                attempt=_CodexAttempt(
                    argv=["codex"],
                    prompt="main input",
                    process=_CompletedProcess(),
                    output_path=initial_output,
                    attempt_index=0,
                    repair_index=0,
                ),
                spawn_attempt=spawn_attempt,
                original_input="main input",
                context_paths=(),
                context_sections=(),
                output_schema_section=None,
                output_model_source_section=None,
                read_paths=(),
                write_paths=(),
                subcwd_files=(),
                cwd=root,
                review_config=object(),
                review_runner=run_review,
                output_validators=(validate,),
                repair_validation_failures=True,
                max_repairs=1,
                max_validation_repairs=1,
                max_review_repairs=1,
                log_dir=root,
            )
            execution.start_background()
            with self.assertRaises(CodexValidationError) as caught:
                execution.result(timeout=2)

        error = caught.exception
        self.assertEqual(error.failure_stage, "review")
        self.assertEqual(error.repairs, 1)
        self.assertEqual(error.attempts, 2)
        self.assertEqual(error.validation_diagnostic, "")
        self.assertEqual(error.review_response.content, '{"passed": false}')
        self.assertEqual(error.review_stdout, "review stdout")
        self.assertEqual(error.review_argv, ["review"])
        self.assertEqual(execution.review_findings, ("review requires correction",))
        self.assertEqual(len(spawned), 1)
        repair_entries = [
            entry for entry in execution.activity_log
            if entry.phase == "repair" and entry.event == "prompt"
        ]
        self.assertEqual(len(repair_entries), 1)
        self.assertIn("source=validator", repair_entries[0].message)

    def test_wait_any_returns_first_completed_job(self):
        first = CodexJob(config=self.config, input="first")
        second = CodexJob(config=self.config, input="second")
        second.cancel()

        self.assertIs(CodexJob.wait_any([first, second], timeout=0), second)
        self.assertIsNone(CodexJob.wait_any([first], timeout=0))

    def test_total_cost_uses_turn_ledger_and_matches_model_breakdown(self):
        job = CodexJob(config=self.config, input="ledger")
        job._response = CodexResponse(
            content="ledger",
            returncode=0,
            stdout="",
            stderr="",
            usage={"input_tokens": 10},
            estimated_cost_usd=0.10,
        )
        job._usage_by_model = {
            "main-model": {"input_tokens": 10},
            "reviewer-model": {"output_tokens": 4},
        }
        job._estimated_cost_usd_by_model = {
            "main-model": 0.10,
            "reviewer-model": 0.04,
        }
        job._usage_incomplete_reasons = []

        self.assertTrue(job.total_cost_complete)
        self.assertEqual(job.total_estimated_cost_usd_by_model, {
            "main-model": 0.10,
            "reviewer-model": 0.04,
        })
        self.assertAlmostEqual(job.total_estimated_cost_usd, 0.14)

    def test_missing_turn_price_makes_total_cost_unavailable(self):
        job = CodexJob(config=self.config, input="ledger")
        job._response = CodexResponse(
            content="ledger",
            returncode=0,
            stdout="",
            stderr="",
            usage={"input_tokens": 10},
            estimated_cost_usd=0.10,
        )
        job._usage_by_model = {"main-model": {"input_tokens": 10}}
        job._estimated_cost_usd_by_model = {"main-model": 0.10}
        job._usage_incomplete_reasons = []
        job._record_usage_observation(
            role="reviewer protocol repair",
            model="reviewer-model",
            usage={"output_tokens": 4},
            estimated_cost_usd=None,
        )

        self.assertFalse(job.total_cost_complete)
        self.assertIsNone(job.total_estimated_cost_usd_by_model)
        self.assertIsNone(job.total_estimated_cost_usd)

    def test_old_public_entry_points_are_removed(self):
        self.assertFalse(hasattr(codex_job_runner, "CodexClient"))
        self.assertFalse(hasattr(codex_job_runner, "ReviewSession"))
        self.assertIsNone(importlib.util.find_spec("codex_api_wrapper"))

    def test_reflection_argument_is_removed(self):
        with self.assertRaises(TypeError):
            CodexJob(
                config=self.config,
                input="inspect",
                reflection=True,
            )


if __name__ == "__main__":
    unittest.main()
