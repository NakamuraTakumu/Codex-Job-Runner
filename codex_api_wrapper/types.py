"""Compatibility alias for :mod:`codex_job_runner.types`."""

import sys as _sys

from codex_job_runner import types as _types

_sys.modules[__name__] = _types
