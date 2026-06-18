"""Compatibility alias for :mod:`codex_job_runner.client`."""

import sys as _sys

from codex_job_runner import client as _client

_sys.modules[__name__] = _client
