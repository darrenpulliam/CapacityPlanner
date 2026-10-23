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

On-demand mode (--now, 2026-10-17): current readings through the day (latest Body Battery
with its time, today's high/low, latest stress, latest HR, today's RHR, last watch sync,
this morning's tier as reference). Always hits Garmin (no raw cache); never touches the
morning files. Publishes a one-line Telegram card and a [CAPACITY-NOW] mail with a
<<<CAPACITY_NOW_JSON block. Triggers: Start-ScheduledTask \\Capacity\\Capacity_Now (no
schedule) or /capacity on the planner's own bot, @dp_health_consultant_bot ("Health
Consultant", capacity_bot.py, task \\Capacity\\Capacity_Bot). That bot is the ONE getUpdates
poller on its token; this script only sends.

Secrets: Windows environment variables win, then .env in this folder, then (only
with --setup) an interactive prompt. Since 2026-10-18 there is NO CrystalBull fallback:
the planner uses only its own .env (its own Telegram bot token since the move off
@crystalbull_ai_bot). GARMIN_PASSWORD should live only in the Windows user environment;
leave it blank in .env.

Garmin auth policy (shared with HealthAI's garmin_fetch.py):
  * ONE token store: %USERPROFILE%\\.garminconnect (override: GARMINTOKENS).
  * Scheduled runs try the token first. If it is missing/rejected and
    GARMIN_EMAIL/GARMIN_PASSWORD are set, they make AT MOST ONE credential
    login per calendar day (marker: data\\.garmin_relogin_state.json), so a bad
    password can't hammer Garmin into a 429. The retry task never makes a second
    attempt the same day.
  * MFA can't be answered unattended: if Garmin asks for a code the run stops
    and Telegram says to run --setup.
  * Every fallback outcome (re-login OK / MFA needed / login failed / already
    tried today) is sent to Telegram.
  * --setup is the interactive path (email/password/MFA prompts).
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
  python garmin_capacity.py --now          # current readings -> Telegram + [CAPACITY-NOW] mail
  python garmin_capacity.py --now --dry-run
  python garmin_capacity.py --setup-myair    # one-time ResMed myAir login (e-mail/password, e-mailed MFA code)
  python garmin_capacity.py --setup-sleepiq  # one-time Sleep Number login; picks Darren's side if 2 sleepers

Extra sources (2026-10-23, sources_extra.py): ResMed myAir (CPAP usage, AHI, 95th-pct leak, mask
score, myAir score) and Sleep Number SleepIQ (score, in-bed / restful / restless, HR, HRV, RR, bed
entry). Both are optional: a failure logs a WARNING and the run continues with Garmin only.
Rules: sleep_min falls back Garmin -> SleepIQ (in_bed - restless - out_of_bed) -> CPAP usage
(metrics.sleep_source says which); SleepIQ score < SLEEP_SCORE_LOW only counts when Garmin has no
sleep score; AHI >= CPAP_AHI_DOWNGRADE joins the one-tier downgrade (still max one tier total);
AHI >= CPAP_AHI_ALERT adds "check mask/pressure"; CPAP usage < CPAP_MIN_USAGE_MIN sets the sleep-debt
flag (-15 min bedtime, -45 cap unchanged); high leak / low mask score adds a "mask seal" reason only.
Payload gains "cpap" and "sleepnumber" objects (null when missing); subject and JSON markers unchanged.

Requires: pip install --upgrade garminconnect curl_cffi python-dotenv requests asyncsleepiq
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
SLEEP_SCORE_LOW = 60           # sleep score below this joins the one-tier downgrade (Garmin, else SleepIQ)
TIER_ORDER = ["RED", "YELLOW", "GREEN"]

# Extra sources (2026-10-23): ResMed myAir CPAP + Sleep Number SleepIQ thresholds
CPAP_AHI_DOWNGRADE = 5.0       # AHI (events/h) at or above this joins the one-tier downgrade
CPAP_AHI_ALERT = 15.0          # AHI at or above this adds the "check mask/pressure" reason
CPAP_MIN_USAGE_MIN = 240       # CPAP worn less than this (4 h) sets the sleep-debt flag (-15 min bedtime)
CPAP_LEAK_HIGH_LPM = 24.0      # 95th-percentile leak (L/min) above this = "mask seal" reason (ResMed's high-leak line)
CPAP_MASK_SCORE_LOW = 15       # myAir mask-seal score (0-20) below this = "mask seal" reason
SLEEP_DEBT_SHIFT_MIN = 15      # extra bedtime pull when the sleep-debt flag is set (shared with the 3-night average)
TIER_PLAN = {
    "GREEN":  {"deep_blocks": 3, "bedtime_shift": 0,   "guidance": "Full capacity. Front-load the hardest work; outings/events OK."},
    "YELLOW": {"deep_blocks": 2, "bedtime_shift": -15, "guidance": "Moderate. Two deep blocks max, protect the afternoon dip, no new commitments after 6 PM."},
    "RED":    {"deep_blocks": 1, "bedtime_shift": -30, "guidance": "Recovery day. One must-do block, admin only otherwise, walk + daylight, decline optional events."},
}

log = logging.getLogger("capacity")


# ───────────────────────────── HTTP hardening ─────────────────────────────
# 2026-10-17 forensic: on this PC any outbound TCP flow whose first client bytes arrive more
# than ~10 ms after the TCP handshake is black-holed (TLS and plain HTTP alike; 0 ms gap
# passes 100 %, >=10 ms fails 100 %). That is a host-level flow filter (McAfee and Pango
# WFP connect-redirect/stream callouts are registered), not Garmin or Telegram. requests/
# urllib3 opens the socket FIRST and only then loads the CA bundle (40-550 ms) before the
# ClientHello, so every requests call timed out since 10/12 (Telegram) and 10/16 (Garmin
# API tier -> "Failed to retrieve social profile" -> needless re-login attempts), while
# http.client, urllib, curl_cffi and smtplib, which all send immediately, kept working.
# Fix: hand urllib3 a finished TLS context and suppress per-connection CA loading so
# nothing runs between connect() and the ClientHello; Telegram goes through stdlib urllib
# (the same fix CrystalBull made in engines/telegram_transport.py).
import ssl

import certifi
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

_TLS_CTX = ssl.create_default_context(cafile=certifi.where())   # built once, before any socket exists


class ImmediateHelloAdapter(HTTPAdapter):
    """requests adapter whose TLS context is finished before the TCP connect (see above)."""

    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = _TLS_CTX
        return super().init_poolmanager(*args, **kwargs)

    def cert_verify(self, conn, url, verify, cert):
        super().cert_verify(conn, url, verify, cert)
        if verify is True:                 # CA bundle already lives in _TLS_CTX
            conn.ca_certs = None
            conn.ca_cert_dir = None


def harden_requests_session(s) -> None:
    """Mount ImmediateHelloAdapter on a requests.Session (keeps garminconnect's 20/20 pool).
    Retry covers connect errors and read timeouts on idempotent calls only (urllib3 default)."""
    s.mount("https://", ImmediateHelloAdapter(
        pool_connections=20, pool_maxsize=20,
        max_retries=Retry(total=2, connect=2, read=2, backoff_factor=0.3)))


def harden_garmin(g):
    """Harden both requests sessions garminconnect 0.3.x keeps on its Client:
    `cs` (auth / JWT refresh) and `_api_session` (every connectapi call incl. the
    socialProfile token check). The DI-token refresh already goes through curl_cffi."""
    c = getattr(g, "client", None)
    for name in ("cs", "_api_session"):
        s = getattr(c, name, None)
        if s is not None and hasattr(s, "mount"):
            harden_requests_session(s)
    return g


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
    """This folder's .env only (process environment still wins). The CrystalBull .env / .env.prod
    fallback was removed 2026-10-18: the planner has its own bot (@dp_health_consultant_bot) and
    must never borrow @crystalbull_ai_bot's token again."""
    try:
        from dotenv import load_dotenv
    except Exception:
        log.warning("python-dotenv missing — using process environment only")
        return
    load_dotenv(BASE_DIR / ".env", override=False)


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
        client = harden_garmin(Garmin(email, password, prompt_mfa=lambda: input("Garmin MFA code: ").strip()))
        client.login(str(TOKEN_DIR))
        return client

    # Scheduled / non-interactive: token first.
    client = harden_garmin(Garmin())
    try:
        client.login(str(TOKEN_DIR))
        return client
    except Exception as e:
        token_err = f"{type(e).__name__}: {e}"
        log.warning("Garmin token rejected/missing in %s (%s)", TOKEN_DIR, token_err)

    return credential_fallback(Garmin, token_err)


RELOGIN_STATE = DATA_DIR / ".garmin_relogin_state.json"


class MFARequired(Exception):
    pass


def _raise_mfa():
    raise MFARequired("Garmin asked for an MFA code (can't answer unattended)")


def credential_fallback(Garmin, token_err: str):
    """One unattended credential login per day; Telegram alert on every outcome."""
    email, password = os.getenv("GARMIN_EMAIL"), os.getenv("GARMIN_PASSWORD")
    today = str(date.today())
    if not email or not password:
        send_telegram("⚠️ Capacity: Garmin token expired and no GARMIN_EMAIL/PASSWORD set.\n"
                      "Run: python garmin_capacity.py --setup")
        raise RuntimeError(f"no valid Garmin token ({token_err}); no credentials for fallback. Run --setup")

    try:
        state = json.loads(RELOGIN_STATE.read_text(encoding="utf-8"))
    except Exception:
        state = {}
    if state.get("date") == today:
        # Already tried today — don't risk Garmin's 429 throttle with a second attempt.
        if state.get("result") != "ok":
            send_telegram(f"⚠️ Capacity: Garmin still not signed in (today's re-login: {state.get('result')}). "
                          "Not retrying until tomorrow. Run: python garmin_capacity.py --setup")
        raise RuntimeError(f"credential re-login already attempted today ({state.get('result')}); not retrying")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RELOGIN_STATE.write_text(json.dumps({"date": today, "result": "attempting"}), encoding="utf-8")
    log.info("attempting one unattended Garmin credential login")
    try:
        client = harden_garmin(Garmin(email, password, prompt_mfa=_raise_mfa))
        client.login(str(TOKEN_DIR))
        garth = getattr(client, "garth", None)          # older garminconnect: persist tokens ourselves
        if garth is not None and hasattr(garth, "dump"):
            try:
                garth.dump(str(TOKEN_DIR))
            except Exception as de:
                log.warning("token dump failed: %s", de)
    except MFARequired as e:
        result = "mfa_required"
        RELOGIN_STATE.write_text(json.dumps({"date": today, "result": result}), encoding="utf-8")
        send_telegram("🔐 Capacity: Garmin token expired and Garmin wants an MFA code.\n"
                      "Run on the PC: python garmin_capacity.py --setup")
        raise RuntimeError(str(e)) from e
    except Exception as e:
        result = f"failed ({type(e).__name__})"
        RELOGIN_STATE.write_text(json.dumps({"date": today, "result": result}), encoding="utf-8")
        hint = " — likely throttled (429); wait a day" if "429" in str(e) else " — check GARMIN_PASSWORD"
        send_telegram(f"❌ Capacity: Garmin auto re-login failed: {type(e).__name__}: {e}{hint}\n"
                      "Run: python garmin_capacity.py --setup")
        raise RuntimeError(f"Garmin credential re-login failed: {e}") from e

    RELOGIN_STATE.write_text(json.dumps({"date": today, "result": "ok"}), encoding="utf-8")
    send_telegram(f"🔄 Capacity: Garmin token had expired; re-signed in automatically and saved new tokens to {TOKEN_DIR}.")
    log.info("Garmin credential re-login OK; tokens saved to %s", TOKEN_DIR)
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


def merge_extra(m: Dict[str, Any], cpap: Optional[Dict[str, Any]], sn: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """sleep_min fallback chain Garmin -> SleepIQ (in_bed - restless - out_of_bed) -> CPAP usage,
    recorded in metrics.sleep_source. Garmin fields are never overwritten."""
    from sources_extra import sleep_min_from_sleepnumber
    if m.get("sleep_min"):
        m["sleep_source"] = "garmin"
    elif sleep_min_from_sleepnumber(sn):
        m["sleep_min"], m["sleep_source"] = sleep_min_from_sleepnumber(sn), "sleepiq"
    elif cpap and cpap.get("usage_min"):
        m["sleep_min"], m["sleep_source"] = int(cpap["usage_min"]), "cpap"
    else:
        m["sleep_source"] = None
    return m


def previous_bedtime_target(day: date) -> Optional[str]:
    """Last night's bedtime target = yesterday's payload circadian.bedtime_target (data\\<day-1>.json)."""
    f = DATA_DIR / f"{day - timedelta(days=1)}.json"
    if not f.exists():
        return None
    try:
        return dig(json.loads(f.read_text(encoding="utf-8")), "circadian", "bedtime_target")
    except Exception:
        return None


def _hm(iso: Optional[str]) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%H:%M")
    except Exception:
        return str(iso)


def classify(m: Dict[str, Any], cpap: Optional[Dict[str, Any]] = None, sn: Optional[Dict[str, Any]] = None,
             prev_target: Optional[str] = None) -> Dict[str, Any]:
    bb = m.get("bb_wake")
    reasons: List[str] = []
    if bb is None:
        return {"tier": "UNKNOWN", "reasons": ["No Body Battery reading — watch not synced?"], "sleep_debt_flag": False}
    tier = "GREEN" if bb >= GREEN_MIN else "YELLOW" if bb >= YELLOW_MIN else "RED"
    reasons.append(f"Body Battery at wake {bb}")

    downgrade = []
    if m.get("sleep_score") is not None:
        if m["sleep_score"] < SLEEP_SCORE_LOW:
            downgrade.append(f"sleep score {m['sleep_score']}")
    elif sn and sn.get("sleepiq_score") is not None and sn["sleepiq_score"] < SLEEP_SCORE_LOW:
        downgrade.append(f"SleepIQ score {sn['sleepiq_score']} (no Garmin sleep score)")   # SleepIQ stands in only when Garmin is silent
    if m.get("hrv_status") in BAD_HRV:
        downgrade.append(f"HRV {m['hrv_status'].lower()}")
    ahi = cpap.get("ahi") if cpap else None
    if ahi is not None and ahi >= CPAP_AHI_DOWNGRADE:
        downgrade.append(f"AHI {ahi}")
    if downgrade and tier != "RED":
        tier = TIER_ORDER[TIER_ORDER.index(tier) - 1]      # OR of all flags: still exactly one tier
        reasons.append("downgraded one tier: " + ", ".join(downgrade))

    sleep_debt_flag = False
    if cpap:
        if ahi is not None and ahi >= CPAP_AHI_ALERT:
            reasons.append(f"AHI {ahi} — check mask/pressure")
        usage = cpap.get("usage_min")
        if usage is not None and usage < CPAP_MIN_USAGE_MIN:
            sleep_debt_flag = True
            reasons.append(f"CPAP worn {usage} min (< {CPAP_MIN_USAGE_MIN}): sleep-debt flag")
        leak, mask = cpap.get("leak_95"), cpap.get("mask_score")
        if (leak is not None and leak > CPAP_LEAK_HIGH_LPM) or (mask is not None and mask < CPAP_MASK_SCORE_LOW):
            reasons.append(f"mask seal (leak {leak} L/min, mask score {mask})")
    if sn and sn.get("bed_entry"):
        reasons.append(f"Actual bed {_hm(sn['bed_entry'])} vs target {_hm(prev_target) if prev_target else 'n/a'}")
    return {"tier": tier, "reasons": reasons, "sleep_debt_flag": sleep_debt_flag}


def recent_sleep(day: date, nights: int = 3) -> List[int]:
    if not HISTORY_CSV.exists():
        return []
    rows = list(csv.DictReader(HISTORY_CSV.open(encoding="utf-8")))
    cutoff = (day - timedelta(days=nights)).isoformat()
    vals = [int(r["sleep_min"]) for r in rows
            if r.get("sleep_min") and cutoff <= r["date"] < day.isoformat()]
    return vals[-(nights - 1):]


def circadian(day: date, tier: str, m: Dict[str, Any], sleep_debt_flag: bool = False) -> Dict[str, Any]:
    """Tonight's bedtime + tomorrow's wake. Wake is fixed; only bedtime flexes earlier.
    sleep_debt_flag (CPAP usage < CPAP_MIN_USAGE_MIN) adds the same -15 min the 3-night average does."""
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
            shift -= SLEEP_DEBT_SHIFT_MIN
            notes.append(f"sleep debt (avg {avg/60:.1f} h over {len(last)} nights): extra {SLEEP_DEBT_SHIFT_MIN} min")
    if sleep_debt_flag:
        shift -= SLEEP_DEBT_SHIFT_MIN
        notes.append(f"CPAP under {CPAP_MIN_USAGE_MIN} min: extra {SLEEP_DEBT_SHIFT_MIN} min")
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
    src = f", {m['sleep_source']}" if m.get("sleep_source") and m.get("sleep_source") != "garmin" else ""
    return (
        f"{icon} CAPACITY {payload['tier']} — {payload['date']}\n"
        f"Body Battery at wake: {m.get('bb_wake')}  |  Sleep {sleep} (score {m.get('sleep_score')}{src})\n"
        f"HRV {m.get('hrv_last_night')} ({m.get('hrv_status')})  |  RHR {m.get('resting_hr')}  |  Readiness {m.get('readiness_score')}\n"
        f"{cpap_line(payload.get('cpap'))}\n"
        f"{sleepnumber_line(payload.get('sleepnumber'))}\n"
        f"Deep-work blocks: {payload['plan'].get('deep_blocks', '?')}\n"
        f"{payload['plan'].get('guidance', '')}\n"
        f"Caffeine cutoff {t(c['caffeine_cutoff'])} · Wind-down {t(c['wind_down_start'])} · "
        f"Bed {t(c['bedtime_target'])} · Wake {t(c['wake_target'])}\n"
        f"({'; '.join(c['notes'])})"
    )


def cpap_line(c: Optional[Dict[str, Any]]) -> str:
    if not c:
        return "CPAP: no data"
    u = c.get("usage_min")
    usage = f"{u//60}h{u%60:02d}" if isinstance(u, int) else "n/a"
    return (f"CPAP {usage}  |  AHI {c.get('ahi')}  |  leak95 {c.get('leak_95')} L/min  |  "
            f"mask {c.get('mask_score')}/20  |  myAir {c.get('myair_score')}")


def sleepnumber_line(s: Optional[Dict[str, Any]]) -> str:
    if not s:
        return "SleepIQ: no data"
    ib = s.get("in_bed_min")
    in_bed = f"{ib//60}h{ib%60:02d}" if isinstance(ib, int) else "n/a"
    return (f"SleepIQ {s.get('sleepiq_score')}  |  in bed {in_bed} (restful {s.get('restful_min')} / "
            f"restless {s.get('restless_min')} min)  |  HR {s.get('avg_hr')}  |  HRV {s.get('avg_hrv')}  |  "
            f"RR {s.get('avg_rr')}  |  bed {_hm(s.get('bed_entry'))}")


def send_telegram(text: str) -> bool:
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        log.warning("Telegram not configured")
        return False
    # stdlib urllib, not requests: see "HTTP hardening" above (ClientHello must follow connect immediately)
    import urllib.error
    import urllib.request
    body = json.dumps({"chat_id": chat, "text": text}).encode("utf-8")
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15, context=_TLS_CTX) as r:
            return 200 <= r.status < 300
    except urllib.error.HTTPError as e:
        log.warning("Telegram failed: HTTP %s %s", e.code, e.read(200))
        return False
    except Exception as e:
        log.warning("Telegram failed: %s", e)
        return False


def send_mail(payload: Dict[str, Any], text: str, subject: Optional[str] = None,
              marker: str = "CAPACITY_JSON") -> bool:
    """Gmail SMTP (smtplib sends immediately after connect, so it is not hit by the host flow
    filter). Default subject/marker = the morning [CAPACITY] mail; --now passes its own."""
    import smtplib
    from email.mime.text import MIMEText
    user = (os.getenv("GMAIL_USER") or os.getenv("GMAIL_USERNAME") or os.getenv("GMAIL_ADDRESS")
            or os.getenv("EMAIL_USER") or "").strip()
    pw = (os.getenv("GMAIL_APP_PASSWORD") or os.getenv("SMTP_PASSWORD") or "").strip()
    to = (os.getenv("CAPACITY_MAIL_TO") or os.getenv("GMAIL_LOG_TO") or user).strip()
    if not user or not pw:
        log.warning("Gmail credentials not set — capacity mail not sent")
        return False
    body = text + f"\n\n<<<{marker}\n" + json.dumps(payload, indent=2) + f"\n{marker}>>>\n"
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject or f"[CAPACITY] {payload['date']} {payload['tier']} BB{payload['metrics'].get('bb_wake')}"
    msg["From"], msg["To"] = user, to
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(user, pw)
            s.send_message(msg)
        return True
    except Exception as e:
        log.warning("Capacity mail failed: %s", e)
        return False


# ───────────────────────────── on-demand NOW mode ─────────────────────────────
# --now (2026-10-17): "what is my capacity right now", not the wake snapshot. Three Garmin
# calls on the hardened client: get_stats (today's BB high/low/most-recent, RHR, avg stress,
# lastSyncTimestampGMT), get_stress_data (3-min stress AND Body Battery series with
# timestamps) and get_heart_rates (2-min HR series + today's RHR). Never reads the raw
# cache and never writes data\<date>.json / latest.json / history.csv (those are the morning
# snapshot the calendar writer consumes); it writes data\now\<date>_<HHMM>.json and
# data\now_latest.json instead. Garmin series timestamps are epoch ms (GMT); strings such as
# lastSyncTimestampGMT are GMT without a zone marker.
NOW_DIR = DATA_DIR / "now"
NOW_STALE_MIN = 60             # flag the card when the watch last synced more than this many minutes ago
STRESS_NO_READING = (-1, -2)   # Garmin stress codes: -1 = not measurable, -2 = during activity


def _ms_local(ms) -> Optional[datetime]:
    try:
        return datetime.fromtimestamp(int(ms) / 1000)
    except Exception:
        return None


def _gmt_local(s) -> Optional[datetime]:
    """'2026-10-18T03:11:31.317' (GMT, no zone marker) -> naive local datetime."""
    try:
        from datetime import timezone
        return datetime.fromisoformat(str(s)).replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
    except Exception:
        return None


def _descr_index(descriptors, key: str, default: int) -> int:
    """Column index of `key` in a Garmin descriptor list (both naming styles)."""
    for d in descriptors or []:
        if isinstance(d, dict) and key in (d.get("key"), d.get("bodyBatteryValueDescriptorKey")):
            idx = d.get("index", d.get("bodyBatteryValueDescriptorIndex"))
            return int(idx) if isinstance(idx, int) else default
    return default


def _last_point(arr, value_idx: int, skip=()):
    """Latest [timestamp_ms, ...] row whose value column is a real number -> (local dt, value)."""
    for row in reversed(arr or []):
        if isinstance(row, list) and len(row) > value_idx:
            v = row[value_idx]
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v not in skip:
                return _ms_local(row[0]), v
    return None, None


def pull_now(day: date) -> Dict[str, Any]:
    c = garmin_client()
    d = day.isoformat()
    raw = {
        "_meta": {"fetched_at": datetime.now().isoformat(timespec="seconds"), "date": d,
                  "source": "garmin_capacity.py --now"},
        "stats": safe_call(c.get_stats, d),
        "stress": safe_call(c.get_stress_data, d),
        "heart_rates": safe_call(c.get_heart_rates, d),
    }
    if dig(raw, "stats", "lastSyncTimestampGMT") is None:        # fallback for the sync time only
        raw["device"] = safe_call(c.get_device_last_used)
    return raw


def morning_reference(day: date) -> Dict[str, Any]:
    """This morning's tier from the wake pull (data\\<date>.json), if it exists."""
    f = DATA_DIR / f"{day}.json"
    if not f.exists():
        return {}
    try:
        p = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {"tier": p.get("tier"), "bb_wake": dig(p, "metrics", "bb_wake"), "generated_at": p.get("generated_at")}


def extract_now(raw: Dict[str, Any], day: date, at: datetime) -> Dict[str, Any]:
    stats = raw.get("stats") or {}
    stress = raw.get("stress") or {}
    hr = raw.get("heart_rates") or {}

    bb_idx = _descr_index(stress.get("bodyBatteryValueDescriptorsDTOList"), "bodyBatteryLevel", 2)
    bb_at, bb_now = _last_point(stress.get("bodyBatteryValuesArray"), bb_idx)
    if bb_now is None:
        bb_now = stats.get("bodyBatteryMostRecentValue")
    st_idx = _descr_index(stress.get("stressValueDescriptorsDTOList"), "stressLevel", 1)
    st_at, st_now = _last_point(stress.get("stressValuesArray"), st_idx, skip=STRESS_NO_READING)
    hr_idx = _descr_index(hr.get("heartRateValueDescriptors"), "heartrate", 1)
    hr_at, hr_now = _last_point(hr.get("heartRateValues"), hr_idx)

    synced = _gmt_local(stats.get("lastSyncTimestampGMT")) or _ms_local(dig(raw, "device", "lastUsedDeviceUploadTime"))
    iso = lambda t: t.isoformat(timespec="minutes") if t else None
    return {
        "bb_now": bb_now, "bb_now_at": iso(bb_at),
        "bb_high": stats.get("bodyBatteryHighestValue"), "bb_low": stats.get("bodyBatteryLowestValue"),
        "bb_wake": stats.get("bodyBatteryAtWakeTime"),
        "stress_now": st_now, "stress_now_at": iso(st_at),
        "stress_avg_today": stats.get("averageStressLevel"), "stress_max_today": stats.get("maxStressLevel"),
        "hr_now": hr_now, "hr_now_at": iso(hr_at),
        "resting_hr": hr.get("restingHeartRate") or stats.get("restingHeartRate"),
        "resting_hr_7d": hr.get("lastSevenDaysAvgRestingHeartRate") or stats.get("lastSevenDaysAvgRestingHeartRate"),
        "hr_min_today": hr.get("minHeartRate") or stats.get("minHeartRate"),
        "hr_max_today": hr.get("maxHeartRate") or stats.get("maxHeartRate"),
        "synced_at": iso(synced),
        "synced_age_min": int((at - synced).total_seconds() // 60) if synced else None,
        "stale": (int((at - synced).total_seconds() // 60) > NOW_STALE_MIN) if synced else True,
        "morning": morning_reference(day),
    }


def now_card(payload: Dict[str, Any]) -> str:
    """Line 1 is the fixed format "NOW <time>: BB <n> (high/low), stress <n>, HR <n>, synced <time>";
    line 2 carries the reading times, RHR and this morning's tier."""
    m = payload["metrics"]
    hm = lambda s: datetime.fromisoformat(s).strftime("%H:%M") if s else "?"
    age = m.get("synced_age_min")
    stale = ""
    if m.get("stale"):
        stale = f" ⚠️ STALE ({age // 60}h{age % 60:02d} ago)" if isinstance(age, int) else " ⚠️ no sync time"
    line1 = (f"NOW {hm(payload['generated_at'])}: BB {m.get('bb_now')} ({m.get('bb_high')}/{m.get('bb_low')}), "
             f"stress {m.get('stress_now')}, HR {m.get('hr_now')}, synced {hm(m.get('synced_at'))}{stale}")
    mo = m.get("morning") or {}
    ref = f"Morning {mo.get('tier')} (BB wake {mo.get('bb_wake')})" if mo else "Morning: no wake pull yet today"
    line2 = (f"{ref} · RHR {m.get('resting_hr')} (7d {m.get('resting_hr_7d')}) · stress avg {m.get('stress_avg_today')} · "
             f"read BB {hm(m.get('bb_now_at'))} / stress {hm(m.get('stress_now_at'))} / HR {hm(m.get('hr_now_at'))}")
    return line1 + "\n" + line2


def run_now(day: date, dry_run: bool) -> int:
    at = datetime.now()
    try:
        raw = pull_now(day)
    except Exception as e:
        log.error("Garmin NOW fetch failed, nothing published: %s", e)
        return 2
    if raw.get("stats") is None and raw.get("stress") is None:
        log.error("Garmin NOW fetch returned no stats and no stress series; nothing published")
        return 2
    metrics = extract_now(raw, day, at)
    payload = {
        "kind": "capacity_now",
        "date": str(day),
        "generated_at": at.isoformat(timespec="seconds"),
        "metrics": metrics,
        "source": "garmin",
        "fetched_at": dig(raw, "_meta", "fetched_at"),
    }
    text = now_card(payload)
    print(text)

    NOW_DIR.mkdir(parents=True, exist_ok=True)
    (NOW_DIR / f"{day}_{at.strftime('%H%M')}.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (DATA_DIR / "now_latest.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (DATA_DIR / "now_latest.raw.json").write_text(json.dumps(raw, indent=2, default=str), encoding="utf-8")
    if dry_run:
        return 0

    subject = f"[CAPACITY-NOW] {day} {at.strftime('%H:%M')} BB{metrics.get('bb_now')}"
    tg = send_telegram(text)
    ml = send_mail(payload, text, subject=subject, marker="CAPACITY_NOW_JSON")
    log.info("published NOW: telegram=%s mail=%s", tg, ml)
    return 0 if (tg or ml) else 2


# ───────────────────────────── main ─────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--setup", action="store_true", help="interactive first login (stores tokens)")
    ap.add_argument("--date", help="YYYY-MM-DD (default today)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("--refresh", action="store_true", help=f"ignore the <{CACHE_MAX_AGE_H} h raw cache and hit Garmin")
    ap.add_argument("--if-missing", action="store_true", help="retry run: skip if today already published with data")
    ap.add_argument("--now", action="store_true",
                    help="on-demand current readings -> Telegram + [CAPACITY-NOW] mail (ignores the raw cache; "
                         "--dry-run prints only; --mock/--if-missing/--refresh don't apply)")
    ap.add_argument("--setup-myair", action="store_true", help="interactive ResMed myAir login (e-mail MFA), saves tokens")
    ap.add_argument("--setup-sleepiq", action="store_true", help="interactive Sleep Number login, picks the sleeper")
    ap.add_argument("--no-extra", action="store_true", help="skip the myAir / SleepIQ fetch (Garmin only)")
    a = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_env()
    day = date.fromisoformat(a.date) if a.date else date.today()

    if a.setup_myair or a.setup_sleepiq:
        import sources_extra
        rc = 0
        if a.setup_myair:
            rc |= sources_extra.setup_myair()
        if a.setup_sleepiq:
            rc |= sources_extra.setup_sleepiq()
        return rc

    if a.now:
        return run_now(day, a.dry_run)

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
    cpap = sn = None
    if a.mock:
        cpap = {"usage_min": 212, "ahi": 6.3, "leak_95": 9.6, "mask_score": 19, "myair_score": 78,
                "date": str(day - timedelta(days=1))}
        sn = {"sleepiq_score": 64, "in_bed_min": 431, "restful_min": 362, "restless_min": 55, "out_of_bed_min": 14,
              "avg_hr": 61, "avg_hrv": 38, "avg_rr": 15, "bed_entry": f"{day - timedelta(days=1)}T23:58",
              "sleeper": "Darren", "date": str(day)}
    elif not a.no_extra:
        try:   # both fetches are best-effort inside sources_extra; this guard covers an import error too
            import sources_extra
            cpap = sources_extra.fetch_cpap(day)
            sn = sources_extra.fetch_sleepnumber(day)
        except Exception as e:
            log.warning("extra sources skipped: %s: %s", type(e).__name__, e)
    metrics = merge_extra(metrics, cpap, sn)
    cls = classify(metrics, cpap, sn, previous_bedtime_target(day))
    tier = cls["tier"]
    payload = {
        "date": str(day),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "tier": tier,
        "reasons": cls["reasons"],
        "metrics": metrics,
        "plan": TIER_PLAN.get(tier, {"deep_blocks": 2, "guidance": "No data — plan as YELLOW until the watch syncs."}),
        "circadian": circadian(day, tier, metrics, cls.get("sleep_debt_flag", False)),
        "cpap": cpap,
        "sleepnumber": sn,
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
