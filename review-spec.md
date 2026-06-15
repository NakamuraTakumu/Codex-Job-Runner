# Codex Job Runner Review Spec

- Created: 2026-06-14
- Status: active

Responsibility: automatic review job と public `ReviewSession` の契約を定義する。

## ReviewConfig

`ReviewConfig` は automatic review と review session の設定 object である。

| Field | Default | Meaning |
| --- | --- | --- |
| `instructions` | required | reviewer への指示。 |
| `prompt_path` | `None` | reviewer prompt template file。 |
| `read_paths` | `None` | reviewer に許可する read path。未指定時は主 job の `read_paths` を流用する。 |
| `timeout` | `None` | review job の timeout 設定。v1 では独立した SDK reviewer turn timeout を保証しない。呼び出し側が確実な停止境界を必要とする場合は `job.result(timeout=...)` を使う。 |
| `model` | `None` | reviewer model。未指定時は主 job と同じ。 |
| `reasoning_effort` | `None` | reviewer effort。未指定時は主 job と同じ。 |
| `reuse_session` | `False` | review fail 後の再 review で同じ reviewer session を再利用する。 |
| `include_main_prompt` | `False` | reviewer input に main job prompt 全文を含める。 |

## Automatic Review

`review=ReviewConfig(...)` を指定すると、main attempt と wrapper-side validator が成功した後に reviewer job を 1 turn 実行する。

reviewer input は既定で次を含む。

- original input
- final response
- `${SUBCWD}` が reviewer の read permission に含まれる場合の subcwd path
- reviewer 用 instructions

main job prompt 全文は既定では含めない。必要な job だけ `ReviewConfig(include_main_prompt=True)` を指定する。

reviewer は JSON object として `passed`, `repair_text`, `summary` を返す。

wrapper は automatic review の activity log に、review input の byte size、sha256、preview を持つ要約を記録する。review stream event には、対応する main attempt の `attempt` と `repair_index` を付ける。

## Review Failure

`passed=false` は review failure として扱う。

`repair_validation_failures=False` の場合、wrapper は `CodexValidationError` を raise する。

`repair_validation_failures=True` の場合、wrapper は `repair_text` を repair prompt に入れて main attempt を再実行する。review repair 回数は `max_review_repairs` で制御する。未指定時は `max_repairs` と同じ。

review output parse failure や review job execution failure は `CodexExecutionError` として扱う。

## ReviewSession

`client.start_review_session(...)` は reviewer 用の継続 session を返す。

| API | Meaning |
| --- | --- |
| `ReviewSession.send(message, output_schema_path=None)` | reviewer session に 1 turn 送る。 |
| `ReviewSession.close()` | session を閉じる。 |
| `ReviewSession.history` | `(role, content)` tuple の履歴を返す。 |

`ReviewSession` は same thread を継続利用する。`history` は wrapper 側が保持する会話履歴であり、変更用 API は提供しない。

`ReviewSession.send(...)` が返す `CodexResponse.activity_log` は、manual reviewer turn の stream event 要約を `phase="stream"`、`detail="review"` として保持する。
