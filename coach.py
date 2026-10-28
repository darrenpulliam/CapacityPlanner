#!/usr/bin/env python3
"""Health-consultant coach: pure message logic for capacity_bot.py (no Telegram, no network).

  drink_advice()      "can I drink tonight?" -> verdict + one-line reason
  evening_message()   13:30 the day before a BB RISK shift: bedtime, alcohol, caffeine cutoff
  late_close_message() when you should be home from a shift that ended after 9 PM: wind-down, earliest bed, mask on
  weekly_message()    Sunday 18:00: which shift types and habits cost the most Body Battery
  due()               which scheduled messages are due now (deduped through data\\coach_state.json)

Drink rules until DRINK_PERSONAL_NIGHTS nights of personal data exist (operator, 2026-10-28): NO if the next shift
starts before 9 AM, the day's tier is RED, or bed is under 3 h away. Otherwise max 1, finished 3 h before bed.
After that the personal effect of drinks on next-morning HRV / recharge is appended. These are coaching rules,
not medical advice.
"""
import json
import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import garmin_capacity as gc
import shift_scorecard as sc

log = logging.getLogger("coach")

STATE_FILE = gc.DATA_DIR / "coach_state.json"
EVENING_TIME = (13, 30)             # day-before message, local
WEEKLY_TIME = (18, 0)               # Sundays
WINDOW_H = 3                        # a message missed by the bot being down is still sent this long after its time
EARLY_NO_BEFORE_H = 9               # next shift starting before this: no drinks
DRINK_MIN_HOURS_TO_BED = 3          # fewer hours to bed than this: no drinks
DRINK_FINISH_BEFORE_BED_H = 3       # a permitted drink is finished this long before bed
DRINK_PERSONAL_NIGHTS = 30
WIND_DOWN_MIN = gc.WIND_DOWN_MIN
DEFAULT_BED = (23, 30)


def evening_date(now: datetime) -> date:
    """The calendar day the current evening belongs to (00:00-03:59 is still last night)."""
    return (now - timedelta(hours=4)).date()


def clock(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


def tonight(now: datetime) -> Dict[str, Any]:
    """Tier and bedtime target from data\\latest.json when that card is for this evening's date, else unknowns."""
    out: Dict[str, Any] = {"tier": None, "bed": None, "assumed": False}
    try:
        p = json.loads((gc.DATA_DIR / "latest.json").read_text(encoding="utf-8"))
        if p.get("date") == evening_date(now).isoformat():
            out["tier"] = p.get("tier")
            out["bed"] = datetime.fromisoformat(p["circadian"]["bedtime_target"])
    except Exception as e:                                   # noqa: BLE001
        log.info("no card for tonight: %s: %s", type(e).__name__, e)
    if out["bed"] is None:
        d = evening_date(now)
        out["bed"] = datetime(d.year, d.month, d.day, *DEFAULT_BED)
        out["assumed"] = True
    return out


def tomorrow_shift(now: datetime) -> Optional[Dict[str, Any]]:
    shifts = sc.shifts_on(evening_date(now) + timedelta(days=1))
    return shifts[0] if shifts else None


def personal_effect() -> Optional[str]:
    """One sentence on drinks vs next-morning HRV / recharge once enough logged nights exist, else None."""
    h = sc.habit_effects(days=120)
    logged = h["drink_n"] + h["dry_n"]
    if logged < DRINK_PERSONAL_NIGHTS or h["drink_n"] < 3 or h["dry_n"] < 3:
        return None
    bits = []
    for key, label in (("hrv", "HRV"), ("recharge", "overnight recharge")):
        a, b = h["drink"][key], h["dry"][key]
        if a[0] is not None and b[0] is not None:
            bits.append(f"{label} {a[0]:g} after drinks (n={a[1]}) vs {b[0]:g} without (n={b[1]})")
    return ("Your own data: " + "; ".join(bits) + ".") if bits else None


def drink_advice(now: datetime) -> Tuple[str, str]:
    """('NO'|'YES', message). The reasoning is one line."""
    t = tonight(now)
    bed: datetime = t["bed"]
    nxt = tomorrow_shift(now)
    hours_to_bed = (bed - now).total_seconds() / 3600
    reasons = []
    if nxt is not None and nxt["_s"].hour < EARLY_NO_BEFORE_H:
        reasons.append(f"tomorrow's shift starts {clock(nxt['_s'])} (before 9 AM)")
    if t["tier"] == "RED":
        reasons.append("today is RED tier")
    if hours_to_bed < DRINK_MIN_HOURS_TO_BED:
        reasons.append(f"bed ({clock(bed)}) is under 3 h away" if hours_to_bed > 0 else f"bed ({clock(bed)}) has passed")
    notes = []
    if t["assumed"]:
        notes.append("no bedtime target on today's card, assumed 11:30 PM")
    if t["tier"] is None:
        notes.append("no tier on today's card, RED rule not checked")
    extra = personal_effect()
    tail = (" (" + "; ".join(notes) + ")" if notes else "") + (f"\n{extra}" if extra else "")
    if reasons:
        return "NO", "NO. " + (lambda r: r[0].upper() + r[1:])("; ".join(reasons)) + "." + tail
    finish = bed - timedelta(hours=DRINK_FINISH_BEFORE_BED_H)
    shift_part = f"tomorrow {clock(nxt['_s'])}-{clock(nxt['_e'])}" if nxt else "no shift tomorrow"
    return "YES", (f"Max 1, finished by {clock(finish)} (3 h before your {clock(bed)} bed). "
                   f"Reason: {shift_part}, tier {t['tier'] or 'unknown'}, {hours_to_bed:.1f} h to bed." + tail)


def is_drink_question(text: str) -> bool:
    import re
    t = (text or "").lower().strip()
    if t.startswith("/candrink") or t.startswith("/canidrink"):
        return True
    return bool(re.search(r"\b(can|could|may|should)\s+i\b.*\b(drink|beer|wine|alcohol|bourbon|cocktail)\b", t)
                or re.search(r"\b(ok|okay|alright)\s+to\s+drink\b", t))


def evening_message(now: datetime) -> Optional[str]:
    nxt = tomorrow_shift(now)
    if nxt is None or not nxt["bb_risk"]:
        return None
    t = tonight(now)
    bed: datetime = t["bed"]
    cutoff = bed - timedelta(hours=gc.CAFFEINE_WARN_BEFORE_BED_H)
    flags = " + ".join(f for f in (("clopen" if nxt["clopen"] else ("near-clopen" if nxt["near_clopen"] else "")),
                                   (f"day {nxt['consecutive_days']} in a row" if nxt["consecutive_days"] > 1 else "")) if f)
    _, drink = drink_advice(now)
    return (f"Hard shift tomorrow: {nxt['_s'].strftime('%a')} {clock(nxt['_s'])}-{clock(nxt['_e'])}, "
            f"{nxt['city'].title() or 'unknown'} ({nxt['drive_min']} min drive){' [' + flags + ']' if flags else ''}. BB RISK.\n"
            f"Bed target tonight: {clock(bed)}{' (assumed)' if t['assumed'] else ''}.\n"
            f"Caffeine cutoff: {clock(cutoff)} (8 h before bed).\n"
            f"Alcohol tonight: {drink}")


def late_close(day: date) -> Optional[Dict[str, Any]]:
    for a in sc.shifts_on(day):
        if a["shift_type"] == "CLOSE_LATE":
            return a
    return None


def late_close_message(a: Dict[str, Any]) -> str:
    home = a["_e"] + timedelta(minutes=a["drive_min"])
    bed = home + timedelta(minutes=WIND_DOWN_MIN)
    return (f"Late close done ({clock(a['_e'])}). Home around {clock(home)}.\n"
            f"Wind-down: lights low and screens off, water only, no more caffeine or alcohol, quick shower, "
            f"wash up and meds. Do not start a project.\n"
            f"Earliest realistic bed: {clock(bed)} ({WIND_DOWN_MIN} min after you walk in).\n"
            f"Mask on the moment you lie down, even if you feel wired.")


def weekly_message(today: Optional[date] = None) -> str:
    today = today or date.today()
    rows = sc.build_rows(today=today)
    summ = [s for s in sc.summary_by_type(rows) if s["bb"]["n"] > 0]
    lines = ["Weekly look-back: what costs you Body Battery"]
    if summ:
        worst = min(summ, key=lambda s: s["bb"]["mean"])
        best = max(summ, key=lambda s: s["bb"]["mean"])
        for s in summ:
            lines.append(f"- {s['type']}: next-day BB {s['bb']['mean']:g} (n={s['bb']['n']})"
                         + (" too few to conclude" if s["few"] else ""))
        if worst["type"] != best["type"]:
            lines.append(f"Lowest: {worst['type']} ({worst['bb']['mean']:g}); highest: {best['type']} ({best['bb']['mean']:g})."
                         + (" Early read only." if worst["few"] or best["few"] else ""))
    else:
        lines.append("- No shift has a next-day Body Battery reading yet.")
    week = [r for r in rows if r["date"] >= (today - timedelta(days=7)).isoformat()]
    lines.append(f"This week: {len(week)} shifts, {sum(1 for r in week if r['bb_risk'])} BB RISK, "
                 f"{sum(1 for r in week if r['clopen'] == 'CLOPEN')} clopen, {sum(1 for r in week if r['clopen'] == 'NEAR-CLOPEN')} near-clopen.")
    h = sc.habit_effects(today=today)
    if h["drink_n"] and h["dry_n"]:
        d, y = h["drink"]["recharge"], h["dry"]["recharge"]
        lines.append(f"Alcohol: recharge {d[0]:g} after drinks (n={d[1]}) vs {y[0]:g} dry (n={y[1]})."
                     if d[0] is not None and y[0] is not None else "Alcohol: not enough recharge data yet.")
    if h["late_n"] and h["early_n"]:
        a, b = h["late"]["score"], h["early"]["score"]
        if a[0] is not None and b[0] is not None:
            lines.append(f"Caffeine: sleep score {a[0]:g} when the last dose was 4 PM or later (n={a[1]}) vs {b[0]:g} earlier (n={b[1]}).")
    lines.append("Small groups are anecdotes, not findings.")
    return "\n".join(lines)


# ───────────────────────────── schedule ─────────────────────────────
def _load_state() -> Dict[str, Any]:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:                                        # noqa: BLE001
        return {"sent": {}}


def _mark(key: str, now: datetime) -> None:
    s = _load_state()
    s.setdefault("sent", {})[key] = now.isoformat(timespec="seconds")
    s["sent"] = dict(sorted(s["sent"].items())[-200:])
    STATE_FILE.write_text(json.dumps(s, indent=1), encoding="utf-8")


def _within(now: datetime, at: datetime) -> bool:
    return at <= now < at + timedelta(hours=WINDOW_H)


def due(now: datetime) -> List[Tuple[str, str]]:
    """[(dedupe_key, text)] for scheduled messages due at `now` that have not been sent."""
    sent = _load_state().get("sent", {})
    out: List[Tuple[str, str]] = []
    today = now.date()
    at = datetime(today.year, today.month, today.day, *EVENING_TIME)
    key = f"evening:{today.isoformat()}"
    if _within(now, at) and key not in sent:
        msg = evening_message(now)
        if msg:
            out.append((key, msg))
    a = late_close(today)
    if a is not None:
        home = a["_e"] + timedelta(minutes=a["drive_min"])
        key = f"lateclose:{today.isoformat()}"
        if _within(now, home) and key not in sent:
            out.append((key, late_close_message(a)))
    if now.weekday() == 6:
        at = datetime(today.year, today.month, today.day, *WEEKLY_TIME)
        key = f"weekly:{today.isoformat()}"
        if _within(now, at) and key not in sent:
            out.append((key, weekly_message(today)))
    return out


def tick(send, owner: str, now: Optional[datetime] = None) -> str:
    """Send whatever is due. `send(chat_id, text) -> bool`. A message is marked sent only when send() says True."""
    now = now or datetime.now()
    try:
        did = []
        for key, text in due(now):
            if send(int(owner), text):
                _mark(key, now)
                did.append(key.split(":")[0])
        return "coach_" + "+".join(did) if did else "idle"
    except Exception as e:                                   # noqa: BLE001
        log.warning("coach tick failed: %s: %s", type(e).__name__, e)
        return "error"
