#!/usr/bin/env python3
"""Unit tests for /drinks parsing, night attribution and the alcohol log (capacity_bot.py + garmin_capacity.py).

Run:  python -m unittest test_drinks -v
Uses a temporary CAPACITY_DATA_DIR so data\\alcohol.csv is never touched. No network, no Telegram.
"""
import os
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="capacity_test_")
os.environ["CAPACITY_DATA_DIR"] = _TMP                 # before the imports: both modules read it at import time
sys.path.insert(0, str(Path(__file__).resolve().parent))
import garmin_capacity as gc                            # noqa: E402
import capacity_bot as bot                              # noqa: E402


class ParseDrinks(unittest.TestCase):
    def test_before_2pm_is_last_night(self):
        night, n = bot.parse_drinks("2", datetime(2026, 10, 23, 13, 30))
        self.assertEqual((night, n), (date(2026, 10, 22), 2.0))

    def test_from_2pm_is_tonight(self):
        night, n = bot.parse_drinks("2", datetime(2026, 10, 23, 14, 30))
        self.assertEqual((night, n), (date(2026, 10, 23), 2.0))

    def test_exactly_2pm_is_tonight(self):
        night, _ = bot.parse_drinks("1", datetime(2026, 10, 23, 14, 0))
        self.assertEqual(night, date(2026, 10, 23))

    def test_explicit_date_overrides(self):
        night, n = bot.parse_drinks("2 10/22", datetime(2026, 10, 23, 16, 0))
        self.assertEqual((night, n), (date(2026, 10, 22), 2.0))
        night, _ = bot.parse_drinks("0 2026-10-20", datetime(2026, 10, 23, 9, 0))
        self.assertEqual(night, date(2026, 10, 20))

    def test_month_day_in_the_future_means_last_year(self):
        night, _ = bot.parse_drinks("1 12/31", datetime(2026, 10, 23, 9, 0))
        self.assertEqual(night, date(2025, 12, 31))

    def test_halves_and_zero(self):
        self.assertEqual(bot.parse_drinks("1.5", datetime(2026, 10, 23, 9, 0))[1], 1.5)
        self.assertEqual(bot.parse_drinks("0", datetime(2026, 10, 23, 9, 0))[1], 0.0)

    def test_bad_input(self):
        for bad in ("", "abc", "11", "-1", "2.3", "2 tomorrow-ish", "2 10/22 extra"):
            with self.assertRaises(ValueError, msg=bad):
                bot.parse_drinks(bad, datetime(2026, 10, 23, 9, 0))


class CmdDrinks(unittest.TestCase):
    def setUp(self):
        if gc.ALCOHOL_CSV.exists():
            gc.ALCOHOL_CSV.unlink()

    def test_reply_and_log(self):
        reply = bot.cmd_drinks("2", datetime(2026, 10, 23, 9, 0))
        self.assertEqual(reply, "Logged 2 drinks for Thu 10/22.")
        self.assertEqual(gc.alcohol_for_night(date(2026, 10, 22)), 2.0)
        self.assertEqual(bot.cmd_drinks("1", datetime(2026, 10, 23, 9, 0)), "Logged 1 drink for Thu 10/22.")

    def test_relog_same_night_last_wins_and_both_rows_kept(self):
        bot.cmd_drinks("2", datetime(2026, 10, 23, 9, 0))
        bot.cmd_drinks("3.5", datetime(2026, 10, 23, 9, 5))
        self.assertEqual(gc.alcohol_for_night(date(2026, 10, 22)), 3.5)
        rows = gc.ALCOHOL_CSV.read_text(encoding="utf-8").splitlines()
        self.assertEqual(rows[0], "night_date,drinks,logged_at,source")
        self.assertEqual(len(rows), 3)                        # header + both entries (append-only)
        self.assertTrue(all(r.endswith(",telegram") for r in rows[1:]))

    def test_not_logged_is_none_not_zero(self):
        self.assertIsNone(gc.alcohol_for_night(date(2026, 10, 21)))
        bot.cmd_drinks("0", datetime(2026, 10, 23, 9, 0))
        self.assertEqual(gc.alcohol_for_night(date(2026, 10, 22)), 0.0)   # an explicit 0 is a real value
        self.assertIsNone(gc.alcohol_for_night(date(2026, 10, 21)))

    def test_bad_input_gets_usage_and_logs_nothing(self):
        reply = bot.cmd_drinks("lots", datetime(2026, 10, 23, 9, 0))
        self.assertTrue(reply.startswith("Usage: /drinks N"))
        self.assertFalse(gc.ALCOHOL_CSV.exists())

    def test_handle_routes_drinks_to_owner_only(self):
        sent = []
        bot.send = lambda chat_id, text: sent.append(text) or True
        msg = {"chat": {"id": 42}, "from": {"id": 42}, "text": "/drinks 2 10/22", "date": 1_800_000_000}
        self.assertEqual(bot.handle(msg, "42", now=1_800_000_000), "drinks")
        self.assertEqual(sent[-1], "Logged 2 drinks for Thu 10/22.")
        self.assertEqual(bot.handle({**msg, "chat": {"id": 7}}, "42", now=1_800_000_000), "ignored_non_owner")
        self.assertIn("/drinks", bot.help_text())


class SleepTiming(unittest.TestCase):
    RAW = {"sleep": {"dailySleepDTO": {"sleepStartTimestampLocal": 1792633439000, "sleepEndTimestampLocal": 1792668299000}}}
    SN = {"bed_entry": "2026-10-23T01:05", "bed_exit": "2026-10-23T10:20"}

    def test_garmin_first(self):
        t = gc.sleep_timing(self.RAW, self.SN)
        self.assertEqual(t["timing_source"], "garmin")
        self.assertEqual((t["sleep_onset"], t["wake_time"]), ("2026-10-22T01:43", "2026-10-22T11:24"))
        self.assertEqual(t["sleep_midpoint"], "2026-10-22T06:34")

    def test_sleepiq_fallback_then_null(self):
        t = gc.sleep_timing({"sleep": {"dailySleepDTO": {}}}, self.SN)
        self.assertEqual((t["timing_source"], t["sleep_onset"], t["wake_time"], t["sleep_midpoint"]),
                         ("sleepiq", "2026-10-23T01:05", "2026-10-23T10:20", "2026-10-23T05:42"))
        t = gc.sleep_timing(None, None)
        self.assertEqual(t, {"sleep_onset": None, "wake_time": None, "sleep_midpoint": None, "timing_source": None})
        self.assertEqual(gc.slept_line(t), "Slept: no timing (watch off, no SleepIQ)")
        self.assertEqual(gc.alcohol_line({"alcohol_drinks": None}), "Alcohol last night: not logged — /drinks N")
        self.assertEqual(gc.alcohol_line({"alcohol_drinks": 2.0}), "Alcohol last night: 2")


if __name__ == "__main__":
    unittest.main(verbosity=2)
