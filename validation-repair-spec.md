# Codex API Wrapper Validation Repair Spec

- Created: 2026-06-02T00:00:00Z
- Updated: 2026-06-03T04:05:59Z
- Repository: none
- Related-Commit: none

Responsibility: `codex_api_wrapper` が `codex exec` の最終出力を validator script と review job で確認し、失敗時に同じ model と同じ `${SUBCWD}` を使って repair attempt を行う仕様を定義する。

## Background

`output_schema_path` は `codex exec --output-schema <FILE>` に渡す model 出力指定であり、wrapper 側での検証結果を保証しない。skill script や job template では、最終応答や生成 file が caller の期待する JSON、schema、domain rule、file set を満たすかを、別の validator script で確認したい場合がある。

validator だけでは、最終応答や `${SUBCWD}` 内成果物が元の依頼や wrapper prompt に本当に従っているかを十分に判断できない場合がある。そのため、validator 成功後に別の Codex review job を追加実行し、review fail 時も repair へ接続できるようにする。

validation failure は、別 model への fallback より先に、同じ model へ前回出力と validator error を渡して修正させる方が自然な場合がある。特に `${SUBCWD}` に作られた file を含む job では、repair attempt が前回 attempt の作業 file を読める必要がある。

## Result

**validator**: `output_validator` で指定する command。`codex exec` が exit code `0` で完了した後、wrapper が最終応答と必要な metadata を渡して実行する。

**validation failure**: validator が non-zero exit code で終了した状態。`codex exec` の失敗とは区別し、`CodexValidationError` として扱う。

**repair attempt**: validation failure の後、同じ model、同じ cwd、同じ permission、同じ `${SUBCWD}` を使い、repair prompt で再度 `codex exec` を起動する attempt。

**review job**: validator 成功後に wrapper が追加で起動する Codex job。`ReviewConfig` に基づき、主 job の `original_input`、主 job に実際に渡した prompt 全体、最終応答、`${SUBCWD}` を見て `passed` / `repair_text` を返す。

**attempt group**: original attempt と repair attempts をまとめた単位。`${SUBCWD}` の lifecycle、file collection、cleanup は attempt group 単位で扱う。

**default behavior**: validator と repair は opt-in とする。`output_validator` がない場合、既存挙動は変えない。`repair_validation_failures=False` の場合、validation failure は repair せず `CodexValidationError` にする。

## API

`client.start(...)` と `client.run(...)` に次の引数を追加する。

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `output_validator` | `str | list[str] | None` | `None` | 最終出力を検証する command。`str` は単一 executable、`list[str]` は argv として扱う。 |
| `validator_timeout` | `int | float | None` | `30` | validator subprocess の timeout 秒数。`None` は無制限。 |
| `repair_validation_failures` | `bool` | `False` | validation failure 時に repair attempt を行う。 |
| `max_repairs` | `int` | `1` | repair attempt の最大回数。`repair_validation_failures=False` では使わない。 |
| `repair_prompt_path` | `str | None` | `None` | repair prompt template file。未指定なら wrapper の default prompt を使う。 |

`output_validator` は shell 文字列として解釈しない。wrapper は `subprocess.run(argv, shell=False, ...)` 相当で実行する。引数が必要な validator は `list[str]` で渡す。

```python
client.run(
    input="結果を JSON で返してください",
    output_validator=["python3", "scripts/validate_result.py", "--strict"],
)
```

`output_validator` の先頭要素が path として解釈できる場合は、`cwd` 相対または absolute path を受け取る。存在しない path は実行前に error にする。先頭要素が `python3` のような command 名だけの場合は PATH lookup に委ねる。

`repair_prompt_path` は存在しない path を実行前に error にする。

`max_repairs` は `0` 以上の整数だけを受け取る。`repair_validation_failures=True` かつ `max_repairs=0` の場合、repair は行わず validation failure を返す。これは retry を一時的に無効化するために許す。

## Validator Contract

validator command は次の契約で呼び出す。

```bash
<output_validator argv...>
```

wrapper は resolved `cwd` を validator subprocess の working directory にする。validator は stdout と stderr に人間向けまたは repair prompt 向けの診断を出す。

### Validator Input Data

wrapper は `CodexResponse.content` 相当の text を stdin に渡す。stdin は envelope ではなく、model の final response text そのものとする。これにより、JSON response を検証する validator は `sys.stdin.read()` をそのまま `json.loads(...)` できる。

wrapper は補助 metadata を environment variable で渡す。

| Env Var | Required | Meaning |
| --- | --- | --- |
| `CODEX_WRAPPER_CWD` | yes | resolved `cwd`。 |
| `CODEX_WRAPPER_ATTEMPT` | yes | original attempt を `0` とする attempt index。repair ごとに増える。 |
| `CODEX_WRAPPER_REPAIR_ATTEMPT` | yes | original attempt では `0`、repair attempt では `1` から始まる repair index。 |
| `CODEX_WRAPPER_OUTPUT_SCHEMA_PATH` | no | `output_schema_path` が指定された場合の resolved path。 |
| `CODEX_WRAPPER_SUBCWD` | no | `${SUBCWD}` が作られた場合の resolved path。validator はこの directory 内の file を読める。 |

`attachments`、`read_paths`、`write_paths` の完全な list は v1 の validator input には含めない。validator が job output file を確認する必要がある場合は、`${SUBCWD}` を使い、`CODEX_WRAPPER_SUBCWD` 配下を読む。

validator は stdin の final response と、必要なら `CODEX_WRAPPER_SUBCWD` 内の file を検証する。`response.files` は attempt group の最終状態が確定した後に回収されるため、validator には渡さない。

exit code の意味は次の通り。

| Exit Code | Meaning |
| --- | --- |
| `0` | validation success。wrapper は response を成功として返す。 |
| non-zero | validation failure。repair 対象または `CodexValidationError` 対象。 |

validator は output を変更しない。validation の副作用を必要とする場合は、その副作用を validator script 側の責務として明示し、wrapper は validation result だけを見る。

## Repair Prompt

repair attempt では、元の task を自由にやり直させるのではなく、前回 attempt の出力を validator error に合うよう修正させる。

default repair prompt は次の情報を含める。

- original input
- attempt number
- validator return code
- validator stdout
- validator stderr
- previous final response content
- `${SUBCWD}` がある場合、その directory を再利用してよいこと
- final output だけを返す指示

概念的な prompt:

```text
前回の出力は validator に失敗しました。
元の task を最初から広げ直すのではなく、validator error を満たすように前回出力と必要な file を修正してください。

Original request:
...

Validator return code:
...

Validator stdout:
...

Validator stderr:
...

Previous final response:
...

Corrected final response only:
```

`${SUBCWD}` がある場合は次を追加する。

```text
前回 attempt が作成した file は同じ ${SUBCWD} に残っています。
必要ならそれらを読んで修正し、生成 file も同じ ${SUBCWD} 配下へ書いてください。
```

`repair_prompt_path` が指定された場合、wrapper はその template を読み、上記情報を埋め込む。template 形式は実装時に単純な `{name}` placeholder 置換に留める。

## Execution Behavior

1. wrapper は original attempt を通常通り起動する。
2. original attempt が non-zero exit の場合、validation と repair は行わず `CodexExecutionError` にする。
3. original attempt が exit code `0` で終了したら、`--output-last-message` の内容を読む。
4. `output_validator` がなければ、既存仕様通り response を返す。
5. validator を実行し、exit code `0` なら response を返す。
6. validator が non-zero の場合、`repair_validation_failures` と `max_repairs` を確認する。
7. repair しない場合、`CodexValidationError` を raise する。
8. repair する場合、同じ model、cwd、attachments、read_paths、write_paths、permission profile、`output_schema_path`、schema context、`output_validator` を使い、repair prompt で次の attempt を起動する。
9. repair attempt の final response に対して validator を再実行する。
10. validation success なら、最終 repair attempt の response を返す。
11. `max_repairs` に達しても validation failure の場合、最後の validation result を持つ `CodexValidationError` を raise する。

`CodexJob.done()` は attempt group が成功、失敗、timeout、cancel のいずれかへ到達したときに `True` になる。individual attempt の終了だけでは `done()` にしない。

## Subcwd Lifecycle

`${SUBCWD}` が使われる場合、wrapper は attempt group 開始時に 1 つだけ subcwd を作る。repair attempt は同じ subcwd を再利用する。

repair attempt は、original attempt が作成した file を読める。`write_paths` は同じ path の read access も含むため、同じ `${SUBCWD}` を `write` permission として渡せばよい。

cleanup は attempt group の最後に一度だけ行う。

| State | Subcwd Handling |
| --- | --- |
| validation success | 必要な file collection 後に削除する。 |
| validation failure without repair | error に必要な情報を保持し、必要な file collection 後に削除する。 |
| repair success | 最終 attempt の file collection 後に削除する。 |
| repair exhausted | error に必要な情報を保持し、必要な file collection 後に削除する。 |
| timeout / cancel | subprocess 停止後に削除を試みる。 |

`collect_files=True` の場合、file collection は attempt group の最終状態が確定した後に一度だけ行う。repair 前に collection して subcwd を消してはいけない。

## Error Shape

`CodexValidationError` は `CodexExecutionError` を継承する。

保持する field:

| Field | Type | Meaning |
| --- | --- | --- |
| `response` | `CodexResponse | None` | validator に失敗した response。 |
| `validator_returncode` | `int | None` | validator subprocess の exit code。timeout では `None`。 |
| `validator_stdout` | `str` | validator stdout。 |
| `validator_stderr` | `str` | validator stderr。 |
| `validator_argv` | `list[str]` | 実行した validator command。 |
| `attempts` | `int` | original attempt を含む実行 attempt 数。 |
| `repairs` | `int` | 実行した repair attempt 数。 |
| `files` | `dict[str, CodexFile]` | `collect_files=True` の場合に回収できた file。 |
| `activity_log` | `list[CodexActivity]` | wrapper 側 phase log。main attempt / validator / review / repair / file collection / cleanup の start/end/error と、その時点の argv / stdout / stderr / returncode を追える。 |

validator timeout は `CodexValidationError` として扱う。`CodexTimeoutError` は `codex exec` attempt 自体の timeout にだけ使う。

review subprocess failure や review output parse failure は `CodexExecutionError` として返す。これらの error も `stdout`、`stderr`、`argv`、`activity_log` を保持するため、review 側の失敗詳細が `codex exec communication failed: ...` に潰れないことを v1 の契約に含める。

## Scope Limits

別 model への fallback はこの仕様に含めない。model fallback は wrapper の外側の orchestration policy として扱う。

repair は validation failure にだけ適用する。`codex exec` の non-zero exit、Popen failure、permission setup failure、attachment path 不在、schema path 不在、validator path 不在には repair を適用しない。

repair は同じ model を使う。caller が別 model で再実行したい場合は、`CodexValidationError` を受け取った後に外側で新しい call を行う。

stdout や JSON event の streaming validation は扱わない。wrapper は `codex exec` subprocess の終了後に `--output-last-message` を読み、それを validator に渡す。

## Open Questions

- validator に `response.files` の内容を渡す標準 method を追加するか。v1 では stdin の `content` と `${SUBCWD}` の read/write 再利用だけで始める。
- `repair_prompt_path` template の placeholder 名を固定するか、default prompt だけを公開して template は後回しにするか。
- validation failure 時に `collect_files=True` なら error.files を必ず回収するか、回収失敗を validation error に統合するか。
