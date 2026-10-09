"""Demo company analysis job with schema, validation, review, and repair."""

import json
from pathlib import Path

from codex_job_runner import (
    CodexJob,
    CodexResponse,
    ReviewConfig,
    SessionConfig,
    ValidationConfig,
)


HERE = Path(__file__).resolve().parent
PROMPT_PATH = HERE / "prompt.md"
OUTPUT_SCHEMA_PATH = HERE / "output.schema.json"
CHECKLIST_PATH = "workflow_checklist.json"
EVIDENCE_LEDGER_PATH = "evidence_ledger.json"

_DEFAULT_REVIEW_INSTRUCTIONS = """\
Review the Toyota-versus-Honda two-period research as an evidence audit. Use the cited primary-source URLs and precise locators as the starting point, and inspect evidence_ledger.json as a review artifact.

Own the semantic validation that the local validator deliberately does not perform. For every primary and derived claim, verify field completeness, claim-ID/source-ID/input-ID cross-reference integrity, the fiscal period, consolidated versus non-consolidated scope, as-of date, unit normalization, definition, and whether every cited locator actually supports the value. Missing or invalid evidence-ledger fields and locators are review findings, not reasons to assume local validation should have failed.

For each company, check that employee populations are not mixed across derived calculations. Recalculate all five year-over-year changes, both operating margins, and both revenue-per-consolidated-employee values from their declared inputs, formula, unit, and rounding rule. Examine discrepancies between official materials and require a scope/as_of/definition explanation and an explicit adoption reason when values differ.

For every cross-company comparison, verify fiscal-period alignment, accounting-definition compatibility, scope and unit alignment, the declared comparability basis, both input claims, formula, rounding direction, and limitations. Require missing or non-comparable values to appear in comparability_exceptions rather than being estimated. Reject unsupported causal explanations. Assess all discovered issues in each round; do not deliberately defer an issue to a later round.

Report a finding only for a concrete unsupported, inconsistent, incorrectly calculated, or materially incomplete claim. Do not invent a finding when the evidence and calculations pass these checks.
"""

class DemoCompanyAnalysisJob(CodexJob):
    """Analyze one company and return a structured JSON report."""

    def __init__(
        self,
        *,
        config: SessionConfig,
        company_name,
        read_paths=None,
        write_paths=None,
        extra_input=None,
        review=None,
        **kwargs,
    ):
        context = config.context
        fixed_context = [PROMPT_PATH]
        if context is not None:
            fixed_context.extend(
                [context] if isinstance(context, (str, Path)) else list(context)
            )
        config = config.with_overrides(
            context=fixed_context,
            ignore_global_context=True,
            allowed_skills=[],
            allow_shell_commands=False,
        )
        prompt = _build_input(company_name=company_name, extra_input=extra_input)
        resolved_review = review or ReviewConfig(
            context=[_DEFAULT_REVIEW_INSTRUCTIONS],
            artifact_paths=[f"${{SUBCWD}}/{EVIDENCE_LEDGER_PATH}"],
            max_repairs=4,
        )
        readable = list(read_paths or [])
        writable = list(write_paths or [])
        subcwd_write_path = "${SUBCWD}/."
        if subcwd_write_path not in writable:
            writable.append(subcwd_write_path)
        config = config.with_overrides(
            workspace=config.workspace.with_overrides(
                read_paths=readable,
                write_paths=writable,
                subcwd_files=_build_subcwd_files(),
            )
        )
        super().__init__(
            config=config,
            input=prompt,
            output_schema_path=str(OUTPUT_SCHEMA_PATH),
            review=resolved_review,
            validation=ValidationConfig(repair=True),
            **kwargs,
        )

    def validate_output(self, response: CodexResponse) -> None:
        """Validate only the evidence-ledger container before semantic review."""
        subcwd = self.subcwd_path
        _require(subcwd is not None, "SUBCWD is required for evidence-ledger validation")
        ledger_path = subcwd / EVIDENCE_LEDGER_PATH
        _require(ledger_path.is_file(), "evidence_ledger.json is missing from SUBCWD")
        try:
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"evidence_ledger.json is not valid JSON: {exc}") from exc
        _require(isinstance(ledger, dict), "evidence_ledger.json must be an object")
        if "version" in ledger:
            _require(isinstance(ledger["version"], (str, int)) and not isinstance(ledger["version"], bool), "evidence_ledger.json version must be a string or integer")
        _require(isinstance(ledger.get("claims"), list), "evidence_ledger.json claims must be an array")


def _build_input(*, company_name, extra_input):
    sections = [
        f"Company name:\n{company_name}",
        _checklist_context(),
    ]
    if extra_input:
        sections.append(f"Additional input:\n{extra_input}")
    return "\n\n".join(sections)


def _checklist_context():
    return (
        "Workflow checklist:\n"
        f"- Update `${{SUBCWD}}/{CHECKLIST_PATH}` as you complete each required step.\n"
        "- Before finishing, every checklist item must be set to true."
    )


def _build_subcwd_files():
    checklist = {
        "checked_primary_sources": False,
        "checked_period_and_scope": False,
        "checked_units": False,
        "calculated_derived_metrics": False,
        "reconciled_official_differences": False,
        "checked_cross_company_comparability": False,
        "updated_evidence_ledger": False,
    }
    return {
        CHECKLIST_PATH: {
            "text": json.dumps(checklist, ensure_ascii=False, indent=2) + "\n",
        },
        EVIDENCE_LEDGER_PATH: {
            "text": json.dumps(
                {
                    "version": 1,
                    "claim_template": {
                        "claim_id": "string",
                        "claim_type": "primary_derived_or_comparison",
                        "metric": "string",
                        "fiscal_year_end": "YYYY-MM-DD",
                        "raw_value": "number_or_source_string",
                        "normalized_value": "number",
                        "unit": "string",
                        "scope": "consolidated_non_consolidated_derived_or_cross_company",
                        "as_of": "string",
                        "definition": "string",
                        "source_locators": [
                            {"source_id": "string", "locator": "string"}
                        ],
                        "formula": "string_or_not_applicable",
                        "inputs": ["claim_id_for_derived_metrics"],
                        "rounding": "string",
                        "limitation": "string",
                    },
                    "claims": [],
                },
                ensure_ascii=False,
                indent=2,
            ) + "\n",
        },
    }


def _require(condition, message):
    if not condition:
        raise ValueError(message)
