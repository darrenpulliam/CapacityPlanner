#!/usr/bin/env python3
"""Tests for shift_scorecard.py, coach.py, the caffeine log (garmin_capacity.py) and their wiring in capacity_bot.py.

Run:  python -m unittest test_shifts_coach -v      (or pytest)
Every test points the modules at its own temporary data dir (patched attributes), so it is safe next to the other
test files, which share one CAPACITY_DATA_DIR. No network, no Telegram.
"""
import csv
import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

_TMP0 = tempfile.mkdtemp(prefix="capacity_shifts_")
os.environ.setdefault("CAPACITY_DATA_DIR", _TMP0)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import garmin_capacity as gc        # noqa: E402
import shift_scorecard as sc        # noqa: E402
import coach                        # noqa: E402
import capacity_bot as bot          # noqa: E402

OWNER = "42"
HIST_FIELDS = ["date", "tier", "bb_wake", "sleep_min", "sleep_score", "hrv_last_night", "resting_hr",
               "bedtime_target", "sleep_onset", "wake_time", "bat_charged"]


def ev(day, start, end, loc="1300 US HIGHWAY 127 S, FRANKFORT, KY 40601", title="Shift in 09855S"):
    return {"summary": title, "location": loc,
            "start": {"dateTime": f"{day}T{start}:00-04:00"}, "end": {"dateTime": f"{day}T{end}:00-04:00"}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cap_sc_"))
        d = self.tmp
        self.patches = [
            mock.patch.object(gc, "DATA_DIR", d), mock.patch.object(gc, "CAFFEINE_CSV", d / "caffeine_events.csv"),
            mock.patch.object(gc, "ALCOHOL_CSV", d / "alcohol.csv"), mock.patch.object(gc, "HISTORY_CSV", d / "history.csv"),
            mock.patch.object(gc, "DRINK_EVENTS_CSV", d / "drink_events.csv"),
            mock.patch.object(sc, "DATA_DIR", d), mock.patch.object(sc, "SHIFTS_CSV", d / "shifts.csv"),
            mock.patch.object(sc, "SCORECARD_CSV", d / "shift_scorecard.csv"),
            mock.patch.object(sc, "ANALYSIS_DIR", d / "analysis"),
            mock.patch.object(sc, "SCORECARD_PNG", d / "analysis" / "shift_scorecard.png"),
            mock.patch.object(sc, "SCORECARD_MD", d / "analysis" / "shift_scorecard_report.md"),
            mock.patch.object(coach, "STATE_FILE", d / "coach_state.json"),
            mock.patch.dict(os.environ, {}, clear=False),
        ]
        for p in self.patches:
            p.start()
        for k in ("CAFFEINE_MG_BLOOM", "CAFFEINE_MG_ENERGY", "CAFFEINE_MG_COFFEE"):
            os.environ.pop(k, None)
        self.sent = []
        self._orig_send = bot.send
        bot.send = lambda chat, text: (self.sent.append(text), True)[1]

    def tearDown(self):
        bot.send = self._orig_send
        for p in reversed(self.patches):
            p.stop()

    def shifts(self, *events):
        sc.import_shifts(sc.shifts_from_events(list(events)))

    def card(self, day, tier="GREEN", bed="2026-10-28T23:30:00"):
        (self.tmp / "latest.json").write_text(json.dumps(
            {"date": day, "tier": tier, "circadian": {"bedtime_target": bed}}), encoding="utf-8")

    def history(self, rows):
        with (self.tmp / "history.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=HIST_FIELDS)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in HIST_FIELDS})

    def msg(self, text, ts=None):
        return {"chat": {"id": int(OWNER)}, "from": {"id": int(OWNER)}, "text": text, "date": ts or datetime.now().timestamp()}


class CaffeineParse(Base):
    def test_free_text_forms(self):
        p = bot.parse_caffeine_text
        self.assertEqual(p("bloom"), (1, "bloom"))
        self.assertEqual(p("Bloom!"), (1, "bloom"))
        self.assertEqual(p("2 coffee"), (2, "coffee"))
        self.assertEqual(p("coffee x2"), (2, "coffee"))
        self.assertEqual(p("red bull"), (1, "red bull"))
        self.assertEqual(p("redbull"), (1, "red bull"))
        self.assertEqual(p("Monster"), (1, "monster"))
        self.assertEqual(p("energy drink"), (1, "energy drink"))
        self.assertEqual(p("2 energy drinks"), (2, "energy drink"))
        self.assertEqual(p("had a coffee"), (1, "coffee"))
        self.assertEqual(p("just had an energy drink"), (1, "energy drink"))

    def test_command_forms(self):
        p = lambda t: bot.parse_caffeine_text(t, command=True)
        self.assertEqual(p("/caffeine 2 bloom"), (2, "bloom"))
        self.assertEqual(p("/caffeine bloom 2"), (2, "bloom"))
        self.assertEqual(p("/caffeine bloom"), (1, "bloom"))
        self.assertEqual(p("/caffeine@dp_bot 1 coffee"), (1, "coffee"))   # first token (the command) is skipped as is
        self.assertIsNone(p("/caffeine"))
        self.assertIsNone(p("/caffeine 0 bloom"))
        self.assertIsNone(p("/caffeine 11 bloom"))
        self.assertIsNone(p("/caffeine tea"))

    def test_sentences_are_not_logs(self):
        for t in ("can I drink coffee tonight", "the coffee shop is closed", "bloom is too sweet", "no more coffee today",
                  "coffee with Tricia at 3"):
            self.assertIsNone(bot.parse_caffeine_text(t), t)

    def test_command_without_args_returns_usage(self):
        self.assertIn("Usage", bot.cmd_caffeine_text("/caffeine", command=True))
        self.assertFalse(gc.caffeine_events())


class CaffeineLog(Base):
    def test_default_and_configured_mg(self):
        self.assertEqual(gc.caffeine_mg_each("bloom"), 180)
        self.assertEqual(gc.caffeine_mg_each("energy drink"), 200)
        self.assertEqual(gc.caffeine_mg_each("monster"), 200)
        self.assertEqual(gc.caffeine_mg_each("red bull"), 200)
        self.assertEqual(gc.caffeine_mg_each("coffee"), 95)
        os.environ["CAFFEINE_MG_BLOOM"] = "195"
        self.assertEqual(gc.caffeine_mg_each("bloom"), 195)
        os.environ["CAFFEINE_MG_BLOOM"] = "junk"
        self.assertEqual(gc.caffeine_mg_each("bloom"), 180)

    def test_log_row_shape_and_totals(self):
        at = datetime(2026, 10, 28, 10, 15)
        reply = bot.cmd_caffeine_text("/caffeine 2 bloom", at, command=True)
        rows = list(csv.reader((self.tmp / "caffeine_events.csv").open(encoding="utf-8")))
        self.assertEqual(rows[0], ["ts", "day", "count", "what", "mg"])
        self.assertEqual(rows[1], ["2026-10-28T10:15:00", "2026-10-28", "2", "bloom", "360"])
        self.assertIn("360 mg", reply)
        bot.cmd_caffeine_text("coffee", datetime(2026, 10, 28, 12, 0))
        self.assertEqual(gc.caffeine_mg_for_day(date(2026, 10, 28)), 455)
        self.assertTrue(gc.caffeine_last_time(date(2026, 10, 28)).startswith("2026-10-28T12:00"))

    def test_after_midnight_belongs_to_the_previous_day(self):
        gc.caffeine_log(1, "coffee", at=datetime(2026, 10, 29, 2, 0))
        self.assertEqual(gc.caffeine_mg_for_day(date(2026, 10, 28)), 95)
        self.assertEqual(gc.caffeine_mg_for_day(date(2026, 10, 29)), 0)

    def test_warns_only_past_the_cutoff(self):
        self.card("2026-10-28", bed="2026-10-29T00:15:00")                 # cutoff 4:15 PM
        before = bot.cmd_caffeine_text("coffee", datetime(2026, 10, 28, 15, 0))
        after = bot.cmd_caffeine_text("bloom", datetime(2026, 10, 28, 17, 0))
        self.assertNotIn("cutoff", before)
        self.assertIn("Past your caffeine cutoff (4:15 PM", after)

    def test_warn_names_an_assumed_bedtime_when_no_card(self):
        out = bot.cmd_caffeine_text("coffee", datetime(2026, 10, 28, 20, 0))
        self.assertIn("assumed", out)

    def test_undo_removes_caffeine_then_drink_in_time_order(self):
        bot.cmd_drink_text("beer", datetime(2026, 10, 28, 19, 0))
        bot.cmd_caffeine_text("coffee", datetime(2026, 10, 28, 20, 0))
        self.assertIn("coffee", bot.cmd_undo(datetime(2026, 10, 28, 20, 5)))       # latest was the coffee
        self.assertEqual(gc.caffeine_mg_for_day(date(2026, 10, 28)), 0)
        self.assertIn("beer", bot.cmd_undo(datetime(2026, 10, 28, 20, 6)))
        self.assertEqual(bot.cmd_undo(datetime(2026, 10, 28, 20, 7)), "Nothing to undo.")

    def test_card_line(self):
        gc.caffeine_log(1, "bloom", at=datetime(2026, 10, 27, 14, 30))
        self.assertEqual(gc.caffeine_card_line({"date": "2026-10-28"}), "Caffeine yesterday: 180 mg, last at 2:30 PM")
        self.assertEqual(gc.caffeine_card_line({"date": "2026-10-30"}), "Caffeine yesterday: none logged")


class Routing(Base):
    def test_energy_drink_is_caffeine_not_alcohol(self):
        self.assertEqual(bot.handle(self.msg("energy drink"), OWNER), "caffeine")
        self.assertEqual(gc.caffeine_mg_for_day(gc.caffeine_day(datetime.now())), 200)
        self.assertIsNone(gc.alcohol_for_night(date.today()))

    def test_can_i_drink_is_a_question_and_logs_nothing(self):
        for t in ("can I drink tonight?", "Can I drink tonight", "/candrink", "is it ok to drink tonight"):
            self.sent.clear()
            self.assertEqual(bot.handle(self.msg(t), OWNER), "candrink", t)
            self.assertTrue(self.sent)
        self.assertFalse(gc.drink_events())
        self.assertFalse((self.tmp / "alcohol.csv").exists())

    def test_beer_still_logs_a_drink(self):
        self.assertEqual(bot.handle(self.msg("beer"), OWNER), "drink_text")
        self.assertEqual(len(gc.drink_events()), 1)

    def test_caffeine_command_and_help(self):
        self.assertEqual(bot.handle(self.msg("/caffeine 2 bloom"), OWNER), "caffeine")
        self.assertEqual(gc.caffeine_mg_for_day(gc.caffeine_day(datetime.now())), 360)
        names = {c["command"] for c in bot.COMMANDS}
        self.assertTrue({"caffeine", "candrink", "scorecard"} <= names)

    def test_non_owner_ignored(self):
        m = self.msg("bloom")
        m["from"] = {"id": 7}
        self.assertEqual(bot.handle(m, OWNER), "ignored_non_owner")
        self.assertFalse(gc.caffeine_events())


class ShiftBasics(Base):
    def test_city_and_drive_table(self):
        self.assertEqual(sc.city_of("1300 US HIGHWAY 127 S, FRANKFORT, KY 40601"), "FRANKFORT")
        self.assertEqual(sc.city_of("11099 HIGHWAY 44 E, MOUNT WASHINGTON, KY 40047"), "MOUNT WASHINGTON")
        self.assertEqual(sc.city_of(""), "")
        want = {"FRANKFORT": 75, "LAWRENCEBURG": 60, "LEXINGTON": 75, "LOUISVILLE": 45, "MOUNT WASHINGTON": 30,
                "SHEPHERDSVILLE": 30, "ELIZABETHTOWN": 35, "E-TOWN": 35, "SPRINGFIELD": 25, "PROSPECT": 60}
        for c, m in want.items():
            self.assertEqual(sc.drive_minutes(c), m, c)
        self.assertEqual(sc.drive_minutes("BARDSTOWN"), 60)
        self.assertEqual(sc.drive_minutes(""), 60)

    def test_type_boundaries_and_risk(self):
        d = lambda h, m=0, day=28: datetime(2026, 10, day, h, m)
        cases = [((d(7, 59), d(16)), "OPEN_EARLY", True), ((d(8), d(16)), "OPEN_8_9", True),
                 ((d(8, 59), d(17)), "OPEN_8_9", True), ((d(9), d(17)), "MID", False),
                 ((d(10), d(21)), "MID", False), ((d(10), d(21, 1)), "CLOSE_LATE", True),
                 ((d(14), d(22)), "CLOSE_LATE", True), ((d(16), d(2, 0, 29)), "CLOSE_LATE", True)]
        for (s, e), typ, risk in cases:
            self.assertEqual(sc.classify(s, e), typ, (s, e))
            self.assertEqual(sc.bb_risk(s, e), risk, (s, e))

    def test_consecutive_and_clopen(self):
        self.shifts(ev("2026-11-03", "07:00", "15:00"), ev("2026-11-04", "14:00", "22:00"),
                    ev("2026-11-05", "07:00", "15:00"), ev("2026-11-09", "07:00", "15:00"))
        a = sc.annotate(sc.load_shifts())
        self.assertEqual([x["consecutive_days"] for x in a], [1, 2, 3, 1])
        self.assertEqual([x["clopen"] for x in a], [False, False, True, False])    # 22:00 -> 07:00 = 9 h

    def test_non_shift_events_and_all_day_are_ignored(self):
        rows = sc.shifts_from_events([
            ev("2026-11-05", "07:00", "15:00", title="Shift in 09855S"),
            ev("2026-11-05", "07:00", "15:00", title="⚠️ Shift rule: R2 4th day in a row"),
            ev("2026-11-05", "07:00", "15:00", title="Dinner"),
            {"summary": "Shift in X", "start": {"date": "2026-11-05"}, "end": {"date": "2026-11-06"}}])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["start"], "2026-11-05T07:00")
        self.assertEqual(rows[0]["city"], "FRANKFORT")

    def test_reimport_is_idempotent_and_latest_wins(self):
        self.shifts(ev("2026-11-05", "07:00", "15:00"))
        self.shifts(ev("2026-11-05", "07:00", "16:00"))
        rows = sc.load_shifts()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["end"], "2026-11-05T16:00")

    def test_parse_ics(self):
        ics = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nSUMMARY:Shift in 09855S - SWC OF\r\n LAWRENCEBURG\r\n"
               "LOCATION:1300 US HIGHWAY 127 S\\, FRANKFORT\\, KY 40601\r\n"
               "DTSTART;TZID=America/New_York:20261028T140000\r\nDTEND;TZID=America/New_York:20261028T220000\r\n"
               "END:VEVENT\r\nBEGIN:VEVENT\r\nSUMMARY:Shift in utc\r\nDTSTART:20261029T110000Z\r\nDTEND:20261029T190000Z\r\n"
               "END:VEVENT\r\nBEGIN:VEVENT\r\nSUMMARY:Shift in allday\r\nDTSTART;VALUE=DATE:20261030\r\nDTEND;VALUE=DATE:20261031\r\n"
               "END:VEVENT\r\nEND:VCALENDAR\r\n")
        rows = sc.shifts_from_events(sc.parse_ics(ics))
        self.assertEqual(len(rows), 2)
        self.assertEqual((rows[0]["start"], rows[0]["end"], rows[0]["city"]), ("2026-10-28T14:00", "2026-10-28T22:00", "FRANKFORT"))
        self.assertIn("LAWRENCEBURG", rows[0]["summary"])               # folded line rejoined
        self.assertEqual((rows[1]["start"], rows[1]["end"]), ("2026-10-29T07:00", "2026-10-29T15:00"))   # Z -> EDT


class Scorecard(Base):
    def setUp(self):
        super().setUp()
        self.shifts(ev("2026-10-20", "14:00", "22:00"), ev("2026-10-22", "07:00", "15:00"))
        self.history([
            {"date": "2026-10-20", "bedtime_target": "2026-10-21T00:15:00"},
            {"date": "2026-10-21", "bb_wake": "30", "sleep_min": "420", "sleep_score": "70", "hrv_last_night": "40",
             "resting_hr": "66", "sleep_onset": "2026-10-21T00:30:00", "wake_time": "2026-10-21T07:30:00", "bat_charged": "45"},
            {"date": "2026-10-22", "bb_wake": "44"},
            {"date": "2026-10-23", "bb_wake": "52", "sleep_min": "300", "sleep_score": "50", "bat_charged": "20", "resting_hr": "72"}])
        gc.alcohol_log(date(2026, 10, 20), 2)
        gc.caffeine_log(1, "bloom", at=datetime(2026, 10, 20, 12, 30))
        gc.caffeine_log(1, "coffee", at=datetime(2026, 10, 20, 16, 40))

    def test_row_join(self):
        rows = sc.build_rows(today=date(2026, 10, 28))
        r = rows[0]
        self.assertEqual((r["date"], r["shift_type"], r["bb_risk"], r["drive_min"]), ("2026-10-20", "CLOSE_LATE", "BB RISK", 75))
        self.assertEqual((r["bb_wake_next"], r["bb_wake_day2"], r["recharge"], r["sleep_h"], r["sleep_score"], r["hrv"]),
                         ("30", "44", "45", 7.0, "70", "40"))
        self.assertEqual((r["planned_bed"], r["sleep_onset"], r["wake_time"]), ("00:15", "00:30", "07:30"))
        self.assertEqual((r["drinks"], r["caffeine_mg"], r["overnight_hr_proxy"]), ("2", "275", "66"))
        self.assertTrue(r["caffeine_last"].startswith("2026-10-20T16:40"))
        self.assertEqual(rows[1]["bb_wake_next"], "52")
        self.assertEqual(rows[1]["drinks"], "")                      # nothing logged stays blank, never 0

    def test_future_shifts_are_not_scored(self):
        self.assertEqual(len(sc.build_rows(today=date(2026, 10, 21))), 1)

    def test_summary_flags_small_groups(self):
        s = {x["type"]: x for x in sc.summary_by_type(sc.build_rows(today=date(2026, 10, 28)))}
        self.assertTrue(s["CLOSE_LATE"]["few"] and s["OPEN_EARLY"]["few"])
        self.assertEqual(s["CLOSE_LATE"]["bb"]["mean"], 30)

    def test_n_of_five_stops_being_flagged(self):
        rows = [{"shift_type": "MID", "bb_wake_next": str(v), "recharge": "", "sleep_h": ""} for v in (20, 30, 40, 50, 60)]
        mid = [x for x in sc.summary_by_type(rows) if x["type"] == "MID"][0]
        self.assertFalse(mid["few"])
        self.assertEqual((mid["bb"]["mean"], mid["bb"]["median"]), (40, 40))

    def test_build_writes_csv_chart_and_report(self):
        out = sc.build(today=date(2026, 10, 28))
        self.assertEqual(out["rows"], 2)
        self.assertTrue(sc.SCORECARD_CSV.exists() and sc.SCORECARD_PNG.exists() and sc.SCORECARD_PNG.stat().st_size > 1000)
        self.assertEqual(next(csv.reader(sc.SCORECARD_CSV.open(encoding="utf-8"))), sc.SCORE_FIELDS)
        md = sc.SCORECARD_MD.read_text(encoding="utf-8")
        self.assertIn("too few to conclude", md)
        self.assertIn("not a doctor rule", md)
        self.assertIn("CLOSE_LATE", md)

    def test_habit_effects_group_by_next_morning(self):
        gc.alcohol_log(date(2026, 10, 21), 0)
        h = sc.habit_effects(today=date(2026, 10, 28))
        self.assertEqual((h["drink_n"], h["dry_n"]), (1, 1))
        self.assertEqual(h["drink"]["score"], (70.0, 1))
        self.assertEqual(h["late_n"], 1)                             # last dose 16:40 is "4 PM or later"


class DrinkAdvice(Base):
    NOW = datetime(2026, 10, 28, 18, 0)

    def test_no_when_next_shift_starts_before_nine(self):
        self.card("2026-10-28")
        self.shifts(ev("2026-10-29", "07:00", "15:00"))
        v, t = coach.drink_advice(self.NOW)
        self.assertEqual(v, "NO")
        self.assertIn("7:00 AM", t)

    def test_no_when_red(self):
        self.card("2026-10-28", tier="RED")
        v, t = coach.drink_advice(self.NOW)
        self.assertEqual(v, "NO")
        self.assertIn("RED", t)

    def test_no_when_under_three_hours_to_bed(self):
        self.card("2026-10-28", bed="2026-10-28T20:30:00")
        v, t = coach.drink_advice(self.NOW)
        self.assertEqual(v, "NO")
        self.assertIn("under 3 h", t)

    def test_no_when_bed_has_passed(self):
        self.card("2026-10-28", bed="2026-10-28T17:00:00")
        self.assertIn("has passed", coach.drink_advice(self.NOW)[1])

    def test_yes_is_max_one_finished_three_hours_before_bed(self):
        self.card("2026-10-28", bed="2026-10-28T23:30:00")
        self.shifts(ev("2026-10-29", "10:00", "18:00"))
        v, t = coach.drink_advice(self.NOW)
        self.assertEqual(v, "YES")
        self.assertIn("Max 1", t)
        self.assertIn("8:30 PM", t)                                  # 23:30 - 3 h
        self.assertIn("10:00 AM", t)

    def test_exactly_three_hours_is_allowed_and_nine_am_start_is_allowed(self):
        self.card("2026-10-28", bed="2026-10-28T21:00:00")
        self.shifts(ev("2026-10-29", "09:00", "17:00"))
        self.assertEqual(coach.drink_advice(self.NOW)[0], "YES")

    def test_missing_card_is_stated_not_hidden(self):
        v, t = coach.drink_advice(self.NOW)                          # no latest.json
        self.assertIn("assumed 11:30 PM", t)
        self.assertIn("RED rule not checked", t)

    def test_stale_card_is_not_used(self):
        self.card("2026-10-27", tier="RED")
        self.assertIn("RED rule not checked", coach.drink_advice(self.NOW)[1])

    def test_after_midnight_still_belongs_to_last_evening(self):
        self.assertEqual(coach.evening_date(datetime(2026, 10, 29, 0, 40)), date(2026, 10, 28))
        self.assertEqual(coach.evening_date(datetime(2026, 10, 29, 4, 0)), date(2026, 10, 29))

    def test_personal_effect_only_after_thirty_logged_nights(self):
        hist, start = {}, date(2026, 8, 1)
        for i in range(34):
            d = start + timedelta(days=i)
            hist[(d + timedelta(days=1)).isoformat()] = {"date": (d + timedelta(days=1)).isoformat(),
                                                         "hrv_last_night": "30" if i % 2 else "45",
                                                         "bat_charged": "20" if i % 2 else "40"}
            gc.alcohol_log(d, 2 if i % 2 else 0)
        self.history(hist.values())
        real = sc.habit_effects
        with mock.patch.object(sc, "habit_effects", lambda days=30, **k: real(today=date(2026, 9, 10), days=days)):
            self.card("2026-10-28", bed="2026-10-28T23:30:00")
            txt = coach.drink_advice(self.NOW)[1]
            self.assertIn("Your own data", txt)
            self.assertIn("HRV", txt)
        gc.ALCOHOL_CSV.unlink()
        for i in range(5):
            gc.alcohol_log(start + timedelta(days=i), i % 2)
        with mock.patch.object(sc, "habit_effects", lambda days=30, **k: real(today=date(2026, 9, 10), days=days)):
            self.assertNotIn("Your own data", coach.drink_advice(self.NOW)[1])


class CoachMessages(Base):
    def test_evening_message_only_before_a_risky_shift(self):
        self.card("2026-10-28", bed="2026-10-28T23:30:00")
        self.shifts(ev("2026-10-29", "10:00", "18:00"))
        self.assertIsNone(coach.evening_message(datetime(2026, 10, 28, 13, 30)))
        self.shifts(ev("2026-10-30", "07:00", "15:00"))
        self.card("2026-10-29", bed="2026-10-29T22:30:00")
        m = coach.evening_message(datetime(2026, 10, 29, 13, 30))
        self.assertIn("Hard shift tomorrow", m)
        self.assertIn("7:00 AM-3:00 PM", m)
        self.assertIn("10:30 PM", m)                                 # bed target
        self.assertIn("2:30 PM", m)                                  # caffeine cutoff = bed - 8 h
        self.assertIn("Alcohol tonight: NO", m)

    def test_evening_message_mentions_clopen_and_run(self):
        self.shifts(ev("2026-11-04", "14:00", "22:00"), ev("2026-11-05", "07:00", "15:00"))
        self.card("2026-11-04", bed="2026-11-04T23:30:00")
        m = coach.evening_message(datetime(2026, 11, 4, 13, 30))       # tomorrow (11/5) 7 AM follows a 10 PM close
        self.assertIn("clopen", m)

    def test_late_close_message(self):
        self.shifts(ev("2026-10-28", "14:00", "22:00"))
        a = coach.late_close(date(2026, 10, 28))
        m = coach.late_close_message(a)
        self.assertIn("Home around 11:15 PM", m)                     # 22:00 + 75 min Frankfort
        self.assertIn("Earliest realistic bed: 12:15 AM", m)         # + 60 min wind-down
        self.assertIn("Mask on", m)
        self.assertIn("Wind-down", m)

    def test_no_late_close_for_a_day_shift(self):
        self.shifts(ev("2026-10-28", "09:00", "17:00"))
        self.assertIsNone(coach.late_close(date(2026, 10, 28)))

    def test_due_dedupes_through_the_state_file(self):
        self.shifts(ev("2026-10-28", "14:00", "22:00"))
        t = datetime(2026, 10, 28, 23, 20)                           # home 23:15, inside the 3 h window
        out = coach.tick(bot.send, OWNER, t)
        self.assertEqual(out, "coach_lateclose")
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(coach.tick(bot.send, OWNER, t + timedelta(minutes=1)), "idle")
        self.assertEqual(len(self.sent), 1)

    def test_failed_send_is_retried_not_marked(self):
        self.shifts(ev("2026-10-28", "14:00", "22:00"))
        t = datetime(2026, 10, 28, 23, 20)
        self.assertEqual(coach.tick(lambda c, x: False, OWNER, t), "idle")
        self.assertEqual(coach.tick(bot.send, OWNER, t), "coach_lateclose")

    def test_nothing_due_outside_the_window(self):
        self.shifts(ev("2026-10-28", "14:00", "22:00"))
        self.assertEqual(coach.due(datetime(2026, 10, 28, 22, 30)), [])        # before home
        self.assertEqual(coach.due(datetime(2026, 10, 29, 3, 0)), [])          # next day, window long over

    def test_weekly_runs_sundays_only(self):
        sunday = datetime(2026, 11, 1, 18, 5)
        self.assertEqual(sunday.weekday(), 6)
        self.assertEqual([k.split(":")[0] for k, _ in coach.due(sunday)], ["weekly"])
        self.assertEqual(coach.due(datetime(2026, 11, 2, 18, 5)), [])

    def test_weekly_message_content(self):
        self.shifts(ev("2026-10-20", "14:00", "22:00"), ev("2026-10-22", "07:00", "15:00"))
        self.history([{"date": "2026-10-21", "bb_wake": "20"}, {"date": "2026-10-23", "bb_wake": "50"}])
        m = coach.weekly_message(date(2026, 10, 25))
        self.assertIn("CLOSE_LATE: next-day BB 20 (n=1) too few to conclude", m)
        self.assertIn("OPEN_EARLY: next-day BB 50", m)
        self.assertIn("Lowest: CLOSE_LATE", m)
        self.assertIn("anecdotes", m)

    def test_weekly_with_no_data(self):
        self.assertIn("No shift has a next-day", coach.weekly_message(date(2026, 10, 25)))


# ───────────────────────────── clopen / near-clopen (12 h rule) and the daily ICS import ─────────────────────────────
class ClopenRule(Base):
    def flags(self, *events):
        self.shifts(*events)
        return [(x["clopen"], x["near_clopen"]) for x in sc.annotate(sc.load_shifts())]

    def test_strict_clopen_close_9pm_open_9am(self):
        self.assertEqual(self.flags(ev("2026-11-03", "13:00", "21:00"), ev("2026-11-04", "09:00", "17:00")),
                         [(False, False), (True, False)])           # 21:00 -> 09:00 = 12 h: clopen by definition

    def test_close_before_9pm_is_not_clopen_but_near_under_12h(self):
        self.assertEqual(self.flags(ev("2026-11-03", "12:00", "20:00"), ev("2026-11-04", "07:00", "15:00")),
                         [(False, False), (False, True)])           # 11 h gap, closed before 9 PM

    def test_open_after_9am_after_late_close_is_near_only_if_gap_under_12h(self):
        self.assertEqual(self.flags(ev("2026-11-03", "14:00", "22:00"), ev("2026-11-04", "09:30", "17:00")),
                         [(False, False), (False, True)])           # 11.5 h
        self.setUp()
        self.assertEqual(self.flags(ev("2026-11-03", "14:00", "22:00"), ev("2026-11-04", "10:00", "17:00")),
                         [(False, False), (False, False)])          # exactly 12 h

    def test_close_past_midnight_then_early_open_is_clopen(self):
        self.assertEqual(self.flags(ev("2026-11-03", "16:00", "23:30"), ev("2026-11-04", "08:00", "16:00")),
                         [(False, False), (True, False)])

    def test_row_label(self):
        self.shifts(ev("2026-11-03", "14:00", "22:00"), ev("2026-11-04", "07:00", "15:00"),
                    ev("2026-11-05", "09:30", "17:00"), ev("2026-11-06", "10:00", "17:00"))
        self.history([])
        rows = sc.build_rows(today=date(2026, 11, 20))
        self.assertEqual([r["clopen"] for r in rows], ["", "CLOPEN", "", ""])
        self.assertEqual(sc.annotate(sc.load_shifts())[2]["near_clopen"], False)    # 15:00 -> 09:30 next day = 18.5 h


class ImportMerge(Base):
    def test_blank_values_never_overwrite(self):
        self.shifts(ev("2026-11-05", "07:00", "15:00"))
        blank = {"summary": "", "location": "", "start": {"dateTime": "2026-11-05T07:00:00-04:00"},
                 "end": {"dateTime": "2026-11-05T15:00:00-04:00"}}
        blank["summary"] = "Shift in X"
        r = sc.import_shifts(sc.shifts_from_events([blank]))
        rows = sc.load_shifts()
        self.assertEqual((r["added"], r["total"], len(rows)), (0, 1, 1))
        self.assertIn("FRANKFORT", rows[0]["location"])
        self.assertEqual(rows[0]["city"], "FRANKFORT")

    def test_dedupe_by_start_and_location(self):
        self.shifts(ev("2026-11-05", "07:00", "15:00"))
        self.shifts(ev("2026-11-05", "07:00", "15:00"))
        self.assertEqual(len(sc.load_shifts()), 1)
        self.shifts(ev("2026-11-05", "07:00", "15:00", loc="805 BARDSTOWN RD, SPRINGFIELD, KY 40069"))
        self.assertEqual(len(sc.load_shifts()), 2)                  # same start, other store: kept separately


ICS_OK = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nSUMMARY:Shift in 09855S\r\nLOCATION:1300 US HIGHWAY 127 S\\, FRANKFORT\\, KY 40601\r\n"
          "DTSTART;TZID=America/New_York:20261105T070000\r\nDTEND;TZID=America/New_York:20261105T150000\r\n"
          "END:VEVENT\r\nEND:VCALENDAR\r\n")


class IcsTick(Base):
    URL = "https://calendar.google.com" + "/calendar/" + "ical/x%40y/private-SECRETTOKEN/basic.ics"   # fixture, split so the secret scan stays clean

    def setUp(self):
        super().setUp()
        self.extra = [mock.patch.object(sc, "ICS_STATE", self.tmp / "ics_state.json"),
                      mock.patch.object(sc, "_ics_startup_done", False),
                      mock.patch.object(gc, "load_env", lambda *a, **k: None),
                      mock.patch.object(sc, "build", lambda *a, **k: {})]
        for p in self.extra:
            p.start()
        os.environ["SHIFT_ICS_URL"] = self.URL
        self.out = []
        self.fetch_calls = 0

    def tearDown(self):
        os.environ.pop("SHIFT_ICS_URL", None)
        for p in reversed(self.extra):
            p.stop()
        super().tearDown()

    def send(self, chat, text):
        self.out.append(text)
        return True

    def fake_fetch(self, text):
        def f():
            self.fetch_calls += 1
            if isinstance(text, Exception):
                raise text
            return sc.parse_ics(text)
        return f

    def test_silent_when_url_unset(self):
        os.environ.pop("SHIFT_ICS_URL")
        with mock.patch.object(sc, "fetch_ics", self.fake_fetch(ICS_OK)):
            self.assertEqual(sc.ics_tick(self.send, OWNER, datetime(2026, 11, 1, 3, 31)), "idle")
        self.assertEqual((self.fetch_calls, self.out), (0, []))

    def test_startup_import_then_once_a_day_after_0330(self):
        with mock.patch.object(sc, "fetch_ics", self.fake_fetch(ICS_OK)):
            self.assertEqual(sc.ics_tick(self.send, OWNER, datetime(2026, 11, 1, 1, 0)), "ics_import")      # startup
            self.assertEqual(sc.ics_tick(self.send, OWNER, datetime(2026, 11, 1, 1, 5)), "idle")
            self.assertEqual(sc.ics_tick(self.send, OWNER, datetime(2026, 11, 1, 3, 29)), "idle")
            self.assertEqual(sc.ics_tick(self.send, OWNER, datetime(2026, 11, 1, 3, 30)), "ics_import")     # daily slot
            self.assertEqual(sc.ics_tick(self.send, OWNER, datetime(2026, 11, 1, 9, 0)), "idle")
            self.assertEqual(sc.ics_tick(self.send, OWNER, datetime(2026, 11, 2, 3, 31)), "ics_import")     # next day
        self.assertEqual(self.fetch_calls, 3)
        self.assertEqual(len(sc.load_shifts()), 1)
        self.assertEqual(self.out, [])

    def test_error_warns_once_a_day_and_never_prints_the_feed_address(self):
        boom = RuntimeError("HTTP Error 404 for " + self.URL)
        with mock.patch.object(sc, "fetch_ics", self.fake_fetch(boom)):
            self.assertEqual(sc.ics_tick(self.send, OWNER, datetime(2026, 11, 1, 3, 30)), "ics_error")
            sc._ics_startup_done = True
            self.assertEqual(sc.ics_tick(self.send, OWNER, datetime(2026, 11, 1, 3, 40)), "idle")           # no retry hammering
            sc._ics_startup_done = False
            self.assertEqual(sc.ics_tick(self.send, OWNER, datetime(2026, 11, 1, 9, 0)), "ics_error")       # restart same day
            self.assertEqual(sc.ics_tick(self.send, OWNER, datetime(2026, 11, 2, 3, 30)), "ics_error")      # next day
        self.assertEqual(len(self.out), 2)                           # day 1 once, day 2 once
        self.assertTrue(all("SECRETTOKEN" not in m and "calendar.google.com" not in m for m in self.out))
        self.assertIn("[feed]", self.out[0])

    def test_failed_send_is_retried_next_attempt(self):
        boom = ValueError("not an iCal feed")
        with mock.patch.object(sc, "fetch_ics", self.fake_fetch(boom)):
            sc.ics_tick(lambda c, t: False, OWNER, datetime(2026, 11, 1, 3, 30))
            sc._ics_startup_done = False
            sc.ics_tick(self.send, OWNER, datetime(2026, 11, 1, 4, 0))
        self.assertEqual(len(self.out), 1)

    def test_error_leaves_shifts_untouched(self):
        self.shifts(ev("2026-11-05", "07:00", "15:00"))
        with mock.patch.object(sc, "fetch_ics", self.fake_fetch(OSError("net down"))):
            sc.ics_tick(self.send, OWNER, datetime(2026, 11, 1, 3, 30))
        self.assertEqual(len(sc.load_shifts()), 1)

    def test_fetch_rejects_non_ical_response(self):
        class R:
            def read(self):
                return b"<html>sign in</html>"
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
        with mock.patch.object(sc.urllib.request, "urlopen", lambda *a, **k: R()):
            with self.assertRaises(ValueError):
                sc.fetch_ics()

    def test_poll_once_runs_ics_tick(self):
        calls = []
        with mock.patch.object(sc, "ics_tick", lambda s, o: calls.append(o) or "idle"), \
             mock.patch.object(bot, "api", lambda *a, **k: []), \
             mock.patch.object(bot, "checkin_tick", lambda o: "idle"), mock.patch.object(bot, "sweat_tick", lambda o: "idle"), \
             mock.patch.object(coach, "tick", lambda s, o: "idle"), mock.patch.object(bot, "write_heartbeat", lambda *a: None):
            bot.poll_once(None, OWNER)
        self.assertEqual(calls, [OWNER])


if __name__ == "__main__":
    unittest.main()
