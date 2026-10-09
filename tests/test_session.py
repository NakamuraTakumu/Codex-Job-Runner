import pathlib
import sys
import tempfile
import threading
import unittest
from unittest import mock
from types import SimpleNamespace


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from codex_job_runner import (  # noqa: E402
    CodexAlreadyStartedError,
    CodexJob,
    CodexNotStartedError,
    CodexSession,
    CodexSessionClosedError,
    CodexTimeoutError,
    CodexValidationError,
    LoggingConfig,
    SessionConfig,
    ValidationConfig,
    WorkspaceConfig,
)
from codex_job_runner._session import (  # noqa: E402
    _ConversationState,
    _ConversationStore,
)
from codex_job_runner.client import _sdk_thread_open  # noqa: E402


class _FakeTurnSession:
    def __init__(self, conversation_state=None):
        self.messages = []
        self.close_calls = 0
        self.failure = None
        self.conversation_state = conversation_state or _ConversationState(
            _ConversationStore(lambda path: None),
            thread_id="thread-1",
        )

    def _send(self, message, **kwargs):
        self.messages.append((message, kwargs))
        if self.failure is not None:
            raise self.failure
        return f"response:{message}"

    def close(self):
        self.close_calls += 1


class _FakeRunner:
    def __init__(self, turn_session):
        self.turn_session = turn_session
        self.start_session_calls = []
        self.close_calls = 0

    def start_session(self, **kwargs):
        self.start_session_calls.append(kwargs)
        state = kwargs.get("conversation_state")
        if state is not None:
            if state.thread_id is None:
                state.bind_thread("thread-fork")
            self.turn_session.conversation_state = state
        return self.turn_session, "", lambda: None

    def close(self):
        self.close_calls += 1


class CodexSessionTests(unittest.TestCase):
    def setUp(self):
        self.config = SessionConfig(
            workspace=WorkspaceConfig(cwd=ROOT),
        )
        self.turn_session = _FakeTurnSession()
        self.runner = _FakeRunner(self.turn_session)
        self.runner_patch = mock.patch.object(
            CodexSession,
            "_create_runner",
            return_value=self.runner,
        )
        self.create_runner = self.runner_patch.start()
        self.addCleanup(self.runner_patch.stop)

    def test_construction_is_lazy_and_run_sends_initial_input_once(self):
        session = CodexSession(config=self.config, input="inspect")

        self.create_runner.assert_not_called()
        self.assertEqual(self.runner.start_session_calls, [])

        self.assertEqual(session.run(), "response:inspect")

        self.create_runner.assert_called_once_with()
        self.assertEqual(len(self.runner.start_session_calls), 1)
        self.assertEqual(
            [message for message, _kwargs in self.turn_session.messages],
            ["inspect"],
        )
        with self.assertRaises(CodexAlreadyStartedError):
            session.run()

    def test_public_send_reuses_one_underlying_sdk_thread(self):
        session = CodexSession(config=self.config, input="first")

        first = session.run()
        second = session.send("follow up")

        self.assertEqual(first, "response:first")
        self.assertEqual(second, "response:follow up")
        self.assertEqual(len(self.runner.start_session_calls), 1)
        self.assertEqual(
            [message for message, _kwargs in self.turn_session.messages],
            ["first", "follow up"],
        )

    def test_public_send_can_start_a_session(self):
        session = CodexSession(config=self.config, input="inspect")

        self.assertEqual(session.send("start directly"), "response:start directly")
        with self.assertRaises(CodexAlreadyStartedError):
            session.run()

    def test_send_repairs_value_error_on_the_same_session(self):
        responses = iter(
            [
                SimpleNamespace(content="invalid"),
                SimpleNamespace(content="valid"),
            ]
        )

        def send(message, **kwargs):
            self.turn_session.messages.append((message, kwargs))
            return next(responses)

        self.turn_session._send = send
        session = CodexSession(config=self.config)

        response = session.send(
            "produce output",
            validators=[
                lambda candidate: None
                if candidate.content == "valid"
                else (_ for _ in ()).throw(ValueError("must be valid"))
            ],
            validation=ValidationConfig(repair=True),
        )

        self.assertEqual(response.content, "valid")
        self.assertEqual(len(self.turn_session.messages), 2)
        self.assertIn("must be valid", self.turn_session.messages[1][0])

    def test_send_raises_validation_error_without_repair(self):
        self.turn_session._send = lambda message, **kwargs: SimpleNamespace(
            content="invalid"
        )
        session = CodexSession(config=self.config)

        with self.assertRaises(CodexValidationError) as caught:
            session.send(
                "produce output",
                validators=[lambda _response: (_ for _ in ()).throw(ValueError("bad"))],
            )

        self.assertEqual(caught.exception.validation_diagnostic, "bad")

    def test_resume_and_fork_require_a_started_conversation(self):
        session = CodexSession(config=self.config, input="inspect")

        with self.assertRaises(CodexNotStartedError):
            session.resume(input="continue")
        with self.assertRaises(CodexNotStartedError):
            session.fork(input="branch")

    def test_resume_shares_thread_and_fork_shares_only_store(self):
        session = CodexSession(config=self.config, input="inspect")
        session.run()
        session.close()

        resumed = session.resume(input="continue")
        forked = session.fork(input="branch")

        self.assertIs(
            resumed._conversation_state,
            session._conversation_state,
        )
        self.assertIsNot(
            forked._conversation_state,
            session._conversation_state,
        )
        self.assertIs(
            forked._conversation_state.store,
            session._conversation_state.store,
        )
        self.assertEqual(forked._conversation_state.thread_id, "thread-fork")
        self.assertIsNone(forked._conversation_state.forked_from_id)

    def test_sdk_thread_open_uses_resume_and_fork(self):
        calls = []

        class FakeCodex:
            def thread_resume(self, thread_id, **kwargs):
                calls.append(("resume", thread_id, kwargs))
                return SimpleNamespace(id=thread_id)

            def thread_fork(self, thread_id, **kwargs):
                calls.append(("fork", thread_id, kwargs))
                return SimpleNamespace(id="thread-2")

        store = _ConversationStore(lambda path: None)
        codex = FakeCodex()
        resumed = _ConversationState(store, thread_id="thread-1")
        forked = resumed.fork()

        _sdk_thread_open(
            codex,
            SimpleNamespace(),
            conversation_state=resumed,
            reasoning_effort=None,
            cwd=str(ROOT),
            model="gpt-test",
            ephemeral=False,
        )
        _sdk_thread_open(
            codex,
            SimpleNamespace(),
            conversation_state=forked,
            reasoning_effort=None,
            cwd=str(ROOT),
            model="gpt-test",
            ephemeral=False,
        )

        self.assertEqual(
            [(kind, thread_id) for kind, thread_id, _kwargs in calls],
            [("resume", "thread-1"), ("fork", "thread-1")],
        )
        self.assertNotIn("ephemeral", calls[0][2])
        self.assertIs(calls[1][2]["ephemeral"], False)

    def test_timed_turn_is_interrupted_before_timeout_returns(self):
        class BlockingSession:
            def __init__(self):
                self._conversation_state = _ConversationState(
                    _ConversationStore(lambda path: None),
                    thread_id="thread-1",
                )
                self.started = threading.Event()
                self.interrupted = threading.Event()

            def _send(self, message, **kwargs):
                del message, kwargs
                self.started.set()
                self.interrupted.wait()
                return "interrupted"

            def interrupt(self):
                self.interrupted.set()

            def close(self):
                return None

        underlying = BlockingSession()
        runner = _FakeRunner(underlying)
        self.create_runner.return_value = runner
        session = CodexSession(
            config=self.config.with_overrides(timeout=0.01),
        )

        with self.assertRaises(CodexTimeoutError):
            session.send("wait")

        self.assertTrue(underlying.started.is_set())
        self.assertTrue(underlying.interrupted.is_set())

    def test_close_is_idempotent_and_rejects_followup_turns(self):
        session = CodexSession(config=self.config, input="inspect")
        session.run()

        session.close()
        session.close()

        self.assertEqual(self.turn_session.close_calls, 1)
        with self.assertRaises(CodexSessionClosedError):
            session.send("too late")
        with self.assertRaises(CodexSessionClosedError):
            session.run()

    def test_close_before_run_does_not_create_resources(self):
        session = CodexSession(config=self.config, input="inspect")

        session.close()
        session.close()

        self.create_runner.assert_not_called()
        self.assertEqual(self.runner.start_session_calls, [])

    def test_context_manager_closes_on_success_and_failure(self):
        with CodexSession(config=self.config, input="inspect") as session:
            self.assertIsNotNone(session.run())

        self.assertEqual(self.turn_session.close_calls, 1)

        other_turn_session = _FakeTurnSession()
        other_runner = _FakeRunner(other_turn_session)
        self.create_runner.return_value = other_runner
        with self.assertRaisesRegex(ValueError, "body failed"):
            with CodexSession(config=self.config, input="inspect") as session:
                session.run()
                raise ValueError("body failed")

        self.assertEqual(other_turn_session.close_calls, 1)

    def test_timeout_closes_session(self):
        session = CodexSession(config=self.config, input="inspect")
        self.turn_session.failure = CodexTimeoutError("turn timed out")

        with self.assertRaises(CodexTimeoutError):
            session.run()

        self.assertEqual(self.turn_session.close_calls, 1)
        with self.assertRaises(CodexSessionClosedError):
            session.send("unsafe continuation")

    def test_run_without_initial_input_is_rejected_without_allocating(self):
        session = CodexSession(config=self.config)

        with self.assertRaises(ValueError):
            session.run()

        self.create_runner.assert_not_called()

    def test_logging_config_records_session_turns(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = pathlib.Path(temp_dir) / "session.jsonl"
            session = CodexSession(
                config=self.config,
                input="inspect",
                logging=LoggingConfig(path=log_path),
            )

            session.run()

            self.assertIn('"type": "session.turn"', log_path.read_text())


class CodexJobInheritanceTests(unittest.TestCase):
    def test_job_is_a_public_session_subclass(self):
        self.assertTrue(issubclass(CodexJob, CodexSession))

    def test_job_inherits_public_send(self):
        self.assertTrue(hasattr(CodexJob, "send"))


if __name__ == "__main__":
    unittest.main()
