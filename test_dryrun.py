#!/usr/bin/env python3
"""Unit tests: --dry-run is write-free (garmin_capacity.py + sources_extra.py).

Run:  python -m unittest test_dryrun -v
Uses a temporary CAPACITY_DATA_DIR; Garmin, Open-Meteo, Telegram and mail are stubbed. No network.

Covers: a full --dry-run leaves every file under data\\ (incl. raw\\ and the weather raw cache) byte-for-byte
and mtime-identical and prints "DRY RUN: nothing saved"; the same run without --dry-run saves everything;
--dry-run on a published day (update pass) writes nothing; --now --dry-run writes nothing; a dry run never
enters the Garmin credential fallback; --setup saves but does not send.
"""
import io
import json
import os
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="capacity_dryrun_")
os.environ["CAPACITY_DATA_DIR"] = _TMP                 # before the import: the modules read it at import time
os.environ.setdefault("WEATHER_LAT", "37.809")
os.environ.setdefault("WEATHER_LON", "-85.467")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import garmin_capacity as gc                            # noqa: E402
import sources_extra as se                              # noqa: E402

DAY = date(2026, 10, 25)


def open_meteo(day: date):
    """Minimal Open-Meteo hourly payload: 4 days around `day`, pressure drifting down 0.1 hPa/h."""
    t0 = datetime.combine(day - timedelta(days=1), datetime.min.time())
    n = 24 * 4
    return {"hourly": {
        "time": [(t0 + timedelta(hours=i)).isoformat(timespec="minutes") for i in range(n)],
        "pressure_msl": [round(1030.0 - 0.1 * i, 1) for i in range(n)],
        "temperature_2m": [50.0] * n, "relative_humidity_2m": [60] * n,
        "precipitation_probability": [0] * n, "weather_code": [1] * n,
    }}


class FakeResp:
    def __init__(self, j):
        self.j = j

    def json(self):
        return self.j


class FakeHttp:
    def __init__(self):
        self.calls = 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        return FakeResp(open_meteo(DAY))


class FakeClient:
    def __init__(self, weigh_ins_range):
        self.wr, self.calls = weigh_ins_range, 0

    def get_weigh_ins(self, start, end):
        self.calls += 1
        return self.wr


def snapshot():
    """Every file under data\\ (recursively) -> (mtime_ns, size)."""
    out = {}
    for root in {gc.DATA_DIR, se.WEATHER_RAW_DIR.parent}:
        if root.exists():
            for f in root.rglob("*"):
                if f.is_file():
                    st = f.stat()
                    out[str(f)] = (st.st_mtime_ns, st.st_size)
    return out


class DryRun(unittest.TestCase):
    def setUp(self):
        self.sent, self.mailed = [], []
        gc.send_telegram = lambda text: self.sent.append(text) or True
        gc.send_mail = lambda payload, text, **kw: self.mailed.append(json.loads(json.dumps(payload))) or True
        self.http = FakeHttp()
        se._session = lambda: self.http
        raw = gc.mock_raw(DAY)
        raw["_meta"] = {"fetched_at": "2026-10-25T09:00:05", "date": str(DAY), "source": "test"}
        raw["weigh_ins_range"] = gc.mock_weigh_ins(DAY, pm=245.2)
        gc.pull_garmin = lambda day, interactive=False: raw
        gc.DRY_RUN = se.DRY_RUN = False
        for f in (gc.DATA_DIR / f"{DAY}.json", gc.DATA_DIR / "latest.json", gc.HISTORY_CSV, gc.RAW_DIR / f"{DAY}.json",
                  se.WEATHER_RAW_DIR / f"weather_{DAY}.json", gc.DATA_DIR / "now_latest.json",
                  gc.DATA_DIR / "now_latest.raw.json", gc.RELOGIN_STATE):
            if f.exists():
                f.unlink()
        if gc.NOW_DIR.exists():
            for f in gc.NOW_DIR.glob("*.json"):
                f.unlink()
        # something already on disk, so "unchanged" means more than "still empty"
        gc.DATA_DIR.mkdir(parents=True, exist_ok=True)
        gc.HISTORY_CSV.write_text("date,tier\n2026-10-24,YELLOW\n", encoding="utf-8")
        (gc.DATA_DIR / "2026-10-24.json").write_text('{"date": "2026-10-24", "tier": "YELLOW"}', encoding="utf-8")

    def tearDown(self):
        gc.DRY_RUN = se.DRY_RUN = False

    def run_main(self, *args):
        sys.argv = ["garmin_capacity.py", "--date", str(DAY), "--no-extra", *args]
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = gc.main()
        return rc, buf.getvalue()

    @staticmethod
    def payload_from(out: str):
        return json.loads(out.split("<<<CAPACITY_JSON\n", 1)[1].split("\nCAPACITY_JSON>>>", 1)[0])

    def test_dry_run_leaves_data_untouched(self):
        before = snapshot()
        rc, out = self.run_main("--dry-run")
        self.assertEqual(rc, 0)
        self.assertEqual(snapshot(), before)                            # no new file, no mtime, no size change
        self.assertIn("DRY RUN: nothing saved", out)
        self.assertIn("CAPACITY", out.splitlines()[0])                 # the card is still printed
        p = self.payload_from(out)
        self.assertEqual(p["overnight"]["missing"], "am")
        self.assertIsNotNone(p["weather"]["pressure_hpa"])             # weather WAS fetched ...
        self.assertEqual(self.http.calls, 1)
        self.assertFalse((se.WEATHER_RAW_DIR / f"weather_{DAY}.json").exists())   # ... but its raw cache was not written
        self.assertTrue(gc.DRY_RUN and se.DRY_RUN)
        self.assertEqual((self.sent, self.mailed), ([], []))
        self.assertIsNone(gc.published(DAY))

    def test_normal_run_still_saves(self):
        rc, out = self.run_main()
        self.assertEqual(rc, 0)
        self.assertNotIn("DRY RUN", out)
        self.assertFalse(gc.DRY_RUN or se.DRY_RUN)
        for f in (gc.RAW_DIR / f"{DAY}.json", gc.DATA_DIR / f"{DAY}.json", gc.DATA_DIR / "latest.json",
                  se.WEATHER_RAW_DIR / f"weather_{DAY}.json"):
            self.assertTrue(f.exists(), f)
        self.assertIn(f"\n{DAY},GREEN,", gc.HISTORY_CSV.read_text(encoding="utf-8"))
        self.assertIn("2026-10-24,YELLOW", gc.HISTORY_CSV.read_text(encoding="utf-8"))   # other rows kept
        self.assertEqual(gc.published(DAY)["tier"], "GREEN")
        self.assertEqual((len(self.sent), len(self.mailed)), (1, 1))

    def test_dry_run_on_published_day_writes_nothing(self):
        self.run_main()                                                 # 09:00 real run
        before = snapshot()
        fc = FakeClient(gc.mock_weigh_ins(DAY, pm=245.2, am=242.8, am_at="09:40"))
        gc.garmin_client = lambda interactive=False: fc
        gc.pull_garmin = lambda day, interactive=False: self.fail("full pull must not run on a published day")
        rc, out = self.run_main("--dry-run")
        self.assertEqual((rc, fc.calls), (0, 1))
        self.assertIn("Overnight: 245.2 → 242.8 lb", out)             # the would-be payload is shown ...
        self.assertIn("DRY RUN: nothing saved", out)
        self.assertEqual(snapshot(), before)                            # ... and nothing on disk moved
        self.assertEqual(gc.published(DAY)["overnight"]["missing"], "am")
        self.assertEqual((len(self.sent), len(self.mailed)), (1, 1))

    def test_now_dry_run_writes_nothing(self):
        gc.pull_now = lambda day: {"stats": {"x": 1}, "stress": None, "_meta": {"fetched_at": "2026-10-25T15:00:00"}}
        gc.extract_now = lambda raw, day, at: {"bb_now": 40}
        gc.now_card = lambda payload: "NOW CARD"
        before = snapshot()
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = gc.run_now(DAY, dry_run=True)
        self.assertEqual(rc, 0)
        self.assertIn("DRY RUN: nothing saved", buf.getvalue())
        self.assertIn("CAPACITY_NOW_JSON", buf.getvalue())
        self.assertEqual(snapshot(), before)
        self.assertFalse((gc.DATA_DIR / "now_latest.json").exists())
        self.assertEqual((self.sent, self.mailed), ([], []))
        with redirect_stdout(io.StringIO()):
            rc = gc.run_now(DAY, dry_run=False)
        self.assertEqual(rc, 0)
        self.assertTrue((gc.DATA_DIR / "now_latest.json").exists())
        self.assertTrue((gc.DATA_DIR / "now_latest.raw.json").exists())
        self.assertEqual(len(list(gc.NOW_DIR.glob(f"{DAY}_*.json"))), 1)
        self.assertEqual((len(self.sent), len(self.mailed)), (1, 1))

    def test_dry_run_never_enters_credential_fallback(self):
        class FakeGarmin:
            def __init__(self, *a, **k):
                pass

            def login(self, tokendir):
                raise Exception("401 Client Error: token expired")

        fake = types.ModuleType("garminconnect")
        fake.Garmin = FakeGarmin
        saved = sys.modules.get("garminconnect")
        sys.modules["garminconnect"] = fake
        gc.send_telegram = lambda text: self.fail("dry run must not alert")
        try:
            gc.DRY_RUN = True
            gc.credential_fallback = lambda G, err: self.fail("dry run must not try the credential login")
            with self.assertRaises(RuntimeError) as cm:
                gc.garmin_client()
            self.assertIn("dry run", str(cm.exception))
            self.assertFalse(gc.RELOGIN_STATE.exists())                 # no relogin state written
            gc.DRY_RUN = False                                          # the real run still falls back
            gc.credential_fallback = lambda G, err: "FALLBACK"
            self.assertEqual(gc.garmin_client(), "FALLBACK")
        finally:
            if saved is not None:
                sys.modules["garminconnect"] = saved
            else:
                sys.modules.pop("garminconnect", None)

    def test_setup_saves_but_does_not_send(self):
        gc.garmin_client = lambda interactive=False: object()
        rc, out = self.run_main("--setup")
        self.assertEqual(rc, 0)
        self.assertIn("Garmin login OK", out)
        self.assertIn("CAPACITY_JSON", out)
        self.assertNotIn("DRY RUN", out)
        self.assertEqual(gc.published(DAY)["tier"], "GREEN")              # the setup run is the day's real fetch: saved
        self.assertTrue((gc.RAW_DIR / f"{DAY}.json").exists())
        self.assertEqual((self.sent, self.mailed), ([], []))            # but nothing published


if __name__ == "__main__":
    unittest.main(verbosity=2)
