#!/usr/bin/env python3

"""Run the demo company analysis job for Toyota."""

from pathlib import Path
import sys


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jobs.demo_company_analysis import DemoCompanyAnalysisClient
from codex_api_wrapper import CodexExecutionError, CodexValidationError


WORKDIR = HERE / "_demo_workspace"


def main():
    WORKDIR.mkdir(parents=True, exist_ok=True)
    client = DemoCompanyAnalysisClient(
        cwd=str(WORKDIR),
    )
    try:
        response = client.run(
            company_name="トヨタ自動車",
        )
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


if __name__ == "__main__":
    main()
