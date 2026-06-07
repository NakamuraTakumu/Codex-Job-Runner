# Codex API Wrapper

`codex_api_wrapper` は、`openai-codex` SDK を使って Codex job を扱う小さな pure Python wrapper である。
Python から Codex job を起動する必要がある skill、workflow、hook、script 作成者向けの補助 library として使う。OpenAI SDK 互換ではなく、最小の job-style API を提供する。

この directory は Codex skill でもある。`SKILL.md` を entrypoint、この README を実用 API reference、`spec.md` を設計契約として扱う。

```python
from codex_api_wrapper import CodexClient, ReviewConfig

client = CodexClient(
    cwd="/home/nakamura/recruit",
    subcwd_root="tmp/codex-jobs",
    timeout=300,
    ignore_global_context=True,
)

job = client.start(
    input="このコードを説明して",
    attachments=["src/main.py", "README.md"],
    read_paths=["src", "README.md"],
    write_paths=["tmp/codex-output"],
)

response = job.result(timeout=300)
print(response.content)
print(response.usage)
print(response.argv)
for entry in response.activity_log:
    print(entry.phase, entry.event, entry.message)
```

## API

- `CodexClient.start(...)`: Codex job を起動し、すぐに返る。
- `CodexClient.run(...)`: `start(...).result(...)` の convenience method。
- `CodexClient.start_review_session(...)`: reviewer と継続会話できる `ReviewSession` を返す。
- `CodexClient.wait_any(jobs, timeout=None)`: 最初に完了した job を返す。なければ `None` を返す。
- `CodexJob.done()`: subprocess が終了済みかを返す。
- `CodexJob.result(timeout=None)`: `CodexResponse` を返す。失敗時は error を raise する。
- `CodexJob.cancel()`: subprocess を終了する。
- `ReviewSession.send(...)`: reviewer へ追加入力を送り、次の返答を得る。
- `ReviewSession.close()`: reviewer session を閉じる。

## Review Session

```python
from codex_api_wrapper import CodexClient, ReviewConfig

client = CodexClient(cwd="/home/nakamura/recruit")
session = client.start_review_session(
    review=ReviewConfig(
        instructions="コードレビュー担当として、指摘と補足説明を返してください。",
    ),
    read_paths=["src", "README.md"],
)

try:
    first = session.send("まずこの差分をレビューしてください。")
    second = session.send("その 2 点目をもう少し具体的に説明してください。")
finally:
    session.close()

print(first.content)
print(second.content)
```

`ReviewSession` は reviewer の会話履歴を保持し、同じ thread を継続利用する。

## 入力

- `input`: 必須の task text。
- `attachments`: prompt で重要 context として明示する path。
- `read_paths`: Codex が読んでよい path。
- `write_paths`: Codex が書いてよい path。
- `subcwd_files`: `${SUBCWD}` に事前配置する file 実体。
- `output_schema_path`: 最終応答 shape を指定する JSON Schema file path。
- `output_pydantic_model`: `model_json_schema()` / `model_validate_json()` を持つ Pydantic model 相当の class。`output_schema_path` の代わりに使い、wrapper はこの model から JSON Schema を生成して SDK に渡す。さらに custom `output_validator` の前に `model_validate_json()` を実行する。
- `output_validator`: 最終応答を検証する command。
- `collect_files`: `${SUBCWD}` 配下の通常 file を `response.files` へ回収する。

`attachments` は model の初期 context に入れる file または directory path である。通常 file は既定で上限付き inline され、path と本文が prompt に入る。

directory attachment は本文 inline されず、path だけ列挙される。通常 file の本文を入れたくない場合だけ `inline_attachments=False` を明示する。

`read_paths` は、attachments 以外に Codex が必要時に読んでよい filesystem 範囲である。attachments に入れた file だけを読ませる場合、通常は `read_paths` を別途広げる必要はない。

`write_paths=["${SUBCWD}"]` を指定すると、job ごとの一時 directory が `subcwd_root` 配下に作られる。`subcwd_root` を省略した場合は `cwd` を使う。`collect_files=True` の場合、job 終了後に通常 file を memory 上へ回収してから一時 directory を削除する。

`subcwd_files` を使うと、wrapper は job 開始前に file 実体を `${SUBCWD}` へ書き出す。worker はそれらを直接読んで編集できる。`${SUBCWD}` を `attachments` や `read_paths` へ直接渡す必要はない。

```python
response = client.run(
    input="inputs/neighborhood_tree.json を直接編集して完成させてください",
    subcwd_files={
        "inputs/neighborhood_tree.json": {
            "text": '{"status": "draft"}\\n',
        }
    },
    collect_files=True,
)

print(response.files["inputs/neighborhood_tree.json"].text)
```

```python
response = client.run(
    input="report.md を作成してください",
    write_paths=["${SUBCWD}"],
    collect_files=True,
)

print(response.files["report.md"].text)
```

## 実行設定

- `model`
- `reasoning_effort`
- `cwd`
- `subcwd_root`
- `timeout`
- `ignore_global_context`
- `allowed_skills`
- `allow_shell_commands`

`ignore_global_context=True` は次を追加する。

- `--ignore-user-config`
- `-c 'project_doc_max_bytes=0'`
- `allowed_skills` が未指定の場合は `-c 'skills.include_instructions=false'`

`CODEX_HOME` は変更しない。global `AGENTS.md` が隠れることも保証しない。

`allowed_skills=[]` は automatic skills instructions block を隠す。非空 list は Codex skill config override に基づく best-effort な prompt-level filter として扱う。

wrapper の既定値は `model="gpt-5.4-mini"` と `reasoning_effort="medium"` である。明示的に別値へ変えたい場合だけ指定する。Codex CLI または user config の既定値へ委譲したい場合は、`CodexClient(model=None, reasoning_effort=None, ...)` のように client default を `None` にする。

`reasoning_effort` は SDK turn の `effort` に渡す。thinking effort を job ごとに変えたい場合に使う。wrapper 既定値は `medium` であり、client または per-call で別値を指定した場合だけ上書きする。

`subcwd_root` は `${SUBCWD}` を使う job の一時 directory の親ディレクトリを指定する client default である。`cwd` 相対または absolute path を受け付けるが、正規化後に `cwd` 配下でなければならない。`${SUBCWD}` token は受け付けない。未指定時は `cwd` を使う。

`allow_shell_commands=True` が既定値である。shell command は Permission Profile の filesystem sandbox 内で実行され、`read_paths` / `write_paths` の境界に従う。

`allow_shell_commands=False` を明示した場合、wrapper は `-c 'features.shell_tool=false'` と `-c 'allow_login_shell=false'` を渡し、file edit 以外の shell command 実行を基本的に使わせない。

command prefix ごとの allowlist / denylist は v1 API では扱わない。必要な場合は、Codex CLI の rules 設定を wrapper の外側で管理する。

`read_paths` は filesystem permission であり、file 内容を prompt に入れる機能ではない。初期 context として内容を渡す場合は `attachments` を使う。

`subcwd_files` は `${SUBCWD}` workspace に初期 file を staged する per-call API である。key は `${SUBCWD}` 内の相対 path、value は `{"text": "..."}` または `{"bytes": b"..."}` とする。小さい UTF-8 text でも wrapper は自動 inline せず、prompt には staged file の path 一覧だけを出す。

`output_schema_path` は SDK turn の `output_schema` に渡す。さらに schema file の本文を `Output schema` section として初期 context に入れる。schema は UTF-8 text の通常 file とし、inline attachment と同じ 1 file byte 上限に従う。wrapper は schema validation を自前実装しない。

`output_pydantic_model` を指定した場合、wrapper は `model_json_schema()` から一時 JSON Schema file を生成し、それを `output_schema_path` と同様に SDK turn と prompt context に渡す。さらに final response に対して `model_validate_json()` を走らせ、失敗した場合は `output_validator` より前に validation failure として扱う。`output_schema_path` と `output_pydantic_model` の同時指定はできない。

`output_validator` は `response.content` を stdin に受け取る command として実行される。引数が必要な場合は shell 文字列ではなく argv list で渡す。

```python
response = client.run(
    input="結果を JSON で返してください",
    output_schema_path="schemas/result.schema.json",
    output_validator=["python3", "scripts/validate_result.py"],
    repair_validation_failures=True,
    max_repairs=1,
    review=ReviewConfig(
        instructions="元の依頼と最終成果物が一致しているか確認し、問題があれば修正指示を返してください。",
    ),
)
```

validator が non-zero で終了した場合は `CodexValidationError` になる。`repair_validation_failures=True` の場合、wrapper は同じ model と同じ `${SUBCWD}` で repair attempt を行い、成功した最終応答だけを返す。さらに `review=ReviewConfig(...)` を渡すと、validator 成功後に review session を 1 turn 実行し、review fail 時も repair prompt に修正指示を流し込む。詳細契約は `validation-repair-spec.md` を参照する。

`CodexResponse.usage` は `dict[str, int] | None` で、SDK が返した `turn.completed.usage` を保持する。repair が発生した job では attempt ごとに合算した値を返す。

`CodexResponse.argv` は最終的に成功した main attempt の argv を保持する。`CodexResponse.activity_log` は wrapper が積んだ phase log で、main attempt / validator / review / repair / file collection / cleanup / timeout / cancel の start/end/error を順に読める。各 entry には `phase`、`event`、`message`、必要に応じて `attempt`、`repair_index`、`argv`、`returncode`、`stdout`、`stderr` が入る。

package install なしで script から使う場合は、次を設定する。

```bash
PYTHONPATH="$CODEX_HOME/skills/codex-job-runner"
```

## 権限

wrapper は `-c` override で job ごとの Permission Profile を組み立てる。

```bash
-c 'default_permissions="<job-profile>"'
-c 'permissions.<job-profile>.filesystem={":minimal"="read",":workspace_roots"={...}}'
```

`read_paths` は `read`、`write_paths` は `write` になる。wrapper は `danger-full-access` を公開しない。

`write_paths` は同じ path の read access も含む。`${SUBCWD}` もこの扱いに従う。

`subcwd_files` がある job では、wrapper は `${SUBCWD}` を writable path に含める。worker が staged input を直接編集する用途を想定するためである。実運用では `ignore_global_context=True` を推奨し、広い `read_paths` / `write_paths` は避ける。最も安全なのは job 専用 `cwd` を使う構成である。

`allow_shell_commands=True` の場合、wrapper は sandbox 内の command runner が起動できるように、実行中の Codex runtime directory を内部 read-only path として Permission Profile に追加する。これは `attachments` やユーザー指定の `read_paths` とは別扱いであり、作業対象 data の読み取り許可を広げるものではない。

v1 の `read_paths` / `write_paths` は `cwd` 配下だけを受け取る。wrapper は path を `cwd` からの相対 path に変換し、Permission Profile の `:workspace_roots` に渡す。`cwd` の外側の path は実行前に `ValueError` になる。

旧 `--sandbox` / `sandbox_mode` は wrapper の主経路ではない。Codex CLI は Permission Profile と旧 sandbox 設定を併用しないため、strict な path permission が必要な job では `ignore_global_context=True` を併用して user config の旧 sandbox 設定の影響を減らす。

Permission Profile は Codex CLI の sandbox runtime が機能していることを前提にする。Linux / WSL2 では sandbox runtime が失敗すると、許可した `write_paths` への書き込みも失敗しうる。この wrapper はその状態を bypass せず、`stderr` と error として呼び出し側へ返す。

## エラー

- `CodexExecutionError`: Codex job attempt が non-zero で終了した。
- `CodexTimeoutError`: `result(timeout=...)` が timeout し、process が停止された。
- `CodexCancelledError`: `cancel()` 後に `result()` が呼ばれた。
- `CodexValidationError`: Codex job は成功したが、`output_validator` が失敗した。

error は `returncode`、`stdout`、`stderr`、`argv` を持つ。さらに `activity_log` で wrapper 側 phase log を読める。`CodexValidationError` は従来どおり validator / review 専用 field も保持するため、review output parse failure や review subprocess failure でも `codex job communication failed: ...` に潰れず、review 側の stdout / stderr / argv を追える。

## テスト

実行:

```bash
python -m unittest discover -s "$CODEX_HOME/skills/codex-job-runner/tests"
```

unit test は fake SDK を使うため、network access や実際の Codex session は不要である。
