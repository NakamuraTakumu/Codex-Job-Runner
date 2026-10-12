# Codex Job Runner

`codex_job_runner`は、PythonからCodex jobを起動するための小さなwrapperである。
公開APIは、最小の会話単位である`CodexSession`、未起動の処理を表す`CodexJob`、並列実行を担う`CodexWorkerPool`に分かれる。

```python
from codex_job_runner import CodexJob, SessionConfig, WorkspaceConfig

config = SessionConfig(
    context=["README.md"],
    timeout=300,
    workspace=WorkspaceConfig(
        cwd="/path/to/workspace",
        read_paths=["src", "README.md"],
        write_paths=["tmp/codex-output"],
    ),
)

job = CodexJob(
    config=config,
    input="このコードを説明してください。",
)

response = job.start().result()
print(response.content)
```

`CodexJob(...)`はprocess、thread、Function Tool bridgeを作らない`pending` instanceを返す。
`start()`は同じinstanceを返し、`run()`は`start().result()`の同期shortcutになる。
用途固有のfield、constructor、hookが必要な場合は`CodexJob`を継承する。

検証や並列状態を必要としない同期処理には`CodexSession`を使う。
初回と継続を問わず、同じ会話へ送るturnには公開`send()`を使う。

```python
from codex_job_runner import CodexSession

with CodexSession(
    config=config,
    input="対象を調査してください。",
) as session:
    response = session.run()
    response = session.send("観点を変えて再検討してください。")

continued = session.resume(input="同じ会話を続けてください。")
branched = session.fork(input="別案を検討してください。")
```

`resume()`は同じCodex threadを継続し、`fork()`は履歴を引き継ぐ別threadを作る。
両方とも会話stateの保存領域を共有し、SessionごとのSDK clientやTool bridgeは作り直す。
`CodexSession`の保存領域は、最後の関連SessionがGCされると削除する。
`CodexJob`が自分で作った保存領域（reviewerとRunner管理子を含む）は、Jobが終端状態（成功、失敗、cancel、timeout）に達した時点で、rolloutをarchiveしてから削除する。
そのため終了済みJobへの`resume()`と`fork()`は`CodexSessionClosedError`になる。

## 公開API

- `CodexSession(...)`：同じSDK threadへ同期的にturnを送る最小実行単位。
- `CodexSession.send(...)`：同じ会話へturnを送り、必要ならschema、Pydantic model、同期validator、および同一会話でのvalidation repairを適用する。
- `CodexSession.resume(...)`：同じ会話を新しいSessionで継続する。
- `CodexSession.fork(...)`：履歴を分岐した新しいSessionを作る。
- `SessionConfig(...)`：Session共通の不変な実行設定。
  `web_search="live" | "cached" | "disabled"`でCodex起動時の検索モードを指定する。既定の`None`はoverrideを追加しない（従来どおり）。
- `WorkspaceConfig(...)`と`ToolConfig(...)`：workspaceとFunction Toolの設定。
- `ValidationConfig(...)`、`FileCollectionConfig(...)`、`LoggingConfig(...)`、`SubagentConfig(...)`：Job機能の設定。
- `CodexJob(...).start()`：pending Jobを起動する。
- `CodexJob.run()`：単一Jobを同期実行する。
- `CodexJob.wait_any(jobs, timeout=None)`：最初に完了したJobを返す。
- `CodexJob.done()`、`result()`、`cancel()`：状態の観測と停止。
- `CodexWorkerPool(...)`：構築済みpending Jobをbounded concurrencyで実行する。
- `CodexBatch.submit(job)`、`close()`、`result()`、`cancel()`：open batchの管理。
- `Tool(...)`：main attemptとvalidation repairから呼べる同期Python callable。
- `ReviewConfig(...)`：automatic reviewの設定。

`CodexClient`、standalone `ReviewSession`、`codex_api_wrapper` packageは削除した。

## Runner管理subagent

`CodexJob(subagents=...)`は、native subagentを使わず、直接の子を通常の`CodexJob`として起動する。
機能、設定、Tool、実行フロー、権限、および制約の全体像は[`document/runner-managed-subagent-guide.md`](document/runner-managed-subagent-guide.md)を参照する。
子は孫を生成できず、生成元の親だけが操作できる。
子のworkspace、shell、Skill、環境変数、Tool、およびtimeoutは親の権限を超えられない。

```python
from codex_job_runner import CodexJob, SessionConfig, SubagentConfig

child = SessionConfig(
    model="gpt-6-luna",
    reasoning_effort="medium",
    allow_shell_commands=False,
)
job = CodexJob(
    config=SessionConfig(model="gpt-6-luna"),
    input="調査してください。",
    subagents=SubagentConfig(
        session=child,
        allowed_models={"gpt-6-luna": ("low", "medium", "high")},
        max_concurrent=2,
    ),
)
```

親には`spawn_agent`、`send_message`、`followup_task`、`interrupt_agent`、`list_agents`、`wait_agent`の六つのToolが追加される。
`spawn_agent`は親inputと履歴を継承せず、`message`だけを子inputにする。
子には`ask_parent(message)`だけが追加され、親の回答を待って同じturnを再開できる。
`wait_agent`は子の質問、終端、またはtimeoutまで待ち、子Jobのfile logに前回取得後追加された行をそのまま返す。
子Jobのactivity logは`[Subagent /root/<task_name>]`をactor prefixに使い、親やReviewerの`[Main Attempt]`と区別する。
子ごとの結果は`job.subagent_results`、子だけの推定費用は`job.subagent_estimated_cost_usd`で参照する。
親、reviewer、および両者のRunner管理子を含む再帰合計は`job.total_usage`と`job.total_estimated_cost_usd`、モデル別内訳は`job.total_usage_by_model`と`job.total_estimated_cost_usd_by_model`で参照する。
`job.total_cost_complete`と`job.total_usage_incomplete_reasons`は、再帰合計の完全性と欠落理由を公開する。
費用が欠落した場合、推定費用は`None`となり、`subagent_cost_complete=False`で欠落を示すが、追加turnは拒否しない。
子の累積数、全子の総turn数、子ごとのturn数には上限を設けず、`max_concurrent`だけが同時実行数を制限する。
automatic Reviewerへは`ReviewConfig.subagents`で独立した設定を指定し、main設定は継承しない。

## Mainとreviewerの一往復

`ReviewConfig.enable_peer_exchange=True`を指定すると、main attemptとreviewerへ内部Tool `ask_peer(message)`を追加する。
このToolは相手の直近の完了済み会話から、write pathを設定しない一時的なforkを作り、質問文を一つ送り、回答文だけを返す。
元のmainとreviewerの履歴は変更しない。
answer forkのshell可否はfork元となる相手の`SessionConfig.allow_shell_commands`を継承する。
reviewerからmainへ質問する場合はmain設定、mainからreviewerへ質問する場合は`ReviewConfig.session`設定を使う。
write path、一般Tool、subagent、hook、Skillはshell可否にかかわらずanswer forkへ渡さない。
ただし、shellを有効にした実行環境では、一時的なsandbox内への書き込みまで拒否されるとは限らない。
`write_paths=()`が保証するのは、answer forkへ永続的な書き込み先を設定しないことである。

```python
from codex_job_runner import ReviewConfig

job = CodexJob(
    config=config,
    input="設計案を作成してください。",
    review=ReviewConfig(
        context="要件漏れと安全性を確認してください。",
        enable_peer_exchange=True,
        artifact_paths=["report.md"],
        reuse_session=True,
    ),
)
response = job.run()
```

`context`はreviewerへ与える評価基準・指示であり、会話履歴ではない。
peer reviewは初回から全roundで同じreviewer会話を継続するため、`reuse_session=True`が必須である。`artifact_paths`には査読対象のworkspace内text fileまたはdirectoryを指定する。
`False`でも、mainは直近の完了済みreviewer会話へ質問できる。
reviewerの判定がJSONまたはprotocol要件を満たさない場合は、同じreviewer会話へ診断を返して再生成する。
artifact読取失敗や実行errorは再生成せず、Jobを失敗させる。
機能を無効にした場合は内部Toolも追加せず、通常のautomatic reviewの動作を維持する。

## 具体Job

```python
class AnalysisJob(CodexJob):
    def __init__(self, *, config: SessionConfig, company_name: str):
        self.company_name = company_name
        self.progress = 0
        super().__init__(
            config=config,
            input=f"{company_name}を分析してください。",
        )
```

hookは引数なしのinstance methodとして上書きする。

```python
class ObservableJob(AnalysisJob):
    def before_main_attempt(self):
        self.progress += 1
```

## Jobごとのvalidator、Tool、hook

`output_validators`、`tools`、`hooks.<phase>`は、`pending`のJobへ追加できるlist互換fieldである。
`start()`またはPool dispatch後はsnapshot化され、変更できない。

```python
from codex_job_runner import Tool

def echo(value: str) -> str:
    return value

job.output_validators.append(
    lambda response: None if response.content.strip() else (_ for _ in ()).throw(ValueError("empty output"))
)
job.tools.append(
    Tool(
        echo,
        "Return the supplied value.",
        args={"value": "A value."},
        returns="The supplied value.",
    )
)
job.hooks.after_cleanup.append(lambda: print("cleaned up"))
```

Pydantic、`validate_output()` override、`ToolConfig.tools`、既存hook method overrideも同じfieldへ自己登録される。
validatorとhookは追加順に実行される。

## 出力検証とrepair

`ValidationConfig`はrepairの挙動だけを設定する。外部validator command、argv、stdin、timeoutは公開APIにない。
出力内容の検証は具体`CodexJob`の同期method `validate_output(response: CodexResponse) -> None`へ置く。

```python
from codex_job_runner import CodexJob, CodexResponse, ValidationConfig


class JsonJob(CodexJob):
    def __init__(self, *, config):
        super().__init__(
            config=config,
            input="JSONを返してください。",
            validation=ValidationConfig(repair=True),
        )

    def validate_output(self, response: CodexResponse) -> None:
        if not response.content.startswith("{"):
            raise ValueError("output must be a JSON object")
```

`ValueError`はrepair可能な診断として扱われ、`repair=True`かつ回数上限内ならrepair turnを行う。最終的な`CodexValidationError`は診断を`validation_diagnostic`に保持する。
`ValueError`以外はJobの実行失敗でありrepairしない。
`output_pydantic_model`を指定した場合はPydantic validatorが先に実行され、失敗時は後続validatorを呼ばない。
`output_schema_path`はSDKへ渡すschemaであり、method validationやPydanticの代替ではない。

## Worker Pool

```python
from codex_job_runner import CodexWorkerPool

jobs = [
    AnalysisJob(config=config, company_name="A"),
    AnalysisJob(config=config, company_name="B"),
]

pool = CodexWorkerPool(max_workers=4)
result = pool.start_many(jobs).result()
```

PoolはJobを生成せず、pending Jobをatomicにclaimする。
同じTool instance列、`ToolConfig.max_concurrent_calls`、`ToolConfig.timeout`を持つJobは、batch内で一つのbridgeを共有する。
単発JobのbridgeはJob専有であり、終了時に自動で閉じる。
Batch結果は、全Jobの再帰合計を`usage`と`estimated_cost_usd`、モデル別内訳を`usage_by_model`と`estimated_cost_usd_by_model`で公開する。
成功・失敗別のモデル内訳は`successful_usage_by_model`と`failed_usage_by_model`で参照する。
`usage_complete`と`usage_incomplete_reasons`は、失敗Jobを含む全Jobのusage完全性を表す。

open batchでは、構築済みJobを途中追加できる。

```python
batch = pool.start_batch()
batch.submit(AnalysisJob(config=config, company_name="A"))
batch.submit(AnalysisJob(config=config, company_name="B"))
batch.close()
result = batch.result()
```

## 仕様

公開API、状態遷移、設定配置、資源所有の正本は
[`document/job-centered-public-api-spec.md`](document/job-centered-public-api-spec.md)である。
旧`CodexClient`からの変更手順は
[`document/job-centered-api-migration-guide.md`](document/job-centered-api-migration-guide.md)にまとめている。
sandbox、SUBCWD、validation repair、review、loggingを含む現行契約も同じ公開API仕様を正本とする。
# Native subagent usage telemetry

`CodexResponse.native_usage` contains one `CodexNativeUsage` record for each
observed app-server `rawResponse/completed` notification, including native
subagent turns. A record is `exact=True` only when both server usage and model
attribution are available. Model attribution prefers a runtime reroute or
thread setting; when neither is emitted, it may be the native collab request
model or configured `default_subagent_model`, rather than a billing-confirmed
runtime model. `estimated_cost_usd` uses
these records exactly once, per attributed model, when every observed record
is exact; then `usage_cost_source="app_server_exact"`. Otherwise it retains
the parent-stream estimate and reports `usage_cost_source="parent_stream_fallback"`.
Neither value is a billing invoice.
