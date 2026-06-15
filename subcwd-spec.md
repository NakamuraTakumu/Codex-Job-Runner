# Codex API Wrapper Subcwd Spec

- Created: 2026-06-01T10:31:37Z
- Updated: 2026-06-01T10:57:26Z
- Repository: none
- Related-Commit: none

Responsibility: `codex_api_wrapper` の job ごとに衝突しない workspace directory を作り、wrapper-managed input staging と file collection を含む `${SUBCWD}` 仕様を定義する。

## Background

`response.content` で返せるのは小さい text 出力に限られる。複数 file、大きい text、binary、途中失敗時に確認したい成果物は filesystem 経由で扱う必要がある。

共有された固定 output directory を使うと、並列 job や再実行で file 名が衝突しやすい。さらに、worker に入力 file のコピーを直接編集させたい場合は、job ごとに独立した workspace が必要になる。そのため、wrapper が job ごとに一意な workspace directory を `subcwd_root` 配下へ作り、`read_paths` / `write_paths` と `subcwd_files` から実 path ではなく placeholder で参照できるようにする。

## Result

**subcwd**: job ごとの workspace directory。`client.start(...)` の resolved `subcwd_root` 配下に UUID 名の directory として作る。`subcwd_root` 未指定時は resolved `cwd` を使う。

**作成条件**: `read_paths` または `write_paths` に `${SUBCWD}/...` が含まれる場合、または `subcwd_files` が非空の場合に作る。

**placeholder**: public API では `${SUBCWD}` を使う。利用者は UUID directory 名を組み立てない。

**削除条件**: job が `completed`、`failed`、`timeout`、`cancelled` のいずれかへ遷移し、wrapper が必要な読み取りと file collection を終えた後に削除する。

**scope**: `subcwd` は wrapper が事前投入した入力と worker の生成物を両方含む一時 workspace である。永続化が必要な file は `collect_files=True` で `response.files` へ回収する。

## API

`write_paths` は次の形を受け取る。

| Value | Meaning |
| --- | --- |
| `"${SUBCWD}/relative/path"` | job 専用 `subcwd` 内の相対 path への write permission。 |
| その他の path | 既存仕様通り、`cwd` 相対または absolute path として扱う。 |

`read_paths` も次の形を受け取る。

| Value | Meaning |
| --- | --- |
| `"${SUBCWD}/relative/path"` | job 専用 `subcwd` 内の相対 path への read permission。 |
| その他の path | 既存仕様通り、`cwd` 相対または absolute path として扱う。 |

`subcwd_files` は次の形を受け取る。

| Value | Meaning |
| --- | --- |
| `{"path/in/subcwd.txt": {"text": "..."}}` | UTF-8 text などの text file を staged する。 |
| `{"path/in/subcwd.bin": {"bytes": b"..."}}` | binary file を staged する。 |

bare な `${SUBCWD}` は `read_paths` / `write_paths` / `attachments` のいずれでも受け取らない。`${SUBCWD}` の staged input は wrapper が専用 API と prompt 通知で扱うが、実際の read/write 権限は `${SUBCWD}/...` を明示した path だけに限定する。

`CodexClient(..., subcwd_root=...)` は `${SUBCWD}` を使う job の親ディレクトリを指定する。`cwd` 相対または absolute path を受け付けるが、resolved `cwd` 配下のみ許可する。`${SUBCWD}` token は受け付けない。`start(...)` と `run(...)` に per-call override は追加しない。

`CodexJob` と `CodexResponse` は UUID directory 名を通常の属性として公開しない。debug 用の `argv` や prompt には、Codex CLI に実 path を渡す必要があるため、完全な秘匿は保証しない。

`client.start(...)` と `client.run(...)` は file collection 用に次を受け取る。

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `collect_files` | `bool` | `False` | job 終了後、`subcwd` 削除前に通常 file を memory 上へ回収し、`response.files` に入れる。 |
| `max_files` | `int` | `10000` | 回収する file 数上限。 |
| `max_file_bytes` | `int` | `1073741824` | 1 file あたりの byte 上限。既定は 1 GiB。 |
| `max_total_file_bytes` | `int` | `4294967296` | 回収 file 合計 byte 上限。既定は 4 GiB。 |

上限は通常の小さな成果物では当たらず、明らかに危険な巨大出力や暴走だけを止めるための既定値とする。上限超過時は cleanup 前に `CodexExecutionError` または dedicated collection error を raise する候補にするが、v1 実装では wrapper 固有 error を増やさず `CodexExecutionError` でよい。

`collect_files=True` でも `subcwd` が作られない job では `response.files` は空 dict とする。

## Execution Behavior

1. `client.start(...)` は `cwd` を解決する。
2. wrapper は `subcwd_root` を解決する。未指定時は `cwd` を使う。
3. `read_paths` または `write_paths` に `${SUBCWD}/...` placeholder がある場合、または `subcwd_files` が非空の場合、`subcwd_root / <uuid>` を作成する。`subcwd_root` の親ディレクトリが未作成なら wrapper が作成する。
4. `subcwd_files` がある場合、wrapper は subprocess 起動前に file 実体を `${SUBCWD}` へ書き出す。
5. wrapper は `read_paths` / `write_paths` 内の `${SUBCWD}/...` を実 path に展開し、Permission Profile の `read` / `write` entry に入れる。`subcwd_files` がある job でも `${SUBCWD}` 権限は自動付与しない。staged file は少なくとも 1 つの `read_paths` または `write_paths` で被覆されていなければならない。
6. wrapper は prompt に `${SUBCWD}` の path と staged file 一覧を明記する。file 内容は inline しない。
7. main attempt 終了後、wrapper は SDK turn の final response から `response.content` を作る。
8. `collect_files=True` なら、wrapper は `subcwd` 配下の通常 file を memory 上へ回収し、relative path keyed の `response.files` を作る。
9. wrapper は `subcwd` を削除する。削除は成功、失敗、timeout、cancel のすべてで試みる。親の `subcwd_root` 自体は削除しない。

`subcwd` の UUID は `uuid.uuid4().hex` で作る。directory 名は UUID だけにし、利用者に意味を持つ prefix や task 名を含めない。

## Cleanup and Failure

`subcwd` 削除に失敗した場合、primary result は失敗させない。削除失敗は debug 情報として保持する候補にするが、v1 では最低限 stderr へ追記する実装でよい。

file collection は通常 file だけを対象にする。directory は再帰 traversal の対象であり、`response.files` には入れない。staged input file も最終状態に存在すれば回収対象に含む。symlink、device file、socket、FIFO は回収しない。symlink はリンク先が `subcwd` 内でも拒否する。

`response.files` は relative POSIX path を key にした dict とする。path は `subcwd` からの相対 path であり、absolute path、`..`、空 path は許さない。

回収された file は memory 上の bytes として保持する。

```python
response.files["report.md"].bytes
response.files["report.md"].text
response.files["data.json"].json()
```

`.text` は UTF-8 decode を行う。decode できない場合は `UnicodeDecodeError` を raise してよい。`.json()` は `.text` を JSON parse する convenience method とする。

`subcwd` 作成前に同名 directory が存在した場合は、別 UUID で再試行する。一定回数失敗した場合は `FileExistsError` または wrapper 固有 error を raise する。

## Prompt Contract

placeholder を使った job では、wrapper が prompt に次の内容を追加する。

```text
Temporary output directory:
${SUBCWD} = <resolved-subcwd-path>

Create output files only under this directory.
This directory name is temporary; do not mention it in the final response unless necessary.
```

`subcwd_files` がある場合は、さらに `${SUBCWD}` 内の staged file 一覧を追加し、「事前配置済みだが、読んだり編集したりできるのは明示的に許可された path だけ」と明記する。

UUID を外部にできるだけ出さないため、利用者向け API、通常の `response.content`、README の利用例では `${SUBCWD}` を使う。実 path は permission、prompt、debug 情報にだけ現れる。

## Cleanup Failure

subcwd cleanup が失敗した場合、wrapper は専用の cleanup warning field を追加しない。`stderr` に追記し、`activity_log` に cleanup error として記録する。
