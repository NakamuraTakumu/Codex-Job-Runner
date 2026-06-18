"""Review conversation session for the Codex job runner."""

from __future__ import annotations

import threading
from dataclasses import replace

from ._client_helpers import _emit_activity_stdout, _stream_activity_from_line


class ReviewSession:
    """Conversation session for a reviewer."""

    def __init__(
        self,
        *,
        send_turn,
        close_session,
    ):
        self._send_turn = send_turn
        self._close_session = close_session
        self._lock = threading.RLock()
        self._closed = False
        self._history: list[tuple[str, str]] = []

    @property
    def history(self):
        with self._lock:
            return tuple(self._history)

    def send(self, message, *, output_schema_path=None):
        activity_log = []

        def activity_callback(line):
            entry = _stream_activity_from_line(line, detail="review")
            if entry is None:
                return
            activity_log.append(entry)
            _emit_activity_stdout(entry)

        response = self._send(
            message,
            output_schema_path=output_schema_path,
            activity_callback=activity_callback,
        )
        if not activity_log and response.stdout:
            for line in response.stdout.splitlines():
                entry = _stream_activity_from_line(line, detail="review")
                if entry is not None:
                    activity_log.append(entry)
                    _emit_activity_stdout(entry)
        if activity_log:
            return replace(response, activity_log=tuple(activity_log))
        return response

    def _send(self, message, *, output_schema_path=None, activity_callback=None):
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message must be a non-empty string")
        with self._lock:
            if self._closed:
                raise RuntimeError("review session is closed")
            history = tuple(self._history)
        response = self._send_turn(
            message=message.strip(),
            history=history,
            output_schema_path=output_schema_path,
            activity_callback=activity_callback,
        )
        with self._lock:
            if self._closed:
                raise RuntimeError("review session is closed")
            self._history.append(("runner", message.strip()))
            self._history.append(("reviewer", response.content))
        return response

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._close_session()
