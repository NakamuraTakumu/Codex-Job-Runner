import json
import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace
from contextlib import contextmanager

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from codex_job_runner._peer_review import (
    capture_artifacts, changes, parse_main_response,
    main_finding_response_schema, parse_reviewer_decision, reviewer_decision_schema,
)
from codex_job_runner import CodexJob
from codex_job_runner.errors import CodexReviewProtocolError
from codex_job_runner._models import _CodexAttempt, _ReviewResult
from codex_job_runner.types import CodexResponse


class _Process:
    returncode = 0
    def communicate(self, prompt): return "", ""
    def poll(self): return 0
    def terminate(self): pass


class PeerReviewProtocolTests(unittest.TestCase):
    def test_capture_requires_initial_direct_file_but_allows_later_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            with self.assertRaises(CodexReviewProtocolError):
                capture_artifacts(cwd=root, paths=("missing.txt",), content="x", initial=True)
            captured = capture_artifacts(cwd=root, paths=("missing.txt",), content="x", initial=False)
        self.assertEqual(captured.files, {})
        self.assertEqual(captured.manifest[0]["artifact"], {"kind": "response_content", "path": None})

    def test_changes_are_deterministic_zero_context_hunks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "a.txt").write_text("one\n", encoding="utf-8")
            before = capture_artifacts(cwd=root, paths=("a.txt",), content="old\nkeep\n", initial=True)
            (root / "a.txt").unlink(); (root / "b.txt").write_text("two\n", encoding="utf-8")
            after = capture_artifacts(cwd=root, paths=("a.txt", "b.txt"), content="new\nkeep\n", initial=False)
            items = changes(before, after)
            self.assertEqual(items, changes(before, after))
        self.assertEqual([x["change_id"] for x in items], ["C001", "C002", "C003"])
        self.assertEqual([set(x) for x in items], [{"change_id", "artifact", "diff"}] * 3)
        self.assertEqual(items[0]["artifact"], {"kind": "response_content", "path": None})
        self.assertEqual(items[0]["diff"], "@@ -1,1 +1,1 @@\n-old\n+new\n")
        self.assertEqual(items[1]["artifact"], {"kind": "file", "path": "a.txt"})
        self.assertEqual(items[2]["artifact"], {"kind": "file", "path": "b.txt"})

    def test_strict_decision_rejects_unknown_top_level_field(self):
        with self.assertRaises(CodexReviewProtocolError):
            parse_reviewer_decision('{"decisions":[],"new_findings":[],"extra":1}', open_ids=(), initial=True)

    def test_main_response_must_answer_every_open_finding_once(self):
        valid = '{"finding_responses":[{"finding_id":"F002","disposition":"disputed","response":"no"},{"finding_id":"F001","disposition":"addressed","response":"done"}]}'
        self.assertEqual(
            [x["finding_id"] for x in parse_main_response(valid, open_ids=("F001", "F002"))],
            ["F001", "F002"],
        )
        for text in (
            '{"finding_responses":[{"finding_id":"F001","disposition":"addressed","response":"done"}]}',
            '{"finding_responses":[{"finding_id":"F001","disposition":"addressed","response":""},{"finding_id":"F002","disposition":"addressed","response":"x"}]}',
            '{"finding_responses":[],"changes":[]}',
        ):
            with self.assertRaises(CodexReviewProtocolError):
                parse_main_response(text, open_ids=("F001", "F002"))

    def test_protocol_schema_factories_constrain_ids_and_objects(self):
        reviewer = reviewer_decision_schema(open_ids=("F001",), initial=False)
        main = main_finding_response_schema(open_ids=("F001",))

        self.assertFalse(reviewer["additionalProperties"])
        self.assertEqual(reviewer["required"], ["decisions", "new_findings"])
        self.assertEqual(
            reviewer["properties"]["decisions"]["items"]["properties"]["resolution"]["enum"],
            ["resolved", "withdrawn", "unresolved", "conflicts"],
        )
        self.assertFalse(main["additionalProperties"])
        self.assertEqual(main["required"], ["finding_responses"])
        item = main["properties"]["finding_responses"]["items"]
        self.assertEqual(item["properties"]["finding_id"]["enum"], ["F001"])
        self.assertEqual(item["properties"]["disposition"]["enum"], ["addressed", "not_addressed", "disputed"])

    def test_initial_reviewer_schema_does_not_emit_empty_enum(self):
        reviewer = reviewer_decision_schema(open_ids=(), initial=True)

        decisions = reviewer["properties"]["decisions"]
        self.assertEqual(decisions["minItems"], 0)
        self.assertEqual(decisions["maxItems"], 0)
        self.assertNotIn(
            "enum",
            decisions["items"]["properties"]["finding_id"],
        )

    def _job(self, root, *, spawn_attempt, reviewer, artifact_paths=(), max_repairs=1):
        initial = root / "initial.txt"
        if not initial.exists():
            initial.write_text("draft", encoding="utf-8")
        @contextmanager
        def open_answer_fork(message):
            class Fork:
                _turn_session = SimpleNamespace(_trusted_internal_output_schema_paths=set())
                def _send_turn(self, prompt, *, output_schema_path, activity_callback):
                    turn = spawn_attempt(prompt=prompt, attempt_index=1, repair_index=1,
                                         turn_output_schema_path=output_schema_path)
                    stdout, stderr = turn.process.communicate(prompt)
                    return CodexResponse(content=turn.output_path.read_text(),
                                         returncode=turn.process.returncode, stdout=stdout,
                                         stderr=stderr, argv=tuple(turn.argv))
            yield Fork()
        execution = object.__new__(CodexJob)
        execution._initialize_runtime(
            attempt=_CodexAttempt(["codex"], "input", _Process(), initial, 0, 0),
            spawn_attempt=spawn_attempt, open_answer_fork=open_answer_fork, original_input="input", context_paths=(), context_sections=(),
            output_schema_section=None, output_model_source_section=None, read_paths=(), write_paths=(), subcwd_files=(),
            cwd=root, review_config=SimpleNamespace(artifact_paths=artifact_paths, max_repairs=max_repairs), review_runner=reviewer,
            log_dir=root,
        )
        return execution

    @staticmethod
    def _decision(protocol, *, resolution="resolved", comment="ok"):
        if protocol["initial"]:
            decision = {"decisions": [], "new_findings": [{"comment": "fix it", "relation": {"kind": "new", "finding_id": None}}]}
        else:
            decision = {"decisions": [{"finding_id": "F001", "resolution": resolution, "comment": comment, "conflicts_with": None}], "new_findings": []}
        response = CodexResponse(content="{}", returncode=0, stdout="", stderr="", argv=("review",))
        return _ReviewResult(False, (), "", response, "", "", ["review"], decision=decision)

    def test_finding_response_correction_and_storage_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            calls = []
            def spawn_attempt(*, prompt, attempt_index, repair_index, turn_output_schema_path=None):
                path = root / f"out-{len(calls)}.txt"; calls.append(prompt)
                if "Return only a JSON object with finding_responses." in prompt:
                    first = sum("finding_responses." in x for x in calls) == 1
                    path.write_text("not json" if first else '{"finding_responses":[{"finding_id":"F001","disposition":"addressed","response":"fixed"}]}', encoding="utf-8")
                else: path.write_text("repaired", encoding="utf-8")
                return _CodexAttempt(["codex"], prompt, _Process(), path, attempt_index, repair_index)
            packages = []
            def reviewer(**kwargs):
                packages.append(kwargs["protocol"]["package"])
                return self._decision(kwargs["protocol"])
            execution = self._job(root, spawn_attempt=spawn_attempt, reviewer=reviewer)
            execution.start_background(); response = execution.result(timeout=2)
            self.assertEqual(response.content, "repaired")
            self.assertEqual(packages[0], {"schema_version": 3, "round": 0, "open_findings": [], "closed_findings": [], "changes": [], "finding_responses": []})
            self.assertEqual(packages[1]["changes"][0]["diff"], "@@ -1,1 +1,1 @@\n-draft\n+repaired\n")
            self.assertEqual(packages[1]["finding_responses"], [{"finding_id": "F001", "disposition": "addressed", "response": "fixed"}])
            review_root = pathlib.Path(execution.log_path).parent / execution.id / "review"
            self.assertEqual(
                sorted(p.relative_to(review_root).as_posix() for p in review_root.rglob("*") if p.is_file()),
                [
                    "finding-ledger.json",
                    "round-000/after-manifest.json", "round-000/initial-response.txt",
                    "round-000/review-package.json", "round-000/reviewer-decision.json",
                    "round-001/after-manifest.json", "round-001/before-manifest.json",
                    "round-001/main-finding-response-attempt-00.diagnostic.txt",
                    "round-001/main-finding-response-attempt-00.txt",
                    "round-001/review-package.json", "round-001/reviewer-decision.json",
                ],
            )
            ledger = json.loads((review_root / "finding-ledger.json").read_text(encoding="utf-8"))
            self.assertEqual(ledger["schema_version"], 3)
            self.assertEqual(ledger["payload"][0]["status"], "resolved")

    def test_finding_response_fails_after_two_corrections(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            calls = []
            def spawn_attempt(*, prompt, attempt_index, repair_index, turn_output_schema_path=None):
                path = root / f"out-{len(calls)}.txt"; calls.append(prompt)
                path.write_text("not json" if "finding_responses." in prompt else "repaired", encoding="utf-8")
                return _CodexAttempt(["codex"], prompt, _Process(), path, attempt_index, repair_index)
            execution = self._job(root, spawn_attempt=spawn_attempt, reviewer=lambda **kwargs: self._decision(kwargs["protocol"]))
            execution.start_background()
            with self.assertRaises(CodexReviewProtocolError):
                execution.result(timeout=2)
            self.assertEqual(sum("finding_responses." in x for x in calls), 3)

    def test_two_repairs_send_one_package_each_round(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            artifact = root / "canonical.json"
            artifact.write_text('{"revision": 0}\n', encoding="utf-8")
            calls = []

            def spawn_attempt(*, prompt, attempt_index, repair_index, turn_output_schema_path=None):
                path = root / f"out-{len(calls)}.txt"
                calls.append(prompt)
                if "finding_responses." in prompt:
                    path.write_text('{"finding_responses":[{"finding_id":"F001","disposition":"addressed","response":"fixed"}]}', encoding="utf-8")
                else:
                    artifact.write_text('{"revision": ' + str(repair_index) + '}\n', encoding="utf-8")
                    path.write_text(f"repair-{repair_index}", encoding="utf-8")
                return _CodexAttempt(["codex"], prompt, _Process(), path, attempt_index, repair_index)

            protocols = []

            def reviewer(**kwargs):
                protocol = kwargs["protocol"]
                protocols.append(protocol)
                if protocol["package"]["round"] == 1:
                    return self._decision(protocol, resolution="unresolved", comment="revision is still 1")
                return self._decision(protocol)

            execution = self._job(root, spawn_attempt=spawn_attempt, reviewer=reviewer,
                                  artifact_paths=("canonical.json",), max_repairs=2)
            execution.start_background()
            response = execution.result(timeout=2)

            self.assertEqual(response.content, "repair-2")
            repair_prompts = [x for x in calls if "finding_responses." not in x]
            self.assertIn("F001: fix it", repair_prompts[0])
            self.assertNotIn("Reviewer (unresolved)", repair_prompts[0])
            self.assertIn("F001: fix it\n  Reviewer (unresolved): revision is still 1", repair_prompts[1])
            self.assertEqual([item["package"]["round"] for item in protocols], [0, 1, 2])
            for protocol in protocols[1:]:
                self.assertEqual(protocol["artifact_files"], ["canonical.json"])
                self.assertEqual(
                    [x["artifact"] for x in protocol["package"]["changes"]],
                    [{"kind": "response_content", "path": None}, {"kind": "file", "path": "canonical.json"}],
                )
                self.assertEqual(protocol["package"]["open_findings"], [{"finding_id": "F001", "comment": "fix it"}])

    def test_finding_unresolved_twice_in_a_row_stops_before_max_repairs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            calls = []
            def spawn_attempt(*, prompt, attempt_index, repair_index, turn_output_schema_path=None):
                path = root / f"out-{len(calls)}.txt"; calls.append(prompt)
                if "finding_responses." in prompt:
                    path.write_text('{"finding_responses":[{"finding_id":"F001","disposition":"addressed","response":"fixed"}]}', encoding="utf-8")
                else:
                    path.write_text(f"repair-{repair_index}", encoding="utf-8")
                return _CodexAttempt(["codex"], prompt, _Process(), path, attempt_index, repair_index)
            execution = self._job(root, spawn_attempt=spawn_attempt, max_repairs=None,
                                  reviewer=lambda **kwargs: self._decision(kwargs["protocol"], resolution="unresolved"))
            execution.start_background()
            with self.assertRaises(Exception) as caught:
                execution.result(timeout=2)
            self.assertEqual(caught.exception.failure_stage, "review")
            self.assertEqual(caught.exception.repairs, 2)
            self.assertIn("did not converge: F001", str(caught.exception) + caught.exception.validation_diagnostic)

    def test_capture_artifact_keeps_invalid_json_as_reviewable_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            artifact = root / "canonical.json"
            artifact.write_text("{invalid\n", encoding="utf-8")
            captured = capture_artifacts(
                cwd=root,
                paths=("canonical.json",),
                content="response",
                initial=True,
            )
        self.assertEqual(captured.files, {"canonical.json": "{invalid\n"})


if __name__ == "__main__":
    unittest.main()
