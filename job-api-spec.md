# Codex Job Runner Job API Spec

- Created: 2026-06-14
- Status: active

Responsibility: `CodexClient` と `CodexJob` の public job API、完了判定、timeout、cancel の契約を定義する。

## Public API

| API | Meaning |
| --- | --- |
| `CodexClient.start(...)` | non-blocking に job を起動し、`CodexJob` を返す。 |
| `CodexClient.run(...)` | `start(...).result()` の convenience method。 |
| `CodexClient.wait_any(jobs, timeout=None)` | 複数 job のうち最初に完了した job を返す。 |
| `CodexJob.done()` | job が完了済みか返す。 |
| `CodexJob.result(timeout=None)` | 成功時は `CodexResponse`、失敗時は exception。 |
| `CodexJob.cancel()` | 実行中 job を cancel する。 |

上位 orchestration、task graph、worker pool、一般 retry policy、ledger、結果統合はこの wrapper の責務に含めない。

## Start

`start(...)` は job を起動してすぐ `CodexJob` を返す。main attempt、validator、review、repair、cleanup は job group として扱う。

`start(...)` の入力契約は `spec.md` を正本にし、機能別の詳細は各 feature spec に分ける。

## Run

`run(...)` は同期利用の convenience method である。

```python
def run(...):
    return self.start(...).result()
```

## Completion

`done()` は job group 全体が完了したときに `True` になる。main attempt だけが終了しても、validator、review、repair、file collection、cleanup が残っている場合は完了ではない。

`status` は次のいずれかである。

| Status | Meaning |
| --- | --- |
| `running` | job group が実行中。 |
| `completed` | job group が成功した。 |
| `failed` | job group が失敗した。 |
| `timeout` | `result(timeout=...)` により timeout 処理された。 |
| `cancelled` | `cancel()` により停止された。 |

stdout の文字列、stream event、output file の存在は完了判定に使わない。

## Timeout

`CodexClient(timeout=...)` または `start(timeout=...)` の timeout は、`job.result(timeout=None)` が使う既定 timeout である。

`job.result(timeout=<seconds>)` に明示値を渡した場合、その値が待機 timeout として優先される。

timeout した場合、wrapper は実行中 process の停止を試み、`CodexTimeoutError` を raise する。

## Cancel

`job.cancel()` は実行中 process の停止を試み、job status を `cancelled` にする。

cancel 後に `job.result()` を呼ぶと `CodexCancelledError` を raise する。

## Wait Any

`wait_any(jobs, timeout=None)` は、渡された job collection のうち最初に `done()` になった job を返す。

timeout までに完了 job がない場合は `None` を返す。
