"""Live smoke test for peer exchange and restricted answer forks."""

from __future__ import annotations

import json
import pathlib
from unittest import mock

from codex_job_runner import (
    CodexJob,
    CodexSession,
    ReviewConfig,
    SessionConfig,
    Tool,
    ToolConfig,
    ValidationConfig,
    WorkspaceConfig,
)


ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "tests" / "live_peer_exchange_fixture"
ALLOWED_FILE = FIXTURE_DIR / "allowed.txt"
BLOCKED_FILE = FIXTURE_DIR / "blocked.txt"
READ_MARKER = "PEER_READ_MARKER_7E19C4"


def main() -> int:
    if BLOCKED_FILE.exists():
        BLOCKED_FILE.unlink()

    tool_calls: list[str] = []
    fork_records: dict[int, dict[str, object]] = {}
    original_fork = CodexSession.fork
    original_run = CodexSession.run

    def permission_probe(request: str) -> str:
        tool_calls.append(request)
        return "GENERAL_TOOL_RAN"

    def tracking_fork(self, *, input, config=None, **kwargs):
        source_thread_id = self._conversation_state.thread_id
        fork = original_fork(self, input=input, config=config, **kwargs)
        fork_records[id(fork)] = {
            "source_thread_id": source_thread_id,
            "fork_thread_id": fork._conversation_state.thread_id,
            "input": input,
            "config": {
                "read_paths": [
                    str(path) for path in fork.config.workspace.read_paths
                ],
                "write_paths": [
                    str(path) for path in fork.config.workspace.write_paths
                ],
                "tools": fork.config.tools is not None,
                "allow_shell_commands": fork.config.allow_shell_commands,
                "subagents_enabled": fork.config.subagents_enabled,
                "ignore_global_context": fork.config.ignore_global_context,
                "allowed_skills": fork.config.allowed_skills,
            },
        }
        return fork

    def tracking_run(self):
        response = original_run(self)
        record = fork_records.get(id(self))
        if record is not None:
            record["response"] = response.content
            record["stdout"] = response.stdout
            record["usage"] = response.usage
        return response

    probe_tool = Tool(
        permission_probe,
        "Record that the general main-only permission probe was invoked.",
        args={"request": "The probe request."},
        returns="A marker proving that the general Tool ran.",
        name="permission_probe",
    )
    common_workspace = WorkspaceConfig(
        cwd=ROOT,
        read_paths=(ALLOWED_FILE,),
    )
    main_config = SessionConfig(
        model="gpt-5.6-luna",
        reasoning_effort="low",
        timeout=300,
        ignore_global_context=True,
        allowed_skills=(),
        allow_shell_commands=True,
        subagents_enabled=False,
        tools=ToolConfig(tools=(probe_tool,), timeout=120),
        workspace=common_workspace,
    )
    review_session = SessionConfig(
        model="gpt-5.6-luna",
        reasoning_effort="low",
        timeout=300,
        ignore_global_context=True,
        allowed_skills=(),
        allow_shell_commands=True,
        subagents_enabled=False,
        workspace=common_workspace,
    )
    permission_question = f"""
This is a live permission audit of your temporary answer fork.
Use actual available capabilities; do not claim success without observing it.
Perform these checks and return a concise plain-text report:
1. Read {ALLOWED_FILE} and report READ_RESULT=<its exact single-line content>.
2. Attempt to create {BLOCKED_FILE} containing BLOCKED_WRITE_SUCCEEDED and
   report WRITE_RESULT=SUCCEEDED or DENIED.
3. Attempt to execute the shell command `pwd` and report
   SHELL_RESULT=SUCCEEDED or UNAVAILABLE.
4. Attempt to call the general Tool permission_probe and report
   GENERAL_TOOL_RESULT=SUCCEEDED or UNAVAILABLE.
5. Attempt to call ask_peer and report NESTED_PEER_RESULT=SUCCEEDED or
   UNAVAILABLE.
6. Attempt to spawn a subagent and report SUBAGENT_RESULT=SUCCEEDED or
   UNAVAILABLE.
Do not substitute guesses for actual tool results.
""".strip()
    review_context = f"""
You are the automatic reviewer in a live peer-exchange test.
If the main response is exactly INITIAL_NEEDS_REVIEW:
1. You MUST call ask_peer exactly once with this message:

{permission_question}

2. After receiving its answer, return a failing review whose only finding
instructs main to call ask_peer exactly once against the latest completed
reviewer using the complete permission-audit message above, then return
LIVE_PEER_EXCHANGE_SUCCESS. Include that complete message verbatim in the
finding so main can use it without access to this review context.
If the revised main response contains LIVE_PEER_EXCHANGE_SUCCESS, do not call
ask_peer and pass the review.
""".strip()
    job_input = """
Return exactly INITIAL_NEEDS_REVIEW on the initial attempt.
On a repair attempt, follow the automatic review finding exactly.
Do not call permission_probe yourself.
""".strip()

    with (
        mock.patch.object(CodexSession, "fork", tracking_fork),
        mock.patch.object(CodexSession, "run", tracking_run),
    ):
        response = CodexJob(
            config=main_config,
            input=job_input,
            review=ReviewConfig(
                context=review_context,
                session=review_session,
                reuse_session=False,
                enable_peer_exchange=True,
                max_repairs=1,
            ),
            validation=ValidationConfig(repair=True),
            max_total_repairs=1,
        ).run()

    records = list(fork_records.values())
    exchange_messages = [
        entry.message
        for entry in response.activity_log
        if entry.phase == "peer_exchange"
    ]
    start_messages = [
        entry.message
        for entry in response.activity_log
        if entry.phase == "peer_exchange" and entry.event == "start"
    ]
    end_messages = [
        entry.message
        for entry in response.activity_log
        if entry.phase == "peer_exchange" and entry.event == "end"
    ]
    fork_configs_safe = all(
        record["config"]["write_paths"] == []
        and record["config"]["tools"] is False
        and record["config"]["allow_shell_commands"] is True
        and record["config"]["subagents_enabled"] is False
        and record["config"]["ignore_global_context"] is True
        and record["config"]["allowed_skills"] == ()
        for record in records
    )
    distinct_threads = all(
        record["source_thread_id"] != record["fork_thread_id"]
        for record in records
    )
    read_observed = all(
        READ_MARKER in str(record.get("response", ""))
        for record in records
    )
    permission_report_observed = any(
        READ_MARKER in str(record.get("response", ""))
        and "SHELL_RESULT=SUCCEEDED" in str(record.get("response", ""))
        and "GENERAL_TOOL_RESULT=UNAVAILABLE" in str(record.get("response", ""))
        and "NESTED_PEER_RESULT=UNAVAILABLE" in str(record.get("response", ""))
        and "SUBAGENT_RESULT=UNAVAILABLE" in str(record.get("response", ""))
        for record in records
    )
    command_event_observed = any(
        '"item_type": "command' in str(record.get("stdout", "")).lower()
        for record in records
    )
    transient_write_observed = any(
        "WRITE_RESULT=SUCCEEDED" in str(record.get("response", ""))
        for record in records
    )
    checks = {
        "job_completed": "LIVE_PEER_EXCHANGE_SUCCESS" in response.content,
        "reviewer_to_main": any(
            "caller=reviewer target=main" in message
            for message in exchange_messages
        ),
        "main_to_reviewer": any(
            "caller=main target=reviewer" in message
            for message in exchange_messages
        ),
        "exactly_two_exchanges": len(start_messages) == len(end_messages) == 2,
        "exactly_two_forks": len(records) == 2,
        "distinct_parent_and_fork_threads": distinct_threads,
        "fork_configs_are_restricted": fork_configs_safe,
        "permission_report_observed": permission_report_observed,
        "blocked_file_absent": not BLOCKED_FILE.exists(),
        "general_tool_not_called": tool_calls == [],
        "command_event_observed": command_event_observed,
        "usage_aggregated": (
            response.usage is not None
            and sum(
                int(record.get("usage", {}).get("input_tokens", 0))
                for record in records
            )
            <= response.usage.get("input_tokens", 0)
        ),
    }
    report = {
        "checks": checks,
        "response": response.content,
        "usage": response.usage,
        "estimated_cost_usd": response.estimated_cost_usd,
        "log_path": response.log_path,
        "tool_calls": tool_calls,
        "observations": {
            "read_marker_observed_by_all_forks": read_observed,
            "transient_write_observed": transient_write_observed,
        },
        "forks": records,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
