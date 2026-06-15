# demo_company_analysis

`demo_company_analysis` は、`output_schema`、`output_validator`、`review`、`repair` をまとめて試すための demo job である。

企業名を入力し、短い企業分析 JSON を返す。Web 参照を許可し、参考文献 URL を必須にする。review は参考文献を起点に、要約、給与、従業員数の裏どりを行う。さらに `subcwd` に staged した `workflow_checklist.json` を更新させ、validator が最後に全項目 `true` を検証する。

## Output

出力は次を含む。

- `company_name`
- `summary`
- `employee_count`
- `salary`
- `references`

## Usage

```python
from jobs.demo_company_analysis import DemoCompanyAnalysisClient

client = DemoCompanyAnalysisClient(
    cwd="/path/to/workspace",
)

response = client.run(
    company_name="OpenAI",
    extra_input="Focus on hiring scale and compensation signals.",
)
print(response.content)
```

補助資料を渡す場合は `attachments` と `read_paths` に追加する。

```python
response = client.run(
    company_name="OpenAI",
    attachments=["notes/openai.md"],
    read_paths=["notes/openai.md"],
)
```

固定サンプルとして、`野村総合研究所` を対象にした実行スクリプトもある。

```bash
PYTHONPATH="$CODEX_HOME/skills/codex-job-runner" \
python "$CODEX_HOME/skills/codex-job-runner/jobs/demo_company_analysis/run_nomura_demo.py"
```

この script は `.codex` 直下を `cwd` にせず、`jobs/demo_company_analysis/_demo_workspace/` を専用作業ディレクトリとして使う。

## Notes

- 既定で `ignore_global_context=True`
- 既定で `allowed_skills=[]`
- 既定で `allow_shell_commands=False`
- 既定で `timeout=None`
- 既定で validator、review、repair を有効にする
- 既定で `${SUBCWD}/workflow_checklist.json` を staged し、validator が完了確認に使う
- Web 参照前提なので、実行環境でネットワーク利用が必要
