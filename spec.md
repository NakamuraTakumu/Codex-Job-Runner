# Codex Job Runner Spec

- Created: 2026-06-14
- Status: active

Responsibility: Python から Codex job を起動する利用者に対して、入力、出力、sandbox 境界、失敗時の扱いを短く定義する。

## Summary

`codex-job-runner` は、Python から Codex SDK-backed job を起動し、完了後に `CodexResponse` を返す薄い wrapper である。

利用者が最初に見るべき契約は次の 3 つだけである。

1. **入力**: `context` で初期 model context、`input` で run-specific な依頼や parameter、`read_paths` / `write_paths` で filesystem 権限を渡す。
2. **出力**: 成功時は `CodexResponse.content` を主出力として読む。必要なら usage、cost 概算、回収 file、activity log を読む。
3. **sandbox**: job が読める path と書ける path は Permission Profile で制限する。`danger-full-access` は public API に出さない。

## Minimal Use

```python
from codex_job_runner import CodexClient

client = CodexClient(cwd="/path/to/project")

response = client.run(
    input="README を要約してください",
    context=["README.md"],
)

print(response.content)
```

## Input

### Primary Input

| Field | Required | Meaning |
| --- | --- | --- |
| `input` | yes | run-specific な依頼文または parameter。空文字列は `ValueError`。 |

`messages=[...]` は受け取らない。OpenAI Chat Completions 互換 API ではない。

`input` は固定指示や file 本文を置く場所ではない。prompt 構築時は安定した `context` を先に置き、per-run に変わる `input` を後に置く。

### Context Input

| Field | Meaning |
| --- | --- |
| `context` | model の初期 context に入れる text、file、directory、path-like item。通常 file は既定で本文を inline し、directory は path だけ列挙する。 |
| `read_paths` | job が追加で読んでよい file または directory。content injection ではない。 |
| `write_paths` | job が書いてよい file または directory。同じ path の read 権限も含む。 |
| `subcwd_files` | `${SUBCWD}` に事前配置する file。権限は自動付与しない。詳細は `subcwd-spec.md`。 |
| `inline_context` | context 内の file 本文 inline を行うか。 |

`context` は、従来別概念になりがちな attachment、instruction、job prompt material を同じ初期 model context item として扱う public API である。固定または安定した共通 item を先に、volatile または run-specific な item を後に並べる。OpenAI prompt caching は prefix-based automatic caching なので、wrapper は安定順序を作れるようにするだけで、item-level の cache flag や cache metadata は持たない。

string context item は次の規則で解釈する。`cwd` から解決した path が存在する場合は file または directory context として扱う。`PathLike` item と、path 区切り・絶対 path・拡張子などにより path らしい string が存在しない場合は error にする。それ以外の string は inline text context として扱う。`inline_context=False` は file 本文 inline だけを抑制し、inline text context は残す。

`context` と `read_paths` は役割が違う。model に最初から見せたい file は `context`、必要になったときだけ path 経由で読めればよい範囲は `read_paths` に入れる。file 内容を `context` に入れる場合、同じ内容を `input` にも貼らない。path-only access は `read_paths` / `write_paths` で表す。

`${SUBCWD}` を使う場合、bare な `"${SUBCWD}"` は受け付けない。読む file は `read_paths=["${SUBCWD}/path"]`、書く file は `write_paths=["${SUBCWD}/path"]` のように明示する。

### Output Control

| Field | Meaning |
| --- | --- |
| `output_schema_path` | final response の JSON Schema file。SDK turn の `output_schema` と prompt context の両方に渡す。 |
| `output_pydantic_model` | Pydantic model 相当 class から schema を生成し、final response を `model_validate_json()` で検証する。 |
| `output_validator` | final response を stdin に受け取る validator command。 |
| `validator_timeout` | validator subprocess の timeout 秒数。 |
| `review` | validator 成功後に reviewer job を走らせる `ReviewConfig`。 |
| `repair_validation_failures` | validator / review failure 時に repair attempt を行うか。 |
| `max_repairs` | repair attempt の上限。validator / review 個別上限は詳細 spec を正本にする。 |
| `repair_prompt_path` | repair prompt template file。 |
| `collect_files` | `${SUBCWD}` から生成 file を回収する。 |

`output_schema_path` と `output_pydantic_model` は同時指定できない。structured output の詳細は `structured-output-spec.md`、validator / review / repair の詳細は `validation-repair-spec.md` を正本にする。

### Execution Settings

| Field | Default | Meaning |
| --- | --- | --- |
| `model` | `"gpt-5.4-mini"` | SDK thread / turn に渡す model。 |
| `reasoning_effort` | `"medium"` | SDK turn の effort。 |
| `cwd` | process cwd | job の基準 directory。 |
| `timeout` | `None` | `result()` の既定 timeout 秒数。 |
| `ephemeral` | `True` | SDK thread を ephemeral として起動する。 |
| `allow_shell_commands` | `True` | shell tool を使わせる。filesystem access は sandbox に従う。 |
| `ignore_global_context` | `False` | project doc と automatic skill instructions を best-effort で抑制する。 |
| `allowed_skills` | `None` | v1 では `None` または `[]` のみ。`[]` は automatic skill instructions を出さない。 |
| `env` | `{}` | SDK config と validator subprocess に追加する environment。client default のみ。 |
| `log_dir` | `cwd/.codex-job-runner-logs` | activity log の既定出力先。client default のみ。 |
| `log_path` | auto | job ごとの activity log 出力先。 |

非空 `allowed_skills` は v1 では未対応で、実行前に `ValueError` にする。sandbox と global context の詳細は `sandbox-spec.md`、logging の詳細は `logging-spec.md` を正本にする。

repair attempt が発生する場合、original attempt と repair attempts は同じ main SDK thread を使う。validator / review / repair の thread lifecycle は `validation-repair-spec.md` を正本にする。

## Output

### Success

成功時は `CodexResponse` を返す。

| Field | Meaning |
| --- | --- |
| `content` | Codex の final response。通常はこれを主出力として読む。 |
| `returncode` | 最終 main attempt の exit code。成功時は `0`。 |
| `usage` | SDK usage。repair がある場合は attempt 合算。 |
| `pricing` | wrapper 内蔵価格表から引いた model pricing。未対応 model では `None`。 |
| `estimated_cost_usd` | `usage` と `pricing` から計算した概算 cost。実請求額の保証ではない。 |
| `files` | `collect_files=True` の場合に `${SUBCWD}` から回収した file。 |
| `activity_log` | wrapper 側 phase log。debug と監査用。 |
| `log_path` | activity log を書いた file path。 |
| `stdout` / `stderr` | debug 用。主出力として扱わない。 |
| `argv` | debug 用の `openai-codex thread.run` 互換表示。 |

### File Output

job に file を作らせて回収したい場合は、`${SUBCWD}/...` を `write_paths` に入れ、`collect_files=True` を指定する。

```python
response = client.run(
    input="report.md を作成してください",
    write_paths=["${SUBCWD}/report.md"],
    collect_files=True,
)

print(response.files["report.md"].text)
```

file collection の詳細と上限は `subcwd-spec.md` を正本にする。

## Sandbox

### Filesystem Boundary

wrapper は job ごとに Permission Profile を作る。

| Input | Permission |
| --- | --- |
| `read_paths` | read |
| `write_paths` | write。write は同じ path の read も含む。 |
| 指定なし | read/write を許可しない。 |

v1 の `read_paths` と `write_paths` は `cwd` 配下だけを受け取る。`cwd` の外側は実行前に `ValueError` にする。

`allow_shell_commands=True` の場合、command runner 起動に必要な Codex runtime directory を検出できたときだけ、内部 read-only path として追加する。この追加は user data の読み取り許可ではない。

### Shell Commands

`allow_shell_commands=True` が既定である。shell command は使えるが、読める path と書ける path は Permission Profile に従う。

`allow_shell_commands=False` の場合、wrapper は shell tool と login shell を無効化する config override を渡す。context の inline 挙動は shell command 設定に依存しない。

command prefix ごとの allowlist / denylist は v1 API に含めない。必要な場合は Codex CLI 側の rules 設定を wrapper の外側で管理する。

### Codex Runtime Features

wrapper は runner job を非対話実行として扱い、次を常時渡す。

- approval policy は SDK `ApprovalMode.deny_all` を `thread_start` に渡す。
- MCP server config は `mcp_servers={}` で空にする。
- known apps / MCP apps 経路は `features.apps=false` と `features.enable_mcp_apps=false` で抑制する。
- multi-agent / collab は `features.multi_agent=false`、`features.multi_agent_v2=false`、`features.collaboration_modes=false` で抑制する。
- memory は `features.memories=false`、`memories.generate_memories=false`、`memories.use_memories=false` で抑制する。

`connectors={}` は現 installed Codex CLI の strict config で unknown field になるため渡さない。wrapper が抑制するのは known apps / MCP apps と `mcp_servers={}` の経路であり、将来の未知 connector 設定まで完全に無効化する保証ではない。

### Global Context

`ignore_global_context=True` は best-effort mode である。

行うこと:

- project doc の prompt 注入を `project_doc_max_bytes=0` で抑制する。
- global hook を `features.hooks=false` で抑制する。
- `allowed_skills` が未指定なら `[]` として扱い、automatic skill instructions を出さない。
- prompt 上で global `AGENTS.md` と skills に依存しないよう指示する。

保証しないこと:

- `CODEX_HOME` の差し替え。
- global `AGENTS.md` の完全な遮断。
- Codex runtime の sandbox 失敗時の bypass。

## Failure

失敗時は exception を raise する。

| Error | Condition |
| --- | --- |
| `CodexExecutionError` | main attempt が non-zero、または review job が実行・parse に失敗した。 |
| `CodexTimeoutError` | `job.result(timeout=...)` が timeout した。 |
| `CodexCancelledError` | `job.cancel()` 後に `job.result()` を呼んだ。 |
| `CodexValidationError` | validator または review が成果物を不合格にした。 |

error は `message`, `returncode`, `stdout`, `stderr`, `argv`, `activity_log`, `log_path` を持つ。

## Public API

| API | Meaning |
| --- | --- |
| `client.start(...)` | non-blocking に job を起動し、`CodexJob` を返す。 |
| `client.run(...)` | `start(...).result()` の convenience method。 |
| `client.wait_any(jobs, timeout=None)` | 複数 job のうち最初に完了した job を返す。 |
| `job.done()` | job が完了済みか返す。 |
| `job.result(timeout=None)` | 成功時は `CodexResponse`、失敗時は exception。 |
| `job.cancel()` | 実行中 job を cancel する。 |
| `client.start_review_session(...)` | reviewer 用の継続 session を開始する。 |
| `ReviewSession.send(...)` | reviewer session に 1 turn 送る。 |
| `ReviewSession.close()` | reviewer session を閉じる。 |
| `ReviewSession.history` | reviewer session の会話履歴を返す。 |

## Out Of Scope

v1 は次を扱わない。

- OpenAI SDK 完全互換。
- HTTP server。
- Chat Completions `messages` 形式。
- streaming response API。
- tool calling API。
- image/audio content parts。
- sampling parameter。
- command prefix ごとの allowlist / denylist。
- `danger-full-access` public API。

## Feature Specs

- `${SUBCWD}` と file collection: `subcwd-spec.md`
- sandbox、shell command、global context: `sandbox-spec.md`
- structured output と Pydantic output: `structured-output-spec.md`
- validator、review、repair: `validation-repair-spec.md`
- review session: `review-spec.md`
- job API、完了判定、timeout、cancel: `job-api-spec.md`
- logging: `logging-spec.md`
- skill としての利用導線: `SKILL.md`
- 実用例: `README.md`
