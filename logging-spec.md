# Codex Job Runner Logging Spec

- Created: 2026-06-14
- Status: active

Responsibility: `codex-job-runner` の activity log、file log、stdout progress output の契約を定義する。

## Result

**activity_log**: wrapper が保持する structured log。成功時は `CodexResponse.activity_log`、失敗時は error の `activity_log` で読む。

**log_path**: activity log を人間向け text として書いた file path。成功時は `CodexResponse.log_path`、失敗時は error の `log_path` で読む。

**stdout progress output**: wrapper は activity が発生するたびに `[Phase][Event] ...` 形式の進捗行を process stdout に出す。v1 ではこの stdout progress output を止める public option はない。

stdout progress output では、`attempt=...` と `repair=...` は attempt / turn の start と end 相当の行だけに出す。usage、stream item、validator detail、review detail、repair prompt 要約などの途中行には出さない。

## Log Destination

`CodexClient(log_dir=...)` は job log の既定 directory を指定する。未指定時は resolved `cwd/.codex-job-runner-logs` を使う。

`client.start(..., log_path=...)` または `client.run(..., log_path=...)` は job 単位の log file path を指定する。`log_path` は `log_dir` より優先される。

`log_path` は resolved `cwd` 配下の path として正規化される。

## Activity Entry

`CodexActivity` は次の field を持つ。

| Field | Meaning |
| --- | --- |
| `phase` | `main_attempt`, `stream`, `validator`, `review`, `file_collection`, `cleanup`, `timeout`, `cancel` など。repair attempt は `main_attempt` と `repair_index` で表す。 |
| `event` | `start`, `end`, `error`, `detail`, `usage` など。 |
| `message` | 人間向け summary。 |
| `detail` | 補助分類。 |
| `attempt` | main attempt index。 |
| `repair_index` | repair attempt index。 |
| `argv` | 関連 command または debug argv。 |
| `returncode` | 関連 process の return code。 |
| `stdout` / `stderr` | 関連 process の captured output。 |

## Content

activity log には次を含める。

- main attempt の start / end / error
- SDK stream event の要約
- SDK stream event の `attempt` / `repair_index`
- usage と cost 概算
- validator の start / end / stdout / stderr
- review 対象 response の要約、review input の要約、summary、repair request
- repair prompt の要約、repair attempt の main attempt start / end
- file collection
- cleanup
- timeout / cancel

`repair prompt` と `review input` は全文ではなく、byte size、sha256、preview を持つ要約として記録する。

`log_path` は activity log の保存先であり、SDK の全生ログ、全 stdout、全 prompt を完全保存するものではない。

## Failure

log file の directory 作成または write に失敗しても、job 自体は失敗させない。wrapper は stdout progress output に file log write failure を出す。
