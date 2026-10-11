---
name: codex-job-runner
description: Python から Codex SDK-backed job wrapper を設計または実装するときに使う。詳細はこの skill の workflow に従う。
---

# Codex Job Runner

## 目的

skill、workflow、hook、local scriptからCodex jobをprogrammaticに呼び出すときに使う。
一般的なtask graph、retry policy、ledger、結果統合は呼び出し側へ置く。

## Workflow

1. [`document/job-centered-public-api-spec.md`](document/job-centered-public-api-spec.md)で公開APIと資源所有を確認する。
2. 旧APIから移行する場合は[`document/job-centered-api-migration-guide.md`](document/job-centered-api-migration-guide.md)を確認する。
3. 単一処理はpending `CodexJob`、並列処理はpending Jobを受け取る`CodexWorkerPool`として設計する。
4. Session共通設定は`SessionConfig`、workspace設定は`WorkspaceConfig`、Job機能は対応する機能別Configへ置く。
5. 変更後はunit testsとskill validationを実行する。

## 最小例

```python
from codex_job_runner import CodexJob, SessionConfig, WorkspaceConfig

job = CodexJob(
    config=SessionConfig(
        context=["README.md"],
        workspace=WorkspaceConfig(
            cwd="/path/to/workspace",
            read_paths=["README.md"],
        ),
    ),
    input="READMEをレビューしてください。",
)

response = job.run()
print(response.content)
```

## 具体Job

再利用する処理は`jobs/`配下の`CodexJob` subclassとして定義する。
constructorは用途固有の値と`config`を受け取り、fieldを初期化してから`super().__init__(...)`へJob定義を渡す。
hookは引数なしのinstance methodとして上書きする。

```python
class ReviewJob(CodexJob):
    def __init__(self, *, config, target):
        self.target = target
        self.progress = 0
        config = config.with_overrides(
            workspace=config.workspace.with_overrides(
                read_paths=[target],
            )
        )
        super().__init__(
            config=config,
            input=f"{target}をレビューしてください。",
        )

    def before_main_attempt(self):
        self.progress += 1
```

## Jobごとの拡張

`output_validators`、`tools`、`hooks.<phase>`は`pending`の間だけ編集できるlist互換fieldである。
`start()`またはPool dispatchがこれらをfreezeすると、変更は`RuntimeError`になる。

```python
from codex_job_runner import Tool

def require_report(text: str) -> str:
    return text

job.output_validators.append(
    lambda response: None if response.content.strip() else (_ for _ in ()).throw(ValueError("empty output"))
)
job.tools.append(
    Tool(
        require_report,
        "Return the supplied report text.",
        args={"text": "Report text."},
        returns="The report text.",
    )
)
job.hooks.after_cleanup.append(lambda: print("job finished"))
```

Pydantic validator、`validate_output()` override、Config由来Tool、legacy hook overrideも同じregistryへ自動登録される。
validatorとhookは登録順のFIFOで実行される。

## 並列実行

```python
from codex_job_runner import CodexWorkerPool

jobs = [
    ReviewJob(config=config, target="a.py"),
    ReviewJob(config=config, target="b.py"),
]
batch = CodexWorkerPool(max_workers=4).start_many(jobs)
result = batch.result()
```

PoolはJob class、入力、Configを生成しない。
dependency、priority、retry、意味的な結果統合は扱わない。

## 主要契約

- `CodexJob(...)`はresourceを生成しないpending instanceを返す。
- `start()`は同じinstanceを返し、`run()`は同期shortcutになる。
- `CodexSession.send(input, ...)`は同じ会話へturnを送り、指定されたschema、Pydantic model、validator、およびvalidation repairをそのturnへ適用する。
- `CodexSession.resume(input=...)`は同じthreadを新しいSessionで継続する。
- `CodexSession.fork(input=...)`は履歴を引き継ぐ別threadを作る。
- threadの保存領域は関連するSessionまたはJobが存在する間だけ保持し、最後の所有objectのGC後に削除する。
- model、context、timeoutなどSession共通設定は`SessionConfig`へ置く。
- `SessionConfig.web_search`は`"live"` / `"cached"` / `"disabled"`のみ指定可能。既定の`None`では検索モードのoverrideを追加せず、`with_overrides`で派生したSessionには設定を保持する。
- Runner管理subagentは`CodexJob.subagents`へ`SubagentConfig`として置き、`SessionConfig`へ再帰的な設定を入れない。
- 子は汎用`CodexJob`として実行し、生成元の親だけが操作できる。子へsubagent Toolを渡さず、孫は生成しない。
- 子には`ask_parent(message)`だけを内部公開し、親の`send_message`による回答後に同じturnを再開する。
- `spawn_agent`は親inputと履歴を継承せず、Toolの`message`だけを子inputにする。
- `wait_agent`は子の質問、終端、またはtimeoutまで待ち、子Jobの未返却file logを差分で返す。
- 子Jobのactivity logは`[Subagent /root/<task_name>]`をactor prefixに使い、親やReviewerの`[Main Attempt]`と区別する。
- 子のworkspace、shell、Skill、環境変数、Tool、およびtimeoutは親の権限を超えてはならない。
- `SubagentConfig`は`max_concurrent`で同時実行数だけを制限する。子の累積数、全子の総turn数、子ごとのturn数は制限しない。費用は集計するが、費用額または費用情報の欠落を起動制限には使わない。
- automatic Reviewerのsubagentは`ReviewConfig.subagents`へ独立指定し、mainの設定を継承しない。
- Jobの`total_usage`、`total_usage_by_model`、`total_estimated_cost_usd`、`total_estimated_cost_usd_by_model`は、Main、Reviewer、および両者のRunner管理子を再帰集計する。
- Batchのusageと費用は成功・失敗Jobを含む再帰合計とし、モデル別内訳も公開する。`usage_complete`は全構成要素のusageを確認できた場合だけ`True`にする。
- cwd、path、SUBCWD、inline contextは`WorkspaceConfig`へ置く。
- filesystemのread/writeには、`SessionConfig.allow_shell_commands=True`（shell設定をon）にする必要がある。`read_paths` / `write_paths`は対象pathの許可範囲を定める設定であり、これらを指定しても`allow_shell_commands=False`ならfilesystemのread/writeは有効にならない。
- Tool、validation、review、file collection、loggingは対応する機能別Configへ置く。
- inputとoutput schemaはSessionまたはJob constructorへ置く。
- 単発JobのTool bridgeはJob専有であり、自動で終了する。
- Poolは互換するTool設定ごとにbridgeをbatch内共有する。
- standalone review sessionは公開しない。automatic reviewのsession再利用は内部で維持する。
- `ReviewConfig.enable_peer_exchange=True`では、mainとreviewerだけに内部`ask_peer(message)` Toolを追加する。
- `ask_peer`は相手の直近の完了済み会話からwrite pathを設定しない一時forkを作り、元の履歴を変更せず一つの回答文を返す。
- answer forkのshell可否はfork元となる相手の`SessionConfig.allow_shell_commands`を継承する。
- answer forkはshell可否にかかわらずwrite path、一般Tool、subagent、hook、Skillを持たない。
- shell有効時は一時sandbox内への書き込みまで拒否されるとは限らない。`write_paths=()`は永続的な書き込み先をanswer forkへ設定しない契約である。
- `ReviewConfig.context`はreviewerの評価基準・指示であり、会話履歴ではない。
- automatic peer reviewは同じReviewer sessionを初回から全roundで再利用する。
- peer reviewのReviewer判定turnには、open finding IDと初回・再査読の別からRunnerが生成したstrict JSON SchemaをSDK output schemaとして渡す。
- Mainの回答作成turnには、Runner生成change IDとopen finding IDから生成したstrict JSON SchemaをSDK output schemaとして渡す。
- SDK output schemaで表現できないIDの完全被覆、順序、双方向参照、およびdisposition間の相関はRunnerのlocal parserで検証する。
- Reviewer判定のJSONまたはprotocol不適合は、同じReviewer sessionへ診断を返す修正turnで再生成する。artifact読取失敗や実行errorは修正対象にしない。
- 起動したJobまたはBatchは、完了まで待つかcancelする。
- job実行のfinal reportには、成功時は`response.log_path`、失敗時はerrorの`log_path`を示す。
- job実行中のlogは、ユーザーまたは適用中の`AGENTS.md`がwatchを明示した場合だけ反復取得する。

## Validator

外部validator commandは使わない。
具体`CodexJob` subclassが同期`validate_output(response: CodexResponse) -> None`を上書きする。
`ValueError`はrepair可能な診断であり、他の例外はrepairせずJobを失敗させる。
最終的な`CodexValidationError`は診断を`validation_diagnostic`に保持する。
`ValidationConfig`は`repair`と`repair_prompt_path`だけを持つ。repairは成功まで回数で制限しない。
`output_pydantic_model`を指定した場合はPydantic validatorが先であり、失敗時は後続validatorを呼ばない。
`output_schema_path`はSDK出力schema専用であり、method validationとは別である。

## 検証

```bash
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s /home/nakamura/.codex/skills/codex-job-runner/tests
python3 /home/nakamura/.codex/skills/.system/skill-creator/scripts/quick_validate.py /home/nakamura/.codex/skills/codex-job-runner
```
