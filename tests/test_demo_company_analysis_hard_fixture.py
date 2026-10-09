import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from codex_job_runner import CodexJob, SessionConfig, WorkspaceConfig
from jobs.demo_company_analysis import DemoCompanyAnalysisJob
from jobs.demo_company_analysis.client import _DEFAULT_REVIEW_INSTRUCTIONS


HERE = Path(__file__).resolve().parents[1] / "jobs" / "demo_company_analysis"


class DemoCompanyAnalysisHardFixtureTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.subcwd = Path(self.temporary_directory.name)
        self.job = DemoCompanyAnalysisJob(
            config=SessionConfig(
                workspace=WorkspaceConfig(cwd=self.temporary_directory.name)
            ),
            company_name="トヨタ自動車",
        )

    def validate_ledger(self, text):
        (self.subcwd / "evidence_ledger.json").write_text(text, encoding="utf-8")
        with mock.patch.object(
            CodexJob,
            "subcwd_path",
            new_callable=mock.PropertyMock,
            return_value=self.subcwd,
        ):
            self.job.validate_output(SimpleNamespace(content="{}"))

    def test_minimal_structural_ledger_passes(self):
        self.validate_ledger(json.dumps({"version": 1, "claims": []}))

    def test_incomplete_claim_is_left_for_reviewer(self):
        self.validate_ledger(json.dumps({"version": 1, "claims": [{"claim_id": "c1"}]}))

    def test_malformed_json_fails(self):
        with self.assertRaisesRegex(ValueError, "not valid JSON"):
            self.validate_ledger("{")

    def test_non_object_fails(self):
        with self.assertRaisesRegex(ValueError, "must be an object"):
            self.validate_ledger("[]")

    def test_non_list_claims_fails(self):
        with self.assertRaisesRegex(ValueError, "claims must be an array"):
            self.validate_ledger(json.dumps({"version": 1, "claims": {}}))

    def test_reviewer_owns_evidence_semantics(self):
        instructions = _DEFAULT_REVIEW_INSTRUCTIONS
        for phrase in (
            "field completeness",
            "cross-reference integrity",
            "fiscal period",
            "scope",
            "unit normalization",
            "cited locator",
            "Recalculate all five year-over-year changes",
            "cross-company comparison",
            "comparability_exceptions",
            "do not deliberately defer an issue",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, instructions)

    def test_two_company_schema_and_four_repair_bound(self):
        schema = json.loads((HERE / "output.schema.json").read_text(encoding="utf-8"))
        companies = schema["properties"]["companies"]
        self.assertEqual((companies["minItems"], companies["maxItems"]), (2, 2))
        company_key = companies["items"]["properties"]["company_key"]
        self.assertEqual(company_key["enum"], ["toyota", "honda"])
        self.assertIn("cross_company_comparisons", schema["required"])
        self.assertIn("comparability_exceptions", schema["required"])
        self.assertEqual(self.job._definition["review"].max_repairs, 4)


if __name__ == "__main__":
    unittest.main()
