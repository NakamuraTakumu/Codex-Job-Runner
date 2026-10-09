# Jobs

`jobs/` には、`codex_job_runner` で起動する再利用可能な wrapper job を置く。

各jobは正式なagentやsubagentではなく、`CodexJob` subclass、prompt、schemaをまとめたjob templateである。

標準構成:

```text
template_review/
  README.md
  __init__.py
  client.py
  prompt.md
  output.schema.json
```

`client.py`ではjob専用の`CodexJob` subclassを定義する。subclassのconstructorはjob固有の入力を受け取り、最後に`super().__init__(...)`を呼ぶ。

`start()`と`run()`は`CodexJob`から継承する。
