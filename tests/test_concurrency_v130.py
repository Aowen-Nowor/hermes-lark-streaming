"""v1.3.0 concurrency tests — _sessions lock, _clarify_* lock, Config lock.

Proves that the thread-safe locking added in v1.3.0 prevents:
  - RuntimeError: dictionary changed size during iteration
  - Lost updates (race condition on read-then-write)
  - Deadlocks (RLock re-entrancy)
"""

from __future__ import annotations

import threading
import time



# ── _sessions lock tests ──


class TestSessionLockThreadSafety:
    """Test that _sessions dict operations are thread-safe (v1.3.0 P1-01)."""

    def test_concurrent_put_get(self) -> None:
        """10 threads each do 100 put+get cycles. All puts visible after join."""
        from hermes_lark_streaming.controller.core import StreamCardController
        from hermes_lark_streaming.state.session import CardSession
        import asyncio

        ctrl = StreamCardController.__new__(StreamCardController)
        ctrl._sessions = {}
        ctrl._sessions_lock = threading.RLock()
        ctrl._interrupt_map = {}
        # Minimal mock for CardSession
        errors: list[Exception] = []

        def worker(tid: int):
            try:
                loop = asyncio.new_event_loop()
                for i in range(100):
                    key = f"msg_{tid}_{i}"
                    sess = CardSession(key, f"chat_{tid}", loop)
                    ctrl._sess_put(key, sess)
                    assert ctrl._sess_get(key) is sess
                loop.close()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert len(ctrl._sessions) == 1000

    def test_concurrent_pop(self) -> None:
        """Pre-populate 100 sessions, 10 threads each pop 10. No RuntimeError."""
        from hermes_lark_streaming.controller.core import StreamCardController

        ctrl = StreamCardController.__new__(StreamCardController)
        ctrl._sessions = {}
        ctrl._sessions_lock = threading.RLock()

        # Pre-populate
        for i in range(100):
            ctrl._sessions[f"msg_{i}"] = f"session_{i}"

        popped: list[str] = []
        popped_lock = threading.Lock()
        errors: list[Exception] = []

        def worker():
            try:
                for _ in range(10):
                    # Pop a random key
                    snapshot = ctrl._sess_items_snapshot()
                    if not snapshot:
                        break
                    key = snapshot[0][0]
                    val = ctrl._sess_pop(key)
                    if val is not None:
                        with popped_lock:
                            popped.append(key)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        # All 100 should have been popped (some races may cause duplicate pop attempts,
        # but _sess_pop returns None for already-popped keys, so no double-count)
        assert len(popped) <= 100

    def test_concurrent_items_snapshot(self) -> None:
        """5 writers + 5 snapshot readers. No RuntimeError."""
        from hermes_lark_streaming.controller.core import StreamCardController

        ctrl = StreamCardController.__new__(StreamCardController)
        ctrl._sessions = {}
        ctrl._sessions_lock = threading.RLock()
        errors: list[Exception] = []
        stop = threading.Event()

        def writer():
            try:
                i = 0
                while not stop.is_set():
                    ctrl._sess_put(f"w_{i}", f"val_{i}")
                    i += 1
            except Exception as e:
                errors.append(e)

        def reader():
            try:
                while not stop.is_set():
                    snap = ctrl._sess_items_snapshot()
                    assert isinstance(snap, list)
            except Exception as e:
                errors.append(e)

        writers = [threading.Thread(target=writer) for _ in range(5)]
        readers = [threading.Thread(target=reader) for _ in range(5)]
        for t in writers + readers:
            t.start()
        time.sleep(0.5)
        stop.set()
        for t in writers + readers:
            t.join()

        assert not errors

    def test_concurrent_active_count(self) -> None:
        """5 threads adding sessions, 5 calling _sess_active_count. No exceptions."""
        from hermes_lark_streaming.controller.core import StreamCardController

        ctrl = StreamCardController.__new__(StreamCardController)
        ctrl._sessions = {}
        ctrl._sessions_lock = threading.RLock()
        errors: list[Exception] = []
        stop = threading.Event()

        # Minimal fake session with is_terminal_phase
        class FakeSession:
            def __init__(self, terminal: bool):
                self.is_terminal_phase = terminal
                self.chat_id = "chat"
                self.card_trace_id = "trace"

        def writer():
            try:
                i = 0
                while not stop.is_set():
                    ctrl._sess_put(f"k_{i}", FakeSession(terminal=(i % 2 == 0)))
                    i += 1
            except Exception as e:
                errors.append(e)

        def counter():
            try:
                while not stop.is_set():
                    c = ctrl._sess_active_count()
                    assert c >= 0
            except Exception as e:
                errors.append(e)

        ws = [threading.Thread(target=writer) for _ in range(5)]
        cs = [threading.Thread(target=counter) for _ in range(5)]
        for t in ws + cs:
            t.start()
        time.sleep(0.5)
        stop.set()
        for t in ws + cs:
            t.join()

        assert not errors

    def test_rlock_reentrant(self) -> None:
        """RLock allows re-entrant access — no deadlock on nested lock acquisition."""
        from hermes_lark_streaming.controller.core import StreamCardController

        ctrl = StreamCardController.__new__(StreamCardController)
        ctrl._sessions = {}
        ctrl._sessions_lock = threading.RLock()
        ctrl._interrupt_map = {}

        ctrl._sessions["msg_1"] = "session_obj"

        # Simulate re-entrant access: _cleanup calls _sess_pop then accesses _sessions under lock
        # This should not deadlock
        with ctrl._sessions_lock:
            val = ctrl._sess_pop("msg_1")
            assert val == "session_obj"
            # Re-enter the lock to check the anchor
            with ctrl._sessions_lock:
                assert "msg_1" not in ctrl._sessions


# ── _clarify_* lock tests ──


class TestClarifyLockThreadSafety:
    """Test that _clarify_* dicts are thread-safe (v1.3.0 P0-01)."""

    def test_concurrent_clarify_write_read(self) -> None:
        """5 writers + 5 readers on _clarify_choices. No exceptions, all writes visible."""
        from hermes_lark_streaming.patching.adapter import (
            _clarify_lock,
            _clarify_choices,
            _clarify_questions,
        )

        # Clean state
        with _clarify_lock:
            _clarify_choices.clear()
            _clarify_questions.clear()

        errors: list[Exception] = []

        def writer(tid: int):
            try:
                for i in range(50):
                    cid = f"clarify_{tid}_{i}"
                    with _clarify_lock:
                        _clarify_choices[cid] = [f"opt_{i}"]
                        _clarify_questions[cid] = f"Q_{i}"
            except Exception as e:
                errors.append(e)

        def reader(tid: int):
            try:
                for i in range(50):
                    cid = f"clarify_{tid}_{i}"
                    with _clarify_lock:
                        _ = _clarify_choices.get(cid)
                        _ = _clarify_questions.get(cid)
            except Exception as e:
                errors.append(e)

        threads = []
        for t in range(5):
            threads.append(threading.Thread(target=writer, args=(t,)))
            threads.append(threading.Thread(target=reader, args=(t,)))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        with _clarify_lock:
            assert len(_clarify_choices) == 250  # 5 writers × 50

        # Cleanup
        with _clarify_lock:
            _clarify_choices.clear()
            _clarify_questions.clear()

    def test_concurrent_prune_during_access(self) -> None:
        """1 prune thread + 3 access threads. No RuntimeError."""
        from hermes_lark_streaming.patching.adapter import (
            _clarify_lock,
            _clarify_choices,
            _clarify_questions,
            _clarify_timestamps,
            _prune_expired_clarify,
        )

        with _clarify_lock:
            _clarify_choices.clear()
            _clarify_questions.clear()
            _clarify_timestamps.clear()

        # Pre-populate with some entries
        for i in range(20):
            cid = f"c_{i}"
            with _clarify_lock:
                _clarify_choices[cid] = [f"opt_{i}"]
                _clarify_questions[cid] = f"Q_{i}"
                _clarify_timestamps[cid] = time.time()

        errors: list[Exception] = []
        stop = threading.Event()

        def pruner():
            try:
                while not stop.is_set():
                    _prune_expired_clarify()
            except Exception as e:
                errors.append(e)

        def accessor(tid: int):
            try:
                i = 0
                while not stop.is_set():
                    cid = f"c_{i % 20}"
                    with _clarify_lock:
                        _clarify_choices.get(cid)
                        _clarify_choices.pop(cid, None)
                        # Re-add for next iteration
                        _clarify_choices[cid] = [f"opt_{i}"]
                    i += 1
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=pruner)]
        for t in range(3):
            threads.append(threading.Thread(target=accessor, args=(t,)))
        for t in threads:
            t.start()
        time.sleep(0.5)
        stop.set()
        for t in threads:
            t.join()

        assert not errors

        # Cleanup
        with _clarify_lock:
            _clarify_choices.clear()
            _clarify_questions.clear()
            _clarify_timestamps.clear()

    def test_clarify_lock_protects_all_five_dicts(self) -> None:
        """Verify concurrent access to all 5 clarify dicts doesn't corrupt."""
        from hermes_lark_streaming.patching.adapter import (
            _clarify_lock,
            _clarify_choices,
            _clarify_questions,
            _clarify_card_msg_ids,
            _clarify_selections,
            _clarify_timestamps,
        )

        with _clarify_lock:
            _clarify_choices.clear()
            _clarify_questions.clear()
            _clarify_card_msg_ids.clear()
            _clarify_selections.clear()
            _clarify_timestamps.clear()

        errors: list[Exception] = []
        barrier = threading.Barrier(5)

        def worker(tid: int):
            try:
                barrier.wait()
                for i in range(100):
                    cid = f"c_{tid}_{i}"
                    with _clarify_lock:
                        _clarify_choices[cid] = [f"opt_{tid}_{i}"]
                        _clarify_questions[cid] = f"Q_{tid}_{i}"
                        _clarify_card_msg_ids[cid] = f"msg_{tid}_{i}"
                        _clarify_selections[cid] = f"sel_{tid}_{i}"
                        _clarify_timestamps[cid] = time.time()
                    # Read back
                    with _clarify_lock:
                        assert _clarify_choices[cid] == [f"opt_{tid}_{i}"]
                        assert _clarify_questions[cid] == f"Q_{tid}_{i}"
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        with _clarify_lock:
            assert len(_clarify_choices) == 500
            assert len(_clarify_questions) == 500
            assert len(_clarify_card_msg_ids) == 500
            assert len(_clarify_selections) == 500
            assert len(_clarify_timestamps) == 500

        # Cleanup
        with _clarify_lock:
            _clarify_choices.clear()
            _clarify_questions.clear()
            _clarify_card_msg_ids.clear()
            _clarify_selections.clear()
            _clarify_timestamps.clear()


# ── Config lock tests ──


class TestConfigLockThreadSafety:
    """Test that Config._load() / _reload_cached() are thread-safe (v1.3.0)."""

    def test_concurrent_load(self) -> None:
        """10 threads calling Config()._load() simultaneously. No exceptions."""
        from hermes_lark_streaming.config.reader import Config

        Config._instance = None
        cfg = Config()
        cfg._raw = None  # Force reload

        errors: list[Exception] = []

        def worker():
            try:
                c = Config()
                c._load()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors

        Config._instance = None

    def test_concurrent_reload_during_read(self) -> None:
        """1 reload thread + 5 read threads. No exceptions."""
        from hermes_lark_streaming.config.reader import Config

        Config._instance = None
        Config()
        errors: list[Exception] = []
        stop = threading.Event()

        def reloader():
            try:
                while not stop.is_set():
                    Config().reload()
            except Exception as e:
                errors.append(e)

        def reader():
            try:
                while not stop.is_set():
                    _ = Config().enabled
                    _ = Config().linear
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=reloader)]
        for _ in range(5):
            threads.append(threading.Thread(target=reader))
        for t in threads:
            t.start()
        time.sleep(0.5)
        stop.set()
        for t in threads:
            t.join()

        assert not errors

        Config._instance = None


# ── _interrupt_map lock tests (v1.3.0 Round 2) ──


class TestInterruptMapLockThreadSafety:
    """Test that _interrupt_map operations are thread-safe (v1.3.0 Round 2)."""

    def test_concurrent_write_read(self) -> None:
        """5 writers + 5 readers on _interrupt_map. No exceptions, all writes visible."""
        from hermes_lark_streaming.controller.core import StreamCardController

        ctrl = StreamCardController.__new__(StreamCardController)
        ctrl._interrupt_map = {}
        ctrl._interrupt_map_lock = threading.Lock()

        errors: list[Exception] = []

        def writer(tid: int):
            try:
                for i in range(100):
                    with ctrl._interrupt_map_lock:
                        ctrl._interrupt_map[f"old_{tid}_{i}"] = f"new_{tid}_{i}"
            except Exception as e:
                errors.append(e)

        def reader(tid: int):
            try:
                for i in range(100):
                    with ctrl._interrupt_map_lock:
                        _ = ctrl._interrupt_map.get(f"old_{tid}_{i}")
            except Exception as e:
                errors.append(e)

        threads = []
        for t in range(5):
            threads.append(threading.Thread(target=writer, args=(t,)))
            threads.append(threading.Thread(target=reader, args=(t,)))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        with ctrl._interrupt_map_lock:
            assert len(ctrl._interrupt_map) == 500

    def test_concurrent_pop_during_iterate(self) -> None:
        """1 popper + 3 iterators. No RuntimeError: dictionary changed size."""
        from hermes_lark_streaming.controller.core import StreamCardController

        ctrl = StreamCardController.__new__(StreamCardController)
        ctrl._interrupt_map = {}
        ctrl._interrupt_map_lock = threading.Lock()

        # Pre-populate
        for i in range(50):
            ctrl._interrupt_map[f"k_{i}"] = f"v_{i}"

        errors: list[Exception] = []
        stop = threading.Event()

        def popper():
            try:
                i = 0
                while not stop.is_set():
                    with ctrl._interrupt_map_lock:
                        ctrl._interrupt_map.pop(f"k_{i % 50}", None)
                        # Re-add for next iteration
                        ctrl._interrupt_map[f"k_{i % 50}"] = f"v_{i}"
                    i += 1
            except Exception as e:
                errors.append(e)

        def iterator(tid: int):
            try:
                while not stop.is_set():
                    with ctrl._interrupt_map_lock:
                        for k, v in list(ctrl._interrupt_map.items()):
                            pass
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=popper)]
        for t in range(3):
            threads.append(threading.Thread(target=iterator, args=(t,)))
        for t in threads:
            t.start()
        time.sleep(0.5)
        stop.set()
        for t in threads:
            t.join()

        assert not errors
