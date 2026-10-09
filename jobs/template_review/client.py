"""Copyable template for a Codex wrapper job."""

from pathlib import Path

from codex_job_runner import CodexJob, SessionConfig


HERE = Path(__file__).resolve().parent
PROMPT_PATH = HERE / "prompt.md"
OUTPUT_SCHEMA_PATH = HERE / "output.schema.json"


class TemplateReviewJob(CodexJob):
    """Review one target path with a fixed prompt and output schema."""

    def __init__(
        self,
        *,
        config: SessionConfig,
        target_path,
        read_paths=None,
        write_paths=None,
        extra_input=None,
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
        )
        prompt = _build_input(target_path=target_path, extra_input=extra_input)
        config = config.with_overrides(
            workspace=config.workspace.with_overrides(
                read_paths=read_paths or [target_path],
                write_paths=write_paths or [],
            )
        )
        super().__init__(
            config=config,
            input=[target_path, prompt],
            output_schema_path=str(OUTPUT_SCHEMA_PATH),
            **kwargs,
        )


def _build_input(*, target_path, extra_input):
    sections = [f"Target path:\n{target_path}"]
    if extra_input:
        sections.append(f"Additional input:\n{extra_input}")
    return "\n\n".join(sections)
