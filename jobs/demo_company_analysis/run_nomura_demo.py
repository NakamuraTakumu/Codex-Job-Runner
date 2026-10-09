#!/usr/bin/env python3

"""Run the demo company analysis job for Toyota and Honda."""

from pathlib import Path
import sys


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jobs.demo_company_analysis import DemoCompanyAnalysisJob
from codex_job_runner import (
    CodexExecutionError,
    CodexValidationError,
    SessionConfig,
    WorkspaceConfig,
)


WORKDIR = HERE / "_demo_workspace"


def main():
    WORKDIR.mkdir(parents=True, exist_ok=True)
    job = DemoCompanyAnalysisJob(
        config=SessionConfig(
            workspace=WorkspaceConfig(cwd=str(WORKDIR)),
        ),
        company_name="トヨタ自動車と本田技研工業",
    )
    try:
        response = job.run()
    except (CodexExecutionError, CodexValidationError) as exc:
        print("JOB_FAILED")
        print(f"message={exc.message}")
        print(f"returncode={exc.returncode}")
        if exc.stdout:
            print("STDOUT_START")
            print(exc.stdout)
            print("STDOUT_END")
        if exc.stderr:
            print("STDERR_START")
            print(exc.stderr)
            print("STDERR_END")
        raise
    print(response.content)
    print("JOB_METADATA")
    print(f"log_path={response.log_path}")
    print(f"attempts={job.current_attempt}")
    print(f"repair_index={job.repair_index}")
    print(f"estimated_cost_usd={job.total_estimated_cost_usd}")
    print(f"review_findings={len(job.review_findings)}")


if __name__ == "__main__":
    main()
