"""Paid live acceptance test for main and reviewer-managed direct children."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from codex_job_runner import (
    CodexJob,
    ReviewConfig,
    SessionConfig,
    SubagentConfig,
    ValidationConfig,
    WorkspaceConfig,
)


class _LiveSubagentJob(CodexJob):
    def validate_output(self, response):
        if not self.subagent_results:
            raise ValueError(
                "The required spawn_agent call was not made. Call it exactly "
                "once, wait for the child, then answer."
            )


def _subagents(*, cwd: pathlib.Path, model: str) -> SubagentConfig:
    child = SessionConfig(
        model=model,
        reasoning_effort="low",
        timeout=90,
        allow_shell_commands=False,
        workspace=WorkspaceConfig(cwd=cwd),
    )
    return SubagentConfig(
        session=child,
        allowed_models={model: ("low",)},
        max_concurrent=1,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--reviewer", action="store_true")
    args = parser.parse_args()
    cwd = pathlib.Path.cwd().resolve()
    subagents = _subagents(cwd=cwd, model=args.model)
    review = None
    if args.reviewer:
        review = ReviewConfig(
            context=(
                "A review is invalid unless you call spawn_agent exactly once. "
                "Delegate the arithmetic verification. Tell the child to call "
                "ask_parent exactly once to ask for the second operand, answer "
                "25 with send_message, wait for the child, and only then return "
                "the review JSON."
            ),
            session=SessionConfig(
                model=args.model,
                reasoning_effort="low",
                timeout=120,
                allow_shell_commands=False,
                workspace=WorkspaceConfig(cwd=cwd),
            ),
            subagents=_subagents(cwd=cwd, model=args.model),
        )
    job = _LiveSubagentJob(
        config=SessionConfig(
            model=args.model,
            reasoning_effort="low",
            timeout=180,
            workspace=WorkspaceConfig(cwd=cwd),
        ),
        input=(
            "Use spawn_agent exactly once. Tell the child to calculate 17 plus "
            "a value that it must obtain by calling ask_parent exactly once. "
            "When wait_agent reports its question, answer 25 with send_message. "
            "Wait for the child and return only the verified number."
        ),
        review=review,
        subagents=subagents,
        validation=ValidationConfig(repair=True, max_repairs=1),
        max_total_repairs=1,
    )
    response = job.run()
    result = {
        "content": response.content,
        "main_children": job.subagent_results,
        "reviewer_children": job.reviewer_subagent_results,
        "main_child_cost_complete": job.subagent_cost_complete,
        "reviewer_child_cost_complete": job.reviewer_subagent_cost_complete,
        "total_estimated_cost_usd": job.total_estimated_cost_usd,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if not job.subagent_results:
        raise RuntimeError("main did not start a child")
    if args.reviewer and not job.reviewer_subagent_results:
        raise RuntimeError("reviewer did not start a child")
    if not job.subagent_cost_complete or not job.reviewer_subagent_cost_complete:
        raise RuntimeError("subagent cost accounting is incomplete")
    for child in (*job.subagent_results, *job.reviewer_subagent_results):
        log_path = child.get("log_path")
        if log_path is None:
            raise RuntimeError("child log path is unavailable")
        log_text = pathlib.Path(log_path).read_text(encoding="utf-8")
        if "ask_parent" not in log_text:
            raise RuntimeError(
                f"child did not call ask_parent: {log_path}"
            )
        if "[Subagent][" in log_text:
            raise RuntimeError(
                f"child started a native descendant: {log_path}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
