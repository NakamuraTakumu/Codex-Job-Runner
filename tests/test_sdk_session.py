import gc
import unittest
import weakref

from codex_job_runner._session import (
    _ConversationState,
    _ConversationStore,
    _SdkSession,
)


class _FakeCodex:
    def __init__(self, *, close_error=None):
        self.close_calls = 0
        self._close_error = close_error

    def close(self):
        self.close_calls += 1
        if self._close_error is not None:
            raise self._close_error


class _FakeThread:
    def __init__(self, thread_id="thread-1"):
        self.id = thread_id


class SdkSessionTests(unittest.TestCase):
    def test_get_or_create_is_lazy_and_reuses_the_created_session(self):
        codex = _FakeCodex()
        thread = _FakeThread()
        config_home = object()
        create_calls = []

        def create_session():
            create_calls.append(None)
            return codex, thread, config_home

        session = _SdkSession(
            create_session=create_session,
            cleanup_tree=lambda path: None,
        )

        self.assertEqual(create_calls, [])
        self.assertEqual(
            session.get_or_create(),
            (codex, thread, config_home),
        )
        self.assertEqual(
            session.get_or_create(),
            (codex, thread, config_home),
        )
        self.assertEqual(len(create_calls), 1)

    def test_failed_creation_is_not_cached(self):
        codex = _FakeCodex()
        thread = _FakeThread()
        config_home = object()
        failure = RuntimeError("create failed")
        attempts = 0

        def create_session():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise failure
            return codex, thread, config_home

        session = _SdkSession(
            create_session=create_session,
            cleanup_tree=lambda path: None,
        )

        with self.assertRaises(RuntimeError) as captured:
            session.get_or_create()

        self.assertIs(captured.exception, failure)
        self.assertEqual(
            session.get_or_create(),
            (codex, thread, config_home),
        )
        self.assertEqual(attempts, 2)

    def test_close_before_creation_does_not_create_resources(self):
        create_calls = []
        cleanup_calls = []
        session = _SdkSession(
            create_session=lambda: create_calls.append(None),
            cleanup_tree=cleanup_calls.append,
        )

        session.close()

        self.assertEqual(create_calls, [])
        self.assertEqual(cleanup_calls, [])

    def test_closed_session_rejects_creation(self):
        session = _SdkSession(
            create_session=lambda: (_FakeCodex(), _FakeThread(), object()),
            cleanup_tree=lambda path: None,
        )
        session.close()

        with self.assertRaisesRegex(RuntimeError, "session is closed"):
            session.get_or_create()

    def test_close_is_idempotent(self):
        codex = _FakeCodex()
        config_home = object()
        cleanup_calls = []
        session = _SdkSession(
            create_session=lambda: (codex, _FakeThread(), config_home),
            cleanup_tree=cleanup_calls.append,
        )
        session.get_or_create()

        session.close()
        session.close()

        self.assertEqual(codex.close_calls, 1)
        self.assertEqual(cleanup_calls, [])
        session.conversation_state.store.delete()
        self.assertEqual(cleanup_calls, [config_home])

    def test_close_attempts_codex_and_tree_cleanup_independently(self):
        codex = _FakeCodex(close_error=RuntimeError("codex close failed"))
        config_home = object()
        cleanup_calls = []

        def failing_cleanup(path):
            cleanup_calls.append(path)
            raise RuntimeError("tree cleanup failed")

        session = _SdkSession(
            create_session=lambda: (codex, _FakeThread(), config_home),
            cleanup_tree=failing_cleanup,
        )
        session.get_or_create()

        session.close()

        self.assertEqual(codex.close_calls, 1)
        self.assertEqual(cleanup_calls, [])
        session.conversation_state.store.delete()
        self.assertEqual(cleanup_calls, [config_home])
        with self.assertRaisesRegex(RuntimeError, "session is closed"):
            session.get_or_create()

    def test_store_is_cleaned_when_last_sdk_session_is_collected(self):
        cleanup_calls = []
        config_home = object()
        session = _SdkSession(
            create_session=lambda: (
                _FakeCodex(),
                _FakeThread(),
                config_home,
            ),
            cleanup_tree=cleanup_calls.append,
        )
        session.get_or_create()
        session.close()
        reference = weakref.ref(session)

        del session
        gc.collect()

        self.assertIsNone(reference())
        self.assertEqual(cleanup_calls, [config_home])

    def test_forked_states_keep_one_shared_store_alive(self):
        cleanup_calls = []
        config_home = object()
        store = _ConversationStore(cleanup_calls.append)
        store.attach_home(config_home)
        source = _ConversationState(store, thread_id="thread-1")
        forked = source.fork()

        del source
        del store
        gc.collect()
        self.assertEqual(cleanup_calls, [])

        del forked
        gc.collect()
        self.assertEqual(cleanup_calls, [config_home])


if __name__ == "__main__":
    unittest.main()
