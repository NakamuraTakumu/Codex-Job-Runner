import os
import pathlib
import stat
import sys
import tempfile
import textwrap
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from codex_api_wrapper import (  # noqa: E402
    CodexCancelledError,
    CodexClient,
    CodexExecutionError,
    CodexTimeoutError,
    CodexValidationError,
    ReviewConfig,
)


class CodexClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = pathlib.Path(self.tmp.name)
        self.codex_bin = self._make_fake_codex()
        self.project = self.tmp_path / "project"
        self.project.mkdir()
        (self.project / "README.md").write_text("hello\n", encoding="utf-8")
        (self.project / "out").mkdir()
        (self.project / "result.schema.json").write_text(
            '{"type":"object"}\n',
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_run_returns_response_content(self):
        client = CodexClient(codex_bin=str(self.codex_bin), cwd=str(self.project))

        response = client.run(
            input="README を要約して",
            attachments=["README.md"],
            read_paths=["README.md"],
            write_paths=["out"],
            timeout=3,
        )

        self.assertEqual(response.content, "fake final answer")
        self.assertEqual(response.returncode, 0)
        self.assertIn("FAKE_STDOUT", response.stdout)
        self.assertEqual(
            response.usage,
            {
                "input_tokens": 100,
                "cached_input_tokens": 25,
                "output_tokens": 10,
                "reasoning_output_tokens": 3,
            },
        )

    def test_json_mode_is_enabled_and_usage_is_aggregated_across_repairs(self):
        validator = self._make_validator(
            "validate_json_only.py",
            """
            import json
            import sys

            json.loads(sys.stdin.read())
            sys.exit(0)
            """,
        )
        client = CodexClient(codex_bin=str(self.codex_bin), cwd=str(self.project))

        response = client.run(
            input="NEEDS_REPAIR",
            output_validator=["python3", str(validator)],
            repair_validation_failures=True,
            max_repairs=1,
            timeout=3,
        )

        self.assertEqual(response.content, '{"ok": true}')
        self.assertIn('"type": "turn.completed"', response.stdout)
        self.assertEqual(
            response.usage,
            {
                "input_tokens": 250,
                "cached_input_tokens": 60,
                "output_tokens": 25,
                "reasoning_output_tokens": 8,
            },
        )

    def test_argv_contains_context_controls_and_permissions(self):
        client = CodexClient(
            codex_bin=str(self.codex_bin),
            cwd=str(self.project),
            model="gpt-test",
            ignore_global_context=True,
            allow_shell_commands=False,
        )

        job = client.start(
            input="調べて",
            read_paths=["README.md"],
            write_paths=["out"],
        )
        response = job.result(timeout=3)

        self.assertEqual(response.content, "fake final answer")
        argv = job.argv
        self.assertLess(argv.index("--ask-for-approval"), argv.index("exec"))
        self.assertEqual(argv[argv.index("--ask-for-approval") + 1], "never")
        self.assertIn("--json", argv)
        self.assertIn("--ignore-user-config", argv)
        self.assertIn("--model", argv)
        self.assertIn("gpt-test", argv)
        self.assertIn("project_doc_max_bytes=0", argv)
        self.assertIn("skills.include_instructions=false", argv)
        self.assertIn("features.shell_tool=false", argv)
        self.assertIn("allow_login_shell=false", argv)
        filesystem_args = [item for item in argv if item.startswith("permissions.")]
        self.assertEqual(len(filesystem_args), 1)
        self.assertIn('":minimal"="read"', filesystem_args[0])
        self.assertIn('":workspace_roots"', filesystem_args[0])
        self.assertIn('"README.md"="read"', filesystem_args[0])
        self.assertIn('"read"', filesystem_args[0])
        self.assertIn('"out"="write"', filesystem_args[0])
        self.assertIn('"write"', filesystem_args[0])

    def test_allow_shell_commands_default_allows_shell(self):
        client = CodexClient(
            codex_bin=str(self.codex_bin),
            cwd=str(self.project),
        )

        job = client.start(input="x")
        job.result(timeout=3)

        self.assertNotIn("features.shell_tool=false", job.argv)
        self.assertNotIn("allow_login_shell=false", job.argv)
        filesystem_args = [item for item in job.argv if item.startswith("permissions.")]
        self.assertIn(f'"{self.tmp_path}"="read"', filesystem_args[0])

    def test_default_model_and_reasoning_effort_are_passed_to_cli(self):
        client = CodexClient(
            codex_bin=str(self.codex_bin),
            cwd=str(self.project),
        )

        job = client.start(input="x")
        job.result(timeout=3)

        self.assertIn("--model", job.argv)
        self.assertEqual(
            job.argv[job.argv.index("--model") + 1],
            "gpt-5.4-mini",
        )
        self.assertIn('model_reasoning_effort="medium"', job.argv)

    def test_explicit_none_client_defaults_delegate_to_cli_defaults(self):
        client = CodexClient(
            codex_bin=str(self.codex_bin),
            cwd=str(self.project),
            model=None,
            reasoning_effort=None,
        )

        job = client.start(input="x")
        job.result(timeout=3)

        self.assertNotIn("--model", job.argv)
        self.assertNotIn('model_reasoning_effort="medium"', job.argv)

    def test_reasoning_effort_adds_cli_config(self):
        client = CodexClient(
            codex_bin=str(self.codex_bin),
            cwd=str(self.project),
            reasoning_effort="low",
        )

        job = client.start(input="x")
        job.result(timeout=3)

        self.assertIn('model_reasoning_effort="low"', job.argv)

    def test_reasoning_effort_per_call_overrides_client_default(self):
        client = CodexClient(
            codex_bin=str(self.codex_bin),
            cwd=str(self.project),
            reasoning_effort="high",
        )

        job = client.start(input="x", reasoning_effort="low")
        job.result(timeout=3)

        self.assertIn('model_reasoning_effort="low"', job.argv)
        self.assertNotIn('model_reasoning_effort="high"', job.argv)

    def test_empty_reasoning_effort_errors_before_start(self):
        client = CodexClient(codex_bin=str(self.codex_bin), cwd=str(self.project))

        with self.assertRaises(ValueError):
            client.start(input="x", reasoning_effort="")

    def test_paths_outside_cwd_are_rejected_for_permissions(self):
        outside = self.tmp_path / "outside.txt"
        outside.write_text("secret\n", encoding="utf-8")
        client = CodexClient(codex_bin=str(self.codex_bin), cwd=str(self.project))

        with self.assertRaises(ValueError):
            client.start(input="x", read_paths=[str(outside)])

    def test_file_attachments_are_inlined_by_default(self):
        client = CodexClient(codex_bin=str(self.codex_bin), cwd=str(self.project))

        response = client.run(input="ECHO_PROMPT", attachments=["README.md"])

        self.assertIn("Attachments:", response.content)
        self.assertIn("Attached file contents:", response.content)
        self.assertIn("<AttachedFile path=", response.content)
        self.assertIn("hello\n", response.content)

    def test_inline_attachments_none_uses_default_inline(self):
        client = CodexClient(codex_bin=str(self.codex_bin), cwd=str(self.project))

        response = client.run(
            input="ECHO_PROMPT",
            attachments=["README.md"],
            inline_attachments=None,
        )

        self.assertIn("Attached file contents:", response.content)
        self.assertIn("hello\n", response.content)

    def test_inline_attachments_false_lists_paths_without_contents(self):
        client = CodexClient(
            codex_bin=str(self.codex_bin),
            cwd=str(self.project),
        )

        response = client.run(
            input="ECHO_PROMPT",
            attachments=["README.md"],
            inline_attachments=False,
        )

        self.assertIn("Attachments:", response.content)
        self.assertNotIn("Attached file contents:", response.content)
        self.assertNotIn("<AttachedFile path=", response.content)

    def test_inline_attachment_size_limit_errors(self):
        client = CodexClient(codex_bin=str(self.codex_bin), cwd=str(self.project))

        with self.assertRaises(ValueError):
            client.start(
                input="x",
                attachments=["README.md"],
                max_inline_attachment_bytes=1,
            )

    def test_allowed_skills_empty_disables_skill_instructions(self):
        client = CodexClient(codex_bin=str(self.codex_bin), cwd=str(self.project))

        job = client.start(input="x", allowed_skills=[])
        job.result(timeout=3)

        self.assertIn("skills.include_instructions=false", job.argv)

    def test_output_schema_path_adds_cli_option(self):
        client = CodexClient(codex_bin=str(self.codex_bin), cwd=str(self.project))

        job = client.start(
            input="ECHO_PROMPT",
            output_schema_path="result.schema.json",
        )
        response = job.result(timeout=3)

        argv = job.argv
        schema_index = argv.index("--output-schema")
        self.assertEqual(
            argv[schema_index + 1],
            str(self.project / "result.schema.json"),
        )
        self.assertIn("Output schema:", response.content)
        self.assertIn("<OutputSchema path=", response.content)
        self.assertIn('{"type":"object"}', response.content)

    def test_output_schema_inline_size_limit_errors(self):
        client = CodexClient(codex_bin=str(self.codex_bin), cwd=str(self.project))

        with self.assertRaises(ValueError):
            client.start(
                input="x",
                output_schema_path="result.schema.json",
                max_inline_attachment_bytes=1,
            )

    def test_missing_output_schema_path_errors_before_start(self):
        client = CodexClient(codex_bin=str(self.codex_bin), cwd=str(self.project))

        with self.assertRaises(FileNotFoundError):
            client.start(input="x", output_schema_path="missing.schema.json")

    def test_output_validator_success(self):
        validator = self._make_validator(
            "validate_success.py",
            """
            import os
            import sys

            content = sys.stdin.read()
            if content != "fake final answer":
                print("unexpected content", file=sys.stderr)
                sys.exit(2)
            if os.environ["CODEX_WRAPPER_ATTEMPT"] != "0":
                print("unexpected attempt", file=sys.stderr)
                sys.exit(3)
            sys.exit(0)
            """,
        )
        client = CodexClient(codex_bin=str(self.codex_bin), cwd=str(self.project))

        response = client.run(input="x", output_validator=["python3", str(validator)])

        self.assertEqual(response.content, "fake final answer")
        self.assertEqual(response.argv[0], str(self.codex_bin))
        self.assertIn("validator", [entry.phase for entry in response.activity_log])

    def test_output_validator_failure_raises_validation_error(self):
        validator = self._make_validator(
            "validate_failure.py",
            """
            import sys

            sys.stdin.read()
            print("not valid", file=sys.stderr)
            sys.exit(9)
            """,
        )
        client = CodexClient(codex_bin=str(self.codex_bin), cwd=str(self.project))

        with self.assertRaises(CodexValidationError) as caught:
            client.run(input="x", output_validator=["python3", str(validator)])

        self.assertEqual(caught.exception.validator_returncode, 9)
        self.assertIn("not valid", caught.exception.validator_stderr)
        self.assertEqual(caught.exception.attempts, 1)
        self.assertEqual(caught.exception.repairs, 0)
        self.assertIn("validator", [entry.phase for entry in caught.exception.activity_log])
        validator_end = [
            entry for entry in caught.exception.activity_log
            if entry.phase == "validator" and entry.event == "end"
        ]
        self.assertEqual(len(validator_end), 1)
        self.assertEqual(validator_end[0].argv[0], "python3")
        self.assertIn("not valid", validator_end[0].stderr)

    def test_review_passes_after_validator_success(self):
        client = CodexClient(codex_bin=str(self.codex_bin), cwd=str(self.project))

        response = client.run(
            input="x",
            review=ReviewConfig(instructions="REVIEW_SHOULD_PASS"),
            timeout=3,
        )

        self.assertEqual(response.content, "fake final answer")

    def test_review_failure_triggers_repair_and_succeeds(self):
        client = CodexClient(codex_bin=str(self.codex_bin), cwd=str(self.project))

        response = client.run(
            input="REVIEW_TRIGGER_REPAIR",
            review=ReviewConfig(instructions="REVIEW_SHOULD_FAIL_ON_BAD_RESPONSE"),
            repair_validation_failures=True,
            max_repairs=1,
            timeout=3,
        )

        self.assertEqual(response.content, "review repaired answer")
        self.assertIn("repair", [entry.phase for entry in response.activity_log])

    def test_validator_failure_skips_review(self):
        validator = self._make_validator(
            "validate_failure_skip_review.py",
            """
            import sys
            sys.stdin.read()
            print("validator failed", file=sys.stderr)
            sys.exit(5)
            """,
        )
        client = CodexClient(codex_bin=str(self.codex_bin), cwd=str(self.project))

        with self.assertRaises(CodexValidationError) as caught:
            client.run(
                input="x",
                output_validator=["python3", str(validator)],
                review=ReviewConfig(instructions="REVIEW_FORCE_BAD_OUTPUT"),
                timeout=3,
            )

        self.assertEqual(caught.exception.failure_stage, "validator")
        self.assertEqual(caught.exception.validator_returncode, 5)

    def test_review_can_see_original_input_prompt_and_subcwd(self):
        client = CodexClient(codex_bin=str(self.codex_bin), cwd=str(self.project))

        response = client.run(
            input="REVIEW_CONTEXT_MARKER",
            subcwd_files={"state.json": {"text": '{"ok": true}\n'}},
            review=ReviewConfig(instructions="CHECK_REVIEW_CONTEXT_AND_SUBCWD"),
            collect_files=True,
            timeout=3,
        )

        self.assertEqual(response.content, "fake final answer")

    def test_review_timeout_is_execution_error(self):
        client = CodexClient(codex_bin=str(self.codex_bin), cwd=str(self.project))

        with self.assertRaises(CodexExecutionError) as caught:
            client.run(
                input="x",
                review=ReviewConfig(
                    instructions="FORCE_REVIEW_TIMEOUT",
                    timeout=0.1,
                ),
                timeout=3,
            )

        self.assertIn("review job timed out", caught.exception.message)

    def test_review_bad_output_is_execution_error(self):
        client = CodexClient(codex_bin=str(self.codex_bin), cwd=str(self.project))

        with self.assertRaises(CodexExecutionError) as caught:
            client.run(
                input="x",
                review=ReviewConfig(instructions="REVIEW_FORCE_BAD_OUTPUT"),
                timeout=3,
            )

        self.assertIn("review output", caught.exception.message)
        self.assertNotIn("communication failed", caught.exception.message)
        self.assertIn("FAKE_STDOUT", caught.exception.stdout)
        self.assertIn("FAKE_STDERR", caught.exception.stderr)
        self.assertEqual(caught.exception.argv[0], str(self.codex_bin))
        self.assertIn("review", [entry.phase for entry in caught.exception.activity_log])
        review_errors = [
            entry for entry in caught.exception.activity_log
            if entry.phase == "review" and entry.event == "error"
        ]
        self.assertEqual(len(review_errors), 1)
        self.assertIn("FAKE_STDERR", review_errors[0].stderr)

    def test_subcwd_files_stage_text_and_collect_edited_result(self):
        client = CodexClient(codex_bin=str(self.codex_bin), cwd=str(self.project))

        response = client.run(
            input="EDIT_STAGED_TEXT_FILE",
            subcwd_files={
                "inputs/neighborhood_tree.json": {
                    "text": '{"status": "draft"}\n',
                }
            },
            collect_files=True,
            timeout=3,
        )

        self.assertEqual(
            response.files["inputs/neighborhood_tree.json"].text,
            '{"status": "edited"}\n',
        )
        self.assertFalse(self._subcwd_dirs())

    def test_subcwd_files_stage_bytes(self):
        client = CodexClient(codex_bin=str(self.codex_bin), cwd=str(self.project))

        response = client.run(
            input="SUM_STAGED_BINARY_FILE",
            subcwd_files={
                "inputs/blob.bin": {
                    "bytes": b"\x00\x01\x02\x03",
                }
            },
            collect_files=True,
            timeout=3,
        )

        self.assertEqual(response.content, "binary-bytes=4")
        self.assertEqual(
            response.files["outputs/report.txt"].text,
            "binary-bytes=4\n",
        )

    def test_validation_repair_reuses_subcwd_and_collects_final_files(self):
        validator = self._make_validator(
            "validate_json_and_subcwd.py",
            """
            import json
            import os
            import pathlib
            import sys

            data = json.loads(sys.stdin.read())
            subcwd = pathlib.Path(os.environ["CODEX_WRAPPER_SUBCWD"])
            file_data = json.loads((subcwd / "data.json").read_text())
            if data != {"ok": True} or file_data != {"ok": True}:
                print("needs repair", file=sys.stderr)
                sys.exit(4)
            sys.exit(0)
            """,
        )
        client = CodexClient(codex_bin=str(self.codex_bin), cwd=str(self.project))

        response = client.run(
            input="NEEDS_REPAIR MAKE_REPAIR_SUBCWD_FILE",
            write_paths=["${SUBCWD}"],
            collect_files=True,
            output_validator=["python3", str(validator)],
            repair_validation_failures=True,
            max_repairs=1,
            timeout=3,
        )

        self.assertEqual(response.content, '{"ok": true}')
        self.assertEqual(response.files["data.json"].json(), {"ok": True})
        self.assertFalse(self._subcwd_dirs())

    def test_validation_repair_reuses_staged_subcwd_files_without_restaging(self):
        validator = self._make_validator(
            "validate_repair_staged_state.py",
            """
            import json
            import os
            import pathlib
            import sys

            data = json.loads(sys.stdin.read())
            subcwd = pathlib.Path(os.environ["CODEX_WRAPPER_SUBCWD"])
            state = (subcwd / "state.json").read_text()
            if data != {"ok": True} or state != '{"state": "mutated"}\\n':
                print("needs repair", file=sys.stderr)
                sys.exit(6)
            sys.exit(0)
            """,
        )
        client = CodexClient(codex_bin=str(self.codex_bin), cwd=str(self.project))

        response = client.run(
            input="NEEDS_REPAIR_CHECK_STAGED_PERSISTENCE",
            subcwd_files={
                "state.json": {
                    "text": '{"state": "initial"}\n',
                }
            },
            collect_files=True,
            output_validator=["python3", str(validator)],
            repair_validation_failures=True,
            max_repairs=1,
            timeout=3,
        )

        self.assertEqual(response.content, '{"ok": true}')
        self.assertEqual(
            response.files["state.json"].text,
            '{"state": "mutated"}\n',
        )

    def test_missing_output_validator_path_errors_before_start(self):
        client = CodexClient(codex_bin=str(self.codex_bin), cwd=str(self.project))

        with self.assertRaises(FileNotFoundError):
            client.start(input="x", output_validator="./missing-validator")

    def test_collect_files_reads_subcwd_into_response_and_cleans_up(self):
        client = CodexClient(codex_bin=str(self.codex_bin), cwd=str(self.project))

        job = client.start(
            input="MAKE_SUBCWD_FILES",
            write_paths=["${SUBCWD}"],
            collect_files=True,
        )
        response = job.result(timeout=3)

        self.assertEqual(response.files["report.md"].text, "hello from subcwd\n")
        self.assertEqual(response.files["data.json"].json(), {"ok": True})
        self.assertEqual(
            response.to_dict()["files"]["report.md"]["size"],
            len("hello from subcwd\n"),
        )
        self.assertFalse(self._subcwd_dirs())
        filesystem_args = [item for item in job.argv if item.startswith("permissions.")]
        self.assertNotIn("${SUBCWD}", filesystem_args[0])
        self.assertIn('"write"', filesystem_args[0])

    def test_subcwd_root_relative_path_creates_subcwd_under_custom_parent(self):
        client = CodexClient(
            codex_bin=str(self.codex_bin),
            cwd=str(self.project),
            subcwd_root="tmp/jobs",
        )

        response = client.run(
            input="MAKE_SUBCWD_FILES",
            write_paths=["${SUBCWD}"],
            collect_files=True,
            timeout=3,
        )

        self.assertEqual(response.files["report.md"].text, "hello from subcwd\n")
        custom_root = self.project / "tmp" / "jobs"
        self.assertTrue(custom_root.is_dir())
        self.assertEqual(list(custom_root.iterdir()), [])

    def test_subcwd_root_is_created_when_missing(self):
        client = CodexClient(
            codex_bin=str(self.codex_bin),
            cwd=str(self.project),
            subcwd_root="tmp/missing/jobs",
        )

        client.run(
            input="MAKE_SUBCWD_FILES",
            write_paths=["${SUBCWD}"],
            timeout=3,
        )

        self.assertTrue((self.project / "tmp" / "missing" / "jobs").is_dir())

    def test_subcwd_root_accepts_absolute_path_inside_cwd(self):
        custom_root = self.project / "tmp" / "absolute"
        client = CodexClient(
            codex_bin=str(self.codex_bin),
            cwd=str(self.project),
            subcwd_root=str(custom_root),
        )

        client.run(
            input="MAKE_SUBCWD_FILES",
            write_paths=["${SUBCWD}"],
            timeout=3,
        )

        self.assertTrue(custom_root.is_dir())
        self.assertEqual(list(custom_root.iterdir()), [])

    def test_subcwd_root_outside_cwd_is_rejected(self):
        client = CodexClient(
            codex_bin=str(self.codex_bin),
            cwd=str(self.project),
            subcwd_root=str(self.tmp_path / "outside"),
        )

        with self.assertRaises(ValueError):
            client.start(input="x", write_paths=["${SUBCWD}"])

    def test_subcwd_root_rejects_subcwd_token(self):
        client = CodexClient(
            codex_bin=str(self.codex_bin),
            cwd=str(self.project),
            subcwd_root="tmp/${SUBCWD}",
        )

        with self.assertRaises(ValueError):
            client.start(input="x", write_paths=["${SUBCWD}"])

    def test_subcwd_without_collect_files_cleans_up(self):
        client = CodexClient(codex_bin=str(self.codex_bin), cwd=str(self.project))

        response = client.run(
            input="MAKE_SUBCWD_FILES",
            write_paths=["${SUBCWD}"],
            timeout=3,
        )

        self.assertEqual(response.files, {})
        self.assertFalse(self._subcwd_dirs())

    def test_subcwd_files_add_subcwd_write_permission_without_explicit_write_paths(self):
        client = CodexClient(codex_bin=str(self.codex_bin), cwd=str(self.project))

        job = client.start(
            input="EDIT_STAGED_TEXT_FILE",
            subcwd_files={
                "inputs/neighborhood_tree.json": {
                    "text": '{"status": "draft"}\n',
                }
            },
            timeout=3,
        )
        job.result(timeout=3)

        filesystem_args = [item for item in job.argv if item.startswith("permissions.")]
        self.assertIn('"write"', filesystem_args[0])

    def test_collect_files_limit_error_cleans_up(self):
        client = CodexClient(codex_bin=str(self.codex_bin), cwd=str(self.project))

        job = client.start(
            input="MAKE_SUBCWD_FILES",
            write_paths=["${SUBCWD}"],
            collect_files=True,
            max_file_bytes=1,
        )

        with self.assertRaises(CodexExecutionError) as caught:
            job.result(timeout=3)
        self.assertIn("file collection failed", caught.exception.message)
        self.assertFalse(self._subcwd_dirs())

    def test_subcwd_is_rejected_for_read_paths(self):
        client = CodexClient(codex_bin=str(self.codex_bin), cwd=str(self.project))

        with self.assertRaises(ValueError):
            client.start(input="x", read_paths=["${SUBCWD}"])

    def test_subcwd_files_rejects_absolute_path(self):
        client = CodexClient(codex_bin=str(self.codex_bin), cwd=str(self.project))

        with self.assertRaises(ValueError):
            client.start(
                input="x",
                subcwd_files={"/abs.txt": {"text": "x"}},
            )

    def test_subcwd_files_rejects_escape_path(self):
        client = CodexClient(codex_bin=str(self.codex_bin), cwd=str(self.project))

        with self.assertRaises(ValueError):
            client.start(
                input="x",
                subcwd_files={"../escape.txt": {"text": "x"}},
            )

    def test_subcwd_files_rejects_empty_path(self):
        client = CodexClient(codex_bin=str(self.codex_bin), cwd=str(self.project))

        with self.assertRaises(ValueError):
            client.start(
                input="x",
                subcwd_files={"": {"text": "x"}},
            )

    def test_subcwd_files_rejects_invalid_value_shape(self):
        client = CodexClient(codex_bin=str(self.codex_bin), cwd=str(self.project))

        with self.assertRaises(ValueError):
            client.start(
                input="x",
                subcwd_files={"a.txt": {"text": "x", "bytes": b"y"}},
            )

    def test_subcwd_files_rejects_missing_text_or_bytes(self):
        client = CodexClient(codex_bin=str(self.codex_bin), cwd=str(self.project))

        with self.assertRaises(ValueError):
            client.start(
                input="x",
                subcwd_files={"a.txt": {"encoding": "utf-8"}},
            )

    def test_subcwd_files_rejects_invalid_encoding_usage(self):
        client = CodexClient(codex_bin=str(self.codex_bin), cwd=str(self.project))

        with self.assertRaises(ValueError):
            client.start(
                input="x",
                subcwd_files={"a.bin": {"bytes": b"x", "encoding": "utf-8"}},
            )

    def test_subcwd_escape_is_rejected_and_cleaned_up(self):
        client = CodexClient(codex_bin=str(self.codex_bin), cwd=str(self.project))

        with self.assertRaises(ValueError):
            client.start(input="x", write_paths=["${SUBCWD}/../escape"])
        self.assertFalse(self._subcwd_dirs())

    def test_nonzero_exit_raises_execution_error(self):
        client = CodexClient(codex_bin=str(self.codex_bin), cwd=str(self.project))

        job = client.start(input="FORCE_FAIL")

        with self.assertRaises(CodexExecutionError) as caught:
            job.result(timeout=3)
        self.assertEqual(caught.exception.returncode, 7)
        self.assertIn("FAKE_STDERR", caught.exception.stderr)

    def test_result_timeout_raises_timeout_error(self):
        client = CodexClient(codex_bin=str(self.codex_bin), cwd=str(self.project))

        job = client.start(input="sleep")

        with self.assertRaises(CodexTimeoutError):
            job.result(timeout=0.1)
        self.assertEqual(job.status, "timeout")

    def test_job_uses_client_default_timeout(self):
        client = CodexClient(
            codex_bin=str(self.codex_bin),
            cwd=str(self.project),
            timeout=0.1,
        )

        job = client.start(input="sleep")

        with self.assertRaises(CodexTimeoutError):
            job.result()

    def test_cancelled_job_raises_cancelled_error(self):
        client = CodexClient(codex_bin=str(self.codex_bin), cwd=str(self.project))

        job = client.start(input="sleep")
        time.sleep(0.1)
        job.cancel()

        with self.assertRaises(CodexCancelledError):
            job.result(timeout=1)

    def test_wait_any_returns_completed_job(self):
        client = CodexClient(codex_bin=str(self.codex_bin), cwd=str(self.project))

        slow = client.start(input="sleep")
        fast = client.start(input="quick")
        done = client.wait_any({slow, fast}, timeout=3)

        self.assertIs(done, fast)
        slow.cancel()

    def test_missing_attachment_errors_before_start(self):
        client = CodexClient(codex_bin=str(self.codex_bin), cwd=str(self.project))

        with self.assertRaises(FileNotFoundError):
            client.start(input="x", attachments=["missing.txt"])

    def _subcwd_dirs(self):
        return [
            path
            for path in self.project.iterdir()
            if path.is_dir() and path.name != "out"
        ]

    def _make_fake_codex(self):
        path = self.tmp_path / "fake_codex.py"
        path.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import json
                import pathlib
                import sys
                import time

                args = sys.argv[1:]
                output = None
                json_mode = "--json" in args
                for index, arg in enumerate(args):
                    if arg == "--output-last-message":
                        output = args[index + 1]
                prompt = sys.stdin.read()
                is_repair = "The previous output failed validation." in prompt
                final_answer = "fake final answer"
                print("FAKE_STDOUT")
                print("FAKE_STDERR", file=sys.stderr)
                usage = {
                    "input_tokens": 150 if is_repair else 100,
                    "cached_input_tokens": 35 if is_repair else 25,
                    "output_tokens": 15 if is_repair else 10,
                    "reasoning_output_tokens": 5 if is_repair else 3,
                }
                if json_mode:
                    print(json.dumps({"type": "thread.started"}))
                if "sleep" in prompt:
                    time.sleep(10)
                if "FORCE_FAIL" in prompt:
                    sys.exit(7)
                if "NEEDS_REPAIR" in prompt:
                    final_answer = '{"ok": true}' if is_repair else 'not json'
                if "REVIEW_TRIGGER_REPAIR" in prompt:
                    if is_repair and "Please change the final response to review repaired answer." in prompt:
                        final_answer = "review repaired answer"
                    else:
                        final_answer = "needs review fix"
                if "ECHO_PROMPT" in prompt:
                    final_answer = prompt
                if "Internal review mode: codex_api_wrapper" in prompt:
                    if "FORCE_REVIEW_TIMEOUT" in prompt:
                        time.sleep(10)
                    if "REVIEW_FORCE_BAD_OUTPUT" in prompt:
                        final_answer = "not json"
                    elif "CHECK_REVIEW_CONTEXT_AND_SUBCWD" in prompt:
                        if "Original request:\\nREVIEW_CONTEXT_MARKER" not in prompt:
                            final_answer = '{"passed": false, "repair_text": "missing original input", "summary": "missing original input"}'
                        elif "Main job prompt:" not in prompt:
                            final_answer = '{"passed": false, "repair_text": "missing main prompt", "summary": "missing main prompt"}'
                        else:
                            subcwd = None
                            for index, line in enumerate(prompt.splitlines()):
                                if line == "Subcwd path:":
                                    subcwd = pathlib.Path(prompt.splitlines()[index + 1])
                                    break
                            if subcwd is None or (subcwd / "state.json").read_text() != '{"ok": true}\\n':
                                final_answer = '{"passed": false, "repair_text": "missing subcwd state", "summary": "missing subcwd state"}'
                            else:
                                final_answer = '{"passed": true, "repair_text": "", "summary": "ok"}'
                    elif "REVIEW_SHOULD_FAIL_ON_BAD_RESPONSE" in prompt:
                        if "Main job final response:\\nneeds review fix" in prompt:
                            final_answer = '{"passed": false, "repair_text": "Please change the final response to review repaired answer.", "summary": "response does not satisfy review"}'
                        else:
                            final_answer = '{"passed": true, "repair_text": "", "summary": "ok"}'
                    else:
                        final_answer = '{"passed": true, "repair_text": "", "summary": "ok"}'
                if "EDIT_STAGED_TEXT_FILE" in prompt:
                    for line in prompt.splitlines():
                        if line.startswith("${SUBCWD} = "):
                            subcwd = pathlib.Path(line.split(" = ", 1)[1])
                            path = subcwd / "inputs" / "neighborhood_tree.json"
                            path.write_text(
                                '{"status": "edited"}\\n',
                                encoding="utf-8",
                            )
                if "SUM_STAGED_BINARY_FILE" in prompt:
                    for line in prompt.splitlines():
                        if line.startswith("${SUBCWD} = "):
                            subcwd = pathlib.Path(line.split(" = ", 1)[1])
                            size = len((subcwd / "inputs" / "blob.bin").read_bytes())
                            final_answer = f"binary-bytes={size}"
                            (subcwd / "outputs" / "report.txt").parent.mkdir(
                                parents=True,
                                exist_ok=True,
                            )
                            (subcwd / "outputs" / "report.txt").write_text(
                                f"binary-bytes={size}\\n",
                                encoding="utf-8",
                            )
                if "MAKE_SUBCWD_FILES" in prompt:
                    for line in prompt.splitlines():
                        if line.startswith("${SUBCWD} = "):
                            subcwd = pathlib.Path(line.split(" = ", 1)[1])
                            (subcwd / "report.md").write_text(
                                "hello from subcwd\\n",
                                encoding="utf-8",
                            )
                            (subcwd / "data.json").write_text(
                                '{"ok": true}\\n',
                                encoding="utf-8",
                            )
                if "MAKE_REPAIR_SUBCWD_FILE" in prompt:
                    for line in prompt.splitlines():
                        if line.startswith("${SUBCWD} = "):
                            subcwd = pathlib.Path(line.split(" = ", 1)[1])
                            content = '{"ok": true}\\n' if is_repair else '{"ok": false}\\n'
                            (subcwd / "data.json").write_text(
                                content,
                                encoding="utf-8",
                            )
                if "NEEDS_REPAIR_CHECK_STAGED_PERSISTENCE" in prompt:
                    for line in prompt.splitlines():
                        if line.startswith("${SUBCWD} = "):
                            subcwd = pathlib.Path(line.split(" = ", 1)[1])
                            if not is_repair:
                                (subcwd / "state.json").write_text(
                                    '{"state": "mutated"}\\n',
                                    encoding="utf-8",
                                )
                                final_answer = "not json"
                            else:
                                final_answer = '{"ok": true}'
                if output:
                    pathlib.Path(output).write_text(final_answer, encoding="utf-8")
                if json_mode:
                    print(json.dumps({"type": "turn.completed", "usage": usage}))
                sys.exit(0)
                """
            ),
            encoding="utf-8",
        )
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def _make_validator(self, name, source):
        path = self.tmp_path / name
        path.write_text(
            "#!/usr/bin/env python3\n" + textwrap.dedent(source).lstrip(),
            encoding="utf-8",
        )
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path


if __name__ == "__main__":
    unittest.main()
