#!/usr/bin/env python3
"""Shift scorecard: one row per work shift joined to what the body did afterwards.

Source of shifts: data\\shifts.csv (date,start,end,location,city,summary,source). The bot has no Google login, so the
file is filled from the primary calendar's "Shift in ..." events by one of:
  python shift_scorecard.py --import-shifts events.json   (calendar events as JSON: start.dateTime / end.dateTime /
                                                           summary / location, as the Google Calendar API returns them)
  python shift_scorecard.py --import-ics                  (SHIFT_ICS_URL in .env = the calendar's secret .ics address)
Re-importing is safe: a shift is keyed by its start time and the latest import wins.

Outputs (python shift_scorecard.py --build):
  data\\shift_scorecard.csv, data\\analysis\\shift_scorecard.png (next-day BB by shift type),
  data\\analysis\\shift_scorecard_report.md (one printable page for the scheduler / doctor).

Row timing: shift on date D -> the night after it is D->D+1, so "next day" is the history.csv row dated D+1 (wake BB,
sleep, recharge, HRV) and "day+2" is the row dated D+2. Drinks are the alcohol.csv night D; caffeine is day D.
Overnight HR is not in history.csv, so resting HR of the D+1 row stands in for it (column overnight_hr_proxy).
"BB RISK" is a soft flag (start before 9:00 or end after 21:00), not a doctor rule.
"""
import argparse
import csv
import json
import logging
import os
import re
import statistics
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import garmin_capacity as gc

log = logging.getLogger("shift_scorecard")

DATA_DIR = gc.DATA_DIR
SHIFTS_CSV = DATA_DIR / "shifts.csv"
SCORECARD_CSV = DATA_DIR / "shift_scorecard.csv"
ANALYSIS_DIR = DATA_DIR / "analysis"
SCORECARD_PNG = ANALYSIS_DIR / "shift_scorecard.png"
SCORECARD_MD = ANALYSIS_DIR / "shift_scorecard_report.md"

SHIFT_TITLE_RE = re.compile(r"^\s*Shift in\b", re.I)
EARLY_BEFORE_H = 8          # OPEN_EARLY: start before 08:00
OPEN_BEFORE_H = 9           # OPEN_8_9: start 08:00-08:59; the BB RISK flag also uses 9:00
LATE_AFTER_H = 21           # CLOSE_LATE / BB RISK: end after 21:00
CLOPEN_GAP_H = 12           # NEAR-CLOPEN: next shift starts less than this long after the previous one ended
CLOPEN_CLOSE_H = 21         # CLOPEN: previous shift ends at/after this hour ...
CLOPEN_OPEN_H = 9           # ... and the next-day shift starts at/before this hour
MIN_N = 5                   # fewer shifts than this in a type: "too few to conclude"
DEFAULT_DRIVE_MIN = 60
DRIVE_MIN = {               # Bardstown home -> store city, minutes
    "FRANKFORT": 75, "LAWRENCEBURG": 60, "LEXINGTON": 75, "LOUISVILLE": 45, "MOUNT WASHINGTON": 30,
    "SHEPHERDSVILLE": 30, "ELIZABETHTOWN": 35, "E-TOWN": 35, "SPRINGFIELD": 25, "PROSPECT": 60,
}
SHIFT_TYPES = ["OPEN_EARLY", "OPEN_8_9", "MID", "CLOSE_LATE"]
SHIFT_FIELDS = ["date", "start", "end", "location", "city", "summary", "source"]
SCORE_FIELDS = ["date", "weekday", "city", "start", "end", "shift_type", "bb_risk", "drive_min", "consecutive_days",
                "clopen", "planned_bed", "sleep_onset", "wake_time", "sleep_h", "sleep_score", "bb_wake_next",
                "bb_wake_day2", "recharge", "hrv", "overnight_hr_proxy", "drinks", "caffeine_mg", "caffeine_last"]


# ───────────────────────────── shifts store ─────────────────────────────
def city_of(location: str) -> str:
    """'1300 US HIGHWAY 127 S, FRANKFORT, KY 40601' -> 'FRANKFORT'; '' when it cannot be read."""
    parts = [p.strip() for p in (location or "").split(",") if p.strip()]
    if len(parts) >= 3 and re.match(r"^[A-Z]{2}\b", parts[-1]):
        return parts[-2].upper()
    return ""


def drive_minutes(city: str) -> int:
    return DRIVE_MIN.get((city or "").upper(), DEFAULT_DRIVE_MIN)


def _parse_dt(v: Any) -> Optional[datetime]:
    """ISO text (with or without offset) -> naive local datetime. The calendar is America/New_York and so is the
    offset written on its events, so dropping the offset keeps the wall-clock time."""
    try:
        return datetime.fromisoformat(str(v)).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def shifts_from_events(events: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    out = []
    for e in events or []:
        summary = e.get("summary") or ""
        if not SHIFT_TITLE_RE.match(summary):
            continue
        s = _parse_dt((e.get("start") or {}).get("dateTime"))
        t = _parse_dt((e.get("end") or {}).get("dateTime"))
        if not s or not t or t <= s:
            continue                                    # all-day or unreadable: not a timed shift
        loc = e.get("location") or ""
        out.append({"date": s.date().isoformat(), "start": s.isoformat(timespec="minutes"),
                    "end": t.isoformat(timespec="minutes"), "location": loc, "city": city_of(loc),
                    "summary": summary.strip(), "source": e.get("source", "calendar")})
    return out


def load_shifts() -> List[Dict[str, str]]:
    try:
        with SHIFTS_CSV.open(encoding="utf-8", newline="") as f:
            rows = [r for r in csv.DictReader(f) if r.get("start") and r.get("end")]
    except FileNotFoundError:
        return []
    return sorted(rows, key=lambda r: r["start"])


def save_shifts(rows: List[Dict[str, str]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with SHIFTS_CSV.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SHIFT_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: r["start"]))


def import_shifts(new: List[Dict[str, str]]) -> Dict[str, int]:
    """Merge by (start, location); a blank incoming location matches any row with that start. Non-blank incoming
    values win, blank ones never overwrite what is already stored. Returns counts."""
    rows = load_shifts()
    added = changed = 0
    for r in new:
        loc = (r.get("location") or "").strip()
        old = next((o for o in rows if o["start"] == r["start"]
                    and (not loc or (o.get("location") or "").strip() == loc)), None)
        if old is None:
            rows.append({k: r.get(k, "") or "" for k in SHIFT_FIELDS})
            added += 1
            continue
        diff = False
        for k in SHIFT_FIELDS:
            v = r.get(k)
            if v not in (None, "") and old.get(k) != v:
                if k in ("end", "location"):
                    diff = True
                old[k] = v
        changed += diff
    save_shifts(rows)
    return {"added": added, "changed": changed, "total": len(rows)}


def parse_ics(text: str) -> List[Dict[str, Any]]:
    """Minimal .ics reader -> calendar-API-shaped events. Handles folded lines, DTSTART/DTEND with TZID or a trailing Z
    (converted to America/New_York); skips all-day events. No recurrence expansion (shifts are single events)."""
    from zoneinfo import ZoneInfo
    ny = ZoneInfo("America/New_York")
    lines: List[str] = []
    for ln in text.replace("\r\n", "\n").split("\n"):
        if ln[:1] in (" ", "\t") and lines:
            lines[-1] += ln[1:]
        else:
            lines.append(ln)
    events, cur = [], None
    for ln in lines:
        if ln == "BEGIN:VEVENT":
            cur = {}
        elif ln == "END:VEVENT":
            if cur is not None:
                events.append(cur)
            cur = None
        elif cur is not None and ":" in ln:
            key, val = ln.split(":", 1)
            name = key.split(";")[0].upper()
            if name in ("DTSTART", "DTEND"):
                try:
                    dt = datetime.strptime(val.rstrip("Z"), "%Y%m%dT%H%M%S")
                except ValueError:
                    continue                            # all-day (YYYYMMDD) or unreadable
                if val.endswith("Z"):
                    dt = dt.replace(tzinfo=timezone.utc).astimezone(ny).replace(tzinfo=None)
                cur[name] = dt.isoformat()
            elif name in ("SUMMARY", "LOCATION"):
                cur[name] = val.replace("\\,", ",").replace("\\n", " ").replace("\\;", ";")
    return [{"summary": e.get("SUMMARY", ""), "location": e.get("LOCATION", ""), "source": "ics",
             "start": {"dateTime": e["DTSTART"]}, "end": {"dateTime": e["DTEND"]}}
            for e in events if "DTSTART" in e and "DTEND" in e]


def fetch_ics() -> List[Dict[str, Any]]:
    gc.load_env()
    url = os.getenv("SHIFT_ICS_URL", "").strip()
    if not url:
        raise RuntimeError("SHIFT_ICS_URL is not set in .env (Google Calendar > Settings > Integrate calendar > "
                           "Secret address in iCal format)")
    with urllib.request.urlopen(url, timeout=30, context=gc._TLS_CTX) as r:
        text = r.read().decode("utf-8", "replace")
    if "BEGIN:VCALENDAR" not in text:
        raise ValueError("response is not an iCal feed (wrong or revoked address?)")
    return parse_ics(text)


# ───────────────────────────── daily ICS auto-import (called from the bot's poll loop) ─────────────────────────────
ICS_STATE = DATA_DIR / "ics_state.json"
ICS_IMPORT_TIME = (3, 30)
_ics_startup_done = False


def _ics_state() -> Dict[str, str]:
    try:
        return json.loads(ICS_STATE.read_text(encoding="utf-8"))
    except Exception:                                        # noqa: BLE001
        return {}


def _ics_save(st: Dict[str, str]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ICS_STATE.write_text(json.dumps(st, indent=1), encoding="utf-8")


def ics_tick(send, owner: str, now: Optional[datetime] = None) -> str:
    """Import the calendar feed once on bot startup and once a day after 03:30. Silent when SHIFT_ICS_URL is unset.
    A failure is logged and warned about by Telegram at most once a day; the feed address is never printed."""
    global _ics_startup_done
    now = now or datetime.now()
    try:
        gc.load_env()
        url = os.getenv("SHIFT_ICS_URL", "").strip()
        if not url:
            return "idle"
        st = _ics_state()
        today = now.date().isoformat()
        at = datetime(now.year, now.month, now.day, *ICS_IMPORT_TIME)
        startup = not _ics_startup_done
        if not (startup or (now >= at and st.get("last_daily") != today)):
            return "idle"
        _ics_startup_done = True
        if now >= at:
            st["last_daily"] = today                         # a startup after 03:30 counts as today's daily run
        try:
            res = import_shifts(shifts_from_events(fetch_ics()))
            st["last_ok"] = now.isoformat(timespec="seconds")
            _ics_save(st)
            log.info("ics import ok: %s", res)
            try:
                build()
            except Exception as e:                           # noqa: BLE001
                log.warning("scorecard rebuild after import failed: %s: %s", type(e).__name__, e)
            return "ics_import"
        except Exception as e:                               # noqa: BLE001
            reason = f"{type(e).__name__}: {str(e).replace(url, '[feed]')[:120]}"
            log.warning("ics import failed: %s", reason)
            if st.get("last_warn") != today and send(int(owner), (
                    f"Shift calendar import failed ({reason}). shifts.csv unchanged; "
                    f"will retry tomorrow 3:30 AM or when the bot restarts.")):
                st["last_warn"] = today
            _ics_save(st)
            return "ics_error"
    except Exception as e:                                   # noqa: BLE001
        log.warning("ics tick failed: %s: %s", type(e).__name__, e)
        return "error"


# ───────────────────────────── classification ─────────────────────────────
def shift_times(row: Dict[str, str]):
    return datetime.fromisoformat(row["start"]), datetime.fromisoformat(row["end"])


def _end_hour(start: datetime, end: datetime) -> float:
    return end.hour + end.minute / 60 + (24 if end.date() > start.date() else 0)


def classify(start: datetime, end: datetime) -> str:
    if _end_hour(start, end) > LATE_AFTER_H:
        return "CLOSE_LATE"
    if start.hour < EARLY_BEFORE_H:
        return "OPEN_EARLY"
    if start.hour < OPEN_BEFORE_H:
        return "OPEN_8_9"
    return "MID"


def bb_risk(start: datetime, end: datetime) -> bool:
    return start.hour < OPEN_BEFORE_H or _end_hour(start, end) > LATE_AFTER_H


def annotate(shifts: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    """Add type, BB RISK, drive, consecutive-day count and clopen flag (shifts sorted by start)."""
    out, prev = [], None
    for r in sorted(shifts, key=lambda r: r["start"]):
        s, e = shift_times(r)
        a: Dict[str, Any] = dict(r)
        a["_s"], a["_e"] = s, e
        a["shift_type"] = classify(s, e)
        a["bb_risk"] = bb_risk(s, e)
        a["drive_min"] = drive_minutes(r.get("city") or city_of(r.get("location", "")))
        if prev is not None and (s.date() - prev["_s"].date()).days == 1:
            a["consecutive_days"] = prev["consecutive_days"] + 1
        elif prev is not None and prev["_s"].date() == s.date():
            a["consecutive_days"] = prev["consecutive_days"]          # a second shift the same day
        else:
            a["consecutive_days"] = 1
        gap_h = (s - prev["_e"]).total_seconds() / 3600 if prev is not None else None
        a["clopen"] = bool(prev is not None and gap_h > 0 and (s.date() - prev["_s"].date()).days == 1
                           and _end_hour(prev["_s"], prev["_e"]) >= CLOPEN_CLOSE_H
                           and s.hour + s.minute / 60 <= CLOPEN_OPEN_H)
        a["near_clopen"] = bool(prev is not None and not a["clopen"] and 0 < gap_h < CLOPEN_GAP_H)
        out.append(a)
        prev = a
    return out


# ───────────────────────────── join with outcomes ─────────────────────────────
def _num(v) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _hm(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%H:%M")
    except (TypeError, ValueError):
        return ""


def build_rows(hist: Optional[List[Dict[str, Any]]] = None, today: Optional[date] = None) -> List[Dict[str, Any]]:
    """Scorecard rows for every shift that has started. Outcome cells stay blank until their history row exists."""
    today = today or date.today()
    hist = gc.history_rows() if hist is None else hist
    by_day = {r["date"]: r for r in hist}
    rows = []
    for a in annotate(load_shifts()):
        d: date = a["_s"].date()
        if d > today:
            continue
        r0 = by_day.get(d.isoformat(), {})
        r1 = by_day.get((d + timedelta(days=1)).isoformat(), {})
        r2 = by_day.get((d + timedelta(days=2)).isoformat(), {})
        smin = _num(r1.get("sleep_min"))
        cmg = gc.caffeine_mg_for_day(d)
        dr = gc.alcohol_for_night(d)
        rows.append({
            "date": d.isoformat(), "weekday": d.strftime("%a"), "city": a.get("city", ""),
            "start": a["_s"].strftime("%H:%M"), "end": a["_e"].strftime("%H:%M"),
            "shift_type": a["shift_type"], "bb_risk": "BB RISK" if a["bb_risk"] else "",
            "drive_min": a["drive_min"], "consecutive_days": a["consecutive_days"],
            "clopen": "CLOPEN" if a["clopen"] else ("NEAR-CLOPEN" if a["near_clopen"] else ""),
            "planned_bed": _hm(r0.get("bedtime_target", "")),
            "sleep_onset": _hm(r1.get("sleep_onset", "")), "wake_time": _hm(r1.get("wake_time", "")),
            "sleep_h": round(smin / 60, 1) if smin else "", "sleep_score": r1.get("sleep_score", "") or "",
            "bb_wake_next": r1.get("bb_wake", "") or "", "bb_wake_day2": r2.get("bb_wake", "") or "",
            "recharge": r1.get("bat_charged", "") or "", "hrv": r1.get("hrv_last_night", "") or "",
            "overnight_hr_proxy": r1.get("resting_hr", "") or "",
            "drinks": "" if dr is None else f"{dr:g}",
            "caffeine_mg": f"{cmg:g}" if cmg else "", "caffeine_last": gc.caffeine_last_time(d),
        })
    return rows


def write_scorecard(rows: List[Dict[str, Any]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with SCORECARD_CSV.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SCORE_FIELDS)
        w.writeheader()
        w.writerows(rows)


# ───────────────────────────── summaries ─────────────────────────────
def _stats(vals: List[float]) -> Dict[str, Any]:
    return {"n": len(vals), "mean": round(statistics.mean(vals), 1) if vals else None,
            "median": round(statistics.median(vals), 1) if vals else None}


def summary_by_type(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for t in SHIFT_TYPES:
        rs = [r for r in rows if r["shift_type"] == t]
        bb = [_num(r["bb_wake_next"]) for r in rs if _num(r["bb_wake_next"]) is not None]
        rc = [_num(r["recharge"]) for r in rs if _num(r["recharge"]) is not None]
        sl = [_num(r["sleep_h"]) for r in rs if _num(r["sleep_h"]) is not None]
        out.append({"type": t, "shifts": len(rs), "bb": _stats(bb), "recharge": _stats(rc), "sleep_h": _stats(sl),
                    "few": len(bb) < MIN_N})
    return out


def _fmt(s: Dict[str, Any]) -> str:
    return "-" if s["mean"] is None else f"{s['mean']:g} / {s['median']:g}"


def _pearson(xs: List[float], ys: List[float]) -> Optional[float]:
    if len(xs) < 3 or statistics.pstdev(xs) == 0 or statistics.pstdev(ys) == 0:
        return None
    mx, my = statistics.mean(xs), statistics.mean(ys)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (
        (sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys)) ** 0.5)


def habit_effects(hist: Optional[List[Dict[str, Any]]] = None, today: Optional[date] = None, days: int = 30) -> Dict[str, Any]:
    """Per night (last `days`): alcohol and caffeine vs the NEXT morning's sleep score, resting HR (overnight-HR proxy),
    recharge and wake BB. Group means (value, n) plus Pearson r for caffeine mg when 8+ days."""
    today = today or date.today()
    hist = gc.history_rows() if hist is None else hist
    by_day = {r["date"]: r for r in hist}
    nights = []
    for i in range(1, days + 1):
        d = today - timedelta(days=i)
        nxt = by_day.get((d + timedelta(days=1)).isoformat())
        if not nxt:
            continue
        nights.append({"day": d, "drinks": gc.alcohol_for_night(d), "mg": gc.caffeine_mg_for_day(d),
                       "last": gc.caffeine_last_time(d),
                       "score": _num(nxt.get("sleep_score")), "hr": _num(nxt.get("resting_hr")),
                       "recharge": _num(nxt.get("bat_charged")), "bb": _num(nxt.get("bb_wake")),
                       "hrv": _num(nxt.get("hrv_last_night"))})

    def mean_of(group, key):
        v = [g[key] for g in group if g[key] is not None]
        return (round(statistics.mean(v), 1), len(v)) if v else (None, 0)

    drink = [n for n in nights if (n["drinks"] or 0) > 0]
    dry = [n for n in nights if n["drinks"] == 0]
    cn = [n for n in nights if n["mg"]]
    late = [n for n in cn if n["last"] and _hm(n["last"]) >= "16:00"]
    early = [n for n in cn if n["last"] and _hm(n["last"]) < "16:00"]
    keys = ("score", "hr", "recharge", "bb", "hrv")
    out: Dict[str, Any] = {"nights": len(nights), "drink_n": len(drink), "dry_n": len(dry), "caffeine_n": len(cn),
                           "late_n": len(late), "early_n": len(early),
                           "drink": {k: mean_of(drink, k) for k in keys}, "dry": {k: mean_of(dry, k) for k in keys},
                           "late": {k: mean_of(late, k) for k in keys}, "early": {k: mean_of(early, k) for k in keys},
                           "caffeine": {}}
    for k in ("score", "hr", "recharge"):
        pairs = [(n["mg"], n[k]) for n in cn if n[k] is not None]
        out["caffeine"][k] = _pearson([p[0] for p in pairs], [p[1] for p in pairs]) if len(pairs) >= 8 else None
    return out


# ───────────────────────────── chart + report ─────────────────────────────
def write_chart(rows: List[Dict[str, Any]]) -> bool:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as e:                                   # noqa: BLE001
        log.warning("chart skipped: %s", e)
        return False
    ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(7, 4))
    for i, t in enumerate(SHIFT_TYPES):
        v = [_num(r["bb_wake_next"]) for r in rows if r["shift_type"] == t and _num(r["bb_wake_next"]) is not None]
        if v:
            ax.scatter([i] * len(v), v, alpha=.45, s=28, color="#4c78a8")
            ax.hlines(statistics.mean(v), i - .3, i + .3, color="#e45756", lw=2.5)
        ax.text(i, 2, f"n={len(v)}" + ("\ntoo few" if len(v) < MIN_N else ""), ha="center", fontsize=8)
    ax.axhline(gc.BB_WAKE_TARGET, ls="--", color="gray", lw=1)
    ax.text(len(SHIFT_TYPES) - .5, gc.BB_WAKE_TARGET + 1, "target 60", ha="right", fontsize=8, color="gray")
    ax.set_xticks(range(len(SHIFT_TYPES)))
    ax.set_xticklabels(SHIFT_TYPES, fontsize=9)
    ax.set_ylim(0, 100)
    ax.set_ylabel("Body Battery at wake, day after shift")
    ax.set_title("Next-day Body Battery by shift type (dots = shifts, red = mean)")
    fig.tight_layout()
    fig.savefig(SCORECARD_PNG, dpi=130)
    plt.close(fig)
    return True


def report_md(rows: List[Dict[str, Any]], today: Optional[date] = None) -> str:
    today = today or date.today()
    summ = summary_by_type(rows)
    L = [f"# Shift scorecard, {today.isoformat()}", "",
         "Body Battery (BB) is Garmin's 0-100 energy score. \"Next day\" = the morning after the shift night. "
         "BB RISK is a soft flag (shift starts before 9 AM or ends after 9 PM), not a doctor rule.", "",
         "## By shift type (mean / median)", "",
         "| Type | Shifts | With data | Next-day BB | Overnight recharge | Sleep h | Note |", "|---|---|---|---|---|---|---|"]
    for s in summ:
        note = f"too few to conclude (n<{MIN_N})" if s["few"] else ""
        L.append(f"| {s['type']} | {s['shifts']} | {s['bb']['n']} | {_fmt(s['bb'])} | {_fmt(s['recharge'])} | "
                 f"{_fmt(s['sleep_h'])} | {note} |")
    risk = [r for r in rows if r["bb_risk"]]
    clo = [r for r in rows if r["clopen"] == "CLOPEN"]
    near = [r for r in rows if r["clopen"] == "NEAR-CLOPEN"]
    streak = max([int(r["consecutive_days"]) for r in rows], default=0)
    L += ["", f"Flags in this period: {len(risk)} BB RISK shifts, {len(clo)} clopens "
          f"(close at/after 9 PM, open at/before 9 AM next day) and {len(near)} near-clopens "
          f"(any other gap under {CLOPEN_GAP_H} h), longest run {streak} days in a row.", "",
          "## Last 14 shifts", "",
          "| Date | Where | Hours | Type | Flags | Run | Slept | BB next | BB +2 | Recharge | Drinks | Caffeine mg |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows[-14:]:
        L.append(f"| {r['date']} {r['weekday']} | {r['city'].title()} | {r['start']}-{r['end']} | {r['shift_type']} | "
                 f"{(r['bb_risk'] + ' ' + r['clopen']).strip()} | {r['consecutive_days']} | {r['sleep_h']} | "
                 f"{r['bb_wake_next']} | {r['bb_wake_day2']} | {r['recharge']} | {r['drinks']} | {r['caffeine_mg']} |")
    h = habit_effects(today=today)
    L += ["", "## Habits, last 30 days (next morning)", ""]
    if h["nights"]:
        L.append(f"- Drink nights (n={h['drink_n']}) vs dry nights (n={h['dry_n']}): sleep score "
                 f"{h['drink']['score'][0]} vs {h['dry']['score'][0]}, recharge {h['drink']['recharge'][0]} vs "
                 f"{h['dry']['recharge'][0]}, resting HR {h['drink']['hr'][0]} vs {h['dry']['hr'][0]}.")
        L.append(f"- Caffeine logged on {h['caffeine_n']} days. Last dose 4 PM or later (n={h['late_n']}) vs earlier "
                 f"(n={h['early_n']}): sleep score {h['late']['score'][0]} vs {h['early']['score'][0]}, resting HR "
                 f"{h['late']['hr'][0]} vs {h['early']['hr'][0]}, recharge {h['late']['recharge'][0]} vs {h['early']['recharge'][0]}.")
        L.append("- Small groups are anecdotes, not findings. Overnight HR is approximated by resting HR.")
    else:
        L.append("- Not enough history rows yet.")
    L += ["", "Chart: data\\analysis\\shift_scorecard.png. Data: data\\shift_scorecard.csv."]
    return "\n".join(L) + "\n"


def build(today: Optional[date] = None) -> Dict[str, Any]:
    rows = build_rows(today=today)
    write_scorecard(rows)
    chart = write_chart(rows)
    ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
    SCORECARD_MD.write_text(report_md(rows, today), encoding="utf-8")
    return {"rows": len(rows), "chart": chart, "csv": str(SCORECARD_CSV), "report": str(SCORECARD_MD)}


# ───────────────────────────── lookups the coach uses ─────────────────────────────
def shifts_on(day: date) -> List[Dict[str, Any]]:
    return [a for a in annotate(load_shifts()) if a["_s"].date() == day]


def next_shift_after(now: datetime) -> Optional[Dict[str, Any]]:
    for a in annotate(load_shifts()):
        if a["_s"] > now:
            return a
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--import-shifts", metavar="events.json")
    ap.add_argument("--import-ics", action="store_true")
    ap.add_argument("--build", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO)
    if a.import_shifts:
        ev = json.loads(Path(a.import_shifts).read_text(encoding="utf-8"))
        ev = ev.get("events", ev) if isinstance(ev, dict) else ev
        print("imported:", import_shifts(shifts_from_events(ev)))
    if a.import_ics:
        print("imported:", import_shifts(shifts_from_events(fetch_ics())))
    if a.build or not (a.import_shifts or a.import_ics):
        print(build())
    return 0


if __name__ == "__main__":
    sys.exit(main())
