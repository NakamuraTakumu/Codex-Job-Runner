# template_review

`template_review` は、wrapper job を作るときの最小テンプレートである。

対象 path を読み、短いレビュー結果を JSON shape で返す。実際の job を作るときは、この directory をコピーし、class 名、prompt、schema、`start(...)` の引数を用途に合わせて変更する。

## Usage

```python
from codex_job_runner import SessionConfig, WorkspaceConfig
from jobs.template_review import TemplateReviewJob

job = TemplateReviewJob(
    config=SessionConfig(
        workspace=WorkspaceConfig(cwd="/path/to/workspace"),
    ),
    target_path="README.md",
)

response = job.run()
print(response.content)
```
