---
name: codex-job-runner
description: Python から Codex SDK-backed job wrapper を設計または実装するときに使う。詳細はこの skill の workflow に従う。
---

# Codex Job Runner

## 目的

skill、workflow、hook、または local script から Codex job を Python で programmatic に呼び出す必要があるときに使う。

この skill は、通常のユーザー向け chat delegation のためのものではない。Codex job の起動、完了待ち、最終応答の取得、job が read/write できる path の制約を行う local Python 補助 library を提供する。

## 使用場面

- 1 つ以上の Codex job を起動する skill script を設計または実装する。
- 単一の blocking な SDK turn 実行ではなく、non-blocking な job handle が必要である。
- 初期 model context、read path、write path、model、reasoning effort、cwd、timeout、または best-effort な global context 抑制を渡す必要がある。
- 別の orchestration layer の下に置ける小さい API が必要である。

この skill を、top-level agent の直接推論やユーザー向け chat 応答の代替として使わない。task graph、一般 retry policy、worker pool、ledger、結果統合の挙動はこの wrapper に追加せず、呼び出し側で構築する。例外として、`output_validator` に失敗した同一 job の最終出力を修正する validation repair だけは、この wrapper の局所機能として扱う。

## 使い方

script から import するときは、skill directory を `PYTHONPATH` に入れる。

```bash
PYTHONPATH="$CODEX_HOME/skills/codex-job-runner" python path/to/script.py
```

最小の Python 利用例:

```python
from codex_job_runner import CodexClient

client = CodexClient(
    cwd="/path/to/workspace",
)

job = client.start(
    input="この skill script をレビューし、問題点を報告してください。",
    context=["scripts/review_skill.py"],
    read_paths=["scripts", "README.md"],
    write_paths=["tmp/codex-output"],
)

response = job.result()
print(response.content)
```

同期的な script code では `client.run(...)` を使う。複数 job では `client.start(...)` と `client.wait_any(...)` を使う。呼び出し側は、起動したすべての job を完了まで待つか cancel する。

## Jobs

再利用する「サブエージェント的な処理」は、正式な agent や subagent ではなく **wrapper job** として `jobs/` 配下に置く。

各 job directory は、job 専用の `CodexClient` subclass を定義する。subclass の `start(...)` は job 固有の入力を受け取り、`prompt.md` などの固定指示、参照 file、path-like context を `context` として組み立て、`read_paths`、`write_paths`、`output_schema_path` とともに `super().start(...)` に渡す。

job 固有の任意補足入力（例: `extra_input`）は、ユーザーが明示した場合だけ per-run の `input` に追加する。呼び出し元の判断だけで補足入力を作って渡さない。

標準構成:

```text
jobs/
  template_review/
    README.md
    __init__.py
    client.py
    prompt.md
    output.schema.json
```

`CodexClient` は軽い設定 holder なので、job client は必要な場所で生成してよい。orchestration、retry、依存関係管理、結果統合は job subclass に入れず、呼び出し側で扱う。

## Validator Scripts

job の最終出力を機械的に確認する必要がある場合は、job client から `output_validator` を渡す。validator は wrapper が起動する通常の command であり、shell 文字列ではなく argv として指定する。

```python
response = client.run(
    input="結果を JSON で返してください。",
    output_validator=["python3", "scripts/validate_result.py"],
    repair_validation_failures=True,
    max_repairs=5,
)
```

validator 作成者は次の入出力契約に従う。

- stdin から `CodexResponse.content` そのものを読む。JSON を期待する validator は `sys.stdin.read()` をそのまま parse する。
- 成功なら exit code `0` で終了する。
- 失敗なら non-zero exit code で終了し、修正に必要な診断を stdout または stderr に出す。
- `${SUBCWD}` 内の生成 file を確認する場合は、env `CODEX_WRAPPER_SUBCWD` の path から読む。
- `response.files` は validator には渡されない。file validation は cleanup 前の `${SUBCWD}` を直接読む。
- validator は出力を修正しない。修正は validation failure 後の repair attempt に任せる。

wrapper は validator 実行時に次の env を渡す。

| Env Var | Meaning |
| --- | --- |
| `CODEX_WRAPPER_CWD` | resolved `cwd`。 |
| `CODEX_WRAPPER_ATTEMPT` | original attempt を `0` とする attempt index。 |
| `CODEX_WRAPPER_REPAIR_ATTEMPT` | original attempt では `0`、repair attempt では `1` から始まる repair index。 |
| `CODEX_WRAPPER_OUTPUT_SCHEMA_PATH` | `output_schema_path` が指定された場合の resolved path。 |
| `CODEX_WRAPPER_SUBCWD` | `${SUBCWD}` が作られた場合の resolved path。 |

validator の stdout / stderr は、`CodexValidationError` に保持される。`repair_validation_failures=True` の場合は repair prompt にも渡されるため、診断は「何が不足し、どう直すべきか」が分かる短い内容にする。

## 契約

- 実用 API reference は `README.md` として扱う。
- 入力、出力、sandbox、failure、public API の利用者向け正本は `spec.md` として扱う。
- job API、完了判定、timeout、cancel の詳細契約は `job-api-spec.md` として扱う。
- `${SUBCWD}` と file collection の詳細契約は `subcwd-spec.md` として扱う。
- sandbox、shell command、global context の詳細契約は `sandbox-spec.md` として扱う。
- structured output と Pydantic output の詳細契約は `structured-output-spec.md` として扱う。
- wrapper 側 validator と validation failure repair の詳細契約は `validation-repair-spec.md` として扱う。
- automatic review と review session の詳細契約は `review-spec.md` として扱う。
- logging の詳細契約は `logging-spec.md` として扱う。
- public wrapper API は job 用の `start`、`run`、`wait_any`、`done`、`result`、`cancel` と、review session 用の `start_review_session`、`ReviewSession.send`、`ReviewSession.close`、`ReviewSession.history` に留める。
- model の初期 context に入れたい text、file、directory、path-like 情報には `context` を使う。`attachments` と `instructions` は別々の public API 概念にせず、同じ初期 model context item として扱う。
- `context` は固定または安定した item を先に、volatile または run-specific な item を後に並べる。OpenAI prompt caching は prefix-based automatic caching なので、wrapper は安定順序を作れるようにするだけで、item-level の cache flag や cache metadata は持たない。
- per-run の可変要求や parameter は `input` に置く。prompt 構築時は安定した `context` を先に、`input` を後に置く。
- `output_schema_path` は SDK turn の `output_schema` に渡すだけでなく、schema 本文を `Output schema` section として model の初期 context にも入れる。
- file 内容を同じ prompt に二重に入れない。file 内容を model に最初から見せる場合は `context` item として渡し、`input` 内に同じ内容を貼らない。path だけを使わせたい場合は `read_paths` / `write_paths` で filesystem permission を渡す。
- `read_paths` は content injection ではなく filesystem permission として扱う。初期 context として内容を渡す file は `context` に入れる。
- filesystem permission の意図には `read_paths` と `write_paths` を使う。`write_paths` は同じ path への read access も含む。
- `read_paths` と `write_paths` は v1 では `cwd` 配下に限定され、Permission Profile の `:workspace_roots` rule として渡される。
- thinking / reasoning effort を指定する場合は `reasoning_effort` を使い、wrapper は Codex CLI の `model_reasoning_effort` config として渡す。
- `allow_shell_commands=True` では、検出できる場合だけ sandbox 内の command runner 用 Codex runtime directory を内部 read-only path として Permission Profile に追加する。
- shell command 実行は既定で on とし、Permission Profile の filesystem sandbox 内で `read_paths` / `write_paths` の境界に従わせる。
- shell command を使わせない job だけ `allow_shell_commands=False` を明示する。context の inline 挙動は shell command 設定に依存しない。
- command prefix ごとの allowlist / denylist は v1 API に入れず、必要なら呼び出し側が Codex CLI の rules 設定で管理する。
- runner job と review session は、approval request、MCP servers、apps / MCP apps、multi-agent / collab、memory を常時 off にする。具体的な SDK API と config override は `sandbox-spec.md` を正本にする。
- runner job は親 chat の hook、developer instruction、delegation policy、subagent 起動方針を継承しない独立した wrapper job として扱う。job に必要な固定契約は `context`、run-specific な要求は `input`、出力形式は schema で明示する。
- `ignore_global_context=True` は best-effort な抑制としてだけ使う。`CODEX_HOME` は変更せず、global `AGENTS.md` の完全な隠蔽は保証しない。
- automatic skill instructions を抑制する場合は `allowed_skills=[]` を使う。v1 は非空 `allowed_skills` による skill allow-list を扱わず、実行前に `ValueError` にする。

## 検証

この skill または wrapper package を編集した後は、次を実行する。

```bash
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s "$CODEX_HOME/skills/codex-job-runner/tests"
```

利用できる場合は、skill folder も検証する。

```bash
python3 "$CODEX_HOME/skills/.system/skill-creator/scripts/quick_validate.py" "$CODEX_HOME/skills/codex-job-runner"
```
