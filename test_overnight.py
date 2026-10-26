#!/usr/bin/env python3
"""Unit tests for the overnight weigh-in block and the 12:15 update pass (garmin_capacity.py).

Run:  python -m unittest test_overnight -v
Uses a temporary CAPACITY_DATA_DIR; Garmin, Telegram and mail are stubbed. No network.

Sequence under test: 09:00 run with only the bedtime weigh-in -> overnight.missing = "am";
12:15 update pass sees the 09:40 weigh-in -> filled, card + mail re-sent exactly once, tier /
bedtime / reasons unchanged; a repeat pass with the same data -> byte-identical payload, nothing sent.
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="capacity_overnight_")
os.environ["CAPACITY_DATA_DIR"] = _TMP                 # before the import: the module reads it at import time
sys.path.insert(0, str(Path(__file__).resolve().parent))
import garmin_capacity as gc                            # noqa: E402

DAY = date(2026, 10, 25)


class FakeClient:
    def __init__(self, weigh_ins_range):
        self.wr, self.calls = weigh_ins_range, 0

    def get_weigh_ins(self, start, end):
        self.calls += 1
        return self.wr


class FailingClient:
    def get_weigh_ins(self, start, end):
        raise RuntimeError("429 Too Many Requests")


class OvernightUpdate(unittest.TestCase):
    def setUp(self):
        self.sent, self.mailed = [], []
        gc.send_telegram = lambda text: self.sent.append(text) or True
        gc.send_mail = lambda payload, text, **kw: self.mailed.append(json.loads(json.dumps(payload))) or True
        for f in (gc.DATA_DIR / f"{DAY}.json", gc.DATA_DIR / "latest.json", gc.HISTORY_CSV, gc.RAW_DIR / f"{DAY}.json"):
            if f.exists():
                f.unlink()

    def morning_run(self):
        """The 09:00 run: Garmin stubbed; the scale has only the bedtime weigh-in so far."""
        raw = gc.mock_raw(DAY)
        raw["_meta"] = {"fetched_at": "2026-10-25T09:00:05", "date": str(DAY), "source": "test"}
        raw["weigh_ins_range"] = gc.mock_weigh_ins(DAY, pm=245.2)
        gc.pull_garmin = lambda day, interactive=False: raw
        sys.argv = ["garmin_capacity.py", "--date", str(DAY), "--no-extra", "--no-weather"]
        rc = gc.main()
        return rc, gc.published(DAY)

    def test_0900_then_1215_then_repeat(self):
        rc, p = self.morning_run()
        self.assertEqual(rc, 0)
        self.assertEqual(p["overnight"]["missing"], "am")
        self.assertIsNone(p["overnight"]["am_weight_lb"])
        self.assertEqual(p["as_of"], "2026-10-25T09:00:05")
        self.assertIn("(as of 09:00)", self.sent[-1])
        self.assertIn("Overnight: missing morning weigh-in (bed 245.2 lb at 11:35 PM)", self.sent[-1])
        self.assertEqual((len(self.sent), len(self.mailed)), (1, 1))
        frozen = {k: p[k] for k in ("tier", "reasons", "plan", "circadian", "metrics")}
        history_before = gc.HISTORY_CSV.read_bytes()

        # 12:15: the 09:40 morning weigh-in now exists on Garmin
        fc = FakeClient(gc.mock_weigh_ins(DAY, pm=245.2, am=242.8, am_at="09:40"))
        rc = gc.run_overnight_update(DAY, client=fc, now=datetime(2026, 10, 25, 12, 15))
        self.assertEqual((rc, fc.calls), (0, 1))                      # exactly one Garmin request
        p2 = gc.published(DAY)
        o = p2["overnight"]
        self.assertEqual((o["pm_weight_lb"], o["am_weight_lb"], o["delta_lb"], o["delta_ml_est"], o["hours_between"], o["missing"]),
                         (245.2, 242.8, 2.4, 1090, 10.1, None))
        self.assertEqual((o["pm_time"], o["am_time"], o["fetched_at"]), ("2026-10-24T23:35", "2026-10-25T09:40", "2026-10-25T12:15"))
        self.assertEqual({k: p2[k] for k in frozen}, frozen)           # display only: tier / bedtime / blocks untouched
        self.assertEqual(gc.HISTORY_CSV.read_bytes(), history_before)
        self.assertEqual((len(self.sent), len(self.mailed)), (2, 2))   # re-sent once, mail too
        self.assertIn("Overnight: 245.2 → 242.8 lb = −2.4 lb (~1090 mL, 10.1 h) · as of 12:15", self.sent[-1])
        self.assertIn("(as of 09:00)", self.sent[-1])
        self.assertEqual(self.mailed[-1]["overnight"], o)
        self.assertEqual(json.loads((gc.DATA_DIR / "latest.json").read_text(encoding="utf-8"))["overnight"], o)

        # repeat with no new data: identical payload, nothing sent
        before = (gc.DATA_DIR / f"{DAY}.json").read_bytes()
        again = FakeClient(fc.wr)
        rc = gc.run_overnight_update(DAY, client=again, now=datetime(2026, 10, 25, 12, 45))
        self.assertEqual((rc, again.calls), (0, 1))
        self.assertEqual((gc.DATA_DIR / f"{DAY}.json").read_bytes(), before)
        self.assertEqual((len(self.sent), len(self.mailed)), (2, 2))

    def test_scale_failure_changes_nothing(self):
        self.morning_run()
        before = (gc.DATA_DIR / f"{DAY}.json").read_bytes()
        rc = gc.run_overnight_update(DAY, client=FailingClient(), now=datetime(2026, 10, 25, 12, 15))
        self.assertEqual(rc, 0)                                        # never fails the run
        self.assertEqual((gc.DATA_DIR / f"{DAY}.json").read_bytes(), before)
        self.assertEqual((len(self.sent), len(self.mailed)), (1, 1))

    def test_no_weigh_ins_still_none_sends_nothing(self):
        self.morning_run()
        (gc.DATA_DIR / f"{DAY}.json").write_text(json.dumps({**gc.published(DAY), "overnight": None}), encoding="utf-8")
        rc = gc.run_overnight_update(DAY, client=FakeClient(gc.mock_weigh_ins(DAY)), now=datetime(2026, 10, 25, 12, 15))
        self.assertEqual(rc, 0)
        self.assertIsNone(gc.published(DAY)["overnight"])
        self.assertEqual((len(self.sent), len(self.mailed)), (1, 1))

    def test_plain_rerun_routes_to_update_pass(self):
        """The 12:15 trigger runs the same no-arg command line; main() must detect the published day."""
        self.morning_run()
        fc = FakeClient(gc.mock_weigh_ins(DAY, pm=245.2, am=242.8, am_at="09:40"))
        gc.garmin_client = lambda interactive=False: fc
        gc.pull_garmin = lambda day, interactive=False: self.fail("full pull must not run on a published day")
        sys.argv = ["garmin_capacity.py", "--date", str(DAY)]
        self.assertEqual(gc.main(), 0)
        self.assertEqual(fc.calls, 1)
        self.assertEqual(gc.published(DAY)["overnight"]["am_weight_lb"], 242.8)
        self.assertEqual((len(self.sent), len(self.mailed)), (2, 2))

    def test_dry_run_update_prints_only(self):
        self.morning_run()
        before = (gc.DATA_DIR / f"{DAY}.json").read_bytes()
        fc = FakeClient(gc.mock_weigh_ins(DAY, pm=245.2, am=242.8, am_at="09:40"))
        rc = gc.run_overnight_update(DAY, dry_run=True, client=fc, now=datetime(2026, 10, 25, 12, 15))
        self.assertEqual(rc, 0)
        self.assertEqual((gc.DATA_DIR / f"{DAY}.json").read_bytes(), before)   # a dry run never masks the real pass
        self.assertEqual((len(self.sent), len(self.mailed)), (1, 1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
