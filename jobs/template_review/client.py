"""Copyable template for a Codex wrapper job."""

from pathlib import Path

from codex_api_wrapper import CodexClient


HERE = Path(__file__).resolve().parent
PROMPT_PATH = HERE / "prompt.md"
OUTPUT_SCHEMA_PATH = HERE / "output.schema.json"


class TemplateReviewClient(CodexClient):
    """Review one target path with a fixed prompt and output schema."""

    def __init__(self, **kwargs):
        kwargs.setdefault("ignore_global_context", True)
        kwargs.setdefault("allowed_skills", [])
        super().__init__(**kwargs)

    def start(
        self,
        *,
        target_path,
        read_paths=None,
        write_paths=None,
        extra_input=None,
        **kwargs,
    ):
        prompt = _build_input(target_path=target_path, extra_input=extra_input)
        return super().start(
            input=prompt,
            attachments=[target_path],
            read_paths=read_paths or [target_path],
            write_paths=write_paths or [],
            output_schema_path=str(OUTPUT_SCHEMA_PATH),
            **kwargs,
        )


def _build_input(*, target_path, extra_input):
    prompt = PROMPT_PATH.read_text(encoding="utf-8").strip()
    sections = [prompt, f"Target path:\n{target_path}"]
    if extra_input:
        sections.append(f"Additional input:\n{extra_input}")
    return "\n\n".join(sections)
