"""Real OS lock contention, safe cancellation and bounded launch waiting."""
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import agent_history as ah
import trash
from history_app.locks import file_lock, waiting_lock, LockWaitCancelled


class LockWaitingTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.path = self.root / 'collect.lock'

    def test_timeout_and_cancellation_never_open_the_history_database(self):
        with patch.object(ah, 'DATA', self.root), patch.object(ah, 'connect') as connect:
            with file_lock(self.path, False) as held:
                self.assertTrue(held)
                with self.assertRaises(TimeoutError):
                    with trash.locked_db(lock_timeout=0.05):
                        self.fail('Entered protected operation before lock release')
                with self.assertRaises(LockWaitCancelled):
                    with trash.locked_db(lock_timeout=1, cancel=lambda: True):
                        self.fail('Entered cancelled protected operation')
            connect.assert_not_called()
        self.assertFalse((self.root / 'history.db').exists())

    def test_cancel_at_acquisition_releases_the_lock_without_entering_operation(self):
        def cancelled():
            raise LockWaitCancelled()
        with self.assertRaises(LockWaitCancelled):
            with waiting_lock(self.path, 1, on_acquired=cancelled):
                self.fail('Cancelled operation ran')
        with file_lock(self.path, False) as free:
            self.assertTrue(free)

    def test_acquired_lock_stays_exclusive_for_the_whole_operation(self):
        with waiting_lock(self.path, 1) as acquired:
            self.assertTrue(acquired)
            with file_lock(self.path, False) as other:
                self.assertFalse(other)
        with file_lock(self.path, False) as other:
            self.assertTrue(other)

    @unittest.skipUnless(os.name == 'nt', 'Windows CRT blocking lock has a short retry limit')
    def test_windows_wait_survives_the_legacy_nine_second_retry_limit(self):
        waiting, finished = threading.Event(), threading.Event()
        outcome = []
        def waiter():
            with waiting_lock(self.path, 20, on_wait=waiting.set) as acquired:
                outcome.append(acquired)
            finished.set()
        with file_lock(self.path, False) as held:
            self.assertTrue(held)
            worker = threading.Thread(target=waiter, daemon=True)
            worker.start()
            self.assertTrue(waiting.wait(2))
            self.assertFalse(finished.wait(10.2))
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcome, [True])
