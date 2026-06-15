#!/usr/bin/env python3

import json
import os
from pathlib import Path
import sys


def fail(message):
    print(message, file=sys.stderr)
    sys.exit(1)


def require(condition, message):
    if not condition:
        fail(message)


def main():
    try:
        data = json.loads(sys.stdin.read())
    except json.JSONDecodeError as exc:
        fail(f"output is not valid JSON: {exc}")

    require(isinstance(data, dict), "top-level output must be an object")

    for key in (
        "company_name",
        "summary",
        "employee_count",
        "salary",
        "references",
    ):
        require(key in data, f"missing required field: {key}")

    require(isinstance(data["company_name"], str) and data["company_name"].strip(), "company_name must be a non-empty string")
    require(isinstance(data["summary"], str) and data["summary"].strip(), "summary must be a non-empty string")

    employee_count = data["employee_count"]
    require(isinstance(employee_count, dict), "employee_count must be an object")
    require(isinstance(employee_count.get("value"), int) and not isinstance(employee_count.get("value"), bool) and employee_count["value"] >= 0, "employee_count.value must be a non-negative integer")
    require(employee_count.get("scope") in {"consolidated", "non_consolidated", "unknown"}, "employee_count.scope is invalid")
    require(isinstance(employee_count.get("as_of"), str), "employee_count.as_of must be a string")
    require(isinstance(employee_count.get("source_note"), str) and employee_count["source_note"].strip(), "employee_count.source_note must be a non-empty string")

    salary = data["salary"]
    require(isinstance(salary, dict), "salary must be an object")
    require(isinstance(salary.get("value_jpy"), int) and not isinstance(salary.get("value_jpy"), bool) and salary["value_jpy"] >= 0, "salary.value_jpy must be a non-negative integer")
    require(salary.get("label") in {"average_annual_salary", "starting_salary", "estimated", "unknown"}, "salary.label is invalid")
    require(isinstance(salary.get("as_of"), str), "salary.as_of must be a string")
    require(isinstance(salary.get("source_note"), str) and salary["source_note"].strip(), "salary.source_note must be a non-empty string")

    references = data["references"]
    require(isinstance(references, list) and len(references) >= 1, "references must be a non-empty array")
    for index, ref in enumerate(references):
        require(isinstance(ref, dict), f"references[{index}] must be an object")
        require(isinstance(ref.get("title"), str) and ref["title"].strip(), f"references[{index}].title must be a non-empty string")
        require(isinstance(ref.get("url"), str) and ref["url"].startswith(("http://", "https://")), f"references[{index}].url must be an http(s) URL")
        require(isinstance(ref.get("note"), str) and ref["note"].strip(), f"references[{index}].note must be a non-empty string")

    subcwd = os.environ.get("CODEX_WRAPPER_SUBCWD")
    require(isinstance(subcwd, str) and subcwd.strip(), "CODEX_WRAPPER_SUBCWD is required for checklist validation")
    checklist_path = Path(subcwd) / "workflow_checklist.json"
    require(checklist_path.is_file(), "workflow_checklist.json is missing from subcwd")
    try:
        checklist = json.loads(checklist_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail(f"workflow_checklist.json is not valid JSON: {exc}")
    require(isinstance(checklist, dict), "workflow_checklist.json must be an object")
    missing = [key for key, value in checklist.items() if value is not True]
    require(not missing, f"workflow checklist is incomplete: {', '.join(sorted(missing))}")

    sys.exit(0)


if __name__ == "__main__":
    main()
