# Codex Job Runner Sandbox Spec

- Created: 2026-06-14
- Status: active

Responsibility: `codex-job-runner` の filesystem sandbox、shell command、global context 抑制の契約を定義する。

## Result

**Permission Profile**: wrapper は job ごとに一意な Permission Profile を作り、`read_paths` と `write_paths` から filesystem access を決める。

**Read permission**: `read_paths` に指定した file または directory を read として許可する。

**Write permission**: `write_paths` に指定した file または directory を write として許可する。write は同じ path の read も含む。

**Path scope**: v1 の user-specified `read_paths` / `write_paths` は resolved `cwd` 配下に限定する。`cwd` 外の user path は実行前に `ValueError` にする。

**Danger mode**: `danger-full-access` は public API に出さない。

## Shell Commands

`allow_shell_commands=True` が既定である。shell command は使えるが、filesystem access は Permission Profile に従う。

`allow_shell_commands=False` の場合、wrapper は次の config override を渡す。

- `features.shell_tool=false`
- `allow_login_shell=false`

この設定は shell tool の利用を抑制するためのものであり、attachments の inline 挙動には影響しない。

## SDK Sandbox Mode

wrapper は Permission Profile に加えて、SDK の coarse sandbox mode も渡す。

| Condition | SDK Sandbox |
| --- | --- |
| `write_paths` が空 | `read_only` |
| `write_paths` が 1 つ以上ある | `workspace_write` |

SDK sandbox mode は大枠の実行 mode であり、path 単位の境界ではない。path 単位の read/write 境界は job ごとに生成する Permission Profile が担う。

## Runtime Read Permission

`allow_shell_commands=True` の場合、wrapper は `openai_codex` runtime directory を検出できたときだけ、内部 read-only path として Permission Profile に追加する。

この read permission は sandbox 内の command runner 起動に必要な内部許可であり、user data の読み取り許可ではない。検出できない環境では追加しない。

## Global Context

`ignore_global_context=True` は best-effort mode である。

行うこと:

- SDK config override で `project_doc_max_bytes=0` を渡す。
- `allowed_skills` が未指定なら `[]` として扱い、`skills.include_instructions=false` を渡す。
- prompt 上で global `AGENTS.md` と skills に依存しないよう指示する。

保証しないこと:

- `CODEX_HOME` の差し替え。
- global `AGENTS.md` の完全な遮断。
- user config の全影響の遮断。

## Allowed Skills

`allowed_skills` は v1 では `None` または `[]` だけを受け取る。

| Value | Behavior |
| --- | --- |
| `None` | automatic skill instructions を明示的には抑制しない。 |
| `[]` | `skills.include_instructions=false` を渡す。 |
| non-empty list | 未対応。実行前に `ValueError`。 |

非空 list による skill allow-list は v1 public API に含めない。必要な skill 指示は `input`、`attachments`、または呼び出し側の prompt で明示する。

## Failure

Permission Profile や Codex runtime の sandbox が失敗した場合、wrapper は bypass しない。main attempt の stderr と error として呼び出し側へ返す。
