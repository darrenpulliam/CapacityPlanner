#!/usr/bin/env python3
"""Unit tests for the consolidated check-ins (capacity_bot.py): morning night-sweat question (merge, /sweat, timing),
free-text drinks + /undo, the one-time checkins.jsonl import, and its own state file. Temporary CAPACITY_DATA_DIR,
Telegram stubbed, no network.

Run:  python -m unittest test_consolidation -v
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="capacity_consol_")
os.environ["CAPACITY_DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent))
import garmin_capacity as gc                            # noqa: E402
import capacity_bot as bot                              # noqa: E402

OWNER = "42"
EVE = date(2026, 10, 25)                                # the evening; its night ends the morning of 10/26
T0835 = datetime(2026, 10, 26, 8, 35)
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

    def texts(self):
        return [p.get("text", "") for m, p in self.calls if m == "sendMessage"]

    def buttons(self):
        sends = [p for m, p in self.calls if m == "sendMessage" and "reply_markup" in p]
        return [b["text"] for row in sends[-1]["reply_markup"]["inline_keyboard"] for b in row]


def cq(step, value, chat=42, sender=42):
    return {"id": "cq1", "from": {"id": sender}, "message": {"chat": {"id": chat}, "message_id": 101}, "data": f"ci|{step}|{value}"}


def msg(text, ts=1_800_000_000):
    return {"chat": {"id": 42}, "from": {"id": 42}, "text": text, "date": ts}


class Consolidation(unittest.TestCase):
    def setUp(self):
        self.api = FakeApi()
        bot.api = self.api
        bot.send = _ORIG_SEND
        bot._session = bot._sweat = None
        for f in (gc.CHECKIN_CSV, gc.ALCOHOL_CSV, gc.DRINK_EVENTS_CSV, bot.CHECKIN_STATE_FILE):
            if f.exists():
                f.unlink()
        os.environ.pop("MORNING_TIME", None)

    def _evening(self, drinks="2", when=datetime(2026, 10, 25, 23, 15)):
        bot.start_checkin(42, when)
        for step, v in (("drinks", drinks), ("wounds_new", "yes"), ("wounds_count", "2")):
            bot.handle_callback(cq(step, v), OWNER, when)
        bot.handle_callback(cq("wounds_sites", "1"), OWNER, when)
        for step, v in (("wounds_sites", "done"), ("wounds_older", "none"), ("exercise", "no")):
            bot.handle_callback(cq(step, v), OWNER, when)

    # ---- state file
    def test_own_state_file_and_legacy_file_ignored(self):
        self.assertEqual(bot.CHECKIN_STATE_FILE.name, "capacity_checkin_state.json")
        legacy = Path(_TMP) / "checkin_state.json"                                   # the retired checkin_bot's file
        legacy.write_text(json.dumps({"offset": 7, "checkin": None, "morning": None}), encoding="utf-8")
        bot._load_session()
        self.assertEqual((bot._session, bot._sweat), (None, None))
        self.assertEqual(json.loads(legacy.read_text())["offset"], 7)                # never touched by this bot
        bot.start_checkin(42, datetime(2026, 10, 26, 23, 15))
        self.assertEqual(json.loads(legacy.read_text())["offset"], 7)

    def test_tonight_2315_fires_from_empty_state(self):
        bot._load_session()
        self.assertEqual(bot.checkin_tick(OWNER, datetime(2026, 10, 26, 23, 14)), "idle")
        self.assertEqual(bot.checkin_tick(OWNER, datetime(2026, 10, 26, 23, 15)), "started")
        self.assertEqual(bot._session["day"], "2026-10-26")
        bot._session = None
        bot._load_session()                                                          # restart mid-check-in resumes it
        self.assertEqual(bot._session["day"], "2026-10-26")

    def test_sweat_only_row_does_not_block_the_evening_checkin(self):
        gc.checkin_log(gc.checkin_row(date(2026, 10, 26), {"night_sweat": "damp"}, status="", trigger="sweat"))
        self.assertEqual(bot.checkin_tick(OWNER, datetime(2026, 10, 26, 23, 15)), "started")

    # ---- sweat storage + merge
    def test_morning_row_merges_and_never_blanks_evening(self):
        self._evening()
        before = gc.checkin_for_day(EVE)
        self.assertEqual(before["night_sweat"], "")
        bot.start_sweat(42, T0835)
        self.assertEqual(self.api.buttons(), ["No sweating", "Damp (clothes/pillow)", "Soaked the sheets"])
        self.assertEqual(bot.handle_callback(cq("sweat", "soaked"), OWNER, T0835), "sweat_done")
        c = gc.checkin_for_day(EVE)
        self.assertEqual((c["night_sweat"], c["status"], c["drinks"], c["new_wounds"], c["new_wound_count"], c["new_wound_sites"],
                          c["older_wounds"], c["exercise"]), ("soaked", "done", "2", "yes", "2", ["Scalp"], "none", "no"))
        self.assertIn("night sweat: soaked sheets", gc.checkin_line(c))
        self.assertIsNone(bot._sweat)
        self.assertEqual(json.loads(bot.CHECKIN_STATE_FILE.read_text())["sweat"], None)

    def test_redoing_evening_after_sweat_keeps_sweat(self):
        self._evening()
        bot.start_sweat(42, T0835)
        bot.handle_callback(cq("sweat", "damp"), OWNER, T0835)
        self._evening(drinks="4+", when=datetime(2026, 10, 26, 0, 30))               # redo at 00:30 is the 10/25 evening
        c = gc.checkin_for_day(EVE)
        self.assertEqual((c["drinks"], c["night_sweat"]), ("4+", "damp"))

    def test_sweat_without_any_evening_row(self):
        bot.start_sweat(42, T0835)
        bot.handle_callback(cq("sweat", "none"), OWNER, T0835)
        c = gc.checkin_for_day(EVE)
        self.assertEqual((c["night_sweat"], c["status"], c["drinks"]), ("none", "", ""))
        self.assertIn("drinks ?", gc.checkin_line(c))
        self.assertIn("night sweat: none", gc.checkin_line(c))

    def test_old_csv_header_is_migrated_without_losing_rows(self):
        old = [c for c in gc.CHECKIN_FIELDS if c != "night_sweat"]
        gc.CHECKIN_CSV.write_text(",".join(old) + "\n2026-10-24,done,4,yes,unknown,Scalp,,no,,,,2026-10-26T00:25:47,manual,backfill\n", encoding="utf-8")
        self.assertEqual(gc.checkin_for_day(date(2026, 10, 24))["drinks"], "4")
        bot.start_sweat(42, datetime(2026, 10, 25, 8, 35))
        bot.handle_callback(cq("sweat", "damp"), OWNER, datetime(2026, 10, 25, 8, 36))
        self.assertTrue(gc.CHECKIN_CSV.read_text(encoding="utf-8").splitlines()[0].endswith("night_sweat"))
        c = gc.checkin_for_day(date(2026, 10, 24))
        self.assertEqual((c["drinks"], c["night_sweat"], c["source"]), ("4", "damp", "backfill"))

    # ---- /sweat and the morning tick
    def test_sweat_command_any_time_targets_last_night(self):
        bot.send = lambda chat_id, text: True
        self.assertEqual(bot.handle(msg("/sweat", 1_800_000_000), OWNER, now=1_800_000_000), "sweat_start")
        self.assertEqual(bot.handle(msg("/sweat", 1_800_000_000), OWNER, now=1_800_000_000), "sweat_resend")
        self.assertEqual(bot._sweat["day"], (datetime.fromtimestamp(1_800_000_000).date() - (datetime.fromtimestamp(1_800_000_000).date() - bot.sweat_night(datetime.fromtimestamp(1_800_000_000)))).isoformat())
        self.assertIn("sweat", [c["command"] for c in bot.COMMANDS])
        for d in (datetime(2026, 10, 26, 15, 0), datetime(2026, 10, 26, 8, 35), datetime(2026, 10, 26, 3, 0)):
            self.assertEqual(bot.sweat_night(d), date(2026, 10, 25) if d.hour >= 4 else date(2026, 10, 24))

    def test_morning_prompt_time_nudge_and_skip(self):
        self.assertEqual(bot.sweat_tick(OWNER, datetime(2026, 10, 26, 8, 34)), "idle")
        self.assertEqual(bot.sweat_tick(OWNER, T0835), "sweat_started")
        self.assertEqual(bot._sweat["day"], "2026-10-25")
        self.assertIn("night of Sun 10/25", self.api.texts()[-1])
        self.assertEqual(bot.sweat_tick(OWNER, datetime(2026, 10, 26, 9, 19)), "sweat_waiting")
        self.assertEqual(bot.sweat_tick(OWNER, datetime(2026, 10, 26, 9, 20)), "sweat_nudged")   # +45
        self.assertEqual(bot.sweat_tick(OWNER, datetime(2026, 10, 26, 11, 0)), "sweat_waiting")  # only one nudge
        self.assertEqual(sum("Quick one" in t for t in self.api.texts()), 1)
        self.assertEqual(bot.sweat_tick(OWNER, datetime(2026, 10, 26, 13, 59)), "sweat_waiting")
        self.assertEqual(bot.sweat_tick(OWNER, datetime(2026, 10, 26, 14, 0)), "sweat_skipped")
        self.assertEqual(gc.checkin_for_day(EVE)["night_sweat"], "skipped")
        self.assertIn("night sweat ?", gc.checkin_line(gc.checkin_for_day(EVE)))
        self.assertEqual(bot.sweat_tick(OWNER, datetime(2026, 10, 26, 14, 1)), "idle")            # skipped counts as answered

    def test_morning_prompt_not_after_2pm_and_not_if_answered_and_env_time(self):
        self.assertEqual(bot.sweat_tick(OWNER, datetime(2026, 10, 26, 15, 0)), "idle")
        os.environ["MORNING_TIME"] = "07:00"
        self.assertEqual(bot.sweat_tick(OWNER, datetime(2026, 10, 26, 7, 0)), "sweat_started")
        bot.handle_callback(cq("sweat", "none"), OWNER, datetime(2026, 10, 26, 7, 1))
        self.assertEqual(bot.sweat_tick(OWNER, datetime(2026, 10, 26, 8, 0)), "idle")

    def test_sweat_callbacks_stale_and_foreign(self):
        self.assertEqual(bot.handle_callback(cq("sweat", "damp"), OWNER, T0835), "cb_stale")
        bot.start_sweat(42, T0835)
        self.assertEqual(bot.handle_callback(cq("sweat", "damp", chat=7, sender=7), OWNER, T0835), "ignored_non_owner")
        self.assertEqual(bot.handle_callback(cq("sweat", "bogus"), OWNER, T0835), "cb_stale")
        self.assertIsNotNone(bot._sweat)

    def test_sweat_resumes_after_restart(self):
        bot.start_sweat(42, T0835)
        bot._sweat = None
        bot._load_session()
        self.assertEqual(bot._sweat["day"], "2026-10-25")

    # ---- free-text drinks
    def test_parse_drink_text(self):
        P = bot.parse_drink_text
        self.assertEqual(P("beer"), (1, "beer"))
        self.assertEqual(P("2 wine"), (2, "wine"))
        self.assertEqual(P("Bourbon"), (1, "bourbon"))
        self.assertEqual(P("3 beers"), (3, "beer"))
        self.assertEqual(P("whiskey"), (1, "whiskey"))
        self.assertIsNone(P("hello there"))
        self.assertIsNone(P("99 beer"))
        self.assertEqual(P("/drink", command=True), (1, "drink"))
        self.assertEqual(P("/drink 2 wine", command=True), (2, "wine"))
        self.assertEqual(P("/drink 2", command=True), (2, "drink"))

    def test_drink_events_update_alcohol_total_and_last_time(self):
        bot.send = lambda chat_id, text: True
        t = datetime(2026, 10, 25, 21, 5)
        self.assertIn("Logged 1 beer at 9:05 PM", bot.cmd_drink_text("beer", t))
        self.assertIn("Sun 10/25: 3 total", bot.cmd_drink_text("2 wine", datetime(2026, 10, 25, 22, 40)))
        self.assertEqual(gc.alcohol_for_night(EVE), 3.0)
        rows = gc.DRINK_EVENTS_CSV.read_text(encoding="utf-8").splitlines()
        self.assertEqual(rows[0], "ts,night_date,count,what")
        self.assertEqual(rows[2], "2026-10-25T22:40:00,2026-10-25,2,wine")
        self.assertEqual(gc.last_drink_time(EVE), "2026-10-25T22:40:00")
        self._evening(drinks="3")                                                     # check-in total replaces it
        self.assertIn("drinks 3 (last 10:40 PM)", gc.checkin_line(gc.checkin_for_day(EVE)))

    def test_drink_after_midnight_belongs_to_the_evening(self):
        bot.cmd_drink_text("beer", datetime(2026, 10, 26, 0, 30))
        self.assertEqual(gc.alcohol_for_night(EVE), 1.0)

    def test_undo_removes_last_event_and_total(self):
        bot.cmd_drink_text("beer", datetime(2026, 10, 25, 21, 0))
        bot.cmd_drink_text("2 wine", datetime(2026, 10, 25, 22, 40))
        self.assertIn("Removed 2 wine", bot.cmd_undo(datetime(2026, 10, 25, 22, 45)))
        self.assertEqual(gc.alcohol_for_night(EVE), 1.0)
        self.assertEqual(gc.last_drink_time(EVE), "2026-10-25T21:00:00")
        self.assertIn("Removed 1 beer", bot.cmd_undo(datetime(2026, 10, 25, 22, 46)))
        self.assertEqual(gc.alcohol_for_night(EVE), 0.0)
        self.assertEqual(gc.last_drink_time(EVE), "")
        self.assertEqual(bot.cmd_undo(), "Nothing to undo.")

    def test_handle_routes_text_drinks_and_commands(self):
        sent = []
        bot.send = lambda chat_id, text: sent.append(text) or True
        now = 1_800_000_000
        self.assertEqual(bot.handle(msg("2 wine", now), OWNER, now=now), "drink_text")
        self.assertEqual(bot.handle(msg("/drink", now), OWNER, now=now), "drink_text")
        self.assertEqual(bot.handle(msg("/undo", now), OWNER, now=now), "undo")
        self.assertEqual(bot.handle(msg("good morning", now), OWNER, now=now), "ignored_text")
        self.assertEqual(bot.handle(msg("/drinks 2", now), OWNER, now=now), "drinks")          # unchanged
        self.assertEqual(len(sent), 4)
        self.assertEqual(bot.handle({**msg("beer", now), "chat": {"id": 7}, "from": {"id": 7}}, OWNER, now=now), "ignored_non_owner")
        for c in ("drink", "undo", "sweat"):
            self.assertIn(c, [x["command"] for x in bot.COMMANDS])

    # ---- one-time import
    def test_import_jsonl_idempotent_and_archived(self):
        src = Path(_TMP) / "checkins.jsonl"
        src.write_text("\n".join(json.dumps(r) for r in (
            {"type": "morning", "ts": "2026-10-26T08:36:00", "day": "2026-10-25", "skipped": False, "sweat": "damp"},
            {"type": "drink", "ts": "2026-10-25T21:10:00", "day": "2026-10-25", "count": 1, "what": "beer"},
            {"type": "morning", "ts": "2026-10-25T14:00:00", "day": "2026-10-24", "skipped": True},
        )) + "\nnot json\n", encoding="utf-8")
        out = bot.import_checkins_jsonl(src)
        self.assertEqual(out, {"sweat": 2, "drinks": 1, "skipped": 1})
        self.assertEqual(gc.checkin_for_day(EVE)["night_sweat"], "damp")
        self.assertEqual(gc.checkin_for_day(date(2026, 10, 24))["night_sweat"], "skipped")
        self.assertEqual(gc.last_drink_time(EVE), "2026-10-25T21:10:00")
        self.assertFalse(src.exists())
        self.assertTrue((Path(_TMP) / "archive" / "checkins.jsonl").exists())
        self.assertEqual(bot.import_checkins_jsonl(src), {"sweat": 0, "drinks": 0, "skipped": 0})   # nothing left to import

    def test_import_keeps_an_existing_sweat_answer(self):
        gc.checkin_log(gc.checkin_row(EVE, {"night_sweat": "soaked"}, status="", trigger="sweat"))
        src = Path(_TMP) / "checkins.jsonl"
        src.write_text(json.dumps({"type": "morning", "ts": "2026-10-26T08:36:00", "day": "2026-10-25", "skipped": False, "sweat": "damp"}), encoding="utf-8")
        self.assertEqual(bot.import_checkins_jsonl(src, archive=False)["sweat"], 0)
        self.assertEqual(gc.checkin_for_day(EVE)["night_sweat"], "soaked")

    # ---- the morning card
    def test_card_line_and_payload_have_one_checkin_key(self):
        import contextlib
        import io
        self._evening()
        bot.cmd_drink_text("2 wine", datetime(2026, 10, 25, 22, 40))
        bot.start_sweat(42, T0835)
        bot.handle_callback(cq("sweat", "damp"), OWNER, T0835)
        out = io.StringIO()
        sys.argv = ["garmin_capacity.py", "--mock", "checkin-none", "--date", "2026-10-26"]   # mock checkin is forced; use the real reader below
        c = gc.checkin_for_day(EVE)
        line = gc.checkin_line(c)
        self.assertEqual(line, "Check-in Sun 10/25: drinks 2 (last 10:40 PM) · new wounds 2 (Scalp) · older: none open · no exercise · night sweat: damp")
        self.assertEqual(c["night_sweat"], "damp")
        self.assertEqual(c["last_drink_time"], "2026-10-25T22:40:00")
        with contextlib.redirect_stdout(out):
            gc.main()
        payload = json.loads(out.getvalue().split("<<<CAPACITY_JSON")[1].split("CAPACITY_JSON>>>")[0])
        self.assertNotIn("checkin_bot", payload)
        self.assertNotIn("📝", out.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
