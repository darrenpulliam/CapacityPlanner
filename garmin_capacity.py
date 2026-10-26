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
  python garmin_capacity.py --weather        # print today's weather / barometric pain object and exit

Extra sources (2026-10-23, sources_extra.py): ResMed myAir (CPAP usage, AHI, 95th-pct leak, mask seal
score 0-20, mask on/off score 0-5 + count, myAir score) and Sleep Number SleepIQ (score, in-bed /
restful / restless, HR, HRV, RR, bed entry). Both are optional: a failure logs a WARNING and the run
continues with Garmin only.
Rules: sleep_min falls back Garmin -> SleepIQ (in_bed - restless - out_of_bed) -> CPAP usage
(metrics.sleep_source says which); SleepIQ score < SLEEP_SCORE_LOW only counts when Garmin has no
sleep score; AHI >= CPAP_AHI_DOWNGRADE joins the one-tier downgrade (still max one tier total);
AHI >= CPAP_AHI_ALERT adds "check mask/pressure"; CPAP usage < CPAP_MIN_USAGE_MIN sets the sleep-debt
flag (-15 min bedtime, -45 cap unchanged); leak_95 > CPAP_LEAK_HIGH_LPM or mask_seal_score <
CPAP_MASK_SEAL_LOW adds a "mask seal" reason only.
Watch not worn (2026-10-23): when Garmin has no sleep record for the night AND bb_charged_overnight
== 0, the Body Battery at wake is stale (it just carries yesterday's drained value), so the base tier
comes from the SleepIQ score instead (SLEEPIQ_GREEN_MIN / SLEEPIQ_YELLOW_MIN) and the normal one-tier
downgrade OR still applies; metrics.tier_source = "garmin" | "sleepiq" says which. No SleepIQ either:
unchanged (Body Battery as before). The "Actual bed vs target" reason was dropped the same day: its
target was not shift-aware; the cloud calendar writer does that comparison from the calendar.
Weather / barometric pain (2026-10-23, sources_extra.fetch_weather, Open-Meteo, no key): the largest
sea-level pressure change over any 12 h (and 24 h) window from 12 h before to 24 h after the wake
anchor gives weather.pain_level NONE / LOW / MODERATE / HIGH (WEATHER_PAIN_* hPa thresholds) and
weather.pain_index 0-10. The downgrade is RECORD-ONLY by default (WEATHER_PAIN_DOWNGRADE_ENABLED =
False, until ~2026-11-13): HIGH adds a "record-only, no tier change" reason and never changes the
tier; MODERATE adds a reason only. With ENABLED = True, pain_level at/above
WEATHER_PAIN_DOWNGRADE_LEVEL joins the one-tier downgrade OR. Location: WEATHER_PLACE (geocoded
once) or WEATHER_LAT/WEATHER_LON in .env; unset = source skipped. --no-weather skips it.
Payload gains "cpap", "sleepnumber" and "weather" objects (null when missing); subject and JSON markers unchanged.
Record-only fields (2026-10-23, for the capacity review; they never change tier / bedtime / blocks):
  metrics.sleep_onset / wake_time / sleep_midpoint (ISO local) + timing_source "garmin" (dailySleepDTO
  sleepStart/EndTimestampLocal) -> "sleepiq" (bed_entry / bed_exit) -> null; metrics.alcohol_drinks =
  last night's /drinks entry from data\\alcohol.csv (capacity_bot.py), null when nothing was logged —
  never assumed 0. history.csv gains sleep_onset, wake_time, sleep_midpoint, timing_source,
  alcohol_drinks (appended; existing columns untouched). bedtime_target in history.csv is this
  script's fixed-wake anchor and is NOT shift-aware; the shift-aware targets are the calendar's
  bedtime / wake events. backfill_sleep_history.py fills data\\history_backfill.csv for the last year.
Overnight fluid loss (2026-10-25, display only): Garmin Index scale weigh-ins via get_weigh_ins(yesterday,
  today) -> raw["weigh_ins_range"] (dailyWeightSummaries[].allWeightMetrics[]; weight in GRAMS,
  timestampGMT = epoch ms UTC -> converted to OVERNIGHT_TZ; the record's "date" field is a wall-clock value
  in another zone and is ignored). pm = LAST weigh-in 19:00 yesterday - 04:00 today (after the last pee),
  am = FIRST weigh-in 04:00 - 12:00 today (after the first pee). payload.overnight = {pm_weight_lb, pm_time,
  am_weight_lb, am_time, delta_lb (pm - am, positive = lost), delta_ml_est (delta_lb * 453.6, nearest 10),
  hours_between, missing "pm" | "am" | null}; null when both are missing or the scale call failed. Card
  line "Overnight: 245.2 → 242.8 lb = −2.4 lb (~1090 mL, 8.6 h)". Garmin's API returns only weigh-ins
  assigned to this profile, so a reading left unassigned on a shared scale is invisible here. Never
  changes tier / bedtime / blocks; a scale error logs a WARNING and sets overnight to null.
  12:15 update pass (2026-10-25): Capacity_Morning has a SECOND daily trigger at 12:15 (same task, same
  command line). A plain run that finds the day already published (data\\<date>.json with a tier) does not
  re-pull: it fetches only get_weigh_ins(yesterday, today) (one request), recomputes payload.overnight and,
  ONLY if it changed, rewrites the data files and re-sends the Telegram card and the [CAPACITY] mail (same
  subject). Unchanged -> nothing written, nothing sent. --refresh still forces the full re-pull + re-publish.
  payload.as_of = the Garmin pull time, shown on card line 1 "(as of 09:00)"; a late weigh-in carries
  overnight.fetched_at and the Overnight line ends with "· as of 12:15". --dry-run on the update pass prints
  only (no save, no send), so it never masks the real 12:15 change detection.
Nightly check-in (2026-10-25, display only; capacity_bot.py asks it with inline buttons at CHECKIN_TIME, default
  23:15, or on /checkin): drinks today, NEW open wounds (count + sites), older wounds, exercise (types + minutes).
  One row per check-in appended to data\\checkin.csv (CHECKIN_FIELDS; the last row for a day wins; status done |
  skipped). The day follows the /drinks rule (before 14:00 = yesterday), and the drinks answer is ALSO written to
  data\\alcohol.csv (source "checkin"), so metrics.alcohol_drinks and the check-in agree. The morning run puts
  the previous evening's row in payload.checkin (lists for sites / types; null when none) and one card line
  "Check-in Sat 10/25: drinks 2 · new wounds 1 (Scalp) · older: healing · exercise Walking outside 30-60 min".
--dry-run is write-free (2026-10-26): DRY_RUN gates every write under data\\ (raw cache, data\\<date>.json,
  latest.json, history.csv, now\\*, now_latest*, the weather raw cache and location cache in sources_extra)
  and prints "DRY RUN: nothing saved" after the payload. Auth / session files are exempt (Garmin tokens
  refreshed by a SUCCESSFUL token login, myAir tokens.json, SleepIQ session.json): skipping them only forces
  extra logins. A dry run never enters credential_fallback: a rejected Garmin token is an error (no relogin
  state, no Telegram, the one-per-day credential attempt is kept for the real run). --setup still saves
  (it is the day's one real fetch) but does not send.

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
ALCOHOL_CSV = DATA_DIR / "alcohol.csv"      # night_date,drinks,logged_at,source — append-only, last row per night wins
ALCOHOL_MAX_DRINKS = 10.0                    # /drinks accepts 0-10 in halves
ALCOHOL_CUTOVER_H = 14                       # /drinks before 14:00 = last night; from 14:00 on = tonight
CHECKIN_CSV = DATA_DIR / "checkin.csv"      # nightly check-in rows (capacity_bot.py) — append-only, last row per day wins
CHECKIN_FIELDS = ["day", "status", "drinks", "new_wounds", "new_wound_count", "new_wound_sites", "older_wounds",
                  "exercise", "exercise_types", "exercise_minutes", "started_at", "finished_at", "trigger", "source"]
CHECKIN_LIST_FIELDS = ("new_wound_sites", "exercise_types")   # stored ";"-joined, read back as lists
TOKEN_DIR = Path(os.getenv("GARMINTOKENS", str(Path.home() / ".garminconnect")))
CACHE_MAX_AGE_H = 20           # reuse data\raw\<date>.json if fetched less than this many hours ago
DRY_RUN = False                # set by main() from --dry-run: no write under data\, no send (auth/session files exempt)

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
CPAP_MASK_SEAL_LOW = 15        # myAir mask SEAL score (GraphQL leakScore, 0-20) below this = "mask seal" reason
                               #   (myAir score = usage 0-70 + mask on/off 0-5 + seal 0-20 + AHI 0-5)
SLEEPIQ_GREEN_MIN = 80         # watch-not-worn fallback: SleepIQ score at/above this = GREEN base tier
SLEEPIQ_YELLOW_MIN = 60        #   ... at/above this = YELLOW, below = RED (only when Garmin BB is untrusted)
SLEEP_DEBT_SHIFT_MIN = 15      # extra bedtime pull when the sleep-debt flag is set (shared with the 3-night average)

# Overnight fluid loss (2026-10-25): Garmin Index scale, bedtime weigh-in vs first morning weigh-in. Display only.
OVERNIGHT_PM_FROM_H = 19       # bedtime window: 19:00 yesterday ...
OVERNIGHT_PM_TO_H = 4          #   ... to 04:00 today; the LAST weigh-in in it is "pm"
OVERNIGHT_AM_TO_H = 12         # morning window: 04:00 - 12:00 today; the FIRST weigh-in in it is "am"
OVERNIGHT_TZ = "America/New_York"   # weigh-in timestampGMT (epoch ms, UTC) is converted to this zone (ET)
G_PER_LB = 453.592             # Garmin weight field is grams (probe 2026-10-25: 93599.0 g = 206.4 lb)
ML_PER_LB = 453.6              # 1 lb of water ~ 453.6 mL: delta_ml_est = delta_lb * ML_PER_LB, nearest 10

# Weather / barometric pain (2026-10-23): |pressure change| over the worst 12 h / 24 h window, in hPa.
# Migraine / joint-pain literature puts the trigger zone at roughly 5-10 hPa; a frontal passage is 6-12.
WEATHER_PAIN_LOW_HPA_12H = 1.5       # LOW: noticeable to the sensitive, card only
WEATHER_PAIN_MODERATE_HPA_12H = 3.0  # MODERATE: reason on the card, plan for a pain day
WEATHER_PAIN_HIGH_HPA_12H = 6.0      # HIGH: joins the one-tier downgrade OR (only when ENABLED below)
WEATHER_PAIN_MODERATE_HPA_24H = 5.0  # same two levels on the slower 24 h window
WEATHER_PAIN_HIGH_HPA_24H = 10.0
WEATHER_PAIN_DOWNGRADE_LEVEL = "HIGH"   # pain_level at/above this downgrades one tier ("MODERATE" to be stricter)
WEATHER_PAIN_DOWNGRADE_ENABLED = False  # record-only until ~2026-11-13; then compare pain_level vs bb_wake in history.csv
WEATHER_PAIN_ORDER = ["NONE", "LOW", "MODERATE", "HIGH"]
WEATHER_THRESHOLDS = {"low12": WEATHER_PAIN_LOW_HPA_12H, "moderate12": WEATHER_PAIN_MODERATE_HPA_12H,
                      "high12": WEATHER_PAIN_HIGH_HPA_12H, "moderate24": WEATHER_PAIN_MODERATE_HPA_24H,
                      "high24": WEATHER_PAIN_HIGH_HPA_24H}
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

    if DRY_RUN:
        # Never burn the one-per-day credential attempt (or alert / write relogin state) from a dry run.
        raise RuntimeError(f"dry run: Garmin token rejected ({token_err}); not attempting the credential "
                           "re-login. Run without --dry-run, or: python garmin_capacity.py --setup")
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
        "weigh_ins_range": safe_call(c.get_weigh_ins, (day - timedelta(days=1)).isoformat(), d),   # overnight delta
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


# --mock scenarios: one per rule (python garmin_capacity.py --mock <name>; bare --mock = default).
_NO_SLEEP_RAW = {   # watch not worn: no Garmin sleep record, Body Battery charged 0 overnight (10/23 shape)
    "stats": {"bodyBatteryAtWakeTime": None, "bodyBatteryHighestValue": 5, "bodyBatteryMostRecentValue": 5,
              "averageStressLevel": 58, "restingHeartRate": 73, "sleepingSeconds": 0},
    "sleep": {"dailySleepDTO": {"sleepTimeSeconds": None, "sleepScores": {}}},
    "hrv": None,
    "body_battery": [{"charged": 0, "drained": 0, "bodyBatteryValuesArray": [[1792728000000, 5], [1792730520000, 5]]}],
}
_GARMIN_TIMED_SLEEP = {   # 2026-10-22 shape: Local timestamps are wall-clock epoch ms (23:03 -> 08:44)
    "dailySleepDTO": {"sleepTimeSeconds": 33900, "sleepScores": {"overall": {"value": 71}},
                      "sleepStartTimestampLocal": 1792633439000, "sleepEndTimestampLocal": 1792668299000},
    "avgOvernightHrv": 41, "hrvStatus": "BALANCED",
}
MOCK_CASES = {
    "default":          {},                                                       # BB 68 GREEN; AHI 6.3 -> YELLOW; usage 212 -> sleep-debt flag
    "seal-score":       {"cpap": {"leak_95": 9.6, "mask_seal_score": 10}},        # mask seal via seal score < CPAP_MASK_SEAL_LOW
    "seal-leak":        {"cpap": {"leak_95": 34.8, "mask_seal_score": 19}},       # mask seal via leak_95 > CPAP_LEAK_HIGH_LPM
    "seal-ok":          {"cpap": {"leak_95": 9.6, "mask_seal_score": 19}},        # neither: no "mask seal" reason
    "watch-off":        {"raw": _NO_SLEEP_RAW, "cpap": None, "sn": {"sleepiq_score": 73}},   # tier from SleepIQ 73 -> YELLOW
    "watch-off-green":  {"raw": _NO_SLEEP_RAW, "cpap": None, "sn": {"sleepiq_score": 85}},   # SleepIQ 85 -> GREEN
    "watch-off-red":    {"raw": _NO_SLEEP_RAW, "cpap": None, "sn": {"sleepiq_score": 52}},   # SleepIQ 52 -> RED
    "watch-off-ahi":    {"raw": _NO_SLEEP_RAW, "cpap": {"ahi": 5.0, "usage_min": 419, "leak_95": 34.8,
                                                        "mask_seal_score": 10}, "sn": {"sleepiq_score": 73}},  # 10/23: SleepIQ YELLOW, AHI -> RED
    "watch-off-nosiq":  {"raw": _NO_SLEEP_RAW, "cpap": None, "sn": None},                  # no SleepIQ: BB 5 -> RED as before
    "weather-low":      {"cpap": {"ahi": 2.1, "usage_min": 420}, "wx": {"d12": -2.0, "d24": -2.4}},   # LOW: card only, GREEN stays
    "weather-moderate": {"cpap": {"ahi": 2.1, "usage_min": 420}, "wx": {"d12": -4.2, "d24": -4.9}},   # MODERATE: reason only, GREEN stays
    "weather-high":     {"cpap": {"ahi": 2.1, "usage_min": 420}, "wx": {"d12": -7.1, "d24": -9.3}},   # HIGH, record-only (default): GREEN stays, reason says so
    "weather-high-on":  {"cpap": {"ahi": 2.1, "usage_min": 420}, "wx": {"d12": -7.1, "d24": -9.3},
                         "downgrade_enabled": True},                                                  # HIGH with ENABLED=True: joins downgrade -> YELLOW
    "weather-high-24h": {"cpap": {"ahi": 2.1, "usage_min": 420}, "wx": {"d12": 4.0, "d24": 10.5}},    # HIGH via the 24 h window (rising), record-only
    "weather-badlevel": {"cpap": {"ahi": 2.1, "usage_min": 420}, "wx": {"d12": -7.1, "d24": -9.3},
                         "downgrade_enabled": True, "downgrade_level": "SEVERE"},                     # misspelt level: warns once, no downgrade
    "weather-none":     {"cpap": {"ahi": 2.1, "usage_min": 420}, "wx": None},                                 # weather source failed: no line change
    # record-only fields (sleep timing + alcohol); none of these may change tier / bedtime / blocks
    "timing-garmin":    {"raw": {"sleep": _GARMIN_TIMED_SLEEP}},                                              # onset/wake from Garmin (watch)
    "timing-sleepiq":   {},                                                                                  # default raw has no Garmin timestamps -> SleepIQ bed entry/exit
    "timing-null":      {"sn": None},                                                                         # neither -> nulls, "no timing" line
    "alcohol-logged":   {"alcohol": 2},                                                                       # metrics.alcohol_drinks 2 -> "Alcohol last night: 2"
    "alcohol-none":     {"alcohol": None},                                                                    # not logged -> null, never 0
    # overnight weigh-in delta (display only; Garmin Index scale, grams + timestampGMT): tier / bedtime unchanged
    "overnight":         {"overnight": {"pm": 245.2, "am": 242.8}},                                           # both: -2.4 lb (~1090 mL, 8.6 h)
    "overnight-pm-only": {"overnight": {"pm": 245.2}},                                                        # no morning weigh-in: missing "am"
    "overnight-am-only": {"overnight": {"am": 242.8}},                                                        # no bedtime weigh-in: missing "pm"
    "overnight-none":    {"overnight": {}},                                                                   # scale unused: overnight null
    "overnight-late-am": {"overnight": {"pm": 245.2, "am": 242.8, "am_at": "09:40"}},                       # AM weigh-in after the 09:00 run (10.1 h); the 12:15 pass picks it up
    # nightly check-in (display only): payload.checkin + one card line; tier / bedtime unchanged
    "checkin":          {"checkin": {"status": "done", "drinks": "2", "new_wounds": "yes", "new_wound_count": "1",
                                     "new_wound_sites": ["Scalp"], "older_wounds": "healing", "exercise": "yes",
                                     "exercise_types": ["Walking outside"], "exercise_minutes": "30-60"}, "alcohol": 2},
    "checkin-skipped":  {"checkin": {"status": "skipped", "drinks": "", "new_wounds": "", "older_wounds": "", "exercise": ""}},
    "checkin-none":     {"checkin": None},                                                                    # no row -> "Check-in: none"
}


def mock_weigh_ins(day: date, pm: Optional[float] = None, am: Optional[float] = None,
                   pm_at: str = "23:35", am_at: str = "08:11") -> Dict[str, Any]:
    """Garmin get_weigh_ins shape for --mock: grams + timestampGMT (UTC ms) built from ET wall-clock times
    (default bed 23:35 yesterday, morning 08:11 today -> 8.6 h)."""
    from datetime import timezone

    def rec(local: datetime, lb: float) -> Dict[str, Any]:
        try:
            from zoneinfo import ZoneInfo
            utc = local.replace(tzinfo=ZoneInfo(OVERNIGHT_TZ)).astimezone(timezone.utc)
        except Exception:
            utc = local.astimezone(timezone.utc)
        return {"samplePk": int(utc.timestamp() * 1000) + 7, "weight": round(lb * G_PER_LB, 1), "sourceType": "INDEX_SCALE",
                "timestampGMT": int(utc.timestamp() * 1000), "calendarDate": str(local.date()), "weightDelta": None}

    days = {}
    if pm is not None:
        days.setdefault(str(day - timedelta(days=1)), []).append(rec(hm_to_dt(day - timedelta(days=1), pm_at), pm))
    if am is not None:
        days.setdefault(str(day), []).append(rec(hm_to_dt(day, am_at), am))
    return {"dailyWeightSummaries": [{"summaryDate": d, "numOfWeightEntries": len(v), "latestWeight": v[-1], "allWeightMetrics": v}
                                     for d, v in sorted(days.items())],
            "totalAverage": {}, "previousDateWeight": None, "nextDateWeight": None}


def mock_weather(day: date, d12: float = -0.8, d24: float = 1.2) -> Dict[str, Any]:
    from sources_extra import pain_from_pressure
    pain = pain_from_pressure(d12, d24, WEATHER_PAIN_MODERATE_HPA_12H, WEATHER_PAIN_HIGH_HPA_12H,
                              WEATHER_PAIN_MODERATE_HPA_24H, WEATHER_PAIN_HIGH_HPA_24H, WEATHER_PAIN_LOW_HPA_12H)
    return {"pressure_hpa": 1013.2, "trend_3h_hpa": round(d12 / 4, 1),
            "d12_hpa": d12, "d12_window": [f"{day}T08:00", f"{day}T20:00"],
            "d24_hpa": d24, "d24_window": [f"{day - timedelta(days=1)}T20:00", f"{day}T20:00"],
            "pressure_min_hpa": 1006.1, "pressure_max_hpa": 1014.0, **pain,
            "temp_f": 54.3, "temp_high_f": 63.0, "humidity_pct": 71, "precip_prob_max_pct": 40,
            "condition": "overcast", "location": "Mock, KY", "lat": 38.2, "lon": -84.9,
            "anchor": f"{day}T08:00", "date": str(day)}


def mock_extra(day: date, case: str = "default"):
    """Synthetic myAir / SleepIQ / weather objects for --mock, overlaid with the scenario's overrides."""
    cpap = {"usage_min": 212, "ahi": 6.3, "leak_95": 9.6, "mask_seal_score": 19, "mask_onoff_score": 4,
            "mask_pairs": 3, "myair_score": 78, "date": str(day - timedelta(days=1))}
    sn = {"sleepiq_score": 64, "in_bed_min": 431, "restful_min": 362, "restless_min": 55, "out_of_bed_min": 14,
          "avg_hr": 61, "avg_hrv": 38, "avg_rr": 15, "bed_entry": f"{day - timedelta(days=1)}T23:58",
          "bed_exit": f"{day}T07:35", "sleeper": "Darren", "sessions": 1, "date": str(day)}
    c = MOCK_CASES[case]
    if "cpap" in c:
        cpap = None if c["cpap"] is None else {**cpap, **c["cpap"]}
    if "sn" in c:
        sn = None if c["sn"] is None else {**sn, **c["sn"]}
    wx = None if c.get("wx", {}) is None else mock_weather(day, **c.get("wx") or {})
    return cpap, sn, wx


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


def _garmin_local_ms(ms) -> Optional[datetime]:
    """Garmin '...TimestampLocal' = epoch ms already shifted to wall-clock time, so decode it as UTC."""
    try:
        from datetime import timezone
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).replace(tzinfo=None)
    except Exception:
        return None


def sleep_timing(raw: Optional[Dict[str, Any]], sn: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Record-only: when the night started / ended. Garmin sleep record first, SleepIQ bed entry/exit
    second, else nulls. Never raises (a failure logs and returns nulls)."""
    out = {"sleep_onset": None, "wake_time": None, "sleep_midpoint": None, "timing_source": None}
    try:
        dto = dig(raw, "sleep", "dailySleepDTO") or {}
        start, end = _garmin_local_ms(dto.get("sleepStartTimestampLocal")), _garmin_local_ms(dto.get("sleepEndTimestampLocal"))
        src = "garmin"
        if not (start and end and end > start):
            start = end = None
            if sn and sn.get("bed_entry") and sn.get("bed_exit"):
                start, end = datetime.fromisoformat(sn["bed_entry"]), datetime.fromisoformat(sn["bed_exit"])
                src = "sleepiq"
        if start and end and end > start:
            out.update(sleep_onset=start.isoformat(timespec="minutes"), wake_time=end.isoformat(timespec="minutes"),
                       sleep_midpoint=(start + (end - start) / 2).isoformat(timespec="minutes"), timing_source=src)
    except Exception as e:
        log.warning("sleep timing skipped: %s: %s", type(e).__name__, e)
    return out


def alcohol_log(night: date, drinks: float, source: str = "telegram", at: Optional[datetime] = None) -> None:
    """Append one row to data\\alcohol.csv. Re-logging a night appends again; readers take the LAST row."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    new = not ALCOHOL_CSV.exists() or ALCOHOL_CSV.stat().st_size == 0
    with ALCOHOL_CSV.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["night_date", "drinks", "logged_at", "source"])
        w.writerow([night.isoformat(), f"{drinks:g}", (at or datetime.now()).isoformat(timespec="seconds"), source])


def alcohol_for_night(night: date) -> Optional[float]:
    """Last logged drinks for `night` (the evening's date), or None when nothing was logged — never 0 by default."""
    try:
        if not ALCOHOL_CSV.exists():
            return None
        val = None
        with ALCOHOL_CSV.open(encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                if r.get("night_date") == night.isoformat() and r.get("drinks") not in (None, ""):
                    val = float(r["drinks"])
        return val
    except Exception as e:
        log.warning("alcohol log unreadable: %s: %s", type(e).__name__, e)
        return None


def _gmt_ms_to_et(ms) -> Optional[datetime]:
    """Garmin weigh-in timestampGMT (epoch ms, UTC) -> naive local time in OVERNIGHT_TZ (ET)."""
    try:
        from datetime import timezone
        utc = datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
        try:
            from zoneinfo import ZoneInfo
            return utc.astimezone(ZoneInfo(OVERNIGHT_TZ)).replace(tzinfo=None)
        except Exception:
            return utc.astimezone().replace(tzinfo=None)       # no tzdata: machine zone (this PC is ET)
    except Exception:
        return None


def weigh_ins(raw: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Every scale reading in the raw pull as {at (naive ET datetime), weight_lb, source, pk}, oldest first.
    Reads raw["weigh_ins_range"] (get_weigh_ins: dailyWeightSummaries[].allWeightMetrics[]) and, for older
    raw files, raw["weigh_ins"] / raw["body_composition"] (dateWeightList[]). Weight is grams; the time is
    timestampGMT (UTC ms) — the record's "date" field is a wall clock in another zone and is not used."""
    recs: List[Dict[str, Any]] = []
    for key in ("weigh_ins_range", "weigh_ins", "body_composition"):
        w = (raw or {}).get(key)
        if isinstance(w, dict):
            for summ in w.get("dailyWeightSummaries") or []:
                recs.extend(summ.get("allWeightMetrics") or [])
            recs.extend(w.get("dateWeightList") or [])
        elif isinstance(w, list):
            recs.extend(w)
    out, seen = [], set()
    for r in recs:
        if not isinstance(r, dict) or r.get("weight") is None:
            continue
        at = _gmt_ms_to_et(r.get("timestampGMT"))
        key = (r.get("samplePk"), r.get("timestampGMT"))
        if at is None or key in seen:
            continue
        seen.add(key)
        out.append({"at": at, "weight_lb": round(float(r["weight"]) / G_PER_LB, 1),
                    "source": r.get("sourceType"), "pk": r.get("samplePk")})
    out.sort(key=lambda x: x["at"])
    return out


def overnight_delta(raw: Optional[Dict[str, Any]], day: date) -> Optional[Dict[str, Any]]:
    """Display only: bedtime weigh-in (LAST one 19:00 yesterday - 04:00 today) vs morning weigh-in (FIRST one
    04:00 - 12:00 today) from the Garmin Index scale. None when both are missing or the scale data failed;
    with one side missing the object is returned with `missing` = "pm" | "am". Never raises.
    Garmin's API only returns weigh-ins assigned to this profile: a reading left unassigned on a shared
    scale (or claimed by another profile) is not visible here, so a missing side may be an assignment issue."""
    try:
        wi = weigh_ins(raw)
        pm_from = hm_to_dt(day - timedelta(days=1), f"{OVERNIGHT_PM_FROM_H:02d}:00")
        pm_to = hm_to_dt(day, f"{OVERNIGHT_PM_TO_H:02d}:00")
        am_to = hm_to_dt(day, f"{OVERNIGHT_AM_TO_H:02d}:00")
        pm_all = [x for x in wi if pm_from <= x["at"] < pm_to]
        am_all = [x for x in wi if pm_to <= x["at"] < am_to]
        if len(wi) - len(pm_all) - len(am_all):
            log.info("overnight: %d weigh-in(s) outside the bedtime / morning windows ignored",
                     len(wi) - len(pm_all) - len(am_all))
        pm, am = (pm_all[-1] if pm_all else None), (am_all[0] if am_all else None)
        if pm is None and am is None:
            log.info("overnight: no weigh-ins for %s (none assigned to this profile in the window)", day)
            return None
        iso = lambda x: x["at"].isoformat(timespec="minutes") if x else None
        out = {"pm_weight_lb": pm["weight_lb"] if pm else None, "pm_time": iso(pm),
               "am_weight_lb": am["weight_lb"] if am else None, "am_time": iso(am),
               "delta_lb": None, "delta_ml_est": None, "hours_between": None,
               "missing": "pm" if pm is None else "am" if am is None else None}
        if pm and am:
            out["delta_lb"] = round(pm["weight_lb"] - am["weight_lb"], 1)              # positive = weight lost overnight
            out["delta_ml_est"] = int(round(out["delta_lb"] * ML_PER_LB / 10.0) * 10)
            out["hours_between"] = round((am["at"] - pm["at"]).total_seconds() / 3600, 1)
        else:
            log.info("overnight: missing %s weigh-in (unassigned scale readings are not returned by Garmin)", out["missing"])
        return out
    except Exception as e:
        log.warning("overnight weigh-in delta skipped: %s: %s", type(e).__name__, e)
        return None


def checkin_row(day: date, answers: Optional[Dict[str, Any]], started_at: Optional[datetime] = None,
                finished_at: Optional[datetime] = None, status: str = "done", trigger: str = "",
                source: str = "telegram") -> Dict[str, Any]:
    """One data\\checkin.csv row from the bot's answers dict. Unanswered = "" (never 0 / "no" by default)."""
    a = answers or {}

    def cell(k):
        v = a.get(k)
        if v is None:
            return ""
        return ";".join(str(x) for x in v) if isinstance(v, (list, tuple)) else str(v)

    row = {k: cell(k) for k in CHECKIN_FIELDS}
    row.update(day=day.isoformat(), status=status, trigger=trigger, source=source,
               started_at=started_at.isoformat(timespec="seconds") if started_at else "",
               finished_at=(finished_at or datetime.now()).isoformat(timespec="seconds"))
    return row


def checkin_log(row: Dict[str, Any]) -> None:
    """Append one row to data\\checkin.csv (header on first write). Re-doing a day appends; readers take the LAST row."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    new = not CHECKIN_CSV.exists() or CHECKIN_CSV.stat().st_size == 0
    with CHECKIN_CSV.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CHECKIN_FIELDS)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in CHECKIN_FIELDS})


def checkin_for_day(day: date) -> Optional[Dict[str, Any]]:
    """Last check-in row for `day` (the evening's date) with list fields split, or None. Never raises."""
    try:
        if not CHECKIN_CSV.exists():
            return None
        row = None
        with CHECKIN_CSV.open(encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                if r.get("day") == day.isoformat():
                    row = r
        if row is None:
            return None
        out = {k: (row.get(k) or "") for k in CHECKIN_FIELDS}
        for k in CHECKIN_LIST_FIELDS:
            out[k] = [x for x in out[k].split(";") if x]
        return out
    except Exception as e:
        log.warning("check-in log unreadable: %s: %s", type(e).__name__, e)
        return None


def checkin_drinks_value(v: Any) -> Optional[float]:
    """Check-in drinks answer -> number for alcohol.csv: "4+" -> 4.0; "" / "unknown" -> None (not logged)."""
    try:
        t = str(v if v is not None else "").strip().lower()
        if t in ("", "unknown", "?"):
            return None
        return float(t.rstrip("+"))
    except Exception:
        return None


def checkin_line(c: Optional[Dict[str, Any]]) -> str:
    """One card line for last night's check-in. Unanswered pieces show as '?', never as 'no' or 0."""
    if not c:
        return "Check-in: none"
    try:
        d = date.fromisoformat(c.get("day"))
        lbl = f"Check-in {d.strftime('%a')} {d.month}/{d.day}"
    except Exception:
        lbl = "Check-in"
    if c.get("status") == "skipped":
        lbl += " (skipped)"
    parts = []
    dr = (c.get("drinks") or "").strip()
    parts.append("drinks none" if dr == "0" else f"drinks {dr}" if dr and dr != "unknown" else "drinks ?")
    nw = (c.get("new_wounds") or "").lower()
    if nw == "yes":
        n = (c.get("new_wound_count") or "").strip()
        sites = c.get("new_wound_sites") or []
        parts.append(f"new wounds {n if n and n != 'unknown' else '?'} ({', '.join(sites) if sites else '?'})")
    elif nw == "no":
        parts.append("no new wounds")
    else:
        parts.append("new wounds ?")
    older = {"none": "older: none open", "healing": "older: healing", "worse": "older: WORSE"}.get((c.get("older_wounds") or "").lower())
    if older:
        parts.append(older)
    ex = (c.get("exercise") or "").lower()
    if ex == "yes":
        types = c.get("exercise_types") or []
        mins = (c.get("exercise_minutes") or "").strip()
        parts.append(("exercise " + (", ".join(types) if types else "?") + (f" {mins} min" if mins else "")).strip())
    elif ex == "no":
        parts.append("no exercise")
    else:
        parts.append("exercise ?")
    return f"{lbl}: " + " · ".join(parts)


def published(day: date) -> Optional[Dict[str, Any]]:
    """The day's saved payload when the morning run already published it (tier not UNKNOWN), else None."""
    f = DATA_DIR / f"{day}.json"
    try:
        p = json.loads(f.read_text(encoding="utf-8")) if f.exists() else None
    except Exception:
        return None
    return p if isinstance(p, dict) and p.get("tier") not in (None, "UNKNOWN") else None


def _overnight_key(o: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """What counts as a change: every overnight field except the fetch stamp."""
    return {k: v for k, v in o.items() if k != "fetched_at"} if o else None


def run_overnight_update(day: date, dry_run: bool = False, client=None, now: Optional[datetime] = None) -> int:
    """12:15 pass: the day is already published, so fetch ONLY the weigh-in range (one Garmin request),
    recompute payload.overnight and, only when it changed, rewrite data\\<date>.json / latest.json / raw and
    re-send the Telegram card + [CAPACITY] mail. Display only: tier, reasons, plan, circadian and history.csv
    are untouched. Any failure -> WARNING and nothing changes (exit 0). Unchanged -> nothing written or sent."""
    now = now or datetime.now()
    payload = published(day)
    if not payload:
        log.warning("overnight update: %s is not published yet — nothing to update", day)
        return 0
    raw_f = RAW_DIR / f"{day}.json"
    try:
        raw = json.loads(raw_f.read_text(encoding="utf-8")) if raw_f.exists() else {}
    except Exception:
        raw = {}
    try:
        c = client or garmin_client()
        raw["weigh_ins_range"] = c.get_weigh_ins((day - timedelta(days=1)).isoformat(), day.isoformat())
    except Exception as e:
        log.warning("overnight update: Garmin weigh-in fetch failed (%s: %s); overnight unchanged", type(e).__name__, e)
        return 0
    new, old = overnight_delta(raw, day), payload.get("overnight")
    if _overnight_key(new) == _overnight_key(old):
        log.info("overnight update %s %s: no change (%s) — nothing sent", day, now.strftime("%H:%M"), overnight_line(old))
        return 0
    if new:
        new["fetched_at"] = now.isoformat(timespec="minutes")
    payload["overnight"] = new
    payload["overnight_updated_at"] = now.isoformat(timespec="seconds")
    text = card(payload)
    print(text)
    if dry_run:
        print("<<<CAPACITY_JSON" + chr(10) + json.dumps(payload, indent=2) + chr(10) + "CAPACITY_JSON>>>")
        print("DRY RUN: nothing saved")
        return 0
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    raw.setdefault("_meta", {})["overnight_fetched_at"] = now.isoformat(timespec="seconds")
    raw_f.write_text(json.dumps(raw, indent=2, default=str), encoding="utf-8")
    (DATA_DIR / f"{day}.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (DATA_DIR / "latest.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tg = send_telegram(text)
    ml = send_mail(payload, text)
    log.info("overnight update %s %s: changed (%s) -> published: telegram=%s mail=%s",
             day, now.strftime("%H:%M"), overnight_line(new), tg, ml)
    return 0 if (tg or ml) else 2


def _hm(iso: Optional[str]) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%H:%M")
    except Exception:
        return str(iso)


def bb_untrusted(m: Dict[str, Any]) -> bool:
    """Watch not worn overnight: Garmin has no sleep record for the night (sleep_source is not
    "garmin", see merge_extra) AND Body Battery charged 0 overnight, so bb_wake is yesterday's
    stale drained value, not a measurement of recovery."""
    return m.get("sleep_source") != "garmin" and m.get("bb_charged_overnight") == 0


def pressure_phrase(wx: Dict[str, Any]) -> str:
    """'falling 7.1 hPa 02:00-14:00' from the worst 12 h window (24 h window if that is the stronger one)."""
    d12, d24 = wx.get("d12_hpa") or 0.0, wx.get("d24_hpa") or 0.0
    use24 = abs(d24) / WEATHER_PAIN_HIGH_HPA_24H > abs(d12) / WEATHER_PAIN_HIGH_HPA_12H
    d, win, hrs = (d24, wx.get("d24_window"), 24) if use24 else (d12, wx.get("d12_window"), 12)
    direction = "falling" if d < 0 else "rising" if d > 0 else "flat"
    when = f" {win[0][11:16]}-{win[1][11:16]}" + (" (+1)" if win[1][:10] > win[0][:10] else "") if win else ""
    return f"{direction} {abs(d):.1f} hPa / {hrs} h{when}"


_PAIN_LEVEL_WARNED = False


def pain_downgrade_index() -> Optional[int]:
    """Index of WEATHER_PAIN_DOWNGRADE_LEVEL in WEATHER_PAIN_ORDER, or None (downgrade disabled) when the
    constant is misspelt — warns once, never raises."""
    global _PAIN_LEVEL_WARNED
    if WEATHER_PAIN_DOWNGRADE_LEVEL in WEATHER_PAIN_ORDER:
        return WEATHER_PAIN_ORDER.index(WEATHER_PAIN_DOWNGRADE_LEVEL)
    if not _PAIN_LEVEL_WARNED:
        log.warning("WEATHER_PAIN_DOWNGRADE_LEVEL=%r is not one of %s; barometric downgrade disabled",
                    WEATHER_PAIN_DOWNGRADE_LEVEL, WEATHER_PAIN_ORDER)
        _PAIN_LEVEL_WARNED = True
    return None


def classify(m: Dict[str, Any], cpap: Optional[Dict[str, Any]] = None,
             sn: Optional[Dict[str, Any]] = None, wx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Tier + reasons. Sets m["tier_source"] ("garmin" | "sleepiq" | None for UNKNOWN)."""
    bb = m.get("bb_wake")
    reasons: List[str] = []
    m["tier_source"] = None
    if bb is None:
        return {"tier": "UNKNOWN", "reasons": ["No Body Battery reading — watch not synced?"], "sleep_debt_flag": False}
    siq = sn.get("sleepiq_score") if sn else None
    if bb_untrusted(m) and siq is not None:
        tier = "GREEN" if siq >= SLEEPIQ_GREEN_MIN else "YELLOW" if siq >= SLEEPIQ_YELLOW_MIN else "RED"
        m["tier_source"] = "sleepiq"
        reasons.append(f"BB untrusted (no Garmin sleep) — tier from SleepIQ {siq}")
    else:
        tier = "GREEN" if bb >= GREEN_MIN else "YELLOW" if bb >= YELLOW_MIN else "RED"
        m["tier_source"] = "garmin"
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
    pain = (wx or {}).get("pain_level")
    lvl = pain_downgrade_index()
    if pain in WEATHER_PAIN_ORDER and lvl is not None and WEATHER_PAIN_ORDER.index(pain) >= lvl:
        if WEATHER_PAIN_DOWNGRADE_ENABLED:
            downgrade.append(f"barometric pain risk {pain} ({pressure_phrase(wx)})")
        else:
            reasons.append(f"barometric pain risk {pain} ({pressure_phrase(wx)}) — record-only, no tier change")
    elif pain == "MODERATE":
        reasons.append(f"barometric pain risk MODERATE ({pressure_phrase(wx)}) — plan for a pain day")
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
        leak, seal = cpap.get("leak_95"), cpap.get("mask_seal_score")
        if (leak is not None and leak > CPAP_LEAK_HIGH_LPM) or (seal is not None and seal < CPAP_MASK_SEAL_LOW):
            reasons.append(f"mask seal (leak {leak} L/min, seal score {seal}/20)")
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
    """Write raw\\<date>.json, <date>.json, latest.json and the history.csv row. No-op on --dry-run."""
    if DRY_RUN:
        log.info("DRY RUN: nothing saved (raw\\%s.json, %s.json, latest.json, history.csv untouched)", day, day)
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    (RAW_DIR / f"{day}.json").write_text(json.dumps(raw, indent=2, default=str), encoding="utf-8")
    (DATA_DIR / f"{day}.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (DATA_DIR / "latest.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

    m = payload["metrics"]
    fields = ["date", "tier", "bb_wake", "sleep_min", "sleep_score", "hrv_last_night", "hrv_status",
              "stress_avg_yesterday", "resting_hr", "readiness_score", "bedtime_target",
              "pressure_hpa", "pressure_d12_hpa", "pain_level",       # weather columns added 2026-10-23 (older rows blank)
              "sleep_onset", "wake_time", "sleep_midpoint", "timing_source", "alcohol_drinks"]   # record-only, 2026-10-23
    w = payload.get("weather") or {}
    row = {"date": str(day), "tier": payload["tier"], "bedtime_target": payload["circadian"]["bedtime_target"],
           "pressure_hpa": w.get("pressure_hpa"), "pressure_d12_hpa": w.get("d12_hpa"), "pain_level": w.get("pain_level"),
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
        f"{icon} CAPACITY {payload['tier']} — {payload['date']}{as_of_phrase(payload)}\n"
        f"Body Battery at wake: {m.get('bb_wake')}  |  Sleep {sleep} (score {m.get('sleep_score')}{src})\n"
        f"HRV {m.get('hrv_last_night')} ({m.get('hrv_status')})  |  RHR {m.get('resting_hr')}  |  Readiness {m.get('readiness_score')}\n"
        f"{cpap_line(payload.get('cpap'))}\n"
        f"{sleepnumber_line(payload.get('sleepnumber'))}\n"
        f"{weather_line(payload.get('weather'))}\n"
        f"{slept_line(m)}\n"
        f"{alcohol_line(m)}\n"
        f"{checkin_line(payload.get('checkin'))}\n"
        f"{overnight_line(payload.get('overnight'))}\n"
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
            f"seal {c.get('mask_seal_score')}/20  |  mask on/off {c.get('mask_onoff_score')}/5 "
            f"({c.get('mask_pairs')}x)  |  myAir {c.get('myair_score')}")


def sleepnumber_line(s: Optional[Dict[str, Any]]) -> str:
    if not s:
        return "SleepIQ: no data"
    ib = s.get("in_bed_min")
    in_bed = f"{ib//60}h{ib%60:02d}" if isinstance(ib, int) else "n/a"
    return (f"SleepIQ {s.get('sleepiq_score')}  |  in bed {in_bed} (restful {s.get('restful_min')} / "
            f"restless {s.get('restless_min')} min)  |  HR {s.get('avg_hr')}  |  HRV {s.get('avg_hrv')}  |  "
            f"RR {s.get('avg_rr')}  |  bed {_hm(s.get('bed_entry'))}")


def _clock(iso: Optional[str]) -> str:
    try:
        d = datetime.fromisoformat(iso)
        return d.strftime("%#I:%M %p") if os.name == "nt" else d.strftime("%-I:%M %p")
    except Exception:
        return "?"


def slept_line(m: Dict[str, Any]) -> str:
    if not m.get("sleep_onset") or not m.get("wake_time"):
        return "Slept: no timing (watch off, no SleepIQ)"
    src = {"garmin": "watch", "sleepiq": "bed"}.get(m.get("timing_source"), m.get("timing_source"))
    return f"Slept {_clock(m['sleep_onset'])} → {_clock(m['wake_time'])} ({src})"


def alcohol_line(m: Dict[str, Any]) -> str:
    v = m.get("alcohol_drinks")
    if v is None:
        return "Alcohol last night: not logged — /drinks N"
    return f"Alcohol last night: {v:g}"


def as_of_phrase(payload: Dict[str, Any]) -> str:
    """' (as of 09:00)' from payload.as_of (the Garmin pull time), else fetched_at / generated_at."""
    t = payload.get("as_of") or payload.get("fetched_at") or payload.get("generated_at")
    try:
        return f" (as of {datetime.fromisoformat(t).strftime('%H:%M')})"
    except Exception:
        return ""


def overnight_line(o: Optional[Dict[str, Any]]) -> str:
    """'Overnight: 245.2 → 242.8 lb = −2.4 lb (~1090 mL, 8.6 h)' — the sign is the weight CHANGE (loss = −).
    A line refreshed by the 12:15 update pass ends with '· as of HH:MM' (overnight.fetched_at)."""
    if not o:
        return "Overnight: no weigh-ins (scale)"
    tail = f" · as of {_hm(o['fetched_at'])}" if o.get("fetched_at") else ""
    if o.get("missing") == "pm":
        return f"Overnight: missing bedtime weigh-in (AM {o.get('am_weight_lb')} lb at {_clock(o.get('am_time'))}){tail}"
    if o.get("missing") == "am":
        return f"Overnight: missing morning weigh-in (bed {o.get('pm_weight_lb')} lb at {_clock(o.get('pm_time'))}){tail}"
    d = o.get("delta_lb") or 0.0
    sign = "−" if d > 0 else "+" if d < 0 else "±"
    return (f"Overnight: {o.get('pm_weight_lb')} → {o.get('am_weight_lb')} lb = {sign}{abs(d):.1f} lb "
            f"(~{abs(o.get('delta_ml_est') or 0)} mL, {o.get('hours_between')} h){tail}")


def weather_line(w: Optional[Dict[str, Any]]) -> str:
    if not w:
        return "Weather: no data"
    pain = w.get("pain_level") or "n/a"
    wx = f"{w.get('temp_f')}°F" + (f"/{w.get('temp_high_f')}" if w.get("temp_high_f") is not None else "")
    if w.get("condition"):
        wx += f" {w['condition']}"
    if w.get("precip_prob_max_pct"):
        wx += f", precip {w['precip_prob_max_pct']}%"
    return (f"Weather {w.get('pressure_hpa')} hPa {pressure_phrase(w)}  |  pain risk {pain} "
            f"({w.get('pain_index')}/10)  |  {wx}")


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

    if dry_run:
        print("<<<CAPACITY_NOW_JSON" + chr(10) + json.dumps(payload, indent=2) + chr(10) + "CAPACITY_NOW_JSON>>>")
        print("DRY RUN: nothing saved")
        return 0
    NOW_DIR.mkdir(parents=True, exist_ok=True)
    (NOW_DIR / f"{day}_{at.strftime('%H%M')}.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (DATA_DIR / "now_latest.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (DATA_DIR / "now_latest.raw.json").write_text(json.dumps(raw, indent=2, default=str), encoding="utf-8")

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
    ap.add_argument("--mock", nargs="?", const="default", choices=sorted(MOCK_CASES),
                    help="no Garmin call; synthetic data (optional scenario: " + ", ".join(sorted(MOCK_CASES)) + ")")
    ap.add_argument("--refresh", action="store_true", help=f"ignore the <{CACHE_MAX_AGE_H} h raw cache and hit Garmin")
    ap.add_argument("--if-missing", action="store_true", help="retry run: skip if today already published with data")
    ap.add_argument("--now", action="store_true",
                    help="on-demand current readings -> Telegram + [CAPACITY-NOW] mail (ignores the raw cache; "
                         "--dry-run prints only; --mock/--if-missing/--refresh don't apply)")
    ap.add_argument("--setup-myair", action="store_true", help="interactive ResMed myAir login (e-mail MFA), saves tokens")
    ap.add_argument("--setup-sleepiq", action="store_true", help="interactive Sleep Number login, picks the sleeper")
    ap.add_argument("--no-extra", action="store_true", help="skip the myAir / SleepIQ fetch (Garmin only)")
    ap.add_argument("--no-weather", action="store_true", help="skip the Open-Meteo barometric-pain fetch")
    ap.add_argument("--weather", action="store_true", help="print today's weather / barometric pain object and exit")
    a = ap.parse_args()
    global DRY_RUN
    DRY_RUN = bool(a.dry_run)                       # gates every write under data\ (see save / run_now / sources_extra)
    no_send = False                                 # --setup: save (the day's one real fetch) but do not publish

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

    if a.weather:
        import sources_extra
        sources_extra.DRY_RUN = DRY_RUN
        wx = sources_extra.fetch_weather(day, WEATHER_THRESHOLDS)
        print(weather_line(wx))
        print(json.dumps(wx, indent=2))
        return 0 if wx else 1

    if a.if_missing:
        if published(day):
            log.info("already published for %s — retry not needed", day)
            return 0

    if a.setup:
        garmin_client(interactive=True)
        print(f"Garmin login OK — tokens stored in {TOKEN_DIR}. Daily runs are now non-interactive.")
        no_send = True
        a.refresh = True  # the setup run is the day's one real fetch: saved (not a dry run), not sent

    if not a.mock and not a.refresh and published(day):
        # 12:15 trigger (or any plain re-run) on a day that is already out: weigh-ins only, re-send only on change.
        log.info("%s already published — overnight update pass (weigh-ins only); --refresh forces a full re-pull", day)
        return run_overnight_update(day, a.dry_run)

    if a.mock:
        raw = mock_raw(day)
        raw.update(MOCK_CASES[a.mock].get("raw", {}))
        if "overnight" in MOCK_CASES[a.mock]:
            raw["weigh_ins_range"] = mock_weigh_ins(day, **MOCK_CASES[a.mock]["overnight"])
    else:
        raw = None if a.refresh else cached_raw(day)
        if raw is None:
            try:
                raw = pull_garmin(day)
            except Exception as e:
                log.error("Garmin fetch failed, nothing published: %s", e)
                return 2
    metrics = extract(raw)
    cpap = sn = wx = None
    if a.mock:
        cpap, sn, wx = mock_extra(day, a.mock)
        global WEATHER_PAIN_DOWNGRADE_ENABLED, WEATHER_PAIN_DOWNGRADE_LEVEL       # scenario overrides (mock only)
        WEATHER_PAIN_DOWNGRADE_ENABLED = MOCK_CASES[a.mock].get("downgrade_enabled", WEATHER_PAIN_DOWNGRADE_ENABLED)
        WEATHER_PAIN_DOWNGRADE_LEVEL = MOCK_CASES[a.mock].get("downgrade_level", WEATHER_PAIN_DOWNGRADE_LEVEL)
    else:
        try:   # all fetches are best-effort inside sources_extra; this guard covers an import error too
            import sources_extra
            sources_extra.DRY_RUN = DRY_RUN                             # weather raw / location caches live under data\
            if not a.no_extra:
                cpap = sources_extra.fetch_cpap(day)
                sn = sources_extra.fetch_sleepnumber(day)
            if not a.no_weather:
                wx = sources_extra.fetch_weather(day, WEATHER_THRESHOLDS)
        except Exception as e:
            log.warning("extra sources skipped: %s: %s", type(e).__name__, e)
    metrics = merge_extra(metrics, cpap, sn)
    metrics.update(sleep_timing(raw, sn))                                     # record-only
    metrics["alcohol_drinks"] = (MOCK_CASES[a.mock].get("alcohol") if a.mock
                                 else alcohol_for_night(day - timedelta(days=1)))   # None = not logged (never 0)
    overnight = overnight_delta(raw, day)                                       # display only (null on any failure)
    if a.mock:
        ck = MOCK_CASES[a.mock].get("checkin")
        checkin = {"day": str(day - timedelta(days=1)), **ck} if ck else None
    else:
        checkin = checkin_for_day(day - timedelta(days=1))                      # last evening's check-in (display only)
    cls = classify(metrics, cpap, sn, wx)
    tier = cls["tier"]
    payload = {
        "date": str(day),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "as_of": dig(raw, "_meta", "fetched_at") or datetime.now().isoformat(timespec="seconds"),   # Garmin pull time on card line 1
        "tier": tier,
        "reasons": cls["reasons"],
        "metrics": metrics,
        "plan": TIER_PLAN.get(tier, {"deep_blocks": 2, "guidance": "No data — plan as YELLOW until the watch syncs."}),
        "circadian": circadian(day, tier, metrics, cls.get("sleep_debt_flag", False)),
        "cpap": cpap,
        "sleepnumber": sn,
        "weather": wx,
        "overnight": overnight,
        "checkin": checkin,
        "source": "mock" if a.mock else "garmin",
        "fetched_at": dig(raw, "_meta", "fetched_at"),
    }
    text = card(payload)
    print(text)

    if not a.mock:
        save(day, payload, raw)                                         # no-op on --dry-run
    if a.dry_run or a.mock or no_send:
        print("<<<CAPACITY_JSON" + chr(10) + json.dumps(payload, indent=2) + chr(10) + "CAPACITY_JSON>>>")   # what the mail would carry
        if a.dry_run:
            print("DRY RUN: nothing saved")
        return 0

    tg = send_telegram(text)
    ml = send_mail(payload, text)
    log.info("published: telegram=%s mail=%s", tg, ml)
    return 0 if (tg or ml) else 2


if __name__ == "__main__":
    sys.exit(main())
