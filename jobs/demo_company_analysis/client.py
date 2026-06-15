"""Demo company analysis job with schema, validator, review, and repair."""

import json
from pathlib import Path

from codex_api_wrapper import CodexClient, ReviewConfig


HERE = Path(__file__).resolve().parent
PROMPT_PATH = HERE / "prompt.md"
OUTPUT_SCHEMA_PATH = HERE / "output.schema.json"
VALIDATOR_PATH = HERE / "validate_output.py"
CHECKLIST_PATH = "workflow_checklist.json"

_DEFAULT_REVIEW_INSTRUCTIONS = """\
Use the references in the JSON output as the starting point for verification.
Check that summary, employee_count, and salary are supported by the cited sources.
If a cited source does not support a claim, or if the claim conflicts with the source, fail the review and request a repair.
When needed, open the cited URLs and do lightweight web verification against reliable primary or high-quality secondary sources.
Require at least one source that supports employee count and at least one source that supports salary.
"""


class DemoCompanyAnalysisClient(CodexClient):
    """Analyze one company and return a structured JSON report."""

    def __init__(self, **kwargs):
        kwargs.setdefault("ignore_global_context", True)
        kwargs.setdefault("allowed_skills", [])
        kwargs.setdefault("allow_shell_commands", False)
        kwargs.setdefault("timeout", None)
        super().__init__(**kwargs)

    def start(
        self,
        *,
        company_name,
        attachments=None,
        read_paths=None,
        write_paths=None,
        extra_input=None,
        review=None,
        **kwargs,
    ):
        prompt = _build_input(company_name=company_name, extra_input=extra_input)
        resolved_review = review or ReviewConfig(
            instructions=_DEFAULT_REVIEW_INSTRUCTIONS,
        )
        attachment_list = list(attachments or [])
        readable = list(read_paths or attachment_list)
        writable = list(write_paths or [])
        checklist_write_path = f"${{SUBCWD}}/{CHECKLIST_PATH}"
        if checklist_write_path not in writable:
            writable.append(checklist_write_path)
        return super().start(
            input=prompt,
            attachments=attachment_list,
            read_paths=readable,
            write_paths=writable,
            subcwd_files=_build_checklist_subcwd_files(),
            output_schema_path=str(OUTPUT_SCHEMA_PATH),
            output_validator=["python3", str(VALIDATOR_PATH)],
            review=resolved_review,
            repair_validation_failures=True,
            max_repairs=5,
            **kwargs,
        )


def _build_input(*, company_name, extra_input):
    prompt = PROMPT_PATH.read_text(encoding="utf-8").strip()
    sections = [
        prompt,
        f"Company name:\n{company_name}",
        (
            "Workflow checklist:\n"
            f"- Update `${{SUBCWD}}/{CHECKLIST_PATH}` as you complete each required step.\n"
            "- Before finishing, every checklist item must be set to true."
        ),
    ]
    if extra_input:
        sections.append(f"Additional input:\n{extra_input}")
    return "\n\n".join(sections)


def _build_checklist_subcwd_files():
    checklist = {
        "checked_company_summary": False,
        "checked_employee_count": False,
        "checked_salary": False,
        "checked_references": False,
    }
    return {
        CHECKLIST_PATH: {
            "text": json.dumps(checklist, ensure_ascii=False, indent=2) + "\n",
        }
    }
