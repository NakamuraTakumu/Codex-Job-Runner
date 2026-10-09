class _ActivityStreamEmitter:
    def __init__(self, callback):
        self._callback = callback

    def emit_stdout_line(self, line):
        self._callback(line)
