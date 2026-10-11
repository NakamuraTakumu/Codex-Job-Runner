import dataclasses
import pathlib
import unittest

from codex_job_runner._tools import Tool
from codex_job_runner import ReviewConfig
from codex_job_runner.config import (
    FileCollectionConfig,
    LoggingConfig,
    SessionConfig,
    SubagentConfig,
    ToolConfig,
    ValidationConfig,
    WorkspaceConfig,
)


def echo(value: str) -> str:
    return value


def make_tool(*, name=None):
    return Tool(
        echo,
        "Echo a value.",
        args={"value": "Value to echo."},
        returns="The echoed value.",
        name=name,
    )


class ConfigTests(unittest.TestCase):
    def test_session_defaults_and_default_factories(self):
        first = SessionConfig()
        second = SessionConfig()

        self.assertEqual(first.model, "gpt-5.6-luna")
        self.assertEqual(first.reasoning_effort, "medium")
        self.assertIsNone(first.web_search)
        self.assertEqual(first.context, ())
        self.assertEqual(dict(first.env), {})
        self.assertIsNone(first.tools)
        self.assertIsInstance(first.workspace, WorkspaceConfig)
        self.assertIsNot(first.workspace, second.workspace)

    def test_session_normalizes_and_is_deeply_immutable(self):
        context = ["README.md", pathlib.Path("src")]
        environment = {"KEY": "value"}
        config = SessionConfig(
            reasoning_effort="  high ",
            codex_bin=pathlib.Path("bin/codex"),
            timeout=3,
            env=environment,
            context=context,
            allowed_skills=[],
            allow_shell_commands=0,
        )
        context.append("later")
        environment["LATER"] = "not shared"

        self.assertEqual(config.reasoning_effort, "high")
        self.assertEqual(config.codex_bin, "bin/codex")
        self.assertEqual(config.timeout, 3.0)
        self.assertEqual(config.context, ("README.md", pathlib.Path("src")))
        self.assertEqual(dict(config.env), {"KEY": "value"})
        self.assertEqual(config.allowed_skills, ())
        self.assertIs(config.allow_shell_commands, False)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            config.timeout = 4
        with self.assertRaises(TypeError):
            config.env["KEY"] = "changed"

    def test_web_search_modes_are_preserved_and_forwarded(self):
        for mode in (None, "live", "cached", "disabled"):
            with self.subTest(mode=mode):
                config = SessionConfig(web_search=mode)
                self.assertEqual(config.web_search, mode)
                self.assertEqual(config._runner_kwargs()["web_search"], mode)
                self.assertEqual(
                    config.with_overrides(reasoning_effort="high").web_search,
                    mode,
                )
                self.assertIsNone(config.with_overrides(web_search=None).web_search)

    def test_web_search_rejects_invalid_values(self):
        for value in ("", "LIVE", " live ", "auto", True, 1, [], {}, object()):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "web_search"):
                    SessionConfig(web_search=value)
                with self.assertRaisesRegex(ValueError, "web_search"):
                    SessionConfig(web_search="live").with_overrides(web_search=value)

    def test_session_validates_nested_configs(self):
        with self.assertRaisesRegex(TypeError, "workspace"):
            SessionConfig(workspace={})
        with self.assertRaisesRegex(TypeError, "tools"):
            SessionConfig(tools=())

    def test_subagent_config_is_immutable_and_validates_defaults(self):
        allowed = {"gpt-5.6-luna": ["low", "medium"]}
        session = SessionConfig(
            model="gpt-5.6-luna",
            reasoning_effort="medium",
        )
        config = SubagentConfig(
            session=session,
            allowed_models=allowed,
            max_concurrent=2,
        )
        allowed["gpt-5.6-luna"].append("high")

        self.assertEqual(
            config.allowed_models["gpt-5.6-luna"],
            ("low", "medium"),
        )
        with self.assertRaises(TypeError):
            config.allowed_models["other"] = ("low",)
        with self.assertRaisesRegex(ValueError, "present in allowed_models"):
            SubagentConfig(
                session=session,
                allowed_models={"gpt-5.6-terra": ("medium",)},
            )
        with self.assertRaises(TypeError):
            SubagentConfig(
                session=session,
                allowed_models={"gpt-5.6-luna": ("medium",)},
                max_children=2,
            )
        with self.assertRaises(TypeError):
            SubagentConfig(
                session=session,
                allowed_models={"gpt-5.6-luna": ("medium",)},
                max_estimated_cost_usd=1.0,
            )

    def test_reviewer_accepts_independent_subagent_config(self):
        session = SessionConfig(
            model="gpt-5.6-luna",
            reasoning_effort="medium",
            allow_shell_commands=False,
        )
        subagents = SubagentConfig(
            session=session,
            allowed_models={"gpt-5.6-luna": ("medium",)},
        )

        review = ReviewConfig(
            context="review",
            session=session,
            subagents=subagents,
        )

        self.assertIs(review.subagents, subagents)

    def test_workspace_normalizes_all_former_job_fields(self):
        staged = {"note.txt": {"text": "before"}}
        config = WorkspaceConfig(
            cwd="~/workspace",
            subcwd_root="~/workspace/tmp",
            read_paths=["src"],
            write_paths=[pathlib.Path("out")],
            subcwd_files=staged,
            inline_context=0,
            max_inline_context_bytes=0,
            max_inline_context_total_bytes=4,
        )
        staged["note.txt"]["text"] = "after"

        self.assertEqual(config.cwd, pathlib.Path("~/workspace").expanduser())
        self.assertEqual(config.read_paths, ("src",))
        self.assertEqual(config.write_paths, (pathlib.Path("out"),))
        self.assertEqual(config.subcwd_files["note.txt"]["text"], "before")
        self.assertIs(config.inline_context, False)
        with self.assertRaises(TypeError):
            config.subcwd_files["note.txt"]["text"] = "changed"

    def test_tool_config_normalizes_and_validates(self):
        tool = make_tool()
        config = ToolConfig(
            tools=[tool], max_concurrent_calls=2, timeout=3
        )
        self.assertEqual(config.tools, (tool,))
        self.assertEqual(config.max_concurrent_calls, 2)
        self.assertEqual(config.timeout, 3.0)
        with self.assertRaisesRegex(ValueError, "duplicate tool names"):
            ToolConfig(tools=[make_tool(name="same"), make_tool(name="same")])
        with self.assertRaisesRegex(ValueError, "positive integer"):
            ToolConfig(max_concurrent_calls=0)
        with self.assertRaisesRegex(ValueError, "positive number"):
            ToolConfig(timeout=True)

    def test_validation_config_normalizes_and_validates(self):
        config = ValidationConfig(
            repair=1,
            repair_prompt_path="~/repair.md",
        )
        self.assertIs(config.repair, True)
        self.assertEqual(
            config.repair_prompt_path, pathlib.Path("~/repair.md").expanduser()
        )
        with self.assertRaises(TypeError):
            ValidationConfig(max_repairs=0)

    def test_file_collection_and_logging_configs(self):
        files = FileCollectionConfig(
            enabled=1,
            max_files=0,
            max_file_bytes=1,
            max_total_file_bytes=2,
        )
        logging = LoggingConfig(directory="~/logs", path="run.jsonl")

        self.assertIs(files.enabled, True)
        self.assertEqual(files.max_files, 0)
        self.assertEqual(logging.directory, pathlib.Path("~/logs").expanduser())
        self.assertEqual(logging.log_dir, logging.directory)
        self.assertEqual(logging.path, pathlib.Path("run.jsonl"))

    def test_with_overrides_renormalizes_every_config(self):
        workspace = WorkspaceConfig().with_overrides(read_paths=["src"])
        tools = ToolConfig().with_overrides(timeout=4)
        session = SessionConfig().with_overrides(
            workspace=workspace, tools=tools, reasoning_effort=" low "
        )
        validation = ValidationConfig().with_overrides(repair=True)
        files = FileCollectionConfig().with_overrides(enabled=True)
        logging = LoggingConfig().with_overrides(path="run.jsonl")

        self.assertEqual(workspace.read_paths, ("src",))
        self.assertEqual(tools.timeout, 4.0)
        self.assertEqual(session.reasoning_effort, "low")
        self.assertIs(validation.repair, True)
        self.assertIs(files.enabled, True)
        self.assertEqual(logging.path, pathlib.Path("run.jsonl"))

    def test_runner_field_mapping(self):
        workspace = WorkspaceConfig(cwd="/workspace", subcwd_root="tmp")
        tool_config = ToolConfig(tools=[make_tool()], timeout=7)
        config = SessionConfig(
            workspace=workspace,
            tools=tool_config,
            context=["README.md"],
        )
        kwargs = config._runner_kwargs()

        self.assertEqual(kwargs["cwd"], pathlib.Path("/workspace"))
        self.assertEqual(kwargs["subcwd_root"], pathlib.Path("tmp"))
        self.assertEqual(kwargs["context"], ("README.md",))
        self.assertEqual(kwargs["tools"], tool_config.tools)
        self.assertEqual(kwargs["max_concurrent_tool_calls"], 8)
        self.assertEqual(kwargs["tool_timeout"], 7.0)
        self.assertIsNone(kwargs["default_subagent_model"])
        self.assertIsNone(kwargs["default_subagent_reasoning_effort"])
        self.assertIs(kwargs["subagents_enabled"], False)


if __name__ == "__main__":
    unittest.main()
