# Codex Job Runner Structured Output Spec

- Created: 2026-06-14
- Status: active

Responsibility: `output_schema_path` と `output_pydantic_model` による final response shape 制約と wrapper-side validation の契約を定義する。

## Result

**output_schema_path**: 利用者が用意した JSON Schema file。wrapper は SDK turn の `output_schema` に渡し、同じ schema 本文を prompt context の `Output schema` section に入れる。

**output_pydantic_model**: `model_json_schema()` と `model_validate_json()` を持つ Pydantic model 相当 class。wrapper は JSON Schema を一時 file として生成し、SDK turn の `output_schema` に渡す。さらに final response を `model_validate_json()` で検証する。

`output_schema_path` と `output_pydantic_model` は同時指定できない。

## Prompt Context

`output_schema_path` を指定した場合、wrapper は schema file の本文を prompt context に入れる。

`output_pydantic_model` を指定した場合、wrapper は model source context を prompt に入れる。生成した JSON Schema file は SDK turn の `output_schema` に渡すための一時 file として扱う。

## Validation

`output_schema_path` 自体では wrapper-side JSON Schema validation を行わない。shape 制約は SDK turn の `output_schema` に委ねる。

`output_pydantic_model` を指定した場合、wrapper は final response に対して `model_validate_json()` を実行する。失敗した場合は `CodexValidationError` として扱い、custom `output_validator` より前に失敗する。

custom validation が必要な場合は `output_validator` を使う。validator と repair の詳細は `validation-repair-spec.md` を正本にする。

## Limits

schema file は UTF-8 text の通常 file とする。inline する schema 本文は inline context byte limit に従う。

Pydantic から生成した一時 schema file は job cleanup 時に削除する。
