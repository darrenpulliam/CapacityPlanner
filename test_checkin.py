#!/usr/bin/env python3
"""Unit tests for the nightly check-in (capacity_bot.py + garmin_capacity.py).

Run:  python -m unittest test_checkin -v
Temporary CAPACITY_DATA_DIR; the Telegram API is stubbed (every call recorded). No network.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="capacity_checkin_")
os.environ["CAPACITY_DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent))
import garmin_capacity as gc                            # noqa: E402
import capacity_bot as bot                              # noqa: E402

OWNER = "42"
DAY = date(2026, 10, 25)
T2315 = datetime(2026, 10, 25, 23, 15)
_ORIG_SEND = bot.send


class FakeApi:
    def __init__(self):
        self.calls, self.mid = [], 100

    def __call__(self, method, payload=None, timeout=15):
        self.calls.append((method, payload or {}))
        if method == "sendMessage":
            self.mid += 1
            return {"message_id": self.mid}
        return {}

    def texts(self, method="sendMessage"):
        return [p.get("text", "") for m, p in self.calls if m == method]

    def buttons(self, idx=-1):
        sends = [p for m, p in self.calls if m == "sendMessage" and "reply_markup" in p]
        return [b["text"] for row in sends[idx]["reply_markup"]["inline_keyboard"] for b in row]


def cq(step, value, chat=42, sender=42):
    return {"id": "cq1", "from": {"id": sender}, "message": {"chat": {"id": chat}, "message_id": 101},
            "data": f"ci|{step}|{value}"}


class Checkin(unittest.TestCase):
    def setUp(self):
        self.api = FakeApi()
        bot.api = self.api
        bot.send = _ORIG_SEND                      # a test may stub send(); every test starts with the real one (-> FakeApi)
        bot._session = None
        for f in (gc.CHECKIN_CSV, gc.ALCOHOL_CSV, bot.CHECKIN_STATE_FILE):
            if f.exists():
                f.unlink()

    # ---- day rule
    def test_day_rule_matches_drinks(self):
        for now in (datetime(2026, 10, 25, 23, 15), datetime(2026, 10, 26, 1, 0), datetime(2026, 10, 25, 13, 59), datetime(2026, 10, 25, 14, 0)):
            self.assertEqual(bot.checkin_day(now), bot.parse_drinks("1", now)[0], now)
        self.assertEqual(bot.checkin_day(datetime(2026, 10, 26, 1, 0)), DAY)        # 1 AM still belongs to the 10/25 evening

    # ---- full flow
    def test_full_flow_writes_row_and_alcohol(self):
        gc.alcohol_log(DAY, 1, at=datetime(2026, 10, 25, 20, 0))                   # /drinks 1 earlier that evening
        bot.start_checkin(42, T2315, trigger="scheduled")
        self.assertIn("/drinks logged 1 so far", self.api.texts()[-1])
        self.assertEqual(self.api.buttons(), ["None", "1", "2", "3", "4+"])
        self.assertTrue(bot.CHECKIN_STATE_FILE.exists())
        t = T2315
        self.assertEqual(bot.handle_callback(cq("drinks", "2"), OWNER, t), "cb_drinks")
        self.assertIn("✓ 2", [p.get("text", "") for m, p in self.api.calls if m == "editMessageText"][-1])
        self.assertEqual(bot.handle_callback(cq("wounds_new", "yes"), OWNER, t), "cb_wounds_new")
        self.assertEqual(bot.handle_callback(cq("wounds_count", "1"), OWNER, t), "cb_wounds_count")
        self.assertEqual(self.api.buttons()[:2], ["☐ Face/mouth", "☐ Scalp"])
        self.assertEqual(bot.handle_callback(cq("wounds_sites", "1"), OWNER, t), "cb_toggle")      # Scalp on
        self.assertEqual(bot.handle_callback(cq("wounds_sites", "2"), OWNER, t), "cb_toggle")      # Hands on
        self.assertEqual(bot.handle_callback(cq("wounds_sites", "2"), OWNER, t), "cb_toggle")      # Hands off again
        kb = [p for m, p in self.api.calls if m == "editMessageReplyMarkup"][-1]["reply_markup"]["inline_keyboard"]
        self.assertIn("☑ Scalp", [b["text"] for row in kb for b in row])
        self.assertEqual(bot.handle_callback(cq("wounds_sites", "done"), OWNER, t), "cb_wounds_sites")
        self.assertEqual(bot.handle_callback(cq("wounds_older", "healing"), OWNER, t), "cb_wounds_older")
        self.assertEqual(bot.handle_callback(cq("exercise", "yes"), OWNER, t), "cb_exercise")
        self.assertEqual(bot.handle_callback(cq("exercise_types", "2"), OWNER, t), "cb_toggle")    # Walking outside
        self.assertEqual(bot.handle_callback(cq("exercise_types", "done"), OWNER, t), "cb_exercise_types")
        self.assertEqual(self.api.buttons(), ["<15", "15-30", "30-60", "60+"])
        self.assertEqual(bot.handle_callback(cq("exercise_minutes", "30-60"), OWNER, datetime(2026, 10, 25, 23, 19)), "checkin_done")
        self.assertIsNone(bot._session)
        self.assertEqual(json.loads(bot.CHECKIN_STATE_FILE.read_text()), {"evening": None, "sweat": None})
        c = gc.checkin_for_day(DAY)
        self.assertEqual((c["status"], c["drinks"], c["new_wounds"], c["new_wound_count"], c["new_wound_sites"], c["older_wounds"],
                          c["exercise"], c["exercise_types"], c["exercise_minutes"], c["trigger"], c["source"]),
                         ("done", "2", "yes", "1", ["Scalp"], "healing", "yes", ["Walking outside"], "30-60", "scheduled", "telegram"))
        self.assertEqual((c["started_at"], c["finished_at"]), ("2026-10-25T23:15:00", "2026-10-25T23:19:00"))
        self.assertEqual(gc.alcohol_for_night(DAY), 2.0)                             # check-in total replaced the /drinks 1
        self.assertEqual(gc.checkin_line(c), "Check-in Sun 10/25: drinks 2 · new wounds 1 (Scalp) · older: healing · exercise Walking outside 30-60 min · night sweat ?")
        self.assertTrue(self.api.texts()[-1].startswith("Saved. Check-in Sun 10/25"))

    def test_shortest_path_and_four_plus(self):
        bot.start_checkin(42, T2315)
        for step, v in (("drinks", "0"), ("wounds_new", "no"), ("wounds_older", "none"), ("exercise", "no")):
            bot.handle_callback(cq(step, v), OWNER, T2315)
        c = gc.checkin_for_day(DAY)
        self.assertEqual((c["status"], c["drinks"], c["new_wounds"], c["exercise"], c["new_wound_sites"]), ("done", "0", "no", "no", []))
        self.assertEqual(gc.alcohol_for_night(DAY), 0.0)                              # an explicit "None" is a real 0
        self.assertEqual(gc.checkin_line(c), "Check-in Sun 10/25: drinks none · no new wounds · older: none open · no exercise · night sweat ?")
        bot.start_checkin(42, T2315)                                                 # redo: last row wins
        for step, v in (("drinks", "4+"), ("wounds_new", "no"), ("wounds_older", "none"), ("exercise", "no")):
            bot.handle_callback(cq(step, v), OWNER, T2315)
        self.assertEqual(gc.checkin_for_day(DAY)["drinks"], "4+")
        self.assertEqual(gc.alcohol_for_night(DAY), 4.0)
        self.assertEqual(len(gc.CHECKIN_CSV.read_text(encoding="utf-8").splitlines()), 3)   # header + 2 rows, append-only

    # ---- nudge / skip
    def test_nudge_then_skipped_keeps_partial_answers(self):
        bot.start_checkin(42, T2315, trigger="scheduled")
        bot.handle_callback(cq("drinks", "2"), OWNER, datetime(2026, 10, 25, 23, 20))
        self.assertEqual(bot.checkin_tick(OWNER, datetime(2026, 10, 25, 23, 50)), "waiting")
        self.assertEqual(bot.checkin_tick(OWNER, datetime(2026, 10, 26, 0, 5)), "nudged")        # +45 after the last answer
        self.assertIn("Still up?", self.api.texts()[-2])
        self.assertEqual(bot.checkin_tick(OWNER, datetime(2026, 10, 26, 0, 20)), "waiting")      # only ONE nudge
        self.assertEqual(bot.checkin_tick(OWNER, datetime(2026, 10, 26, 0, 49)), "waiting")
        self.assertEqual(bot.checkin_tick(OWNER, datetime(2026, 10, 26, 0, 50)), "skipped")      # +90
        c = gc.checkin_for_day(DAY)
        self.assertEqual((c["status"], c["drinks"], c["new_wounds"], c["exercise"]), ("skipped", "2", "", ""))
        self.assertEqual(gc.alcohol_for_night(DAY), 2.0)                             # the answered part still counts
        self.assertEqual(gc.checkin_line(c), "Check-in Sun 10/25 (skipped): drinks 2 · new wounds ? · exercise ? · night sweat ?")
        self.assertIsNone(bot._session)
        self.assertEqual(bot.checkin_tick(OWNER, datetime(2026, 10, 26, 1, 0)), "idle")          # a skipped row blocks a restart

    # ---- scheduling
    def test_scheduled_start_window(self):
        self.assertEqual(bot.checkin_tick(OWNER, datetime(2026, 10, 25, 23, 14)), "idle")
        self.assertEqual(bot.checkin_tick(OWNER, T2315), "started")
        self.assertEqual(bot._session["day"], str(DAY))
        self.assertEqual(bot.checkin_tick(OWNER, datetime(2026, 10, 25, 23, 16)), "waiting")
        for step, v in (("drinks", "1"), ("wounds_new", "no"), ("wounds_older", "none"), ("exercise", "no")):
            bot.handle_callback(cq(step, v), OWNER, T2315)
        self.assertEqual(bot.checkin_tick(OWNER, datetime(2026, 10, 25, 23, 40)), "idle")        # done: no second start
        # bot was down at 23:15 the next evening: made up at 02:00, not at 06:00
        self.assertEqual(bot.checkin_tick(OWNER, datetime(2026, 10, 27, 2, 0)), "started")
        self.assertEqual(bot._session["day"], "2026-10-26")
        bot._session = None
        self.assertEqual(bot.checkin_tick(OWNER, datetime(2026, 10, 27, 6, 0)), "idle")

    def test_configurable_time(self):
        old = bot.CHECKIN_TIME
        try:
            bot.CHECKIN_TIME = "22:30"
            self.assertEqual(bot.checkin_tick(OWNER, datetime(2026, 10, 25, 22, 29)), "idle")
            self.assertEqual(bot.checkin_tick(OWNER, datetime(2026, 10, 25, 22, 30)), "started")
        finally:
            bot.CHECKIN_TIME = old

    # ---- stale / foreign presses, command routing, help
    def test_stale_and_non_owner_callbacks(self):
        bot.start_checkin(42, T2315)
        self.assertEqual(bot.handle_callback(cq("exercise", "no"), OWNER, T2315), "cb_stale")     # wrong step
        self.assertEqual(bot._session["step"], "drinks")
        self.assertEqual(bot.handle_callback(cq("drinks", "2", chat=7, sender=7), OWNER, T2315), "ignored_non_owner")
        self.assertEqual(bot._session["answers"], {})
        self.assertEqual(self.api.calls[-1][0], "answerCallbackQuery")

    def test_command_routing_and_help(self):
        bot.send = lambda chat_id, text: True
        msg = {"chat": {"id": 42}, "from": {"id": 42}, "text": "/checkin", "date": 1_800_000_000}
        self.assertEqual(bot.handle(msg, OWNER, now=1_800_000_000), "checkin_start")
        self.assertEqual(bot.handle(msg, OWNER, now=1_800_000_000), "checkin_resend")
        self.assertEqual(bot.handle({**msg, "chat": {"id": 7}}, OWNER, now=1_800_000_000), "ignored_non_owner")
        self.assertIn("/checkin", bot.help_text())
        self.assertIn("checkin", [c["command"] for c in bot.COMMANDS])

    def test_resume_after_restart(self):
        bot.start_checkin(42, T2315)
        bot.handle_callback(cq("drinks", "3"), OWNER, T2315)
        bot._session = None
        bot._load_session()
        self.assertEqual((bot._session["step"], bot._session["answers"]["drinks"]), ("wounds_new", "3"))

    # ---- backfill CLI + morning card
    def test_backfill_cli_and_card_line(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = bot.checkin_log_cli(["2026-10-24", "new_wounds=yes", "new_wound_count=unknown", "new_wound_sites=Scalp", "exercise=no"])
        self.assertEqual(rc, 0)
        c = gc.checkin_for_day(date(2026, 10, 24))
        self.assertEqual((c["drinks"], c["new_wounds"], c["new_wound_count"], c["new_wound_sites"], c["older_wounds"], c["exercise"], c["source"]),
                         ("", "yes", "unknown", ["Scalp"], "", "no", "backfill"))
        self.assertIsNone(gc.alcohol_for_night(date(2026, 10, 24)))                   # drinks unknown stays unknown
        self.assertEqual(gc.checkin_line(c), "Check-in Sat 10/24: drinks ? · new wounds ? (Scalp) · no exercise · night sweat ?")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(bot.checkin_log_cli(["2026-10-24", "bogus=1"]), 2)
        self.assertIsNone(gc.checkin_for_day(date(2026, 10, 23)))
        self.assertEqual(gc.checkin_line(None), "Check-in: none")

    def test_morning_payload_has_checkin(self):
        for scenario, expect in (("checkin", "Check-in Sat 10/24: drinks 2 · new wounds 1 (Scalp) · older: healing · exercise Walking outside 30-60 min"),
                                 ("checkin-skipped", "Check-in Sat 10/24 (skipped): drinks ? · new wounds ? · exercise ?"),
                                 ("checkin-none", "Check-in: none")):
            out = io.StringIO()
            sys.argv = ["garmin_capacity.py", "--mock", scenario, "--date", "2026-10-25"]
            with contextlib.redirect_stdout(out):
                self.assertEqual(gc.main(), 0)
            text = out.getvalue()
            self.assertIn(expect, text, scenario)
            payload = json.loads(text.split("<<<CAPACITY_JSON")[1].split("CAPACITY_JSON>>>")[0])
            self.assertIn("checkin", payload)
            self.assertEqual(payload["tier"], "YELLOW")                                # display only


if __name__ == "__main__":
    unittest.main(verbosity=2)
