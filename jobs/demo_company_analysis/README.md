# demo_company_analysis

`demo_company_analysis` は、SDK output schema、Job method validation、`review`、`repair` をまとめて試すための demo job である。

トヨタ自動車と本田技研工業について、2025年3月期と2026年3月期の一次指標、派生指標、会社間比較をJSONで返す。
reviewは一次資料のlocator、期、scope、unit、派生値の再計算、公式資料間の差異、会社間の比較可能性を検査する。
`subcwd`にstagedした`workflow_checklist.json`と`evidence_ledger.json`をMainが更新する。
`DemoCompanyAnalysisJob.validate_output()`は台帳containerの構文だけを検証し、台帳entryと最終JSONの意味上の整合はReviewerが検証する。

## Output

出力は次を含む。

- `analysis_basis`
- `companies`
- `cross_company_comparisons`
- `comparability_exceptions`
- `evidence_ledger_path`

## Usage

```python
from codex_job_runner import SessionConfig, WorkspaceConfig
from jobs.demo_company_analysis import DemoCompanyAnalysisJob

job = DemoCompanyAnalysisJob(
    config=SessionConfig(
        workspace=WorkspaceConfig(cwd="/path/to/workspace"),
    ),
    company_name="OpenAI",
    extra_input="Focus on hiring scale and compensation signals.",
)

response = job.run()
print(response.content)
```

補助資料の本文を最初から見せる場合は `context` に追加する。path 経由で必要時に読めればよい資料は `read_paths` に追加する。

```python
response = DemoCompanyAnalysisJob(
    config=config.with_overrides(context=["notes/openai.md"]),
    company_name="OpenAI",
    read_paths=["notes/openai.md"],
).run()
```

固定サンプルとして、`トヨタ自動車`と`本田技研工業`を対象にした実行スクリプトもある。

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
- 既定でmethod validation、review、repairを有効にする
- 既定で`${SUBCWD}/workflow_checklist.json`と`${SUBCWD}/evidence_ledger.json`をstagedする
- validatorは台帳fileがJSON objectであり、`claims`がarrayであることだけを検査する
- Reviewerは台帳entryの完全性、ID参照、source locator、期、scope、unit、派生値の正しさを検査する
- `ReviewConfig.max_repairs=4`により、初回review後に最大4回のMain repairを許可する
- Reviewerはround 0から最大round 4まで実行されるが、問題が解決すればその時点で終了する
- `validate_output.py`は旧external validator APIへの互換説明だけを残しており、demoは実行しない
- Web 参照前提なので、実行環境でネットワーク利用が必要
