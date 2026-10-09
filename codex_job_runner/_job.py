"""Non-blocking Codex job handle implementation."""

from __future__ import annotations

import pathlib
import json
import shutil
import sys
import threading
import uuid
from dataclasses import dataclass, replace

from ._client_helpers import *  # noqa: F401,F403
from ._constants import (
    _CANCELLED,
    _COMPLETED,
    _DEFAULT_MAX_FILE_BYTES,
    _DEFAULT_MAX_FILES,
    _DEFAULT_MAX_TOTAL_FILE_BYTES,
    _DEFAULT_MODEL,
    _FAILED,
    _RUNNING,
    _SUBCWD_TOKEN,
    _TIMEOUT,
)
from ._models import _ValidationResult
from .errors import (
    CodexCancelledError,
    CodexExecutionError,
    CodexTimeoutError,
    CodexValidationError,
)
from .types import CodexActivity, CodexResponse
from ._peer_review import (
    CONTENT_PATH, capture_artifacts, changes as peer_changes, dump as peer_dump,
    parse_main_response,
)
from .errors import CodexReviewProtocolError


def _exact_native_usage_cost(records):
    """Return the raw-completion cost only when every observed record is usable."""
    if (
        not records
        or not any(record.role == "main" for record in records)
        or any(not record.exact for record in records)
    ):
        return None
    costs = []
    for record in records:
        cost = _estimate_cost_usd_for_usage(
            usage=record.usage,
            pricing=_resolve_model_pricing(record.model),
        )
        if cost is None:
            return None
        costs.append(cost)
    return sum(costs)


@dataclass(frozen=True)
class _ValidationStepOutcome:
    """One repairable validation step, independent of its implementation."""

    success: bool
    failure_stage: str
    repair_limit: int
    validation: _ValidationResult | None = None
    review: object | None = None

    @property
    def diagnostic(self):
        return "" if self.validation is None else self.validation.diagnostic

    @property
    def review_summary(self):
        return "" if self.review is None else self.review.summary

    @property
    def review_findings(self):
        return () if self.review is None else self.review.findings

    def set_failure(self, execution, *, response, attempts, repairs):
        if self.review is not None:
            execution._set_review_error(
                response=response,
                review=self.review,
                attempts=attempts,
                repairs=repairs,
            )
            return
        execution._set_validation_error(
            response=response,
            validation=self.validation,
            attempts=attempts,
            repairs=repairs,
        )


def _is_string_list(value):
    return isinstance(value, list) and all(isinstance(item, str) for item in value)
