"""Review conversation session for the Codex job runner."""

from __future__ import annotations

import json
import pathlib
import threading
import uuid
import weakref
from dataclasses import replace

from ._client_helpers import (
    _build_repair_turn_prompt,
    _default_repair_prompt,
    _make_temp_text_file,
    _make_pydantic_output_validator,
    _normalize_output_pydantic_model,
    _render_pydantic_output_schema_json,
)
from .config import LoggingConfig, SessionConfig, ValidationConfig
from .errors import (
    CodexAlreadyStartedError,
    CodexNotStartedError,
    CodexSessionClosedError,
    CodexTimeoutError,
    CodexValidationError,
)


class _ConversationStoreCleanup:
    """Delete one isolated Codex state home at most once."""

    def __init__(self, cleanup_tree):
        self._cleanup_tree = cleanup_tree
        self._lock = threading.RLock()
        self._home = None
        self._cleaned = False

    def attach(self, home):
        with self._lock:
            if self._cleaned:
                raise RuntimeError("Codex conversation store is already deleted")
            if self._home is None:
                self._home = home
            elif self._home != home:
                raise RuntimeError("Codex conversation store home cannot change")

    @property
    def home(self):
        with self._lock:
            return self._home

    def __call__(self):
        with self._lock:
            if self._cleaned:
                return
            self._cleaned = True
            home = self._home
            self._home = None
        if home is not None:
            try:
                self._cleanup_tree(home)
            except Exception:
                pass


class _ConversationStore:
    """Own the state home shared by related conversation threads."""

    def __init__(self, cleanup_tree):
        self._cleanup = _ConversationStoreCleanup(cleanup_tree)
        self._finalizer = weakref.finalize(self, self._cleanup)

    @property
    def home(self):
        return self._cleanup.home

    def attach_home(self, home):
        self._cleanup.attach(home)

    def delete(self):
        self._finalizer()


class _ConversationState:
    """Identify one thread within a shared conversation store."""

    def __init__(self, store, *, thread_id=None, forked_from_id=None):
        self.store = store
        self.thread_id = thread_id
        self.forked_from_id = forked_from_id
        self.turn_lock = threading.RLock()
        self._history_lock = threading.RLock()
        self._completed_turns: list[tuple[str, str]] = []

    def bind_thread(self, thread_id):
        if self.thread_id is None:
            self.thread_id = thread_id
            self.forked_from_id = None
        elif self.thread_id != thread_id:
            raise RuntimeError("Codex conversation thread cannot change")

    def fork(self):
        if self.thread_id is None:
            raise RuntimeError("Codex conversation has not started")
        forked = _ConversationState(
            self.store,
            forked_from_id=self.thread_id,
        )
        with self._history_lock:
            forked._completed_turns = list(self._completed_turns)
        return forked

    def record_turn(self, input_text, output_text):
        with self._history_lock:
            self._completed_turns.append((str(input_text), str(output_text)))

    def completed_turns(self):
        with self._history_lock:
            return tuple(self._completed_turns)


class CodexSession:
    """Minimal lazy Codex conversation session."""

    def __init__(
        self,
        *,
        config: SessionConfig,
        input=None,
        output_schema_path=None,
        output_pydantic_model=None,
        logging: LoggingConfig | None = None,
        _conversation_state=None,
    ) -> None:
        if not isinstance(config, SessionConfig):
            raise TypeError("config must be a SessionConfig")
        if output_schema_path is not None and output_pydantic_model is not None:
            raise ValueError(
                "output_schema_path and output_pydantic_model are mutually exclusive"
            )
        if logging is not None and not isinstance(logging, LoggingConfig):
            raise TypeError("logging must be a LoggingConfig or None")
        self.config = config
        self.input = input
        self.output_schema_path = output_schema_path
        self.output_pydantic_model = output_pydantic_model
        self.logging = logging
        self.id = "codex_session_" + uuid.uuid4().hex
        self._session_lock = threading.RLock()
        self._session_closed = False
        self._initial_turn_sent = False
        self._session_runner = None
        self._turn_session = None
        self._turn_cleanup = None
        self._context_prefix = ""
        self._first_raw_turn = True
        self._default_timeout = config.timeout
        self._conversation_state = _conversation_state

    def __enter__(self):
        with self._session_lock:
            self._ensure_session_open()
        return self

    def __exit__(self, exc_type, exc_value, traceback_value):
        self.close()

    def run(self):
        with self._session_lock:
            self._ensure_session_open()
            if self._initial_turn_sent:
                raise CodexAlreadyStartedError(
                    "codex session initial turn has already been sent"
                )
            if self.input is None or (
                isinstance(self.input, str) and not self.input.strip()
            ):
                raise ValueError("input must be provided before run()")
            self._initial_turn_sent = True
        return self._send_validated(
            self.input,
            output_schema_path=self.output_schema_path,
            output_pydantic_model=self.output_pydantic_model,
        )

    def send(
        self,
        input,
        *,
        output_schema_path=None,
        output_pydantic_model=None,
        validators=(),
        validation: ValidationConfig | None = None,
        timeout=None,
    ):
        """Send one turn on this conversation and optionally validate its output."""
        with self._session_lock:
            self._ensure_session_open()
            if not self._initial_turn_sent:
                self._initial_turn_sent = True
        return self._send_validated(
            input,
            output_schema_path=output_schema_path,
            output_pydantic_model=output_pydantic_model,
            validators=validators,
            validation=validation,
            timeout=timeout,
        )

    def _send_validated(
        self,
        input,
        *,
        output_schema_path=None,
        output_pydantic_model=None,
        validators=(),
        validation=None,
        timeout=None,
    ):
        if not isinstance(input, str) or not input.strip():
            raise ValueError("input must be a non-empty string")
        if output_schema_path is not None and output_pydantic_model is not None:
            raise ValueError(
                "output_schema_path and output_pydantic_model are mutually exclusive"
            )
        if validation is None:
            validation = ValidationConfig()
        elif not isinstance(validation, ValidationConfig):
            raise TypeError("validation must be a ValidationConfig or None")
        try:
            normalized_validators = tuple(validators)
        except TypeError as exc:
            raise TypeError("validators must be an iterable of callables") from exc
        if any(not callable(validator) for validator in normalized_validators):
            raise TypeError("validators must contain only callables")
        if output_pydantic_model is not None:
            normalized_validators = (
                _make_pydantic_output_validator(output_pydantic_model),
                *normalized_validators,
            )

        original_input = input.strip()
        prompt = original_input
        repairs = 0
        while True:
            response = self._execute_turn(
                prompt,
                output_schema_path=output_schema_path,
                output_pydantic_model=output_pydantic_model,
                timeout=timeout,
            )
            diagnostic = ""
            try:
                for validator in normalized_validators:
                    validator(response)
            except ValueError as exc:
                diagnostic = str(exc) or "output validation failed"

            if not diagnostic:
                return self._record_turn(prompt, response)
            if not validation.repair:
                raise CodexValidationError(
                    "Codex session output validation failed",
                    response=response,
                    validation_diagnostic=diagnostic,
                    attempts=repairs + 1,
                    repairs=repairs,
                )
            repairs += 1
            prompt = self._build_validation_repair_prompt(
                original_input=original_input,
                response=response,
                diagnostic=diagnostic,
                repair_index=repairs,
                repair_prompt_path=validation.repair_prompt_path,
            )

    def _build_validation_repair_prompt(
        self,
        *,
        original_input,
        response,
        diagnostic,
        repair_index,
        repair_prompt_path,
    ):
        values = {
            "original_input": original_input,
            "attempt": str(repair_index),
            "repair_attempt": str(repair_index),
            "failure_stage": "validator",
            "validation_diagnostic": diagnostic,
            "checklist_feedback": "",
            "previous_content": response.content,
            "subcwd_section": "",
            "review_summary": "",
            "review_findings": "",
        }
        if repair_prompt_path is None:
            repair_input = _default_repair_prompt(values)
        else:
            path = pathlib.Path(repair_prompt_path).expanduser()
            if not path.is_absolute():
                cwd = self.config.workspace.cwd or pathlib.Path.cwd()
                path = pathlib.Path(cwd).expanduser().resolve() / path
            repair_input = path.read_text(encoding="utf-8")
            for key, value in values.items():
                repair_input = repair_input.replace("{" + key + "}", value)
        return _build_repair_turn_prompt(input=repair_input)

    def _execute_turn(
        self,
        message,
        *,
        output_schema_path=None,
        output_pydantic_model=None,
        timeout=None,
        activity_callback=None,
    ):
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message must be a non-empty string")
        effective_timeout = self._default_timeout if timeout is None else timeout
        temp_schema_path = None
        if output_schema_path is not None and output_pydantic_model is not None:
            raise ValueError(
                "output_schema_path and output_pydantic_model are mutually exclusive"
            )
        if output_pydantic_model is not None:
            normalized_model = _normalize_output_pydantic_model(
                output_pydantic_model
            )
            temp_schema_path = pathlib.Path(
                _make_temp_text_file(
                    prefix="codex_job_runner_session_schema_",
                    suffix=".json",
                    text=_render_pydantic_output_schema_json(normalized_model),
                )
            )
            output_schema_path = temp_schema_path
        with self._session_lock:
            self._ensure_session_open()
            turn_session = self._get_or_create_turn_session()
            if self._first_raw_turn:
                self._first_raw_turn = False
                if self._context_prefix:
                    message = self._context_prefix + "\n\n" + message.strip()
        try:
            if effective_timeout is None:
                with self._conversation_state.turn_lock:
                    return turn_session._send(
                        message.strip(),
                        output_schema_path=output_schema_path,
                        activity_callback=activity_callback,
                    )
            completed = threading.Event()
            outcome = {}

            def run_turn():
                try:
                    with self._conversation_state.turn_lock:
                        outcome["response"] = turn_session._send(
                            message.strip(),
                            output_schema_path=output_schema_path,
                            activity_callback=activity_callback,
                        )
                except BaseException as exc:
                    outcome["error"] = exc
                finally:
                    completed.set()

            threading.Thread(target=run_turn, daemon=True).start()
            if not completed.wait(effective_timeout):
                turn_session.interrupt()
                completed.wait()
                raise CodexTimeoutError(
                    f"codex session turn timed out after {effective_timeout} seconds"
                )
            if "error" in outcome:
                raise outcome["error"]
            return outcome["response"]
        except CodexTimeoutError:
            self.close()
            raise
        finally:
            if temp_schema_path is not None:
                try:
                    temp_schema_path.unlink()
                except OSError:
                    pass

    def _send_turn(
        self,
        message,
        *,
        output_schema_path=None,
        output_pydantic_model=None,
        timeout=None,
        activity_callback=None,
    ):
        response = self._execute_turn(
            message,
            output_schema_path=output_schema_path,
            output_pydantic_model=output_pydantic_model,
            timeout=timeout,
            activity_callback=activity_callback,
        )
        return self._record_turn(message.strip(), response)

    def close(self):
        with self._session_lock:
            if self._session_closed:
                return
            self._session_closed = True
            turn_session = self._turn_session
            runner = self._session_runner
            cleanup = self._turn_cleanup
            self._turn_session = None
            self._session_runner = None
            self._turn_cleanup = None
        if turn_session is not None:
            try:
                turn_session.close()
            except Exception:
                pass
        if runner is not None:
            try:
                if runner is self:
                    from .client import _close_runtime

                    _close_runtime(runner)
                else:
                    runner.close()
            except Exception:
                pass
        if cleanup is not None:
            try:
                cleanup()
            except Exception:
                pass

    def resume(
        self,
        *,
        input,
        config: SessionConfig | None = None,
        output_schema_path=None,
        output_pydantic_model=None,
        logging: LoggingConfig | None = None,
    ):
        """Create a new Session that continues this conversation."""
        with self._session_lock:
            state = self._conversation_state
        if state is None or state.thread_id is None:
            raise CodexNotStartedError(
                "codex session conversation has not started"
            )
        return CodexSession(
            config=self.config if config is None else config,
            input=input,
            output_schema_path=output_schema_path,
            output_pydantic_model=output_pydantic_model,
            logging=logging,
            _conversation_state=state,
        )

    def fork(
        self,
        *,
        input,
        config: SessionConfig | None = None,
        output_schema_path=None,
        output_pydantic_model=None,
        logging: LoggingConfig | None = None,
    ):
        """Create a new Session that branches from this conversation."""
        with self._session_lock:
            state = self._conversation_state
        if state is None or state.thread_id is None:
            raise CodexNotStartedError(
                "codex session conversation has not started"
            )
        with state.turn_lock:
            forked_state = state.fork()
            branched = CodexSession(
                config=self.config if config is None else config,
                input=input,
                output_schema_path=output_schema_path,
                output_pydantic_model=output_pydantic_model,
                logging=logging,
                _conversation_state=forked_state,
            )
            try:
                turn_session = branched._get_or_create_turn_session()
                open_session = getattr(turn_session, "open", None)
                if open_session is None:
                    open_session = getattr(turn_session, "get_or_create", None)
                if open_session is not None:
                    open_session()
            except BaseException:
                branched.close()
                raise
            return branched

    def _create_runner(self):
        from .client import _configure_runtime

        kwargs = self.config._runner_kwargs()
        if self.logging is not None:
            kwargs["log_dir"] = self.logging.log_dir
        return _configure_runtime(self, **kwargs)

    def _get_or_create_turn_session(self):
        if self._turn_session is not None:
            return self._turn_session
        runner = self._create_runner()
        workspace = self.config.workspace
        try:
            if runner is self:
                from .client import _open_session_runtime

                turn_session, context_prefix, cleanup = _open_session_runtime(
                    runner,
                    config=self.config,
                    read_paths=workspace.read_paths,
                    write_paths=workspace.write_paths,
                    conversation_state=self._conversation_state,
                )
            else:
                turn_session, context_prefix, cleanup = runner.start_session(
                    config=self.config,
                    read_paths=workspace.read_paths,
                    write_paths=workspace.write_paths,
                    conversation_state=self._conversation_state,
                )
        except Exception:
            if runner is self:
                from .client import _close_runtime

                _close_runtime(runner)
            else:
                runner.close()
            raise
        if isinstance(turn_session, CodexSession):
            turn_session = turn_session._turn_session
            if turn_session is None:
                if runner is self:
                    from .client import _close_runtime

                    _close_runtime(runner)
                else:
                    runner.close()
                cleanup()
                raise RuntimeError("nested session did not provide a turn session")
        self._session_runner = runner
        self._turn_session = turn_session
        self._turn_cleanup = cleanup
        self._context_prefix = context_prefix
        self._conversation_state = getattr(
            turn_session,
            "conversation_state",
            getattr(turn_session, "_conversation_state", None),
        )
        if self._conversation_state is None:
            raise RuntimeError("turn session did not provide conversation state")
        return turn_session

    def _ensure_session_open(self):
        if self._session_closed:
            raise CodexSessionClosedError("codex session is closed")

    def _record_turn(self, message, response):
        self._conversation_state.record_turn(
            message,
            getattr(response, "content", str(response)),
        )
        if self.logging is None:
            return response
        path = self.logging.path
        if path is None and self.logging.directory is not None:
            path = self.logging.directory / f"{self.id}.jsonl"
        if path is None:
            return response
        path = pathlib.Path(path)
        if not path.is_absolute():
            cwd = self.config.workspace.cwd or pathlib.Path.cwd()
            path = pathlib.Path(cwd).expanduser().resolve() / path
        path.parent.mkdir(parents=True, exist_ok=True)
        content = getattr(response, "content", str(response))
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "type": "session.turn",
                        "session_id": self.id,
                        "input": message,
                        "output": content,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        if hasattr(response, "log_path"):
            return replace(response, log_path=str(path))
        return response


class _SdkSession:
    """Own one lazily-created Codex SDK client, thread, and temporary home."""

    def __init__(
        self,
        *,
        create_session,
        cleanup_tree,
        conversation_state=None,
        native_usage_collector=None,
    ):
        self._create_session = create_session
        self._cleanup_tree = cleanup_tree
        self._lock = threading.RLock()
        self._closed = False
        self._resources = None
        self._active_turn = None
        self._conversation_state = conversation_state or _ConversationState(
            _ConversationStore(cleanup_tree)
        )
        self._native_usage_collector = native_usage_collector

    @property
    def conversation_state(self):
        return self._conversation_state

    def get_or_create(self):
        with self._lock:
            if self._closed:
                raise RuntimeError("Codex SDK session is closed")
            if self._resources is None:
                resources = self._create_session()
                codex, thread, config_home = resources
                try:
                    if self._native_usage_collector is not None:
                        self._native_usage_collector.attach(
                            codex, thread_id=getattr(thread, "id", None)
                        )
                    self._conversation_state.store.attach_home(config_home)
                    thread_id = getattr(thread, "id", None)
                    if thread_id is not None:
                        self._conversation_state.bind_thread(thread_id)
                except BaseException:
                    try:
                        codex.close()
                    except Exception:
                        pass
                    raise
                self._resources = resources
            return self._resources

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            resources = self._resources
            self._resources = None
            active_turn = self._active_turn
            self._active_turn = None
        if active_turn is not None:
            try:
                active_turn.interrupt()
            except Exception:
                pass
        if resources is None:
            return
        codex, _thread, _config_home = resources
        try:
            codex.close()
        except Exception:
            pass

    def set_active_turn(self, turn):
        with self._lock:
            if self._closed:
                raise RuntimeError("Codex SDK session is closed")
            self._active_turn = turn

    def clear_active_turn(self, turn):
        with self._lock:
            if self._active_turn is turn:
                self._active_turn = None

    def interrupt(self):
        with self._lock:
            turn = self._active_turn
        if turn is None:
            return
        turn.interrupt()
