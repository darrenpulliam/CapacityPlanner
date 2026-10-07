#!/usr/bin/env python3
"""
Capacity Planner — Garmin pull
==============================
Standalone project (C:\\Users\\dppha\\CapacityPlanner). Not part of CrystalBull.

Pulls last night's Garmin data (Body Battery, sleep, HRV, stress, training
readiness), classifies today's capacity (GREEN / YELLOW / RED), computes
tonight's circadian targets, and publishes:

  1. data file   data\\YYYY-MM-DD.json  (+ latest.json, history.csv, raw\\)
  2. Telegram    short capacity card
  3. Gmail       [CAPACITY] mail with a machine-readable JSON block.
                 A cloud Claude scheduled task ("Capacity calendar writer", 08:52 ET)
                 reads it and writes Capacity / caffeine / wind-down / bedtime / wake events.

Secrets: Windows environment variables win, then .env in this folder, then (only
with --setup) an interactive prompt. Keys still missing fall back to CrystalBull's
.env / .env.prod (read-only). GARMIN_PASSWORD should live only in the Windows
user environment; leave it blank in .env.

Garmin auth policy (shared with HealthAI's garmin_fetch.py):
  * ONE token store: %USERPROFILE%\\.garminconnect (override: GARMINTOKENS).
  * Scheduled runs are TOKEN-ONLY. They never attempt a credential login, so a
    bad password can never trip Garmin's 429 throttle. If the token is missing
    or rejected the run fails loudly and says to run --setup.
  * --setup is the only path that logs in with credentials (and MFA).
  * ONE network fetch per day: if data\\raw\\<date>.json was fetched less than
    CACHE_MAX_AGE_H hours ago it is reused (override with --refresh).
    garmin_fetch.py reads the same raw file, so HealthAI never fetches twice.

Usage (Python 3.12+):
  python garmin_capacity.py --setup        # one-time interactive Garmin login (email/password/MFA)
  python garmin_capacity.py                # normal daily run
  python garmin_capacity.py --dry-run      # pull + print, no Telegram/mail
  python garmin_capacity.py --if-missing   # retry task: only runs if today has no data yet
  python garmin_capacity.py --refresh      # ignore the <20 h raw cache and hit Garmin
  python garmin_capacity.py --mock         # no Garmin call; synthetic data
  python garmin_capacity.py --date 2026-10-06

Requires: pip install --upgrade garminconnect curl_cffi python-dotenv requests
"""
from __future__ import annotations

import argparse
import csv
import getpass
import json
import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

# ───────────────────────────── config ─────────────────────────────
BASE_DIR = Path(__file__).resolve().parent                  # ...\CapacityPlanner
CB_ENV_FALLBACKS = [Path(r"C:\CrystalBull\CB_v4.0\.env.prod"), Path(r"C:\CrystalBull\CB_v4.0\.env")]
DATA_DIR = Path(os.getenv("CAPACITY_DATA_DIR", str(BASE_DIR / "data")))
RAW_DIR = DATA_DIR / "raw"
HISTORY_CSV = DATA_DIR / "history.csv"
TOKEN_DIR = Path(os.getenv("GARMINTOKENS", str(Path.home() / ".garminconnect")))
CACHE_MAX_AGE_H = 20           # reuse data\raw\<date>.json if fetched less than this many hours ago

WAKE_ANCHOR = "08:30"          # fixed every day — shift-work anchor (bed 12:15 AM / wake 8:30 AM), set 2026-10-07
SLEEP_NEED_MIN = 480           # 8 h
SLEEP_LATENCY_MIN = 15         # time to fall asleep
WIND_DOWN_MIN = 60             # screens off / lights dim before bedtime
CAFFEINE_CUTOFF_H = 9          # last caffeine this many hours before bedtime
MAX_PULL_EARLIER_MIN = 45      # bedtime never moves more than this earlier

# Capacity tiers (Body Battery at wake)
GREEN_MIN = 50                 # recalibrated 2026-10-07 from 12-mo Garmin history (median wake BB ~35)
YELLOW_MIN = 25
BAD_HRV = {"UNBALANCED", "LOW", "POOR"}
TIER_ORDER = ["RED", "YELLOW", "GREEN"]
TIER_PLAN = {
    "GREEN":  {"deep_blocks": 3, "bedtime_shift": 0,   "guidance": "Full capacity. Front-load the hardest work; outings/events OK."},
    "YELLOW": {"deep_blocks": 2, "bedtime_shift": -15, "guidance": "Moderate. Two deep blocks max, protect the afternoon dip, no new commitments after 6 PM."},
    "RED":    {"deep_blocks": 1, "bedtime_shift": -30, "guidance": "Recovery day. One must-do block, admin only otherwise, walk + daylight, decline optional events."},
}

log = logging.getLogger("capacity")


# ───────────────────────────── helpers ─────────────────────────────
def dig(obj: Any, *path, default=None):
    """Safe nested get across dicts/lists."""
    cur = obj
    for p in path:
        if cur is None:
            return default
        if isinstance(p, int):
            if isinstance(cur, list) and -len(cur) <= p < len(cur):
                cur = cur[p]
            else:
                return default
        else:
            cur = cur.get(p) if isinstance(cur, dict) else None
    return default if cur is None else cur


def hm_to_dt(day: date, hm: str) -> datetime:
    h, m = map(int, hm.split(":"))
    return datetime.combine(day, datetime.min.time()).replace(hour=h, minute=m)


def fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:00")


def load_env():
    """Own .env wins; CrystalBull env files only fill keys still missing (never override)."""
    try:
        from dotenv import load_dotenv
    except Exception:
        log.warning("python-dotenv missing — using process environment only")
        return
    load_dotenv(BASE_DIR / ".env", override=False)
    for f in CB_ENV_FALLBACKS:
        if f.exists():
            load_dotenv(f, override=False)


# ───────────────────────────── Garmin ─────────────────────────────
def garmin_client(interactive: bool = False):
    from garminconnect import Garmin  # imported lazily so --mock works without it

    TOKEN_DIR.mkdir(parents=True, exist_ok=True)

    if interactive:
        # Credential order: Windows env vars (already in os.environ) -> .env (load_env,
        # override=False) -> prompt. The library loads an existing token first and only
        # logs in with these if the token is missing/rejected; after a credential login
        # it writes the new token to TOKEN_DIR itself (garminconnect >= 0.3).
        email = os.getenv("GARMIN_EMAIL") or input("Garmin email: ").strip()
        password = os.getenv("GARMIN_PASSWORD") or getpass.getpass("Garmin password: ")
        client = Garmin(email, password, prompt_mfa=lambda: input("Garmin MFA code: ").strip())
        client.login(str(TOKEN_DIR))
        return client

    # Scheduled / non-interactive: TOKEN ONLY. Never pass credentials here — a silent
    # re-login with a stale password is exactly what trips Garmin's 429 throttle.
    client = Garmin()
    try:
        client.login(str(TOKEN_DIR))
    except Exception as e:
        raise RuntimeError(
            f"no valid Garmin token in {TOKEN_DIR} ({type(e).__name__}: {e}). "
            "Run: python garmin_capacity.py --setup"
        ) from e
    return client


def safe_call(fn, *args):
    try:
        return fn(*args)
    except Exception as e:  # one failed endpoint must not kill the pull
        log.warning("Garmin call %s failed: %s", getattr(fn, "__name__", fn), e)
        return None


def pull_garmin(day: date, interactive: bool = False) -> Dict[str, Any]:
    """One Garmin session, every endpoint either consumer needs (this script + garmin_fetch.py)."""
    c = garmin_client(interactive)
    d = day.isoformat()
    return {
        "_meta": {"fetched_at": datetime.now().isoformat(timespec="seconds"), "date": d,
                  "source": "garmin_capacity.py"},
        "stats": safe_call(c.get_stats, d),
        "sleep": safe_call(c.get_sleep_data, d),
        "hrv": safe_call(c.get_hrv_data, d),
        "body_battery": safe_call(c.get_body_battery, d, d),
        "readiness": safe_call(c.get_training_readiness, d),
        "stress": safe_call(c.get_stress_data, d),
        "rhr": safe_call(c.get_rhr_day, d),
        "body_composition": safe_call(c.get_body_composition, d, d),
        "weigh_ins": safe_call(c.get_daily_weigh_ins, d),
        "hydration": safe_call(c.get_hydration_data, d),
    }


def cached_raw(day: date, max_age_h: float = CACHE_MAX_AGE_H) -> Optional[Dict[str, Any]]:
    """Return data\\raw\\<day>.json if it was fetched from Garmin less than max_age_h ago."""
    f = RAW_DIR / f"{day}.json"
    if not f.exists():
        return None
    try:
        raw = json.loads(f.read_text(encoding="utf-8"))
        fetched = datetime.fromisoformat(dig(raw, "_meta", "fetched_at"))
    except Exception:
        return None  # legacy file without _meta, or unreadable: fetch fresh
    age_h = (datetime.now() - fetched).total_seconds() / 3600
    if age_h < max_age_h and dig(raw, "stats") is not None:
        log.info("using cached raw %s (fetched %.1f h ago) — no Garmin call", f.name, age_h)
        return raw
    return None


def mock_raw(day: date) -> Dict[str, Any]:
    return {
        "stats": {"bodyBatteryAtWakeTime": 68, "bodyBatteryHighestValue": 70, "bodyBatteryMostRecentValue": 64,
                  "averageStressLevel": 31, "restingHeartRate": 58},
        "sleep": {"dailySleepDTO": {"sleepTimeSeconds": 6.6 * 3600, "sleepScores": {"overall": {"value": 71}},
                                    "sleepStartTimestampLocal": None, "sleepEndTimestampLocal": None},
                  "avgOvernightHrv": 41, "hrvStatus": "BALANCED"},
        "hrv": {"hrvSummary": {"lastNightAvg": 41, "weeklyAvg": 44, "status": "BALANCED"}},
        "body_battery": [{"date": day.isoformat(), "charged": 52, "drained": 60}],
        "readiness": [{"score": 63, "level": "MODERATE"}],
    }


# ───────────────────────────── metrics ─────────────────────────────
def extract(raw: Dict[str, Any]) -> Dict[str, Any]:
    stats, sleep, hrv, bb, rd = (raw.get(k) for k in ("stats", "sleep", "hrv", "body_battery", "readiness"))

    bb_wake = dig(stats, "bodyBatteryAtWakeTime")
    if bb_wake is None:
        # fall back: highest reading in the overnight values array, then daily high
        vals = dig(bb, 0, "bodyBatteryValuesArray", default=[]) or []
        levels = [v[-1] if len(v) == 2 else v[2] for v in vals if isinstance(v, list) and len(v) >= 2 and v[-1] is not None]
        levels = [x for x in levels if isinstance(x, (int, float))]
        bb_wake = max(levels) if levels else dig(stats, "bodyBatteryHighestValue")

    sleep_s = dig(sleep, "dailySleepDTO", "sleepTimeSeconds") or dig(stats, "sleepingSeconds")
    hrv_status = (dig(hrv, "hrvSummary", "status") or dig(sleep, "hrvStatus") or "")
    rd0 = rd[0] if isinstance(rd, list) and rd else (rd if isinstance(rd, dict) else {})

    return {
        "bb_wake": int(bb_wake) if bb_wake is not None else None,
        "bb_now": dig(stats, "bodyBatteryMostRecentValue"),
        "bb_charged_overnight": dig(bb, 0, "charged"),
        "sleep_min": round(sleep_s / 60) if sleep_s else None,
        "sleep_score": dig(sleep, "dailySleepDTO", "sleepScores", "overall", "value"),
        "hrv_last_night": dig(hrv, "hrvSummary", "lastNightAvg") or dig(sleep, "avgOvernightHrv"),
        "hrv_weekly": dig(hrv, "hrvSummary", "weeklyAvg"),
        "hrv_status": str(hrv_status).upper() or None,
        "stress_avg_yesterday": dig(stats, "averageStressLevel"),
        "resting_hr": dig(stats, "restingHeartRate") or dig(sleep, "restingHeartRate"),
        "readiness_score": dig(rd0, "score"),
        "readiness_level": dig(rd0, "level"),
    }


def classify(m: Dict[str, Any]) -> Dict[str, Any]:
    bb = m.get("bb_wake")
    reasons: List[str] = []
    if bb is None:
        return {"tier": "UNKNOWN", "reasons": ["No Body Battery reading — watch not synced?"]}
    tier = "GREEN" if bb >= GREEN_MIN else "YELLOW" if bb >= YELLOW_MIN else "RED"
    reasons.append(f"Body Battery at wake {bb}")

    downgrade = []
    if m.get("sleep_score") is not None and m["sleep_score"] < 60:
        downgrade.append(f"sleep score {m['sleep_score']}")
    if m.get("hrv_status") in BAD_HRV:
        downgrade.append(f"HRV {m['hrv_status'].lower()}")
    if downgrade and tier != "RED":
        tier = TIER_ORDER[TIER_ORDER.index(tier) - 1]
        reasons.append("downgraded one tier: " + ", ".join(downgrade))
    return {"tier": tier, "reasons": reasons}


def recent_sleep(day: date, nights: int = 3) -> List[int]:
    if not HISTORY_CSV.exists():
        return []
    rows = list(csv.DictReader(HISTORY_CSV.open(encoding="utf-8")))
    cutoff = (day - timedelta(days=nights)).isoformat()
    vals = [int(r["sleep_min"]) for r in rows
            if r.get("sleep_min") and cutoff <= r["date"] < day.isoformat()]
    return vals[-(nights - 1):]


def circadian(day: date, tier: str, m: Dict[str, Any]) -> Dict[str, Any]:
    """Tonight's bedtime + tomorrow's wake. Wake is fixed; only bedtime flexes earlier."""
    tomorrow = day + timedelta(days=1)
    wake = hm_to_dt(tomorrow, WAKE_ANCHOR)
    base_bed = wake - timedelta(minutes=SLEEP_NEED_MIN + SLEEP_LATENCY_MIN)

    shift = TIER_PLAN.get(tier, {"bedtime_shift": 0})["bedtime_shift"]
    notes = []
    if shift:
        notes.append(f"{tier} day: bedtime {abs(shift)} min earlier")

    last = recent_sleep(day) + ([m["sleep_min"]] if m.get("sleep_min") else [])
    if len(last) >= 2:
        avg = sum(last) / len(last)
        if avg < SLEEP_NEED_MIN - 45:
            shift -= 15
            notes.append(f"sleep debt (avg {avg/60:.1f} h over {len(last)} nights): extra 15 min")
    shift = max(shift, -MAX_PULL_EARLIER_MIN)

    bed = base_bed + timedelta(minutes=shift)
    return {
        "wake_target": fmt(wake),
        "bedtime_target": fmt(bed),
        "wind_down_start": fmt(bed - timedelta(minutes=WIND_DOWN_MIN)),
        "caffeine_cutoff": fmt(bed - timedelta(hours=CAFFEINE_CUTOFF_H)),
        "bedtime_shift_min": shift,
        "notes": notes or ["Standard targets"],
    }


# ───────────────────────────── outputs ─────────────────────────────
def save(day: date, payload: Dict[str, Any], raw: Dict[str, Any]):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    (RAW_DIR / f"{day}.json").write_text(json.dumps(raw, indent=2, default=str), encoding="utf-8")
    (DATA_DIR / f"{day}.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (DATA_DIR / "latest.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

    m = payload["metrics"]
    fields = ["date", "tier", "bb_wake", "sleep_min", "sleep_score", "hrv_last_night", "hrv_status",
              "stress_avg_yesterday", "resting_hr", "readiness_score", "bedtime_target"]
    row = {"date": str(day), "tier": payload["tier"], "bedtime_target": payload["circadian"]["bedtime_target"],
           **{k: m.get(k) for k in fields if k in m}}
    rows = []
    if HISTORY_CSV.exists():
        rows = [r for r in csv.DictReader(HISTORY_CSV.open(encoding="utf-8")) if r["date"] != str(day)]
    rows.append(row)
    rows.sort(key=lambda r: r["date"])
    with HISTORY_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})


def card(payload: Dict[str, Any]) -> str:
    m, c = payload["metrics"], payload["circadian"]
    icon = {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴"}.get(payload["tier"], "⚪")
    t = lambda s: datetime.fromisoformat(s).strftime("%-I:%M %p") if os.name != "nt" else datetime.fromisoformat(s).strftime("%#I:%M %p")
    sleep = f"{m['sleep_min']//60}h{m['sleep_min']%60:02d}" if m.get("sleep_min") else "n/a"
    return (
        f"{icon} CAPACITY {payload['tier']} — {payload['date']}\n"
        f"Body Battery at wake: {m.get('bb_wake')}  |  Sleep {sleep} (score {m.get('sleep_score')})\n"
        f"HRV {m.get('hrv_last_night')} ({m.get('hrv_status')})  |  RHR {m.get('resting_hr')}  |  Readiness {m.get('readiness_score')}\n"
        f"Deep-work blocks: {payload['plan'].get('deep_blocks', '?')}\n"
        f"{payload['plan'].get('guidance', '')}\n"
        f"Caffeine cutoff {t(c['caffeine_cutoff'])} · Wind-down {t(c['wind_down_start'])} · "
        f"Bed {t(c['bedtime_target'])} · Wake {t(c['wake_target'])}\n"
        f"({'; '.join(c['notes'])})"
    )


def send_telegram(text: str) -> bool:
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        log.warning("Telegram not configured")
        return False
    try:
        import requests
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": text}, timeout=15)
        return r.ok
    except Exception as e:
        log.warning("Telegram failed: %s", e)
        return False


def send_mail(payload: Dict[str, Any], text: str) -> bool:
    import smtplib
    from email.mime.text import MIMEText
    user = (os.getenv("GMAIL_USER") or os.getenv("GMAIL_USERNAME") or os.getenv("GMAIL_ADDRESS")
            or os.getenv("EMAIL_USER") or "").strip()
    pw = (os.getenv("GMAIL_APP_PASSWORD") or os.getenv("SMTP_PASSWORD") or "").strip()
    to = (os.getenv("CAPACITY_MAIL_TO") or os.getenv("GMAIL_LOG_TO") or user).strip()
    if not user or not pw:
        log.warning("Gmail credentials not set — capacity mail not sent")
        return False
    body = text + "\n\n<<<CAPACITY_JSON\n" + json.dumps(payload, indent=2) + "\nCAPACITY_JSON>>>\n"
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = f"[CAPACITY] {payload['date']} {payload['tier']} BB{payload['metrics'].get('bb_wake')}"
    msg["From"], msg["To"] = user, to
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(user, pw)
            s.send_message(msg)
        return True
    except Exception as e:
        log.warning("Capacity mail failed: %s", e)
        return False


# ───────────────────────────── main ─────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--setup", action="store_true", help="interactive first login (stores tokens)")
    ap.add_argument("--date", help="YYYY-MM-DD (default today)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("--refresh", action="store_true", help=f"ignore the <{CACHE_MAX_AGE_H} h raw cache and hit Garmin")
    ap.add_argument("--if-missing", action="store_true", help="retry run: skip if today already published with data")
    a = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_env()
    day = date.fromisoformat(a.date) if a.date else date.today()

    if a.if_missing:
        f = DATA_DIR / f"{day}.json"
        if f.exists() and json.loads(f.read_text(encoding="utf-8")).get("tier") != "UNKNOWN":
            log.info("already published for %s — retry not needed", day)
            return 0

    if a.setup:
        garmin_client(interactive=True)
        print(f"Garmin login OK — tokens stored in {TOKEN_DIR}. Daily runs are now non-interactive.")
        a.dry_run = True
        a.refresh = True  # the setup run is the day's one real fetch

    if a.mock:
        raw = mock_raw(day)
    else:
        raw = None if a.refresh else cached_raw(day)
        if raw is None:
            try:
                raw = pull_garmin(day)
            except Exception as e:
                log.error("Garmin fetch failed, nothing published: %s", e)
                return 2
    metrics = extract(raw)
    cls = classify(metrics)
    tier = cls["tier"]
    payload = {
        "date": str(day),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "tier": tier,
        "reasons": cls["reasons"],
        "metrics": metrics,
        "plan": TIER_PLAN.get(tier, {"deep_blocks": 2, "guidance": "No data — plan as YELLOW until the watch syncs."}),
        "circadian": circadian(day, tier, metrics),
        "source": "mock" if a.mock else "garmin",
        "fetched_at": dig(raw, "_meta", "fetched_at"),
    }
    text = card(payload)
    print(text)

    if not a.mock:
        save(day, payload, raw)
    if a.dry_run or a.mock:
        return 0

    tg = send_telegram(text)
    ml = send_mail(payload, text)
    log.info("published: telegram=%s mail=%s", tg, ml)
    return 0 if (tg or ml) else 2


if __name__ == "__main__":
    sys.exit(main())
