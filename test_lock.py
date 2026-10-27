#!/usr/bin/env python3
"""Unit tests for the single-instance lock (capacity_bot.acquire_lock): dead PID, stale heartbeat, live holder.

Run:  python -m unittest test_lock -v
The lock and heartbeat paths point into a temporary folder; _pid_alive is stubbed. No network, no Telegram.
"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="capacity_lock_")
os.environ.setdefault("CAPACITY_DATA_DIR", _TMP)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import capacity_bot as bot                              # noqa: E402

HOLDER = 999_999                                        # never this test's PID


class Lock(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp(prefix="lock_", dir=_TMP))
        self._orig = (bot.LOCK_FILE, bot.HEARTBEAT_FILE, bot._pid_alive)
        bot.LOCK_FILE, bot.HEARTBEAT_FILE = d / "capacity_bot.lock", d / "bot_heartbeat"   # DATA_DIR binds at first import

    def tearDown(self):
        bot.LOCK_FILE, bot.HEARTBEAT_FILE, bot._pid_alive = self._orig

    def _held_by(self, pid, lock_age_s=0.0, beat_age_s=None, beat_pid=None):
        bot.LOCK_FILE.write_text(str(pid))
        t = time.time() - lock_age_s
        os.utime(bot.LOCK_FILE, (t, t))
        if beat_age_s is not None:
            bot.HEARTBEAT_FILE.write_text(f"{time.time() - beat_age_s:.0f}\n2026-10-27T14:00:00\n"
                                          f"pid={beat_pid or pid} last_update_id=1 last_cmd=-\n", encoding="utf-8")

    def _lock_pid(self):
        return bot.LOCK_FILE.read_text().strip()

    def test_no_lock_is_acquired(self):
        self.assertTrue(bot.acquire_lock(wait_s=0))
        self.assertEqual(self._lock_pid(), str(os.getpid()))

    def test_dead_pid_is_taken_over(self):
        bot._pid_alive = lambda pid: False
        self._held_by(HOLDER, lock_age_s=5, beat_age_s=5)                  # fresh heartbeat does not matter: PID is gone
        self.assertTrue(bot.acquire_lock(wait_s=0))
        self.assertEqual(self._lock_pid(), str(os.getpid()))

    def test_live_holder_with_fresh_heartbeat_is_refused(self):
        bot._pid_alive = lambda pid: True
        self._held_by(HOLDER, lock_age_s=3600, beat_age_s=10)
        self.assertFalse(bot.acquire_lock(wait_s=0))
        self.assertEqual(self._lock_pid(), str(HOLDER))                     # untouched

    def test_live_holder_just_started_is_refused(self):
        bot._pid_alive = lambda pid: True
        self._held_by(HOLDER, lock_age_s=5, beat_age_s=3600, beat_pid=1234)  # old heartbeat is the previous bot's
        self.assertFalse(bot.acquire_lock(wait_s=0))
        self.assertEqual(self._lock_pid(), str(HOLDER))

    def test_live_pid_with_stale_heartbeat_is_taken_over(self):
        bot._pid_alive = lambda pid: True                                  # hung bot, or a reused PID
        self._held_by(HOLDER, lock_age_s=3600, beat_age_s=bot.LOCK_STALE_S + 30)
        self.assertTrue(bot.acquire_lock(wait_s=0))
        self.assertEqual(self._lock_pid(), str(os.getpid()))

    def test_heartbeat_of_another_pid_does_not_keep_the_lock(self):
        bot._pid_alive = lambda pid: True
        self._held_by(HOLDER, lock_age_s=3600, beat_age_s=5, beat_pid=1234)
        self.assertTrue(bot.acquire_lock(wait_s=0))

    def test_holder_exiting_during_the_wait_is_taken_over(self):
        calls = []
        bot._pid_alive = lambda pid: calls.append(pid) or len(calls) < 3   # the 10/27 race: alive, alive, then gone
        self._held_by(HOLDER, lock_age_s=1, beat_age_s=1)
        self.assertTrue(bot.acquire_lock(wait_s=5))
        self.assertEqual(self._lock_pid(), str(os.getpid()))
        self.assertGreaterEqual(len(calls), 3)

    def test_sleep_beating_refreshes_heartbeat(self):
        orig_every = bot.HEARTBEAT_EVERY_S
        try:
            bot.HEARTBEAT_EVERY_S = 0.05
            bot._sleep_beating(0.12, 7, "error X")
        finally:
            bot.HEARTBEAT_EVERY_S = orig_every
        lines = bot.HEARTBEAT_FILE.read_text(encoding="utf-8").splitlines()
        self.assertLess(time.time() - float(lines[0]), 5)
        self.assertIn(f"pid={os.getpid()} last_update_id=7 last_cmd=error X", lines[2])


if __name__ == "__main__":
    unittest.main(verbosity=2)
