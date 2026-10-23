#!/usr/bin/env python3
"""
Capacity Planner — one-time Garmin sleep history backfill (resumable)
=====================================================================
Standalone project (C:\\Users\\dppha\\CapacityPlanner). Not part of CrystalBull.

Fills data\\history_backfill.csv with one row per day for the last --days (default 365) so the
capacity review can relate sleep timing (onset / wake / midpoint), sleep, Body Battery, RHR and
HRV to missed targets, alcohol and swing shifts. Record-only: it never touches history.csv,
latest.json or the daily payloads.

Columns: date, sleep_onset, wake_time, sleep_midpoint, sleep_min, sleep_score, bb_wake,
         bb_charged_overnight, resting_hr, hrv_last_night, hrv_status, source=garmin_backfill
A day with no Garmin sleep record (watch off) is still written, with the sleep fields blank, so a
resumed run does not fetch it again.

Sources per day, in this order:
  1. data\\raw\\<date>.json (the morning run's cache) when it has a sleep record — no network.
  2. Garmin Connect: get_sleep_data, get_body_battery, get_stats (bodyBatteryAtWakeTime + RHR),
     get_hrv_data. At most ONE request per second (REQUEST_GAP_S); the same token store and
     hardened session as garmin_capacity.py (token only — never a credential login here).
Resumable: rows are appended and flushed one day at a time; dates already in the file are skipped.
Ctrl-C or a Garmin 429 stops cleanly; run it again to continue. Newest days first, so an
interrupted run still leaves the most useful part filled.

Usage:
  python backfill_sleep_history.py                 # last 365 days up to yesterday
  python backfill_sleep_history.py --days 30
  python backfill_sleep_history.py --end 2026-10-22 --days 90
  python backfill_sleep_history.py --dry-run       # cache-only pass, nothing fetched
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import garmin_capacity as gc

OUT_CSV = gc.DATA_DIR / "history_backfill.csv"
REQUEST_GAP_S = 1.0
FIELDS = ["date", "sleep_onset", "wake_time", "sleep_midpoint", "sleep_min", "sleep_score", "bb_wake",
          "bb_charged_overnight", "resting_hr", "hrv_last_night", "hrv_status", "source"]
log = logging.getLogger("capacity.backfill")


class Throttle:
    """At most one Garmin request per REQUEST_GAP_S; counts requests for the report."""

    def __init__(self):
        self.last = 0.0
        self.count = 0

    def call(self, fn, *args):
        wait = REQUEST_GAP_S - (time.monotonic() - self.last)
        if wait > 0:
            time.sleep(wait)
        self.last = time.monotonic()
        self.count += 1
        return fn(*args)


class Throttled(Exception):
    pass


def existing_dates() -> set:
    if not OUT_CSV.exists():
        return set()
    try:
        return {r["date"] for r in csv.DictReader(OUT_CSV.open(encoding="utf-8")) if r.get("date")}
    except Exception as e:
        log.warning("could not read %s (%s); starting over", OUT_CSV, e)
        return set()


def from_cache(day: date) -> Optional[Dict[str, Any]]:
    f = gc.RAW_DIR / f"{day}.json"
    if not f.exists():
        return None
    try:
        raw = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None
    return raw if gc.dig(raw, "sleep", "dailySleepDTO") is not None else None


def fetch(client, day: date, th: Throttle) -> Dict[str, Any]:
    d = day.isoformat()
    raw: Dict[str, Any] = {}
    for key, fn, args in (("sleep", client.get_sleep_data, (d,)), ("body_battery", client.get_body_battery, (d, d)),
                          ("stats", client.get_stats, (d,)), ("hrv", client.get_hrv_data, (d,))):
        try:
            raw[key] = th.call(fn, *args)
        except Exception as e:
            if "429" in str(e) or "Too Many" in str(e):
                raise Throttled(str(e)) from e
            log.warning("%s %s failed: %s", d, key, e)
            raw[key] = None
    return raw


def row_for(day: date, raw: Dict[str, Any]) -> Dict[str, Any]:
    m = gc.extract(raw)
    t = gc.sleep_timing(raw, None)             # Garmin only; no SleepIQ history here
    return {"date": day.isoformat(), **{k: t[k] for k in ("sleep_onset", "wake_time", "sleep_midpoint")},
            "sleep_min": m.get("sleep_min"), "sleep_score": m.get("sleep_score"),
            "bb_wake": m.get("bb_wake") if gc.dig(raw, "stats", "bodyBatteryAtWakeTime") is not None else _bb_at_wake(raw, t),
            "bb_charged_overnight": m.get("bb_charged_overnight"), "resting_hr": m.get("resting_hr"),
            "hrv_last_night": m.get("hrv_last_night"), "hrv_status": m.get("hrv_status"), "source": "garmin_backfill"}


def _bb_at_wake(raw: Dict[str, Any], t: Dict[str, Any]) -> Optional[int]:
    """Body Battery reading nearest (at or before) the wake time, when stats has no bodyBatteryAtWakeTime."""
    try:
        if not t.get("wake_time"):
            return None
        wake = datetime.fromisoformat(t["wake_time"])
        vals = gc.dig(raw, "body_battery", 0, "bodyBatteryValuesArray", default=[]) or []
        best = None
        for v in vals:
            if not (isinstance(v, list) and len(v) >= 2 and v[-1] is not None):
                continue
            when = gc._garmin_local_ms(v[0])
            if when and when <= wake + timedelta(minutes=10):
                best = v[-1]
        return int(best) if best is not None else None
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--end", help="last day to fill (YYYY-MM-DD, default yesterday)")
    ap.add_argument("--dry-run", action="store_true", help="cache-only pass; no Garmin requests")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    gc.load_env()

    end = date.fromisoformat(a.end) if a.end else date.today() - timedelta(days=1)
    days = [end - timedelta(days=i) for i in range(a.days)]            # newest first
    done = existing_dates()
    todo = [d for d in days if d.isoformat() not in done]
    log.info("%s: %d of %d days already filled; %d to do (%s .. %s)", OUT_CSV.name, len(days) - len(todo), len(days),
             len(todo), todo[-1] if todo else "-", todo[0] if todo else "-")

    client = None
    th = Throttle()
    new_file = not OUT_CSV.exists() or OUT_CSV.stat().st_size == 0
    gc.DATA_DIR.mkdir(parents=True, exist_ok=True)
    filled = missing = cached = 0
    stopped = None
    with OUT_CSV.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            w.writeheader()
            f.flush()
        try:
            for i, day in enumerate(todo, 1):
                raw = from_cache(day)
                if raw is not None:
                    cached += 1
                elif a.dry_run:
                    continue
                else:
                    if client is None:
                        client = gc.garmin_client(interactive=False)
                    raw = fetch(client, day, th)
                row = row_for(day, raw)
                w.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in FIELDS})
                f.flush()
                if row["sleep_min"]:
                    filled += 1
                else:
                    missing += 1
                if i % 25 == 0 or i == len(todo):
                    log.info("%d/%d  %s  filled=%d missing=%d (cache hits %d, requests %d)",
                             i, len(todo), day, filled, missing, cached, th.count)
        except KeyboardInterrupt:
            stopped = "interrupted (Ctrl-C) — run again to resume"
        except Throttled as e:
            stopped = f"Garmin throttled (429): {str(e)[:80]} — wait a few hours, run again to resume"
        except Exception as e:
            stopped = f"{type(e).__name__}: {e} — run again to resume"

    total_rows = 0
    with_sleep = 0
    if OUT_CSV.exists():
        for r in csv.DictReader(OUT_CSV.open(encoding="utf-8")):
            total_rows += 1
            if r.get("sleep_min"):
                with_sleep += 1
    print(f"this run: {filled} days with sleep, {missing} days without (watch off / no record), "
          f"{cached} from the raw cache, {th.count} Garmin requests")
    print(f"{OUT_CSV}: {total_rows} days written, {with_sleep} with a sleep record, {total_rows - with_sleep} missing")
    if stopped:
        print(f"STOPPED: {stopped}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
