"""Regression tests for isolated answers and explicit review relationships."""
import json
import pathlib
import sys
import tempfile
import unittest
from contextlib import contextmanager
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from codex_job_runner import (CodexJob, CodexExecutionError,
                              CodexReviewConflictError, CodexValidationError,
                              CodexReviewProtocolError, CodexTimeoutError,
                              CodexCancelledError)
from codex_job_runner._models import _CodexAttempt, _ReviewResult
from codex_job_runner._peer_review import parse_reviewer_decision, reviewer_decision_schema
from codex_job_runner._client_helpers import _consume_turn_via_stream
from codex_job_runner.types import CodexResponse


def finding(comment, kind="new", fid=None):
    return {"comment": comment, "relation": {"kind": kind, "finding_id": fid}}


def decide(fid, resolution="resolved", comment="accepted", other=None):
    return {"finding_id": fid, "resolution": resolution,
            "comment": comment, "conflicts_with": other}


class Process:
    returncode = 0
    def communicate(self, prompt):
        return "", ""
    def poll(self):
        return 0
    def terminate(self):
        pass


class Fixture:
    def __init__(self, root, reviewer=None, answer=None, template=None):
        self.root = root
        self.main_prompts = []
        self.answer_prompts = []
        self.schemas = []
        self.packages = []
        self.forks = []
        self.answer = answer
        initial = root / "initial.txt"
        initial.write_text("draft", encoding="utf-8")
        self.job = object.__new__(CodexJob)
        self.job._initialize_runtime(
            attempt=_CodexAttempt(["codex"], "input", Process(), initial, 0, 0),
            spawn_attempt=self.spawn, open_answer_fork=self.open_fork,
            original_input="input", context_paths=(), context_sections=(),
            output_schema_section=None, output_model_source_section=None,
            read_paths=(), write_paths=(), subcwd_files=(), cwd=root,
            review_config=SimpleNamespace(artifact_paths=(), max_repairs=5),
            review_runner=self.review, max_review_repairs=5,
            repair_prompt_template=template, log_dir=root,
        )
        self.reviewer = reviewer or self.default_review

    def spawn(self, *, prompt, attempt_index, repair_index):
        self.main_prompts.append(prompt)
        path = self.root / f"main-{attempt_index}.txt"
        path.write_text(f"repair-{repair_index}", encoding="utf-8")
        return _CodexAttempt(["codex"], prompt, Process(), path, attempt_index, repair_index)

    @contextmanager
    def open_fork(self, message):
        fixture = self
        class Fork:
            def __init__(self):
                self.closed = False
                self.prompts = []
                self._turn_session = SimpleNamespace(_trusted_internal_output_schema_paths=set())
            def _send_turn(self, prompt, *, output_schema_path, activity_callback):
                self.prompts.append(prompt)
                fixture.answer_prompts.append(prompt)
                fixture.schemas.append(json.loads(output_schema_path.read_text()))
                if fixture.answer:
                    return fixture.answer(fixture, self, prompt)
                ids = fixture.schemas[-1]["properties"]["finding_responses"]["items"]["properties"]["finding_id"]["enum"]
                return fixture.answer_response(ids)
        fork = Fork()
        self.forks.append(fork)
        try:
            yield fork
        finally:
            fork.closed = True

    @staticmethod
    def answer_response(ids=("F001",)):
        return CodexResponse(
            content=json.dumps({"finding_responses": [
                {"finding_id": fid, "disposition": "disputed", "response": "cannot satisfy both"}
                for fid in ids]}), returncode=0, stdout="", stderr="",
            argv=("codex", "--model", "gpt-5.6-luna"),
            usage={"input_tokens": 7, "output_tokens": 3, "cached_input_tokens": 0,
                   "reasoning_output_tokens": 0},
        )

    @staticmethod
    def default_review(package):
        if package["round"] == 0:
            return {"decisions": [], "new_findings": [finding("fix A")]}
        return {"decisions": [decide(item["finding_id"], "unresolved", "rebuttal")
                              for item in package["open_findings"]], "new_findings": []}

    def review(self, **kwargs):
        package = kwargs["protocol"]["package"]
        self.packages.append(package)
        decision = self.reviewer(package)
        response = CodexResponse(content=json.dumps(decision), returncode=0,
                                 stdout="", stderr="", argv=("review",))
        return _ReviewResult(False, (), "", response, "", "", ["review"], decision=decision)

    def run(self):
        self.job.start_background()
        return self.job.result(timeout=3)


class ReviewRelationshipTests(unittest.TestCase):
    closed = ({"finding_id": "F001", "resolution": "resolved"},
              {"finding_id": "F002", "resolution": "withdrawn"})

    def parse(self, new=(), decisions=(), open_ids=()):
        return parse_reviewer_decision(json.dumps({"decisions": list(decisions),
                                                  "new_findings": list(new)}),
                                       open_ids=open_ids, initial=not open_ids,
                                       closed_findings=self.closed)

    def test_relation_valid_kinds(self):
        for kind, fid in (("new", None), ("reopens", "F001"),
                          ("reopens", "F002"), ("revises", "F001"), ("conflicts", "F001")):
            with self.subTest(kind=kind, fid=fid):
                self.assertEqual(self.parse([finding("fix", kind, fid)])["new_findings"][0]["relation"]["kind"], kind)

    def test_relation_rejects_missing_unknown_and_inconsistent_values(self):
        invalid = [{"comment": "missing"}, finding("x", "new", "F001"),
                   finding("x", "reopens"), finding("x", "reopens", "F999"),
                   finding("x", "revises", "F002"), finding("x", "conflicts", "F002"),
                   finding("x", "other"), {"comment": "x", "relation": None},
                   finding("x", {}, None), finding("x", "reopens", [])]
        for item in invalid:
            with self.subTest(item=item), self.assertRaises(CodexReviewProtocolError):
                self.parse([item])

    def test_duplicate_reopens_are_rejected(self):
        for fid in ("F001", "F002"):
            with self.subTest(fid=fid), self.assertRaisesRegex(CodexReviewProtocolError, "reopens"):
                self.parse([finding("first", "reopens", fid), finding("second", "reopens", fid)])
        self.parse([finding("A", "reopens", "F001"), finding("B", "reopens", "F002")])
        for kind in ("revises", "conflicts"):
            with self.subTest(kind=kind):
                self.parse([finding("first", kind, "F001"), finding("second", kind, "F001")])

    def test_relation_requires_closed_id_even_if_id_is_open(self):
        with self.assertRaises(CodexReviewProtocolError):
            self.parse([finding("x", "reopens", "F003")], [decide("F003")], ("F003",))

    def test_conflict_decisions_validate_other_and_null(self):
        for other in ("F001", "F002", "F004"):
            with self.subTest(other=other):
                self.parse(decisions=[decide("F003", "conflicts", other=other), decide("F004")],
                           open_ids=("F003", "F004"))
        for resolution, other in (("conflicts", None), ("conflicts", "F003"),
                                  ("conflicts", "F999"), ("resolved", "F001")):
            with self.subTest(resolution=resolution, other=other), self.assertRaises(CodexReviewProtocolError):
                self.parse(decisions=[decide("F003", resolution, other=other)], open_ids=("F003",))
        missing = decide("F003")
        del missing["conflicts_with"]
        with self.assertRaises(CodexReviewProtocolError):
            self.parse(decisions=[missing], open_ids=("F003",))

    def test_schema_limits_closed_ids_and_initial_relations(self):
        schema = reviewer_decision_schema(open_ids=("F003",), initial=False, closed_findings=self.closed)
        relation = schema["properties"]["new_findings"]["items"]["properties"]["relation"]
        self.assertEqual(relation["properties"]["finding_id"]["anyOf"][0]["enum"], ["F001", "F002"])
        self.assertEqual(relation["required"], ["kind", "finding_id"])
        schema = reviewer_decision_schema(open_ids=(), initial=True)
        relation = schema["properties"]["new_findings"]["items"]["properties"]["relation"]
        self.assertEqual(relation["properties"]["kind"]["enum"], ["new"])
        self.assertEqual(relation["properties"]["finding_id"], {"type": "null"})

    def test_reopens_restores_id_and_revises_issues_new_id(self):
        for kind, expected in (("reopens", "F001"), ("revises", "F003")):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                def review(package):
                    round_index = package["round"]
                    if round_index == 0:
                        return {"decisions": [], "new_findings": [finding("fix A")]}
                    if round_index == 1:
                        return {"decisions": [decide("F001", comment="A accepted")],
                                "new_findings": [finding("fix B")]}
                    if round_index == 2:
                        return {"decisions": [decide("F002")],
                                "new_findings": [finding("A again", kind, "F001")]}
                    return {"decisions": [decide(expected)], "new_findings": []}
                fixture = Fixture(pathlib.Path(directory), reviewer=review)
                fixture.run()
                self.assertEqual(fixture.packages[3]["open_findings"], [{"finding_id": expected, "comment": "A again"}])
                closed = fixture.packages[2]["closed_findings"]
                self.assertEqual(closed, [{"finding_id": "F001", "comment": "fix A",
                                          "decision_comment": "A accepted", "resolution": "resolved", "main_response": {
                                              "disposition": "disputed", "response": "cannot satisfy both"}}])

    def test_withdrawn_findings_are_in_closed_package(self):
        with tempfile.TemporaryDirectory() as directory:
            def review(package):
                if package["round"] == 0:
                    return {"decisions": [], "new_findings": [finding("A")]}
                if package["round"] == 1:
                    return {"decisions": [decide("F001", "withdrawn", "dispute accepted")],
                            "new_findings": [finding("B")]}
                return {"decisions": [decide("F002")], "new_findings": []}
            fixture = Fixture(pathlib.Path(directory), reviewer=review)
            fixture.run()
            closed = fixture.packages[2]["closed_findings"][0]
            self.assertEqual(closed["resolution"], "withdrawn")
            self.assertEqual(closed["comment"], "A")
            self.assertEqual(closed["decision_comment"], "dispute accepted")

    def test_new_conflict_stops_with_both_sides_and_response(self):
        with tempfile.TemporaryDirectory() as directory:
            def review(package):
                if package["round"] == 0:
                    return {"decisions": [], "new_findings": [finding("A")]}
                if package["round"] == 1:
                    return {"decisions": [decide("F001", comment="accepted A")], "new_findings": [finding("B")]}
                return {"decisions": [decide("F002")], "new_findings": [finding("undo A", "conflicts", "F001")]}
            fixture = Fixture(pathlib.Path(directory), reviewer=review)
            with self.assertRaises(CodexReviewConflictError) as caught:
                fixture.run()
            error = caught.exception
            self.assertIsInstance(error, CodexValidationError)
            self.assertEqual(error.failure_stage, "review")
            self.assertEqual((error.round_index, error.attempts, error.repairs), (2, 3, 2))
            self.assertEqual([x["finding_id"] for x in error.conflicting_findings], [None, "F001"])
            self.assertIsNone(error.conflicting_findings[0]["main_response"])
            self.assertEqual(error.conflicting_findings[1]["main_response"]["disposition"], "disputed")
            self.assertEqual([x["comment"] for x in error.conflicting_findings], ["undo A", "A"])
            self.assertTrue(all(set(x) == {"finding_id", "comment", "main_response"}
                                for x in error.conflicting_findings))
            self.assertIn("new finding: undo A vs F001: A", str(error))
            self.assertNotIn("; reviewer:", str(error))
            self.assertIsNotNone(error.response)
            self.assertIsNotNone(error.review_response)
            self.assertTrue(pathlib.Path(error.log_path).is_file())
            self.assertEqual(len(fixture.main_prompts), 2)

    def test_decision_conflict_names_open_or_closed_other(self):
        for closed_other in (False, True):
            with self.subTest(closed_other=closed_other), tempfile.TemporaryDirectory() as directory:
                def review(package):
                    if package["round"] == 0:
                        return {"decisions": [], "new_findings": [finding("A"), finding("B")]}
                    if closed_other and package["round"] == 1:
                        return {"decisions": [decide("F001"), decide("F002", "unresolved", "B still needed")], "new_findings": []}
                    decisions = [decide("F002", "conflicts", "B cannot coexist with A", "F001")]
                    if not closed_other:
                        decisions.append(decide("F001"))
                    return {"decisions": decisions, "new_findings": []}
                fixture = Fixture(pathlib.Path(directory), reviewer=review)
                with self.assertRaises(CodexReviewConflictError) as caught:
                    fixture.run()
                self.assertEqual([x["finding_id"] for x in caught.exception.conflicting_findings], ["F002", "F001"])
                self.assertEqual([x["comment"] for x in caught.exception.conflicting_findings], ["B", "A"])
                self.assertIn("; reviewer: B cannot coexist with A", str(caught.exception))
                self.assertTrue(all(x["main_response"]["disposition"] == "disputed"
                                    for x in caught.exception.conflicting_findings))


class AnswerAndExecutionTests(unittest.TestCase):
    def test_corrections_share_fork_and_usage_is_aggregated(self):
        with tempfile.TemporaryDirectory() as directory:
            def answer(fixture, fork, prompt):
                response = fixture.answer_response()
                if len(fixture.answer_prompts) == 1:
                    return SimpleNamespace(**{**response.__dict__, "content": "not JSON"})
                return response
            fixture = Fixture(pathlib.Path(directory), answer=answer)
            with self.assertRaises(CodexValidationError):
                fixture.run()
            self.assertEqual(len(fixture.forks), 2)
            self.assertEqual(len(fixture.forks[0].prompts), 2)
            self.assertTrue(all(f.closed for f in fixture.forks))
            self.assertFalse(any("This is an answer turn" in x for x in fixture.main_prompts))
            self.assertEqual(fixture.schemas[0]["required"], ["finding_responses"])
            self.assertEqual(fixture.job._additional_usage["input_tokens"], 21)
            self.assertEqual(fixture.job._usage_by_model["gpt-5.6-luna"]["output_tokens"], 9)
            self.assertGreater(fixture.job._additional_estimated_cost_usd, 0)
            self.assertEqual(fixture.job._estimated_cost_usd_by_model["gpt-5.6-luna"], fixture.job._additional_estimated_cost_usd)
            self.assertIn("[Main Answer]", pathlib.Path(fixture.job.log_path).read_text())

    def test_previous_responses_reach_default_and_custom_repair_templates(self):
        for template in (None, "custom {review_findings}", "custom without placeholder"):
            with self.subTest(template=template), tempfile.TemporaryDirectory() as directory:
                fixture = Fixture(pathlib.Path(directory), template=template)
                with self.assertRaises(CodexValidationError):
                    fixture.run()
                self.assertIn("Reviewer (unresolved): rebuttal\n  Main (disputed): cannot satisfy both", fixture.main_prompts[1])
                self.assertNotIn("This is an answer turn", fixture.main_prompts[1])

    def test_invalid_answer_exhaustion_closes_fork(self):
        with tempfile.TemporaryDirectory() as directory:
            def answer(fixture, fork, prompt):
                return SimpleNamespace(content="bad", returncode=0, stdout="", stderr="", argv=("codex",), usage=None)
            fixture = Fixture(pathlib.Path(directory), answer=answer)
            with self.assertRaises(CodexReviewProtocolError):
                fixture.run()
            self.assertEqual(len(fixture.forks), 1)
            self.assertEqual(len(fixture.forks[0].prompts), 3)
            self.assertTrue(fixture.forks[0].closed)

    def test_execution_error_subclasses_propagate_unchanged(self):
        for role in ("answer", "reviewer"):
            for error_type in (CodexTimeoutError, CodexCancelledError,
                               CodexValidationError, CodexReviewProtocolError):
                with self.subTest(role=role, error_type=error_type), tempfile.TemporaryDirectory() as directory:
                    error = error_type("child failed")
                    def answer(fixture, fork, prompt):
                        raise error
                    def review(package):
                        if role == "reviewer":
                            raise error
                        return Fixture.default_review(package)
                    fixture = Fixture(pathlib.Path(directory), reviewer=review, answer=answer)
                    with self.assertRaises(error_type) as caught:
                        fixture.run()
                    # Peer-review protocol errors are enriched outside the execution handler.
                    if error_type is not CodexReviewProtocolError or role == "answer":
                        self.assertIs(caught.exception, error)
                        self.assertIs(fixture.job._error, error)
                    self.assertIn("child failed", pathlib.Path(fixture.job.log_path).read_text())
                    self.assertTrue(all(f.closed for f in fixture.forks))

    def test_execution_failures_collect_files_and_include_error_text(self):
        for role in ("answer exception", "answer returncode", "answer message only", "reviewer"):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as directory:
                def answer(fixture, fork, prompt):
                    if role == "answer returncode":
                        return SimpleNamespace(returncode=9, stdout="partial", stderr="stream disconnected", argv=("answer",))
                    raise CodexExecutionError("stream disconnected", returncode=9, stdout="partial",
                                              stderr="" if role == "answer message only" else "stream disconnected",
                                              argv=("answer",))
                def review(package):
                    if role == "reviewer":
                        raise CodexExecutionError("failure", returncode=9, stdout="partial", stderr="API error", argv=("reviewer",))
                    return Fixture.default_review(package)
                fixture = Fixture(pathlib.Path(directory), reviewer=review, answer=answer)
                outputs = pathlib.Path(directory) / "outputs"
                outputs.mkdir()
                (outputs / "result.txt").write_text("collected")
                fixture.job._subcwd_path = outputs
                fixture.job._should_collect_files = True
                with self.assertRaises(CodexExecutionError) as caught:
                    fixture.run()
                error = caught.exception
                self.assertIs(type(error), CodexExecutionError)
                text = "API error" if role == "reviewer" else "stream disconnected"
                self.assertIn(text, str(error))
                self.assertEqual(error.returncode, 9)
                self.assertEqual(error.stdout, "partial")
                self.assertEqual(error.files["result.txt"].text, "collected")
                self.assertTrue(error.activity_log)
                self.assertEqual(error.log_path, fixture.job.log_path)
                self.assertIn(text, pathlib.Path(error.log_path).read_text())
                self.assertTrue(all(f.closed for f in fixture.forks))

    def test_answer_artifact_invariant_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "artifact.txt").write_text("stable")
            def answer(fixture, fork, prompt):
                (root / "artifact.txt").write_text("changed")
                return fixture.answer_response()
            fixture = Fixture(root, answer=answer)
            fixture.job._review_config.artifact_paths = ("artifact.txt",)
            with self.assertRaisesRegex(CodexReviewProtocolError, "changed the final artifacts"):
                fixture.run()
            self.assertTrue(fixture.forks[0].closed)

    def test_sdk_error_events_are_logged_before_failed_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(pathlib.Path(directory))
            messages = []
            for method in ("error", "turn.error", "turn/error"):
                events = [SimpleNamespace(method=method, payload=SimpleNamespace(error=SimpleNamespace(message="stream disconnected"))),
                          SimpleNamespace(method="turn/completed", payload=SimpleNamespace(turn=SimpleNamespace(
                              status="failed", error=SimpleNamespace(message="API error"))))]
                class Stream:
                    def __iter__(self):
                        return iter(events)
                    def close(self):
                        pass
                process = SimpleNamespace(emit_stdout_line=lambda line: (
                    messages.append(line), fixture.job._record_stream_event_line(line, detail="finding_response")))
                with self.assertRaisesRegex(RuntimeError, "API error"):
                    _consume_turn_via_stream(process, SimpleNamespace(stream=lambda: Stream()))
            log = pathlib.Path(fixture.job.log_path).read_text()
            self.assertIn("stream disconnected", log)
            self.assertIn("API error", log)
            self.assertIn("[Main Answer]", log)


if __name__ == "__main__":
    unittest.main()
