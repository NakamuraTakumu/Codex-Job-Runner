# Jobs

`jobs/` には、`codex_job_runner` で起動する再利用可能な wrapper job を置く。

各 job は正式な agent や subagent ではなく、`CodexClient` subclass、prompt、schema をまとめた job template である。

標準構成:

```text
template_review/
  README.md
  __init__.py
  client.py
  prompt.md
  output.schema.json
```

`client.py` では job 専用の `CodexClient` subclass を定義する。subclass の `start(...)` は job 固有の入力を受け取り、最後に `super().start(...)` を呼ぶ。

`run(...)` は `CodexClient.run(...)` を継承して使う。`run(...)` のために job 側で別関数を増やさない。
