---
name: codex-job-runner
description: Python から `codex exec` を使う wrapper を設計または実装するときに使う。詳細はこの skill の workflow に従う。
---

# Codex API Wrapper

## 目的

skill、workflow、hook、または local script から `codex exec` を Python で programmatic に呼び出す必要があるときに使う。

この skill は、通常のユーザー向け chat delegation のためのものではない。Codex job の起動、完了待ち、最終応答の取得、job が read/write できる path の制約を行う local Python 補助 library を提供する。

## 使用場面

- 1 つ以上の Codex job を起動する skill script を設計または実装する。
- 単一の blocking な `codex exec` 呼び出しではなく、non-blocking な job handle が必要である。
- attachment path、read path、write path、model、reasoning effort、cwd、timeout、または best-effort な global context 抑制を渡す必要がある。
- 別の orchestration layer の下に置ける小さい API が必要である。

この skill を、top-level agent の直接推論やユーザー向け chat 応答の代替として使わない。task graph、一般 retry policy、worker pool、ledger、結果統合の挙動はこの wrapper に追加せず、呼び出し側で構築する。例外として、`output_validator` に失敗した同一 job の最終出力を修正する validation repair だけは、この wrapper の局所機能として扱う。

## 使い方

script から import するときは、skill directory を `PYTHONPATH` に入れる。

```bash
PYTHONPATH="$CODEX_HOME/skills/codex-job-runner" python path/to/script.py
```

最小の Python 利用例:

```python
from codex_api_wrapper import CodexClient

client = CodexClient(
    cwd="/path/to/workspace",
    timeout=300,
    ignore_global_context=True,
)

job = client.start(
    input="この skill script をレビューし、問題点を報告してください。",
    attachments=["scripts/review_skill.py"],
    read_paths=["scripts", "README.md"],
    write_paths=["tmp/codex-output"],
)

response = job.result()
print(response.content)
```

同期的な script code では `client.run(...)` を使う。複数 job では `client.start(...)` と `client.wait_any(...)` を使う。呼び出し側は、起動したすべての job を完了まで待つか cancel する。

## Jobs

再利用する「サブエージェント的な処理」は、正式な agent や subagent ではなく **wrapper job** として `jobs/` 配下に置く。

各 job directory は、job 専用の `CodexClient` subclass を定義する。subclass の `start(...)` は job 固有の入力を受け取り、`prompt.md`、`output.schema.json`、`attachments`、`read_paths`、`write_paths`、`output_schema_path` を組み立てて `super().start(...)` に渡す。

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
    max_repairs=1,
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
- 入力、実行設定、permission、完了検知、出力取得、error の設計契約は `spec.md` として扱う。
- `${SUBCWD}` と file collection の詳細契約は `subcwd-spec.md` として扱う。
- wrapper 側 validator と validation failure repair の詳細契約は `validation-repair-spec.md` として扱う。
- public wrapper API は `start`、`run`、`wait_any`、`done`、`result`、`cancel` に留める。
- model の初期 context に入れたい file または directory には `attachments` を使う。通常 file attachment は既定で上限付き inline し、directory は path だけ列挙する。
- `output_schema_path` は `codex exec --output-schema` に渡すだけでなく、schema 本文を `Output schema` section として model の初期 context にも入れる。
- `read_paths` は content injection ではなく filesystem permission として扱う。初期 context として内容を渡す file は `attachments` に入れる。
- filesystem permission の意図には `read_paths` と `write_paths` を使う。`write_paths` は同じ path への read access も含む。
- `read_paths` と `write_paths` は v1 では `cwd` 配下に限定され、Permission Profile の `:workspace_roots` rule として渡される。
- thinking / reasoning effort を指定する場合は `reasoning_effort` を使い、wrapper は Codex CLI の `model_reasoning_effort` config として渡す。
- `allow_shell_commands=True` では、sandbox 内の command runner 用に Codex runtime directory を内部 read-only path として Permission Profile に追加する。
- shell command 実行は既定で on とし、Permission Profile の filesystem sandbox 内で `read_paths` / `write_paths` の境界に従わせる。
- shell command を使わせない job だけ `allow_shell_commands=False` を明示する。attachments の inline 挙動は shell command 設定に依存しない。
- command prefix ごとの allowlist / denylist は v1 API に入れず、必要なら呼び出し側が Codex CLI の rules 設定で管理する。
- `ignore_global_context=True` は best-effort な抑制としてだけ使う。`CODEX_HOME` は変更せず、global `AGENTS.md` の完全な隠蔽は保証しない。
- automatic skill instructions を抑制する場合は `allowed_skills=[]` を使う。使える skill を prompt 上で best-effort に制限する場合は、非空 list を渡す。

## 検証

この skill または wrapper package を編集した後は、次を実行する。

```bash
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s "$CODEX_HOME/skills/codex-job-runner/tests"
```

利用できる場合は、skill folder も検証する。

```bash
python3 "$CODEX_HOME/skills/.system/skill-creator/scripts/quick_validate.py" "$CODEX_HOME/skills/codex-job-runner"
```
