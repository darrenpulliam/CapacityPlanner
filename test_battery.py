#!/usr/bin/env python3
"""Unit tests for the Body Battery charge / drain block (garmin_capacity.py, display only).

Run:  python -m unittest test_battery -v
Uses a temporary CAPACITY_DATA_DIR; no Garmin, Telegram or mail. Scenarios come from MOCK_CASES["battery*"]
(mock_battery_raw builds Garmin-shaped 3-minute Body Battery timelines).
"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="capacity_battery_")
os.environ["CAPACITY_DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent))
import garmin_capacity as gc                            # noqa: E402

DAY = date(2026, 10, 26)


def metrics(case: str, **over):
    spec = dict(gc.MOCK_CASES[case]["battery"])
    spec.update(over)
    raw = {**gc.mock_raw(DAY), **gc.mock_battery_raw(DAY, **spec)}
    return gc.battery_metrics(raw, DAY, None, raw["_mock"]["prev_wake"], raw["stats"]["bodyBatteryAtWakeTime"])


def run_mock(case: str) -> dict:
    sys.argv = ["garmin_capacity.py", "--mock", case, "--date", str(DAY), "--no-extra", "--no-weather"]
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = gc.main()
    out = buf.getvalue()
    payload = json.loads(out.split("<<<CAPACITY_JSON\n", 1)[1].split("\nCAPACITY_JSON>>>", 1)[0])
    return {"rc": rc, "out": out, "payload": payload, "card": out.split("<<<CAPACITY_JSON", 1)[0]}


def write_history(rows, fields=("date", "tier", "bb_wake", "bat_low")):
    lines = [",".join(fields)] + [",".join(str(r.get(k, "")) for k in fields) for r in rows]
    gc.HISTORY_CSV.write_text("\n".join(lines) + "\n", encoding="utf-8")


class Battery(unittest.TestCase):
    def setUp(self):
        gc.DRY_RUN = False
        gc.DATA_DIR.mkdir(parents=True, exist_ok=True)
        for f in (gc.HISTORY_CSV, gc.DATA_DIR / f"{DAY}.json", gc.DATA_DIR / "latest.json",
                  gc.RAW_DIR / f"{DAY}.json", gc.RAW_DIR / f"{DAY - timedelta(days=1)}.json"):
            if f.exists():
                f.unlink()

    def test_full_data(self):
        b = metrics("battery")
        self.assertEqual(set(b) - {"bedtime_at"}, set(gc.BATTERY_FIELDS))
        self.assertEqual((b["charged_overnight"], b["sleep_hours"], b["wake_bb"], b["peak_bb"], b["bb_at_bedtime"], b["hours_awake"], b["low_bb"]),
                         (37, 8.3, 42, 65, 5, 15.2, 5))
        self.assertTrue("2026-10-25T10:15" <= b["peak_time"] <= "2026-10-25T10:30", b["peak_time"])   # first point of the 65 plateau
        self.assertEqual((b["bedtime_at"], b["below25_at"]), ("2026-10-25T23:40", "2026-10-25T14:15"))
        self.assertTrue("2026-10-25T18:20" <= b["below15_at"] <= "2026-10-25T18:40", b["below15_at"])
        hrs = (datetime(2026, 10, 25, 23, 40) - datetime.fromisoformat(b["peak_time"])).total_seconds() / 3600
        self.assertAlmostEqual(b["drain_per_hr"], round((65 - 5) / hrs, 1), places=1)
        self.assertTrue(24 <= b["rest_recharge_min"] <= 60, b["rest_recharge_min"])              # the 12:00 -> 13:00 rise
        lines = gc.battery_lines({**b, "targets": gc.battery_targets(DAY, b["wake_bb"], b["low_bb"])})
        self.assertIn("🔋 Charged +37 overnight (8.3 h) · woke 42", lines)
        self.assertIn("📉 Yesterday: peak 65 @ 10:", lines)
        self.assertIn("→ 5 @ 11:40 PM · −4.5/h · <25 by 2:15 PM · <15 by 6:", lines)
        self.assertIn("🎯 Targets: wake ≥60 (7-day: 0/1) · low ≥15 (7-day: 0/1)", lines)

    def test_missing_sleep_start(self):
        b = metrics("battery-no-sleep-start")
        self.assertEqual(b["charged_overnight"], 36)                     # Garmin's bodyBatteryDuringSleep fallback
        self.assertEqual(b["sleep_hours"], 7.0)                          # sleepTimeSeconds fallback
        for k in ("bb_at_bedtime", "bedtime_at", "drain_per_hr", "hours_awake"):
            self.assertIsNone(b[k], k)
        self.assertEqual(b["peak_bb"], 65)                               # yesterday's timeline still read
        self.assertEqual(b["below25_at"], "2026-10-25T14:15")
        lines = gc.battery_lines({**b, "targets": {}})
        self.assertIn("🔋 Charged +36 overnight (7.0 h) · woke 42", lines)
        self.assertIn("→ ? @ ? · <25 by 2:15 PM", lines)
        self.assertNotIn("/h", lines)

    def test_never_below_15(self):
        b = metrics("battery-never-low")
        self.assertIsNone(b["below25_at"])
        self.assertIsNone(b["below15_at"])
        self.assertEqual((b["low_bb"], b["bb_at_bedtime"], b["charged_overnight"]), (40, 40, 32))
        t = gc.battery_targets(DAY, b["wake_bb"], b["low_bb"])
        self.assertEqual((t["wake_hits"], t["wake_days"], t["low_hits"], t["low_days"]), (1, 1, 1, 1))
        lines = gc.battery_lines({**b, "targets": t})
        self.assertIn("· never <25", lines)
        self.assertIn("🎯 Targets: wake ≥60 (7-day: 1/1) · low ≥15 (7-day: 1/1)", lines)

    def test_day_off_vs_work_day(self):
        off, work = metrics("battery-day-off"), metrics("battery-work-day")
        self.assertGreater(work["drain_per_hr"], off["drain_per_hr"])
        self.assertIsNotNone(work["below15_at"])
        self.assertIsNone(off["below25_at"])
        self.assertEqual(work["rest_recharge_min"], 0)
        self.assertGreater(off["rest_recharge_min"], 0)                  # the 14:00 -> 15:30 nap
        self.assertEqual((off["hours_awake"], work["hours_awake"]), (15.0, 16.5))
        self.assertEqual((off["bedtime_at"], off["bb_at_bedtime"], off["charged_overnight"]), ("2026-10-26T01:30", 45, 29))   # bed after midnight
        self.assertEqual((work["bedtime_at"], work["bb_at_bedtime"], work["charged_overnight"]), ("2026-10-25T22:30", 8, 27))
        self.assertTrue(work["below25_at"] < "2026-10-25T12:30" and work["below15_at"] < "2026-10-25T17:30")

    def test_targets_7day(self):
        rows = [{"date": (DAY - timedelta(days=6 - i)).isoformat(), "tier": "X", "bb_wake": w, "bat_low": lo}
                for i, (w, lo) in enumerate(zip([65, 40, 61, 20, 50, 30], [20, 5, 16, 10, 14, 15]))]
        rows.append({"date": (DAY - timedelta(days=7)).isoformat(), "tier": "X", "bb_wake": 99, "bat_low": 99})   # outside the window
        write_history(rows)
        t = gc.battery_targets(DAY, 42, 24)
        self.assertEqual((t["wake_hits"], t["wake_days"], t["low_hits"], t["low_days"]), (2, 7, 4, 7))
        self.assertIn("wake ≥60 (7-day: 2/7) · low ≥15 (7-day: 4/7)", gc.battery_lines({**metrics("battery"), "targets": t}))
        write_history([{"date": (DAY - timedelta(days=3)).isoformat(), "bb_wake": 65, "bat_low": ""},
                       {"date": (DAY - timedelta(days=2)).isoformat(), "bb_wake": "", "bat_low": 5},
                       {"date": (DAY - timedelta(days=1)).isoformat(), "bb_wake": 61, "bat_low": ""}])
        t = gc.battery_targets(DAY, 42, 24)
        self.assertEqual((t["wake_hits"], t["wake_days"], t["low_hits"], t["low_days"]), (2, 3, 1, 2))   # blanks don't count
        t = gc.battery_targets(DAY, None, None)
        self.assertEqual((t["wake_hits"], t["wake_days"], t["low_hits"], t["low_days"]), (2, 2, 0, 1))

    def test_card_display_only_and_never_crashes(self):
        r = run_mock("battery")
        self.assertEqual(r["rc"], 0)
        self.assertEqual(r["payload"]["battery"]["charged_overnight"], 37)
        self.assertIn("🔋 Charged +37 overnight (8.3 h) · woke 42\n📉 Yesterday: peak 65 @ 10:", r["card"])
        self.assertIn("\n🎯 Targets: wake ≥60 (7-day:", r["card"])
        frozen = {k: r["payload"][k] for k in ("tier", "reasons", "plan", "circadian", "metrics")}
        orig = gc.battery_metrics
        try:
            gc.battery_metrics = lambda *a, **k: {k2: None for k2 in gc.BATTERY_FIELDS + ["bedtime_at"]}
            r2 = run_mock("battery")
        finally:
            gc.battery_metrics = orig
        self.assertEqual({k: r2["payload"][k] for k in frozen}, frozen)   # same tier / reasons / plan / bedtime without the block
        self.assertIn("🔋 Battery: no timeline data", r2["card"])
        for case in ("default", "watch-off", "overnight", "checkin"):                 # no timeline: nulls, no crash
            r3 = run_mock(case)
            self.assertEqual(r3["rc"], 0, case)
            self.assertIn("battery", r3["payload"])
            self.assertIn("🎯 Targets:", r3["card"])

    def test_history_columns_and_backfill(self):
        p = run_mock("battery")["payload"]
        gc.save(DAY, p, {"_meta": {}})
        rows = gc.history_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["bat_charged"], rows[0]["bat_peak"], rows[0]["bat_bedtime_bb"], rows[0]["bat_below25_at"], rows[0]["bat_sleep_h"]),
                         ("37", "65", "5", "2026-10-25T14:15", "8.3"))
        self.assertTrue(set(gc.BATTERY_HISTORY.values()) <= set(rows[0]))
        # backfill: raw files for yesterday (its own 3-min series + its sleep end) and today (sleep + today's series, no stress_yesterday)
        mb = gc.mock_battery_raw(DAY)
        yday = DAY - timedelta(days=1)
        prev_wake_ms = int(datetime(2026, 10, 25, 8, 30).replace(tzinfo=timezone.utc).timestamp() * 1000)
        gc.RAW_DIR.mkdir(parents=True, exist_ok=True)
        (gc.RAW_DIR / f"{yday}.json").write_text(json.dumps({"stress": mb["stress_yesterday"],
                                                             "sleep": {"dailySleepDTO": {"sleepEndTimestampLocal": prev_wake_ms}}}), encoding="utf-8")
        (gc.RAW_DIR / f"{DAY}.json").write_text(json.dumps({"stats": mb["stats"], "sleep": mb["sleep"], "stress": mb["stress"]}), encoding="utf-8")
        write_history([{"date": DAY.isoformat(), "tier": "RED", "bb_wake": 42, "bat_low": ""}])
        before = gc.HISTORY_CSV.read_bytes()
        gc.DRY_RUN = True
        with redirect_stdout(io.StringIO()):
            self.assertEqual(gc.battery_backfill(), 0)
        self.assertEqual(gc.HISTORY_CSV.read_bytes(), before)             # dry run: untouched
        gc.DRY_RUN = False
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(gc.battery_backfill(), 0)
        self.assertIn("filled 1, skipped 0 of 1 rows", buf.getvalue())
        row = gc.history_rows()[0]
        self.assertEqual((row["bat_charged"], row["bat_peak"], row["bat_hours_awake"], row["bat_below25_at"]), ("37", "65", "15.2", "2026-10-25T14:15"))
        self.assertEqual(row["bb_wake"], "42")                             # other columns untouched


if __name__ == "__main__":
    unittest.main(verbosity=2)
