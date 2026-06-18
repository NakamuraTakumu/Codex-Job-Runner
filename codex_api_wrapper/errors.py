"""Compatibility alias for :mod:`codex_job_runner.errors`."""

import sys as _sys

from codex_job_runner import errors as _errors

_sys.modules[__name__] = _errors
