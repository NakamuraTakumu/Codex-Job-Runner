import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from codex_job_runner import (
    CodexBatchResult,
    CodexCancelledError,
    CodexJob,
    CodexResponse,
    SessionConfig,
    CodexTaskCompleted,
    CodexValidationError,
    CodexWorkerPool,
    ToolConfig,
    Tool,
)


class _FakeJob(CodexJob):
    def __init__(
        self,
        *,
        value,
        tracker,
        delay=0.02,
        fail=False,
        failure_response=None,
        prepare_failure=None,
    ):
        self.value = value
        self.config = SessionConfig()
        self._tracker = tracker
        self._delay = delay
        self._fail = fail
        self._failure_response = failure_response
        self._prepare_failure = prepare_failure
        self._done = threading.Event()
        self._cancelled = False
        self._claim_owner = None
        self._started = False
        self.bridge = None
        self._lock = threading.RLock()

    def _run(self):
        time.sleep(self._delay)
        with self._tracker.lock:
            self._tracker.active -= 1
            self._tracker.completed.append(self.value)
        self._done.set()

    def _claim(self, owner):
        with self._lock:
            if self._claim_owner is not None or self._started or self._done.is_set():
                raise RuntimeError("job is not pending")
            self._claim_owner = owner

    def _release_claim(self, owner):
        with self._lock:
            if self._claim_owner is not owner or self._started:
                raise RuntimeError("job claim does not belong to this batch")
            self._claim_owner = None

    def _start_claimed(self, owner, *, tool_bridge=None):
        with self._lock:
            if self._claim_owner is not owner or self._started:
                raise RuntimeError("job is not claimed by this batch")
            if self._prepare_failure is not None:
                raise self._prepare_failure
            self._started = True
            self.bridge = tool_bridge
        with self._tracker.lock:
            self._tracker.started.append((self.value, tuple(self._tracker.completed)))
            self._tracker.active += 1
            self._tracker.max_active = max(
                self._tracker.max_active, self._tracker.active
            )
        threading.Thread(target=self._run, daemon=True).start()

    def _cancel_claimed(self, owner):
        with self._lock:
            if self._claim_owner is not owner:
                return
            self._cancelled = True
            if not self._started:
                self._done.set()

    def done(self):
        return self._done.is_set()

    def result(self):
        self._done.wait()
        if self._cancelled:
            raise CodexCancelledError("cancelled")
        if self._fail:
            if self._failure_response is not None:
                raise CodexValidationError(
                    f"failed: {self.value}",
                    response=self._failure_response,
                )
            raise RuntimeError(f"failed: {self.value}")
        return CodexResponse(
            content=str(self.value),
            returncode=0,
            stdout="",
            stderr="",
            usage={
                "input_tokens": 10,
                "cached_input_tokens": 4,
                "cache_write_tokens": 2,
            },
            estimated_cost_usd=0.25,
        )

    def cancel(self):
        self._cancelled = True

    @property
    def claimed(self):
        with self._lock:
            return self._claim_owner is not None

    @property
    def started(self):
        with self._lock:
            return self._started


class _RecursiveFakeJob(_FakeJob):
    @property
    def total_usage(self):
        return self._recursive_usage

    @property
    def total_usage_by_model(self):
        return self._recursive_usage_by_model

    @property
    def total_estimated_cost_usd(self):
        return self._recursive_cost

    @property
    def total_estimated_cost_usd_by_model(self):
        return self._recursive_cost_by_model

    @property
    def total_usage_incomplete_reasons(self):
        return self._recursive_reasons


class _Tracker:
    def __init__(self, *, fail_value=None, failure_response=None):
        self.lock = threading.RLock()
        self.active = 0
        self.max_active = 0
        self.started = []
        self.completed = []
        self.fail_value = fail_value
        self.failure_response = failure_response

    def job(self, value, **kwargs):
        return _FakeJob(
            value=value,
            tracker=self,
            fail=value == self.fail_value,
            failure_response=self.failure_response,
            **kwargs,
        )

    def jobs(self, *values):
        return [self.job(value) for value in values]


class CodexWorkerPoolTests(unittest.TestCase):
    def test_warmup_completes_before_bounded_parallel_dispatch(self):
        tracker = _Tracker()
        pool = CodexWorkerPool(max_workers=2, warmup_jobs=1)

        result = pool.start_many(
            tracker.jobs("warm", "a", "b", "c")
        ).result(timeout=2)

        self.assertIsInstance(result, CodexBatchResult)
        self.assertEqual(
            tuple(response.content for response in result.responses),
            ("warm", "a", "b", "c"),
        )
        self.assertEqual(result.errors, (None, None, None, None))
        self.assertEqual(tracker.started[0], ("warm", ()))
        self.assertIn("warm", tracker.started[1][1])
        self.assertLessEqual(tracker.max_active, 2)

    def test_batch_aggregates_all_usage_fields_and_cost(self):
        tracker = _Tracker()
        pool = CodexWorkerPool(max_workers=3, warmup_jobs=0)

        result = pool.start_many(tracker.jobs(1, 2, 3)).result(timeout=2)

        self.assertEqual(result.usage["input_tokens"], 30)
        self.assertEqual(result.usage["cached_input_tokens"], 12)
        self.assertEqual(result.usage["cache_write_tokens"], 6)
        self.assertTrue(result.usage_complete)
        self.assertEqual(result.usage_incomplete_reasons, ())
        self.assertEqual(result.successful_usage, result.usage)
        self.assertEqual(result.failed_usage, {})
        self.assertEqual(result.estimated_cost_usd, 0.75)
        self.assertEqual(
            result.usage_by_model,
            {
                SessionConfig().model: result.usage,
            },
        )
        self.assertEqual(result.successful_usage_by_model, result.usage_by_model)
        self.assertEqual(result.failed_usage_by_model, {})
        self.assertEqual(
            result.estimated_cost_usd_by_model,
            {SessionConfig().model: 0.75},
        )

    def test_batch_uses_runtime_job_recursive_usage_without_double_counting(self):
        tracker = _Tracker()
        successful = _RecursiveFakeJob(value="ok", tracker=tracker)
        successful.id = "parent-success"
        successful._recursive_usage = {"input_tokens": 10}
        successful._recursive_usage_by_model = {
            "main-model": {"input_tokens": 4},
            "child-model": {"input_tokens": 6},
        }
        successful._recursive_cost = 1.5
        successful._recursive_cost_by_model = {
            "main-model": 1.0,
            "child-model": 0.5,
        }
        successful._recursive_reasons = ()
        failed = _RecursiveFakeJob(
            value="bad",
            tracker=tracker,
            fail=True,
        )
        failed.id = "parent-failed"
        failed._recursive_usage = {"input_tokens": 5}
        failed._recursive_usage_by_model = {
            "review-model": {"input_tokens": 2},
            "review-child-model": {"input_tokens": 3},
        }
        failed._recursive_cost = None
        failed._recursive_cost_by_model = None
        failed._recursive_reasons = (
            "/root/reviewer_child turn[0] status is cancelled",
        )

        result = CodexWorkerPool(
            max_workers=2,
            warmup_jobs=0,
        ).start_many((successful, failed)).result(timeout=2)

        self.assertEqual(result.usage, {"input_tokens": 15})
        self.assertEqual(result.successful_usage, {"input_tokens": 10})
        self.assertEqual(result.failed_usage, {"input_tokens": 5})
        self.assertEqual(
            result.usage_by_model,
            {
                "main-model": {"input_tokens": 4},
                "child-model": {"input_tokens": 6},
                "review-model": {"input_tokens": 2},
                "review-child-model": {"input_tokens": 3},
            },
        )
        self.assertEqual(
            result.usage_incomplete_reasons,
            (
                "task[1] job[parent-failed] "
                "/root/reviewer_child turn[0] status is cancelled",
                "task[1] failed without a response",
            ),
        )
        self.assertFalse(result.usage_complete)
        self.assertIsNone(result.estimated_cost_usd)
        self.assertIsNone(result.estimated_cost_usd_by_model)

    def test_batch_failure_is_raised_and_pool_can_be_reused(self):
        tracker = _Tracker(fail_value="bad")
        pool = CodexWorkerPool(
            max_workers=2,
            warmup_jobs=1,
            fail_fast=True,
        )

        with self.assertRaisesRegex(RuntimeError, "failed: bad"):
            pool.start_many(tracker.jobs("bad", "unused")).result(timeout=2)

        tracker.fail_value = None
        result = pool.start_many(tracker.jobs("ok")).result(timeout=2)
        self.assertEqual(result.responses[0].content, "ok")

    def test_batch_continues_after_task_failure_by_default(self):
        tracker = _Tracker(fail_value="bad")
        pool = CodexWorkerPool(max_workers=2, warmup_jobs=0)

        result = pool.start_many(tracker.jobs("a", "bad", "c")).result(timeout=2)

        self.assertEqual(
            tuple(
                response.content if response is not None else None
                for response in result.responses
            ),
            ("a", None, "c"),
        )
        self.assertIsNone(result.errors[0])
        self.assertRegex(str(result.errors[1]), "failed: bad")
        self.assertIsNone(result.errors[2])
        self.assertEqual(result.usage["input_tokens"], 20)
        self.assertFalse(result.usage_complete)
        self.assertEqual(
            result.usage_incomplete_reasons,
            ("task[1] failed without a response",),
        )
        self.assertEqual(result.successful_usage["input_tokens"], 20)
        self.assertEqual(result.failed_usage, {})
        self.assertIsNone(result.estimated_cost_usd)

    def test_batch_includes_usage_and_cost_from_failed_task_response_once(self):
        failed_response = CodexResponse(
            content="invalid",
            returncode=0,
            stdout="",
            stderr="",
            usage={
                "input_tokens": 7,
                "cached_input_tokens": 3,
                "output_tokens": 2,
            },
            estimated_cost_usd=0.1,
        )
        tracker = _Tracker(
            fail_value="bad",
            failure_response=failed_response,
        )
        pool = CodexWorkerPool(max_workers=2, warmup_jobs=0)

        result = pool.start_many(tracker.jobs("ok", "bad")).result(timeout=2)

        self.assertIsNone(result.responses[1])
        self.assertIs(result.errors[1].response, failed_response)
        self.assertEqual(result.usage["input_tokens"], 17)
        self.assertEqual(result.usage["cached_input_tokens"], 7)
        self.assertEqual(result.usage["output_tokens"], 2)
        self.assertTrue(result.usage_complete)
        self.assertEqual(result.usage_incomplete_reasons, ())
        self.assertEqual(result.successful_usage["input_tokens"], 10)
        self.assertEqual(result.failed_usage["input_tokens"], 7)
        self.assertEqual(result.failed_usage["output_tokens"], 2)
        self.assertAlmostEqual(result.estimated_cost_usd, 0.35)

    def test_batch_marks_missing_usage_in_response_as_incomplete(self):
        failed_response = CodexResponse(
            content="invalid",
            returncode=0,
            stdout="",
            stderr="",
            usage=None,
            estimated_cost_usd=0.1,
        )
        tracker = _Tracker(
            fail_value="bad",
            failure_response=failed_response,
        )
        pool = CodexWorkerPool(max_workers=2, warmup_jobs=0)

        result = pool.start_many(tracker.jobs("ok", "bad")).result(timeout=2)

        self.assertEqual(result.usage["input_tokens"], 10)
        self.assertFalse(result.usage_complete)
        self.assertEqual(
            result.usage_incomplete_reasons,
            ("task[1] failed response has no usage",),
        )
        self.assertEqual(result.successful_usage["input_tokens"], 10)
        self.assertEqual(result.failed_usage, {})
        self.assertIsNone(result.estimated_cost_usd)

    def test_failed_warmup_does_not_block_non_fail_fast_batch(self):
        tracker = _Tracker(fail_value="bad")
        pool = CodexWorkerPool(max_workers=2, warmup_jobs=1)

        result = pool.start_many(tracker.jobs("bad", "a", "b")).result(timeout=2)

        self.assertIsNotNone(result.errors[0])
        self.assertEqual(
            tuple(
                response.content if response is not None else None
                for response in result.responses
            ),
            (None, "a", "b"),
        )
        self.assertEqual([item[0] for item in tracker.started], ["bad", "a", "b"])

    def test_pool_rejects_overlapping_batches(self):
        tracker = _Tracker()
        pool = CodexWorkerPool(max_workers=1)
        batch = pool.start_many(tracker.jobs("a", "b"))

        with self.assertRaisesRegex(RuntimeError, "running batch"):
            pool.start_many(tracker.jobs("c"))

        batch.result(timeout=2)

    def test_batch_cancel_cancels_running_jobs(self):
        tracker = _Tracker()
        pool = CodexWorkerPool(max_workers=1, warmup_jobs=0)
        batch = pool.start_many(tracker.jobs("a", "b"))

        batch.cancel()

        with self.assertRaises(CodexCancelledError):
            batch.result(timeout=2)

    def test_start_many_claims_all_jobs_before_starting_any(self):
        tracker = _Tracker()
        first = tracker.job("first")
        invalid = tracker.job("invalid")
        invalid._done.set()
        pool = CodexWorkerPool(max_workers=1)

        with self.assertRaisesRegex(RuntimeError, "not pending"):
            pool.start_many([first, invalid])

        self.assertFalse(first.claimed)
        self.assertFalse(first.started)
        self.assertEqual(tracker.started, [])

    def test_same_job_cannot_be_registered_twice(self):
        tracker = _Tracker()
        job = tracker.job("same")
        pool = CodexWorkerPool(max_workers=1)

        with self.assertRaisesRegex(RuntimeError, "not pending"):
            pool.start_many([job, job])

        self.assertFalse(job.claimed)
        self.assertFalse(job.started)

    def test_start_preparation_failure_fails_batch_regardless_of_fail_fast(self):
        for fail_fast in (False, True):
            with self.subTest(fail_fast=fail_fast):
                tracker = _Tracker()
                broken = tracker.job(
                    "broken",
                    prepare_failure=RuntimeError("prepare failed"),
                )
                unused = tracker.job("unused")
                pool = CodexWorkerPool(
                    max_workers=1,
                    warmup_jobs=0,
                    fail_fast=fail_fast,
                )

                with self.assertRaisesRegex(RuntimeError, "prepare failed"):
                    pool.start_many([broken, unused]).result(timeout=2)

                self.assertTrue(broken._cancelled)
                self.assertTrue(unused._cancelled)
                self.assertFalse(unused.started)

    def test_start_many_empty_succeeds(self):
        pool = CodexWorkerPool(max_workers=1)

        result = pool.start_many([]).result(timeout=2)

        self.assertEqual(result.responses, ())
        self.assertEqual(result.errors, ())

    def test_batch_shares_bridges_only_for_identical_tool_configurations(self):
        class FakeBridge:
            instances = []

            def __init__(self, tools, *, max_concurrency, tool_timeout):
                self.configuration = (tools, max_concurrency, tool_timeout)
                self.closed = False
                self.instances.append(self)

            def start(self):
                pass

            def close(self):
                self.closed = True

        tracker = _Tracker()
        def shared_handler() -> str:
            return "shared"

        def distinct_handler() -> str:
            return "distinct"

        shared_tool = Tool(
            shared_handler,
            "Run shared",
            args={},
            returns="A string",
        )
        equivalent_but_distinct_tool = Tool(
            distinct_handler,
            "Run distinct",
            args={},
            returns="A string",
        )
        first, second, third, no_tools = tracker.jobs("a", "b", "c", "d")
        for job in (first, second):
            job.config = SessionConfig(
                tools=ToolConfig(
                    tools=(shared_tool,),
                    max_concurrent_calls=2,
                    timeout=3.0,
                ),
            )
        third.config = SessionConfig(
            tools=ToolConfig(
                tools=(equivalent_but_distinct_tool,),
                max_concurrent_calls=2,
                timeout=3.0,
            ),
        )

        with patch("codex_job_runner._pool._ToolBridge", FakeBridge):
            CodexWorkerPool(max_workers=4, warmup_jobs=0).start_many(
                [first, second, third, no_tools]
            ).result(timeout=2)

        self.assertIs(first.bridge, second.bridge)
        self.assertIsNot(first.bridge, third.bridge)
        self.assertIsNone(no_tools.bridge)
        self.assertEqual(len(FakeBridge.instances), 2)
        self.assertTrue(all(bridge.closed for bridge in FakeBridge.instances))

    def test_pool_configuration_validation(self):
        for value in (0, -1, True, 1.5):
            with self.subTest(max_workers=value):
                with self.assertRaises(ValueError):
                    CodexWorkerPool(max_workers=value)
        for value in (-1, True, 1.5):
            with self.subTest(warmup_jobs=value):
                with self.assertRaises(ValueError):
                    CodexWorkerPool(max_workers=1, warmup_jobs=value)
        for value in (None, 0, 1, "false"):
            with self.subTest(fail_fast=value):
                with self.assertRaises(ValueError):
                    CodexWorkerPool(max_workers=1, fail_fast=value)

    def test_open_batch_accepts_tasks_while_running(self):
        tracker = _Tracker()
        pool = CodexWorkerPool(max_workers=2, warmup_jobs=0)
        batch = pool.start_batch()

        batch.submit(tracker.job("a"))
        deadline = time.monotonic() + 1
        while not tracker.started and time.monotonic() < deadline:
            time.sleep(0.001)
        batch.submit(tracker.job("b"))
        batch.close()

        result = batch.result(timeout=2)
        self.assertEqual(
            tuple(response.content for response in result.responses),
            ("a", "b"),
        )

    def test_closed_batch_rejects_external_submit(self):
        tracker = _Tracker()
        pool = CodexWorkerPool(max_workers=1)
        batch = pool.start_batch()
        batch.close()

        with self.assertRaisesRegex(RuntimeError, "closed"):
            batch.submit(tracker.job("late"))

        result = batch.result(timeout=2)
        self.assertEqual(result.responses, ())
        self.assertEqual(result.errors, ())

    def test_completion_handler_can_add_tasks_after_close(self):
        tracker = _Tracker()
        pool = CodexWorkerPool(max_workers=2, warmup_jobs=0)
        events = []

        def on_task_completed(event):
            self.assertIsInstance(event, CodexTaskCompleted)
            events.append(event.job.value)
            return [tracker.job("child")] if event.job.value == "root" else []

        result = pool.start_many(
            tracker.jobs("root"), on_task_completed=on_task_completed
        ).result(timeout=2)

        self.assertEqual(
            tuple(response.content for response in result.responses),
            ("root", "child"),
        )
        self.assertCountEqual(events, ["root", "child"])

    def test_batch_waits_for_completion_handler(self):
        tracker = _Tracker()
        pool = CodexWorkerPool(max_workers=1, warmup_jobs=0)
        handler_started = threading.Event()
        release_handler = threading.Event()

        def on_task_completed(event):
            handler_started.set()
            release_handler.wait(1)
            return []

        batch = pool.start_many(
            tracker.jobs("root"), on_task_completed=on_task_completed
        )
        self.assertTrue(handler_started.wait(1))
        self.assertFalse(batch.done())
        release_handler.set()
        batch.result(timeout=2)

    def test_completion_handler_failure_fails_batch(self):
        tracker = _Tracker()
        pool = CodexWorkerPool(max_workers=1, warmup_jobs=0)

        def on_task_completed(event):
            raise RuntimeError("handler failed")

        with self.assertRaisesRegex(RuntimeError, "handler failed"):
            pool.start_many(
                tracker.jobs("root"), on_task_completed=on_task_completed
            ).result(timeout=2)

    def test_max_tasks_applies_to_handler_additions(self):
        tracker = _Tracker()
        pool = CodexWorkerPool(max_workers=1, warmup_jobs=0)

        with self.assertRaisesRegex(ValueError, "max_tasks"):
            pool.start_many(
                tracker.jobs("root"),
                on_task_completed=lambda event: [tracker.job("child")],
                max_tasks=1,
            ).result(timeout=2)


if __name__ == "__main__":
    unittest.main()
