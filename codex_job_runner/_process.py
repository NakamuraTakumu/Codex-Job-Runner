"""SDK process adapters for the Codex job runner."""

from __future__ import annotations

import subprocess
import threading


class _SdkProcess:
    """Subprocess-like adapter backed by the ``openai-codex`` SDK."""

    def __init__(self, *, runner):
        self._runner = runner
        self._thread = None
        self._lock = threading.Lock()
        self._terminated = threading.Event()
        self._finished = threading.Event()
        self._stdout = ""
        self._stderr = ""
        self._returncode = None
        self._turn_handle = None
        self._codex = None
        self._error = None
        self._activity_callback = None
        self._emitted_stdout_lines = False

    @property
    def returncode(self):
        return self._returncode

    def communicate(self, prompt=None, timeout=None):
        if self._thread is None:
            if prompt is None:
                raise RuntimeError("communicate() requires prompt on first call")

            def target():
                try:
                    stdout, stderr, returncode = self._runner(self, prompt)
                except Exception as exc:
                    with self._lock:
                        self._stdout = ""
                        self._stderr = str(exc)
                        self._returncode = 1
                        self._error = exc
                else:
                    with self._lock:
                        self._stdout = stdout or ""
                        self._stderr = stderr or ""
                        self._returncode = returncode
                finally:
                    self._finished.set()

            self._thread = threading.Thread(target=target, name="codex_sdk_attempt", daemon=True)
            self._thread.start()
        elif prompt is not None:
            raise RuntimeError("communicate() prompt may only be provided once")

        if not self._finished.wait(timeout):
            raise subprocess.TimeoutExpired(cmd="openai-codex", timeout=timeout)
        return self._stdout, self._stderr

    def poll(self):
        if self._finished.is_set():
            return self._returncode
        return None

    def wait(self, timeout=None):
        if not self._finished.wait(timeout):
            raise subprocess.TimeoutExpired(cmd="openai-codex", timeout=timeout)
        return self._returncode

    def terminate(self):
        self._terminated.set()
        turn_handle = self._turn_handle
        codex = self._codex
        if turn_handle is not None:
            try:
                turn_handle.interrupt()
            except Exception:
                pass
        if codex is not None:
            try:
                codex.close()
            except Exception:
                pass

    def kill(self):
        self.terminate()

    def set_turn_handle(self, turn_handle):
        self._turn_handle = turn_handle

    def set_codex(self, codex):
        self._codex = codex

    def set_activity_callback(self, callback):
        self._activity_callback = callback

    def emit_stdout_line(self, line):
        callback = None
        with self._lock:
            self._emitted_stdout_lines = True
            callback = self._activity_callback
        if callback is not None:
            callback(line)

    def emitted_stdout_lines(self):
        with self._lock:
            return self._emitted_stdout_lines

    def terminated(self):
        return self._terminated.is_set()



class _ActivityStreamEmitter:
    def __init__(self, callback):
        self._callback = callback

    def emit_stdout_line(self, line):
        self._callback(line)
