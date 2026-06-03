# Codex API Wrapper Spec

- Created: 2026-06-01T09:07:35Z
- Updated: 2026-06-03T12:00:00Z
- Model: GPT-5.5
- Reasoning-Effort: high
- Session: current Codex Desktop thread
- Repository: none
- Related-Commit: none

Responsibility: skill、workflow、hook、script の設計または実装時に、`codex exec` を Python から job として起動する最小 wrapper の入力、設定、完了検知、出力、失敗時の扱いを定義する。

## Background

ユーザーは、OpenAI API 完全互換ではなく、`codex` command を Python から API 風に呼べる薄い wrapper を求めている。主な利用場面は、skill、workflow、hook、script を設計または実装するときに、別プロセスの Codex job を起動して結果を回収することである。作業場所は `skills/` 直下の `codex-job-runner/` とする。

## Result

**Public calls**: `client.start(...)` は non-blocking に job を起動する。`job.done()`, `job.result()`, `job.cancel()`, `client.wait_any(...)` で完了済み job から回収する。`client.run(...)` は `start(...).result()` の convenience method とする。

**Primary input**: `input` は Codex への依頼文であり、OpenAI Chat Completions の `messages` 形式は使わない。

**Context inputs**: `attachments` は model の初期 context に入れる file または directory path、`read_paths` は追加で読んでよい path、`write_paths` は書いてよい path として分ける。`output_schema_path` を指定した場合、schema file は Codex CLI の structured output 制約として渡し、同時に model の初期 context にも入れる。

**Execution settings**: `model`, `reasoning_effort`, `cwd`, `timeout`, `ignore_global_context`, `allowed_skills`, `allow_shell_commands` は client default と per-call override の両方で指定できる。filesystem 権限は `read_paths`, `write_paths` から Permission Profile を組み立てて決める。shell command 実行は既定で on にし、Permission Profile の filesystem sandbox に従わせる。

**Output**: 成功時は `CodexResponse.content` を主出力にし、CLI が返した usage は `CodexResponse.usage` に入れる。`CodexResponse.argv` は最終 main attempt の argv、`CodexResponse.activity_log` は wrapper 側 phase log を保持する。失敗時は `CodexExecutionError` または `CodexTimeoutError` を raise し、終了 code、stdout、stderr、argv、activity log を保持する。

**Skill role**: この directory は Codex skill としても使う。`SKILL.md` は使用場面と参照導線、`README.md` は実用 API、`spec.md` は設計契約を担う。

## Detail

### Basic Usage

```python
from codex_api_wrapper import CodexClient

client = CodexClient(
    cwd="/home/nakamura/recruit",
    timeout=300,
)

job = client.start(
    input="このコードを説明して",
    attachments=[
        "/home/nakamura/recruit/src/main.py",
        "/home/nakamura/recruit/README.md",
    ],
    read_paths=[
        "/home/nakamura/recruit/src",
        "/home/nakamura/recruit/pyproject.toml",
    ],
    write_paths=[
        "/home/nakamura/recruit/tmp/codex-output",
    ],
)

response = job.result(timeout=300)
print(response.content)
```

script から local package として import する場合は、次のように skill directory を `PYTHONPATH` に入れる。

```bash
PYTHONPATH="$CODEX_HOME/skills/codex-job-runner" python path/to/script.py
```

同期的に使いたい場合だけ、`client.run(...)` を使う。

```python
response = client.run(input="README を要約して", attachments=["README.md"])
print(response.content)
```

### Parallel Usage

複数 job は同時に起動し、完了した job から順に回収できる。

```python
jobs = {
    client.start(input="a.py を調べて", attachments=["a.py"]),
    client.start(input="b.py を調べて", attachments=["b.py"]),
}

while jobs:
    job = client.wait_any(jobs, timeout=10)
    if job is None:
        continue

    jobs.remove(job)
    response = job.result()
    print(response.content)
```

`wait_any(...)` は、指定した job のうち 1 つでも完了したら返る。全 job の完了を待たない。

### Request Input

`client.start(...)` と `client.run(...)` は次の依頼入力を受け取る。

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `input` | `str` | yes | Codex に実行させる依頼文。 |
| `attachments` | `list[str]` | no | この依頼で特に見てほしい file または directory path。 |
| `read_paths` | `list[str]` | no | Codex が読んでよい file または directory path。 |
| `write_paths` | `list[str]` | no | Codex が書いてよい file または directory path。 |
| `subcwd_files` | `dict[str, dict[str, str \| bytes]]` | no | `${SUBCWD}` workspace に事前配置する file 実体。 |
| `output_schema_path` | `str` | no | Codex の最終応答 shape を制約し、初期 context にも入れる JSON Schema file path。 |
| `output_validator` | `str | list[str]` | no | `CodexResponse.content` を検証する command。 |
| `review` | `ReviewConfig` | no | validator 成功後に追加で走らせる review job の設定。 |
| `inline_attachments` | `bool | None` | no | `attachments` の通常 file 内容を prompt に入れるか。既定と `None` は `True`。 |
| `max_inline_attachment_bytes` | `int` | no | inline する 1 file あたりの byte 上限。 |
| `max_inline_attachment_total_bytes` | `int` | no | inline する attachment 合計 byte 上限。 |

`input`: そのまま Codex への主依頼として扱う。空文字列は error にする。

`attachments`: model の初期 context に入れる file または directory path。存在しない path は実行前に error にする。通常 file は既定で path と本文を prompt に inline する。directory attachment は inline せず path だけ列挙する。通常 file の本文を入れない場合だけ `inline_attachments=False` を指定する。

`read_paths`: 読み取りを許可する範囲として prompt に列挙し、**Command Permissions** の規則で Codex CLI の Permission Profile に反映する。`read_paths` は content injection ではない。初期 context として内容を渡す file は `attachments` に入れる。

`write_paths`: 書き込みを許可する範囲として prompt に列挙し、**Command Permissions** の規則で Codex CLI の Permission Profile に反映する。write 許可は同じ path の read 許可も含むものとして扱う。

`subcwd_files`: `${SUBCWD}` 内の相対 POSIX path を key にし、value は `{"text": "..."}` または `{"bytes": b"..."}` とする。`text` を使う場合だけ `encoding` を任意で指定でき、省略時は `utf-8` とする。wrapper は job 開始前に `${SUBCWD}` へ file を書き出し、worker はその file を直接編集してよい。`attachments` と `read_paths` で `${SUBCWD}` を受ける代わりに、wrapper が staged file 一覧を prompt に通知する。

`output_schema_path`: `codex exec --output-schema <file>` に渡す JSON Schema file。`cwd` 相対または absolute path を受け取る。存在しない path は実行前に error にする。schema file の本文は `<OutputSchema path="...">...</OutputSchema>` として prompt の `Output schema` section にも入れる。schema は UTF-8 text の通常 file とし、inline attachment と同じ 1 file byte 上限に従う。schema 内容の validation は Codex CLI 側へ委ね、wrapper は JSON Schema validator を実装しない。

`output_validator`: wrapper 側で最終応答を検証する command。`str` は単一 executable、`list[str]` は argv として扱い、shell 文字列としては解釈しない。validator と validation failure repair の詳細は `validation-repair-spec.md` を正本とする。

`review`: `ReviewConfig` object。review 関係だけは個別引数を増やさず object にまとめる。review job は validator 成功後にだけ走り、`original_input`、主 job に実際に渡した wrapper prompt 全体、最終応答、`${SUBCWD}` を読んで指示準拠と成果物品質を判定する。review fail 時は `repair_text` を既存 repair prompt に統合する。

### Execution Settings

`CodexClient(...)`, `client.start(...)`, `client.run(...)` は次の実行設定を受け取る。per-call に指定した値は client default を上書きする。

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `codex_bin` | `str` | `"codex"` | 実行する Codex CLI command。client default のみ。 |
| `model` | `str | None` | `"gpt-5.4-mini"` | `codex exec --model <model>` に渡す model 名。 |
| `reasoning_effort` | `str | None` | `"medium"` | Codex CLI の `model_reasoning_effort` config に渡す thinking / reasoning effort。 |
| `cwd` | `str | None` | process cwd | `codex exec -C <cwd>` に渡す作業 directory。 |
| `subcwd_root` | `str | None` | `None` | `${SUBCWD}` を使う job の一時 directory 親 path。client default のみ。未指定時は resolved `cwd` を使う。 |
| `timeout` | `int | float | None` | `None` | subprocess の最大待ち時間。秒単位。 |
| `ignore_global_context` | `bool` | `False` | global な config、`AGENTS.md`、skills の影響を避ける best-effort mode。 |
| `allowed_skills` | `list[str] | None` | `None` | prompt 上で利用可能にする skill 名。`[]` は skill instructions を出さない。 |
| `allow_shell_commands` | `bool` | `True` | Codex job 内で shell tool を使わせるか。既定では Permission Profile の filesystem sandbox 内で shell command 実行を許可する。 |

設定の優先順位は次の通り。

1. `client.start(...)` または `client.run(...)` の per-call setting。
2. `CodexClient(...)` の client default。
3. client default を明示的に `None` にした場合だけ、Codex CLI または `~/.codex/config.toml` の既定値。

wrapper の `model` 既定値は `gpt-5.4-mini` である。client または per-call で別値を指定した場合だけ上書きする。Codex CLI または user config の既定値へ委譲したい場合は、`CodexClient(model=None, ...)` のように client default を `None` にする。

`reasoning_effort` は `codex exec -c 'model_reasoning_effort="<value>"'` として渡す。wrapper 既定値は `medium` であり、client または per-call で別値を指定した場合だけ上書きする。Codex CLI または user config の既定値へ委譲したい場合は、`CodexClient(reasoning_effort=None, ...)` のように client default を `None` にする。wrapper は値の enum を固定せず、空文字列だけを実行前に `ValueError` にする。

`subcwd_root` は `${SUBCWD}` を使う job の一時 directory 親 path を指定する client default である。`cwd` 相対または absolute path を受け付けるが、resolved `cwd` 配下でなければならない。`${SUBCWD}` token は受け付けない。`subcwd_root=None` の場合は resolved `cwd` を使う。per-call override は v1 では提供しない。

`subcwd_files` は per-call だけで指定できる。client default にはしない。`subcwd_files` が非空なら、`write_paths` に `${SUBCWD}` がなくても wrapper は `${SUBCWD}` workspace を作り、Permission Profile の writable path に `${SUBCWD}` を追加する。

`allow_shell_commands=True` が既定値である。shell command は Permission Profile の filesystem sandbox 内で実行され、`read_paths` / `write_paths` の境界に従う。shell command を使わせない job だけ、client または per-call で `allow_shell_commands=False` を明示する。

`allow_shell_commands=False` の場合、wrapper は `-c 'features.shell_tool=false'` と `-c 'allow_login_shell=false'` を渡す。これは file edit 以外の shell command 実行を基本的に使わせないための mode である。

Codex CLI の file read は実質的に shell command に依存する。wrapper は `attachments` を shell availability から独立した初期 context として扱うため、通常 file attachments は既定で prompt に inline する。大きい file、binary file、directory tree の読解が必要な job では、必要 file を小さく分けて attachments に渡すか、`read_paths` と shell access で追加読解させる。

v1 は command prefix ごとの allowlist / denylist を public API に含めない。command 実行の粒度は `allow_shell_commands` の on/off だけにする。コマンド単位の制御が必要な運用では、Codex CLI の rules 設定を wrapper の外側で管理する。

`ignore_global_context=True` の場合、wrapper は次の処理を行う。

- `codex exec --ignore-user-config` を付け、`$CODEX_HOME/config.toml` を読ませない。
- `codex exec -c 'project_doc_max_bytes=0'` を付け、project `AGENTS.md` の prompt 注入を抑える。
- `allowed_skills` が未指定なら `[]` として扱い、`codex exec -c 'skills.include_instructions=false'` を付ける。
- prompt に、global `AGENTS.md` と skills を使わず、必要な指示は `attachments` から読むよう明記する。
- 認証情報と既存 install 状態を保つため、`CODEX_HOME` は差し替えない。

strict な `${SUBCWD}` 隔離が必要な job では `ignore_global_context=True` を推奨する。共有 `cwd` でも実運用は可能だが、最も安全なのは job 専用 `cwd` を使い、`read_paths` / `write_paths` を狭く保つ構成である。

`ignore_global_context=True` は `--ignore-rules` を含まない。`.rules` は `AGENTS.md` や skills と異なり、exec policy として command execution の安全制御に関わるため、既定では維持する。

Codex CLI には global `AGENTS.md` を直接無効化する documented option がないため、`ignore_global_context=True` は global `AGENTS.md` の完全遮断を保証しない。wrapper は一時 `CODEX_HOME` を使わない。

`allowed_skills` の扱いは次の通り。

| Value | Behavior |
| --- | --- |
| `None` | skill filtering を明示しない。`ignore_global_context=True` では `[]` として扱う。 |
| `[]` | `-c 'skills.include_instructions=false'` を付け、Available skills block を prompt に出さない。 |
| `["name", ...]` | prompt 上の Available skills を指定名へ絞る best-effort mode。 |

`allowed_skills` が非空の場合、wrapper は Codex の skill config override を組み立てる。基本形は `-c 'skills.bundled.enabled=false'` と `-c 'skills.config=[{name="...",enabled=false}, ...]'` で、発見済み skill のうち `allowed_skills` に含まれないものを無効化する。これは prompt 上の Available skills を制限するものであり、filesystem 上の skill file を必ず読めなくするものではない。

### Command Permissions

wrapper は `approval_policy` を public API に出さない。非対話 job が承認待ちで停止しないよう、実装では `codex --ask-for-approval never exec ...` を使う。`--ask-for-approval` は `exec` subcommand ではなく top-level `codex` option なので、`exec` より前に置く。

`--ask-for-approval never` は「承認 prompt で停止しない」という指定であり、filesystem や shell command を広く許可する指定ではない。実際の filesystem 境界は Permission Profile で作り、shell command 自体を使わせない job では追加で `features.shell_tool=false` を使う。

command 実行時の file access 権限は Permission Profile で制御する。wrapper は global config を編集せず、job ごとに一意な profile 名を作り、`codex exec -c ...` の inline config override で直接指定する。

| Input | Permission Profile mode |
| --- | --- |
| `read_paths` | `read` |
| `write_paths` | `write` |

概念的な CLI option は次の通り。

```bash
codex --ask-for-approval never exec \
  -c 'default_permissions="<job-profile>"' \
  -c 'permissions.<job-profile>.filesystem={...}' \
  -
```

`permissions.<job-profile>.filesystem` には、`read_paths`, `write_paths` を path ごとの access mode として入れる。profile 名は job ごとに衝突しない名前にする。

実装は `cwd` 配下の path だけを v1 の Permission Profile 対象にする。`read_paths` と `write_paths` は resolved `cwd` からの相対 path に変換し、`:workspace_roots` の nested subpath map として渡す。

```bash
-c 'permissions.<job-profile>.filesystem={":minimal"="read",":workspace_roots"={"docs"="read","out"="write"}}'
```

`cwd` の外側を指す path は、v1 では実行前に `ValueError` にする。Codex CLI の Permission Profile は direct absolute path rule も持つが、write rule は runtime によって拒否されうるため、この wrapper の最小仕様では扱わない。

`allow_shell_commands=True` の場合、sandbox 内の command runner が起動できるように、wrapper は `codex_bin` から解決した Codex runtime directory を direct absolute read rule として Permission Profile に追加する。この内部 read rule は Codex CLI runtime のためだけに使い、ユーザー指定の `read_paths` / `write_paths` とは別に扱う。

同じ path が `read_paths` と `write_paths` の両方にある場合は `write` として扱う。v1 は whitelist 方式とし、指定されていない path は読まず書かない前提にする。明示的な deny list は public API に含めない。

`ignore_global_context=True` または `allowed_skills=[]` の場合、wrapper は Permission Profile に `$CODEX_HOME/skills` を追加しない。これにより、model が shell command で global skill file を直接読む経路も許可しない方針にする。

wrapper は Permission Profile mode では `--sandbox` と `--add-dir` を使わない。legacy sandbox option は profile より粗く、`cwd` や workspace 全体を書き込み可能にしうるため、v1 の権限制御の主経路にしない。

Codex CLI 側では Permission Profile と旧 `sandbox_mode` / `--sandbox` は併用しない。active config layer に `sandbox_mode` がある、または command line で `--sandbox` を渡した場合、CLI は Permission Profile ではなく旧 sandbox 設定を使う。この wrapper は `--ignore-user-config` を指定しない通常 mode では、ユーザー config の旧 sandbox 設定に影響されうる。strict な path permission が必要な job では `ignore_global_context=True` を併用する。

Permission Profile の read/write 制約は Codex CLI の sandbox runtime が実際に機能していることを前提にする。Linux / WSL2 では Codex sandbox が `bubblewrap` または user namespace 作成に依存するため、runtime が失敗すると許可済み path の write も失敗しうる。この場合、`danger-full-access` では write が成功しても、Permission Profile や `workspace-write` では失敗する。wrapper はこの状態を bypass せず、`stderr` と error として呼び出し側へ返す。

Permission Profile を使えない Codex CLI version では、wrapper は strict path permission を提供しない。fallback として legacy `--sandbox read-only` だけを使うか、起動前に明示 error にする。v1 の既定は、Permission Profile が使えない場合は error にする。

`danger-full-access` は public API に出さない。sandbox なしに近い実行が必要な場合は、この wrapper ではなく Codex CLI を直接使う。

### Job API

`client.start(...)` は `codex exec` subprocess を起動して、すぐ `CodexJob` を返す。呼び出し側 thread を Codex の完了まで block しない。

`CodexJob` は次を提供する。

| Method / Field | Meaning |
| --- | --- |
| `job.id` | wrapper が発行する job identifier。 |
| `job.status` | `"running"`, `"completed"`, `"failed"`, `"timeout"`, `"cancelled"` のいずれか。 |
| `job.done()` | subprocess が終了済みなら `True`、実行中なら `False`。 |
| `job.result(timeout=None)` | job の完了を待ち、成功なら `CodexResponse` を返す。失敗なら exception を raise する。 |
| `job.cancel()` | 実行中 subprocess を terminate し、必要なら kill する。 |

`client.wait_any(jobs, timeout=None)` は、渡された job collection のうち最初に完了した `CodexJob` を返す。`timeout` までにどれも完了しなければ `None` を返す。

`client.run(...)` は convenience method として扱う。

```python
def run(...):
    return self.start(...).result()
```

上位 orchestration は `start(...)`, `done()`, `result()`, `cancel()`, `wait_any(...)` だけを使って構築する。task graph、retry、依存関係、結果統合、ledger、worker pool はこの wrapper の責務に含めない。

### Completion Detection

wrapper は `codex exec` subprocess の終了を job completion として扱う。

- subprocess が終了していない場合、`job.status` は `"running"`。
- subprocess が exit code `0` で終了した場合、`job.status` は `"completed"`。
- subprocess が non-zero exit code で終了した場合、`job.status` は `"failed"`。
- `job.result(timeout=...)` の timeout を超えた場合、wrapper は subprocess を止め、`job.status` を `"timeout"` にする。
- `job.cancel()` が呼ばれた場合、wrapper は subprocess を止め、`job.status` を `"cancelled"` にする。

wrapper は stdout の文字列、JSON event、または output file の存在を完了判定に使わない。

### Output

成功時は `CodexResponse` を返す。

| Field | Type | Meaning |
| --- | --- | --- |
| `content` | `str` | Codex の最終応答。通常利用ではこの field だけ読む。 |
| `returncode` | `int` | `codex exec` の終了 code。成功時は `0`。 |
| `stdout` | `str` | `codex exec` の stdout。debug 用。 |
| `stderr` | `str` | `codex exec` の stderr。debug 用。 |
| `usage` | `dict[str, int] \| None` | `codex exec --json` の `turn.completed.usage`。repair を含む job 全体で合算する。 |

### Output Extraction

wrapper は `codex exec --output-last-message <tempfile>` と `--json` を併用して Codex の最終応答と usage を取り出す。`stdout` は進捗、event、warning、JSON event が混ざる可能性があるため、主出力 `content` の source of truth として扱わない。

`ignore_global_context=True` かつ `allowed_skills=[]` の場合、実行時の概念的な command は次の通り。

```bash
codex --ask-for-approval never exec \
  --json \
  --output-last-message <tempfile> \
  --ignore-user-config \
  -c 'model_reasoning_effort="<value>"' \
  -c 'project_doc_max_bytes=0' \
  -c 'skills.include_instructions=false' \
  -c 'default_permissions="<job-profile>"' \
  -c 'permissions.<job-profile>.filesystem={...}' \
  -C <cwd> \
  -
```

wrapper は prompt を stdin に渡し、`codex exec` 終了後に `<tempfile>` を UTF-8 text として読む。読み取った文字列を `CodexResponse.content` に入れる。同時に stdout の JSON event から `turn.completed.usage` を回収し、`CodexResponse.usage` に入れる。

`CodexResponse.stdout` と `CodexResponse.stderr` は subprocess の captured output をそのまま保持する。これらは debugging と error reporting 用であり、通常の呼び出し側は `content` と必要なら `usage` を読む。

### Output Schema And Validation

Codex CLI が対応する `--output-schema <FILE>` を使い、最終応答の shape を JSON Schema file で指定できる。wrapper は同じ schema file の本文を prompt の `Output schema` section にも入れ、model が schema 本文を見て応答を組み立てられるようにする。

```python
response = client.run(
    input="結果を JSON で返してください",
    output_schema_path="schemas/result.schema.json",
)
print(response.content)
```

`output_schema_path` は final response、つまり `CodexResponse.content` に対応する。`subcwd` や `response.files` に回収される file の schema validation は別責務とし、この option では扱わない。

wrapper は schema file を生成しない。schema を別 file として管理し、path と本文を渡す。Python 標準 library だけでは JSON Schema validation を実装しないため、`output_schema_path` 自体では wrapper 側 parse / validate を行わず、機械的な shape 制約は `codex exec --output-schema` に委ねる。

wrapper 側 validation が必要な場合は `output_validator` を使う。wrapper は `CodexResponse.content` を validator の stdin に渡し、metadata を environment variable で渡す。validator が non-zero で終了した場合は `CodexValidationError` を raise する。`repair_validation_failures=True` の場合、wrapper は same-model repair attempt を `max_repairs` まで行う。

最小利用では次だけを使う。

```python
response = client.run(input="README を要約して", attachments=["README.md"])
print(response.content)
```

### Failure Output

`codex exec` が失敗した場合は `CodexExecutionError` を raise する。`job.result(timeout=...)` が timeout した場合は `CodexTimeoutError` を raise する。`job.cancel()` 後に `job.result()` を呼んだ場合は `CodexCancelledError` を raise する。

| Field | Type | Meaning |
| --- | --- | --- |
| `returncode` | `int | None` | subprocess の終了 code。timeout の場合は `None`。 |
| `stdout` | `str` | 失敗時点の stdout。 |
| `stderr` | `str` | 失敗時点の stderr。 |
| `argv` | `list[str]` | 実行した `codex exec` command。 |
| `message` | `str` | 人間向けの短い失敗説明。 |

### Explicitly Out Of Scope

v1 では次を扱わない。

- OpenAI SDK 完全互換。
- HTTP server。
- `messages=[{"role": ..., "content": ...}]` 形式。
- `choices[0].message.content` 形式。
- streaming。
- tool calling。
- image/audio content parts。
- `temperature`, `top_p`, `max_tokens` などの sampling parameter。
- 添付 directory tree や大きな file の自動 prompt 展開。
- shell command prefix ごとの allowlist / denylist。

### Attachment And Path Policy

`attachments` と `read_paths` は統合しない。`attachments` は今回の依頼で注目してほしい対象、`read_paths` は読んでよい範囲を表す。分けることで、広い読み取り許可を与えつつ、prompt context に載せる対象を最小化できる。

v1 は `read_paths` と `write_paths` だけで権限境界を表す。秘密情報や除外したい path がある場合は、親 directory を広く `read_paths` に入れず、必要な file または directory だけを列挙する。

## References

なし。
