"""Bounded scheduling for constructed, pending Codex jobs."""

from __future__ import annotations

import os
import threading
from collections import deque
from dataclasses import dataclass, field

from ._tools import _ToolBridge
from .errors import CodexCancelledError, CodexTimeoutError
from .types import CodexResponse


@dataclass(frozen=True)
class CodexBatchResult:
    """Task results and aggregate usage for one batch."""

    responses: tuple[CodexResponse | None, ...]
    errors: tuple[BaseException | None, ...]
    usage: dict[str, int]
    usage_complete: bool
    usage_incomplete_reasons: tuple[str, ...]
    successful_usage: dict[str, int]
    failed_usage: dict[str, int]
    estimated_cost_usd: float | None
    usage_by_model: dict[str, dict[str, int]] = field(default_factory=dict)
    successful_usage_by_model: dict[str, dict[str, int]] = field(
        default_factory=dict
    )
    failed_usage_by_model: dict[str, dict[str, int]] = field(
        default_factory=dict
    )
    estimated_cost_usd_by_model: dict[str, float] | None = None


@dataclass(frozen=True)
class CodexTaskCompleted:
    """Successful job completion delivered to a batch handler."""

    job: object
    response: CodexResponse


class CodexBatch:
    """Open queue of pending jobs and its ordered result handle."""

    def __init__(
        self,
        *,
        jobs=(),
        closed=False,
        max_workers,
        warmup_jobs,
        fail_fast,
        on_task_completed,
        max_tasks,
        on_done,
    ):
        self._max_workers = max_workers
        self._warmup_jobs = warmup_jobs
        self._fail_fast = fail_fast
        self._on_task_completed = on_task_completed
        self._max_tasks = max_tasks
        self._on_done = on_done
        self._condition = threading.Condition(threading.RLock())
        self._done = threading.Event()
        self._closed = False
        self._pending = deque()
        self._active = {}
        self._handler_count = 0
        self._submitted_count = 0
        self._warmup_completed = 0
        self._responses = []
        self._errors = []
        self._jobs = []
        self._bridges = {}
        self._result = None
        self._error = None

        with self._condition:
            self._enqueue_many_locked(jobs)
            self._closed = closed

        self._thread = threading.Thread(
            target=self._schedule,
            name="codex_worker_pool_batch",
            daemon=True,
        )
        self._thread.start()

    @property
    def warmup_jobs(self):
        return self._warmup_jobs

    @property
    def closed(self):
        with self._condition:
            return self._closed

    def done(self):
        return self._done.is_set()

    def submit(self, job):
        with self._condition:
            if self._closed:
                raise RuntimeError("worker pool batch is closed")
            return self._enqueue_many_locked((job,))[0]

    def close(self):
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def result(self, timeout=None):
        if not self._done.wait(timeout):
            self.cancel()
            raise CodexTimeoutError("codex worker pool batch timed out")
        if self._error is not None:
            raise self._error
        return self._result

    def cancel(self):
        self._fail(CodexCancelledError("codex worker pool batch was cancelled"))

    def _enqueue_many_locked(self, jobs):
        items = _materialize_jobs(jobs)
        if self._error is not None or self._done.is_set():
            raise RuntimeError("worker pool batch is no longer accepting jobs")
        if self._submitted_count + len(items) > self._max_tasks:
            raise ValueError("worker pool batch exceeded max_tasks")

        claimed = []
        try:
            for job in items:
                job._claim(self)
                claimed.append(job)
        except BaseException:
            for job in reversed(claimed):
                job._release_claim(self)
            raise

        indexes = []
        for job in items:
            index = self._submitted_count
            self._submitted_count += 1
            self._responses.append(None)
            self._errors.append(None)
            self._jobs.append(job)
            self._pending.append((index, job))
            indexes.append(index)
        self._condition.notify_all()
        return tuple(indexes)

    def _enqueue_handler_jobs(self, jobs):
        if jobs is None:
            return
        if isinstance(jobs, (str, bytes, os.PathLike)):
            raise ValueError(
                "on_task_completed must return an iterable of pending jobs"
            )
        try:
            items = tuple(jobs)
        except TypeError as exc:
            raise ValueError(
                "on_task_completed must return an iterable of pending jobs"
            ) from exc
        with self._condition:
            self._enqueue_many_locked(items)

    def _can_dispatch_locked(self):
        if self._error is not None or not self._pending:
            return False
        if self._warmup_completed < self._warmup_jobs:
            return not self._active
        return len(self._active) < self._max_workers

    def _should_finish_locked(self):
        return (
            self._closed
            and not self._pending
            and not self._active
            and self._handler_count == 0
        ) or (
            self._error is not None
            and not self._active
            and self._handler_count == 0
        )

    def _schedule(self):
        try:
            while True:
                with self._condition:
                    while not self._can_dispatch_locked():
                        if self._should_finish_locked():
                            self._finish_locked()
                            return
                        self._condition.wait()
                    index, job = self._pending.popleft()
                try:
                    bridge = self._bridge_for(job)
                    job._start_claimed(self, tool_bridge=bridge)
                except BaseException as exc:
                    try:
                        job._cancel_claimed(self)
                    finally:
                        self._fail(exc)
                    continue
                with self._condition:
                    if self._error is not None:
                        cancel_immediately = True
                    else:
                        self._active[index] = job
                        cancel_immediately = False
                        threading.Thread(
                            target=self._wait_job,
                            args=(index, job),
                            name=f"codex_worker_pool_task_{index}",
                            daemon=True,
                        ).start()
                    self._condition.notify_all()
                if cancel_immediately:
                    job._cancel_claimed(self)
        except BaseException as exc:
            self._fail(exc)
            with self._condition:
                while not self._should_finish_locked():
                    self._condition.wait()
                self._finish_locked()

    def _bridge_for(self, job):
        tools, max_concurrent_calls, timeout = job._tool_bridge_spec()
        if not tools:
            return None
        key = (
            tuple(tools),
            max_concurrent_calls,
            timeout,
        )
        bridge = self._bridges.get(key)
        if bridge is None:
            bridge = _ToolBridge(
                tools,
                max_concurrency=max_concurrent_calls,
                tool_timeout=timeout,
            )
            try:
                bridge.start()
            except BaseException:
                bridge.close()
                raise
            self._bridges[key] = bridge
        return bridge

    def _wait_job(self, index, job):
        try:
            response = job.result()
        except BaseException as exc:
            with self._condition:
                self._active.pop(index, None)
                if index < self._warmup_jobs:
                    self._warmup_completed += 1
                if not self._fail_fast:
                    self._errors[index] = exc
                self._condition.notify_all()
            if self._fail_fast:
                self._fail(exc)
            return

        with self._condition:
            self._active.pop(index, None)
            self._responses[index] = response
            if index < self._warmup_jobs:
                self._warmup_completed += 1
            if self._on_task_completed is not None:
                self._handler_count += 1
                threading.Thread(
                    target=self._run_handler,
                    args=(job, response),
                    name=f"codex_worker_pool_handler_{index}",
                    daemon=True,
                ).start()
            self._condition.notify_all()

    def _run_handler(self, job, response):
        try:
            additional_jobs = self._on_task_completed(
                CodexTaskCompleted(job=job, response=response)
            )
            self._enqueue_handler_jobs(additional_jobs)
        except BaseException as exc:
            self._fail(exc)
        finally:
            with self._condition:
                self._handler_count -= 1
                self._condition.notify_all()

    def _fail(self, error):
        with self._condition:
            if self._done.is_set() or self._error is not None:
                return
            self._error = error
            self._closed = True
            pending = tuple(job for _, job in self._pending)
            self._pending.clear()
            active = tuple(self._active.values())
            self._condition.notify_all()
        for job in pending:
            job._cancel_claimed(self)
        for job in active:
            job._cancel_claimed(self)

    def _finish_locked(self):
        if self._error is None:
            responses = tuple(self._responses)
            errors = tuple(self._errors)
            usage = {}
            successful_usage = {}
            failed_usage = {}
            usage_incomplete_reasons = []
            costs = []
            usage_by_model = {}
            successful_usage_by_model = {}
            failed_usage_by_model = {}
            cost_by_model = {}
            for index, (job, response, error) in enumerate(
                zip(self._jobs, responses, errors)
            ):
                task_usage = successful_usage
                task_usage_by_model = successful_usage_by_model
                if response is None:
                    response = getattr(error, "response", None)
                    task_usage = failed_usage
                    task_usage_by_model = failed_usage_by_model
                recursive_usage = getattr(job, "total_usage", None)
                recursive_usage_by_model = getattr(job, "total_usage_by_model", None)
                recursive_cost = getattr(job, "total_estimated_cost_usd", None)
                recursive_cost_by_model = getattr(
                    job,
                    "total_estimated_cost_usd_by_model",
                    None,
                )
                recursive_reasons = getattr(
                    job,
                    "total_usage_incomplete_reasons",
                    None,
                )
                if recursive_usage is not None:
                    task_total_usage = recursive_usage
                    task_total_usage_by_model = recursive_usage_by_model or {}
                    task_cost = recursive_cost
                    task_cost_by_model = recursive_cost_by_model
                    for reason in recursive_reasons or ():
                        usage_incomplete_reasons.append(
                            f"task[{index}] job[{getattr(job, 'id', index)}] {reason}"
                        )
                else:
                    task_total_usage = None if response is None else response.usage
                    task_total_usage_by_model = {}
                    if response is not None and response.usage is not None:
                        model = getattr(getattr(job, "config", None), "model", None)
                        if isinstance(model, str) and model.strip():
                            task_total_usage_by_model = {
                                model: dict(response.usage)
                            }
                    task_cost = (
                        None if response is None else response.estimated_cost_usd
                    )
                    task_cost_by_model = (
                        None
                        if task_cost is None or not task_total_usage_by_model
                        else {
                            next(iter(task_total_usage_by_model)): task_cost
                        }
                    )
                if response is None:
                    usage_incomplete_reasons.append(
                        f"task[{index}] failed without a response"
                    )
                    if recursive_usage is None:
                        continue
                if task_total_usage is None:
                    outcome = "failed" if error is not None else "successful"
                    usage_incomplete_reasons.append(
                        f"task[{index}] {outcome} response has no usage"
                    )
                for key, value in (task_total_usage or {}).items():
                    usage[key] = usage.get(key, 0) + value
                    task_usage[key] = task_usage.get(key, 0) + value
                _merge_model_usage(usage_by_model, task_total_usage_by_model)
                _merge_model_usage(task_usage_by_model, task_total_usage_by_model)
                if task_cost is not None:
                    costs.append(task_cost)
                if task_cost_by_model is not None:
                    for model, cost in task_cost_by_model.items():
                        cost_by_model[model] = cost_by_model.get(model, 0.0) + cost
                elif task_cost is not None and task_total_usage is not None:
                    usage_incomplete_reasons.append(
                        f"task[{index}] cost has no model attribution"
                    )
            usage_incomplete_reasons = list(dict.fromkeys(usage_incomplete_reasons))
            usage_complete = not usage_incomplete_reasons
            self._result = CodexBatchResult(
                responses=responses,
                errors=errors,
                usage=usage,
                usage_complete=usage_complete,
                usage_incomplete_reasons=tuple(usage_incomplete_reasons),
                successful_usage=successful_usage,
                failed_usage=failed_usage,
                estimated_cost_usd=(
                    sum(costs)
                    if usage_complete and len(costs) == len(responses)
                    else None
                ),
                usage_by_model=usage_by_model,
                successful_usage_by_model=successful_usage_by_model,
                failed_usage_by_model=failed_usage_by_model,
                estimated_cost_usd_by_model=(
                    cost_by_model if usage_complete else None
                ),
            )
        bridges = tuple(self._bridges.values())
        self._bridges.clear()
        for bridge in bridges:
            try:
                bridge.close()
            except BaseException as exc:
                if self._error is None:
                    self._error = exc
        self._done.set()
        self._on_done(self)


def _merge_model_usage(target, incoming):
    for model, model_usage in incoming.items():
        current = target.setdefault(model, {})
        for key, value in model_usage.items():
            current[key] = current.get(key, 0) + value


class CodexWorkerPool:
    """Run one batch of constructed jobs at bounded concurrency."""

    def __init__(self, *, max_workers, warmup_jobs=1, fail_fast=False):
        if (
            not isinstance(max_workers, int)
            or isinstance(max_workers, bool)
            or max_workers <= 0
        ):
            raise ValueError("max_workers must be a positive integer")
        if (
            not isinstance(warmup_jobs, int)
            or isinstance(warmup_jobs, bool)
            or warmup_jobs < 0
        ):
            raise ValueError("warmup_jobs must be a non-negative integer")
        if not isinstance(fail_fast, bool):
            raise ValueError("fail_fast must be a bool")
        self.max_workers = max_workers
        self.warmup_jobs = warmup_jobs
        self.fail_fast = fail_fast
        self._lock = threading.RLock()
        self._batch = None

    def start_batch(self, *, on_task_completed=None, max_tasks=1000):
        return self._start_batch(
            jobs=(),
            closed=False,
            on_task_completed=on_task_completed,
            max_tasks=max_tasks,
        )

    def start_many(self, jobs, *, on_task_completed=None, max_tasks=1000):
        items = _materialize_jobs(jobs)
        return self._start_batch(
            jobs=items,
            closed=True,
            on_task_completed=on_task_completed,
            max_tasks=max_tasks,
        )

    def _start_batch(
        self,
        *,
        jobs,
        closed,
        on_task_completed,
        max_tasks,
    ):
        if on_task_completed is not None and not callable(on_task_completed):
            raise ValueError("on_task_completed must be callable or None")
        if (
            not isinstance(max_tasks, int)
            or isinstance(max_tasks, bool)
            or max_tasks <= 0
        ):
            raise ValueError("max_tasks must be a positive integer")
        with self._lock:
            if self._batch is not None and not self._batch.done():
                raise RuntimeError("worker pool already has a running batch")
            batch = CodexBatch(
                jobs=jobs,
                closed=closed,
                max_workers=self.max_workers,
                warmup_jobs=self.warmup_jobs,
                fail_fast=self.fail_fast,
                on_task_completed=on_task_completed,
                max_tasks=max_tasks,
                on_done=self._batch_done,
            )
            self._batch = batch
            return batch

    def _batch_done(self, batch):
        with self._lock:
            if self._batch is batch:
                self._batch = None


def _materialize_jobs(jobs):
    if isinstance(jobs, (str, bytes, os.PathLike)):
        raise ValueError("jobs must be an iterable of pending CodexJob instances")
    try:
        items = tuple(jobs)
    except TypeError as exc:
        raise ValueError(
            "jobs must be an iterable of pending CodexJob instances"
        ) from exc
    from .job import CodexJob

    for job in items:
        if not isinstance(job, CodexJob):
            raise TypeError("worker pool accepts only CodexJob instances")
    return items
