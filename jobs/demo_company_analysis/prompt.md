トヨタ自動車と本田技研工業について、2025年3月期と2026年3月期を比較する構造化調査を行い、JSONだけを返してください。

## 調査対象

両社の各期について、次の一次指標を調査してください。

- 連結従業員数
- 単体従業員数
- 単体平均年間給与
- 売上高または営業収益
- 営業利益

各社について次の派生指標を計算してください。

- 各一次指標の前年差（2026年3月期から2025年3月期を引いた値）
- 各期の営業利益率
- 各期の連結従業員一人当たり売上高

両社の会計期間、scope、unit、指標定義が比較可能な場合だけ、各期の売上高差、営業利益率差、連結従業員一人当たり売上高差を`cross_company_comparisons`へ記録してください。
比較できない組合せや欠損値は、数値を推測せず`comparability_exceptions`へ理由と根拠を記録してください。

## 証拠

- 有価証券報告書、決算短信、会社公式IR資料などの一次資料を優先してください。
- `sources`にはURLに加えて、page、table、section、XBRL elementなど、第三者が値を再発見できるlocatorを記録してください。
- 各一次指標に一意な`claim_id`を付け、根拠となる`source_id`を対応付けてください。
- 派生指標にはscope、as_of、definition、formula、入力claim ID、unit、rounding、limitationを記録してください。
- 公式資料間で値が異なる場合は`discrepancies`へ候補値のscope、as_of、definitionを記録し、採用理由を説明してください。
- 連結値と単体値、期末人数と期中平均、売上高と営業収益など、母集団または定義が異なる値を無条件に比較しないでください。
- 開示資料から確認できない因果関係を推測しないでください。

## 証拠台帳

- `${SUBCWD}/evidence_ledger.json`は事前配置されています。
- `claim_template`のfield名を保ち、全一次指標、派生指標、会社間比較について`claims`を更新してください。
- `source_locators`は`source_id`と、最終JSONの`sources`にある同一locatorを持つobjectの配列です。
- 派生指標と会社間比較にも、全入力値を支えるsource locatorを含めてください。
- 最終JSONと台帳のclaim ID、入力claim ID、source IDを相互に対応付けてください。
- workflowの各項目を終えたら`${SUBCWD}/workflow_checklist.json`の対応項目を`true`に更新してください。

## 出力

- 最終応答は`output.schema.json`に従うJSONだけにしてください。
- `companies`は`toyota`、`honda`の順、各社の`fiscal_periods`は2025-03-31、2026-03-31の順にしてください。
- 金額一次指標は百万円、従業員数は人、平均年間給与は円へ正規化してください。
- roundingには桁だけでなく、四捨五入などの丸め方向も明記してください。
- 不確実性、欠損、比較不能、資料上の限界を空文字で済ませず記録してください。
