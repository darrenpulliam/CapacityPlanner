#!/usr/bin/env python3
"""
Capacity Planner — extra sleep sources (ResMed myAir CPAP + Sleep Number SleepIQ)
================================================================================
Standalone project (C:\\Users\\dppha\\CapacityPlanner). Not part of CrystalBull.

Two optional feeds for garmin_capacity.py. Both are best-effort: every public call returns
a dict or None, logs the failure (the scheduled tasks redirect stderr to data\\task.log) and
NEVER raises into the morning run.

    fetch_cpap(day)        -> {usage_min, ahi, leak_95, mask_score, myair_score, date} | None
    fetch_sleepnumber(day) -> {sleepiq_score, in_bed_min, restful_min, restless_min, out_of_bed_min,
                               avg_hr, avg_hrv, avg_rr, bed_entry, sleeper, date} | None

myAir (NA): Okta classic authn -> optional e-mail MFA (interactive --setup-myair only) ->
OAuth code + PKCE -> AppSync GraphQL (GetPatientSleepRecords). Ported 2026-10-23 from the
Home Assistant resmed_myair integration (client/auth.py, regions.py); the PyPI "myair-py"
0.1.2 fork has no MFA support and an old redirect URL, so it is not used. Tokens live in
%USERPROFILE%\\.myair\\tokens.json (next to .garminconnect): access_token, id_token, country and
the Okta "DT" remembered-device cookie. The access token has no refresh token (scope has no
offline_access), so an unattended run that finds it expired signs in again with MYAIR_EMAIL /
MYAIR_PASSWORD plus the DT cookie; Okta then skips MFA for a remembered device. If Okta still
demands a code unattended, the run logs it and returns None (never prompts).

SleepIQ: asyncsleepiq 1.7.2 (aiohttp). /rest/login key auth, /rest/sleeper to pick the side,
/rest/sleepData?interval=D1 for the night. Session file %USERPROFILE%\\.sleepiq\\session.json
remembers the chosen sleeper (id / name / side / bed). The login key is short-lived, so each
run logs in with SLEEPIQ_EMAIL / SLEEPIQ_PASSWORD (the library re-logs in on 401 itself).

Setup (interactive, once):
    python garmin_capacity.py --setup-myair      # prompts e-mail/password (+ e-mailed MFA code), saves tokens
    python garmin_capacity.py --setup-sleepiq    # prompts e-mail/password, lists sleepers, asks which side
Both write the credentials to .env (gitignored) when they are not already set and never echo them.
"""
from __future__ import annotations

import asyncio
import base64
import getpass
import hashlib
import json
import logging
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urldefrag

log = logging.getLogger("capacity.extra")

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
MYAIR_TOKEN_FILE = Path(os.getenv("MYAIR_TOKENS", str(Path.home() / ".myair" / "tokens.json")))
SLEEPIQ_SESSION_FILE = Path(os.getenv("SLEEPIQ_SESSION", str(Path.home() / ".sleepiq" / "session.json")))
HTTP_TIMEOUT = 20

# ───────────────────────────── myAir (ResMed, NA region) ─────────────────────────────
MYAIR_OKTA = "resmed-ext-1.okta.com"
MYAIR_AUTH_SERVER = "aus4ccsxvnidQgLmA297"
MYAIR_CLIENT_ID = "0oa4ccq1v413ypROi297"
MYAIR_REDIRECT = "https://myair.resmed.com"
MYAIR_GRAPHQL = "https://graphql.myair-prd.dht.live/graphql"
MYAIR_API_KEY = "da2-cenztfjrezhwphdqtwtbpqvzui"
MYAIR_AUTHN = f"https://{MYAIR_OKTA}/api/v1/authn"
MYAIR_AUTHORIZE = f"https://{MYAIR_OKTA}/oauth2/{MYAIR_AUTH_SERVER}/v1/authorize"
MYAIR_TOKEN = f"https://{MYAIR_OKTA}/oauth2/{MYAIR_AUTH_SERVER}/v1/token"
MYAIR_INTROSPECT = f"https://{MYAIR_OKTA}/oauth2/{MYAIR_AUTH_SERVER}/v1/introspect"
MYAIR_RECORD_DAYS = 14          # sleepRecords window requested (days back from `day`)
MYAIR_GQL_HEADERS = {           # the web app sends these; AppSync rejects the call without them
    "rmdhandsetid": "02c1c662-c289-41fd-a9ae-196ff15b5166",
    "rmdlanguage": "en",
    "rmdhandsetmodel": "Chrome",
    "rmdhandsetosversion": "127.0.6533.119",
    "rmdproduct": "myAir",
    "rmdappversion": "1.0.0",
    "rmdhandsetplatform": "Web",
    "accept-language": "en-US,en;q=0.9",
}
SLEEP_RECORDS_QUERY = """query GetPatientSleepRecords {
  getPatientWrapper {
    patient { firstName }
    sleepRecords(startMonth: "START", endMonth: "END") {
      items {
        startDate totalUsage sleepScore usageScore ahiScore maskScore leakScore
        ahi maskPairCount leakPercentile sleepRecordPatientId __typename
      }
      __typename
    }
    __typename
  }
}"""


class MyAirMFARequired(Exception):
    pass


def _session():
    """requests.Session with the planner's immediate-ClientHello adapter when available
    (see garmin_capacity.py "HTTP hardening"); a plain session otherwise."""
    import requests
    s = requests.Session()
    try:
        from garmin_capacity import harden_requests_session
        harden_requests_session(s)
    except Exception:
        pass
    return s


def _jwt_payload(token: str) -> Dict[str, Any]:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except Exception:
        return {}


def _read_json(f: Path) -> Dict[str, Any]:
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_json(f: Path, data: Dict[str, Any]) -> None:
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _env_set(key: str, value: str) -> None:
    """Add KEY=value to .env (gitignored) if the key is absent; never echoes the value."""
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    if any(ln.split("=", 1)[0].strip() == key for ln in lines if "=" in ln and not ln.lstrip().startswith("#")):
        return
    lines.append(f"{key}={value}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ.setdefault(key, value)


class MyAirClient:
    def __init__(self):
        self.s = _session()
        self.state = _read_json(MYAIR_TOKEN_FILE)
        dt = self.state.get("dt")
        if dt:
            self.s.cookies.set("DT", dt, domain=MYAIR_OKTA)

    # -- auth --
    def _token_active(self) -> bool:
        tok = self.state.get("access_token")
        if not tok:
            return False
        try:
            r = self.s.post(MYAIR_INTROSPECT, data={"client_id": MYAIR_CLIENT_ID, "token_type_hint": "access_token",
                                                    "token": tok}, timeout=HTTP_TIMEOUT)
            return bool(r.ok and r.json().get("active") is True)
        except Exception as e:
            log.warning("myAir introspect failed: %s", e)
            return False

    def _save(self) -> None:
        dt = self.s.cookies.get("DT", domain=MYAIR_OKTA) or self.s.cookies.get("DT") or self.state.get("dt")
        self.state["dt"] = dt
        self.state["saved_at"] = datetime.now().isoformat(timespec="seconds")
        _write_json(MYAIR_TOKEN_FILE, self.state)

    def _authn(self, email: str, password: str, interactive: bool) -> str:
        """Okta classic authn -> sessionToken (handles the e-mailed MFA code when interactive)."""
        if not self.s.cookies.get("DT"):
            # prime the DT (device token) cookie the way the web app does
            self.s.get(MYAIR_AUTHORIZE, allow_redirects=False, timeout=HTTP_TIMEOUT)
        r = self.s.post(MYAIR_AUTHN, json={"username": email, "password": password},
                        headers={"Accept": "application/json"}, timeout=HTTP_TIMEOUT)
        j = r.json() if r.content else {}
        status = j.get("status")
        if status == "SUCCESS":
            return j["sessionToken"]
        if status != "MFA_REQUIRED":
            raise RuntimeError(f"myAir login failed: HTTP {r.status_code} {j.get('errorSummary') or status}")
        if not interactive:
            raise MyAirMFARequired("myAir (Okta) wants an e-mailed MFA code; run: python garmin_capacity.py --setup-myair")
        state_token = j["stateToken"]
        factors = j.get("_embedded", {}).get("factors") or []
        factor = next((f for f in factors if f.get("factorType") == "email"), factors[0] if factors else {})
        verify = (factor.get("_links", {}).get("verify", {}).get("href")
                  or f"https://{MYAIR_OKTA}/api/v1/authn/factors/{factor.get('id')}/verify")
        verify += ("&" if "?" in verify else "?") + "rememberDevice=true"
        self.s.post(verify, json={"passCode": "", "stateToken": state_token}, timeout=HTTP_TIMEOUT)  # sends the e-mail
        code = input("myAir MFA code (check your e-mail): ").strip()
        r = self.s.post(verify, json={"passCode": code, "stateToken": state_token}, timeout=HTTP_TIMEOUT)
        j = r.json() if r.content else {}
        if j.get("status") != "SUCCESS":
            raise RuntimeError(f"myAir MFA failed: {j.get('errorSummary') or j.get('status')}")
        return j["sessionToken"]

    def _exchange(self, session_token: str) -> None:
        verifier = re.sub("[^a-zA-Z0-9]+", "", base64.urlsafe_b64encode(os.urandom(40)).decode())
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        r = self.s.get(MYAIR_AUTHORIZE, allow_redirects=False, timeout=HTTP_TIMEOUT, params={
            "client_id": MYAIR_CLIENT_ID, "code_challenge": challenge, "code_challenge_method": "S256",
            "prompt": "none", "redirect_uri": MYAIR_REDIRECT, "response_mode": "fragment",
            "response_type": "code", "sessionToken": session_token, "scope": "openid profile email",
            "state": "abcdef"})
        loc = r.headers.get("location", "")
        code = parse_qs(urldefrag(loc).fragment).get("code", [None])[0]
        if not code:
            raise RuntimeError(f"myAir authorize gave no code (HTTP {r.status_code}, location {loc[:120]!r})")
        r = self.s.post(MYAIR_TOKEN, data={"client_id": MYAIR_CLIENT_ID, "redirect_uri": MYAIR_REDIRECT,
                                           "grant_type": "authorization_code", "code_verifier": verifier,
                                           "code": code},
                        headers={"Accept": "application/json"}, allow_redirects=False, timeout=HTTP_TIMEOUT)
        j = r.json()
        if "access_token" not in j:
            raise RuntimeError(f"myAir token exchange failed: {j.get('error_description') or j}")
        self.state.update(access_token=j["access_token"], id_token=j.get("id_token"),
                          country=_jwt_payload(j.get("id_token", "")).get("myAirCountryId") or self.state.get("country"),
                          expires_in=j.get("expires_in"))
        self._save()

    def connect(self, interactive: bool = False) -> None:
        if self._token_active():
            return
        email = os.getenv("MYAIR_EMAIL") or (input("myAir e-mail: ").strip() if interactive else "")
        password = os.getenv("MYAIR_PASSWORD") or (getpass.getpass("myAir password: ") if interactive else "")
        if not email or not password:
            raise RuntimeError("myAir token expired and MYAIR_EMAIL/MYAIR_PASSWORD not set; run --setup-myair")
        self._exchange(self._authn(email, password, interactive))
        if interactive:
            _env_set("MYAIR_EMAIL", email)
            _env_set("MYAIR_PASSWORD", password)

    # -- data --
    def sleep_records(self, start: date, end: date) -> List[Dict[str, Any]]:
        q = SLEEP_RECORDS_QUERY.replace("START", start.isoformat()).replace("END", end.isoformat())
        headers = dict(MYAIR_GQL_HEADERS, **{
            "x-api-key": MYAIR_API_KEY, "Authorization": f"Bearer {self.state['access_token']}",
            "rmdcountry": self.state.get("country") or "US"})
        r = self.s.post(MYAIR_GRAPHQL, json={"operationName": "GetPatientSleepRecords", "variables": {}, "query": q},
                        headers=headers, timeout=HTTP_TIMEOUT)
        j = r.json()
        if j.get("errors"):
            raise RuntimeError(f"myAir GraphQL error: {j['errors'][0].get('message')}")
        items = (((j.get("data") or {}).get("getPatientWrapper") or {}).get("sleepRecords") or {}).get("items") or []
        return sorted(items, key=lambda it: it.get("startDate") or "")


def _cpap_from_record(rec: Dict[str, Any]) -> Dict[str, Any]:
    f = lambda k: rec.get(k) if isinstance(rec.get(k), (int, float)) and not isinstance(rec.get(k), bool) else None
    return {
        "usage_min": int(f("totalUsage")) if f("totalUsage") is not None else None,
        "ahi": round(float(f("ahi")), 1) if f("ahi") is not None else None,
        "leak_95": round(float(f("leakPercentile")), 1) if f("leakPercentile") is not None else None,
        "mask_score": f("maskScore"),
        "myair_score": f("sleepScore"),
        "date": rec.get("startDate"),
    }


def fetch_cpap(day: date, interactive: bool = False) -> Optional[Dict[str, Any]]:
    """Last night's myAir record for the morning `day`. myAir dates a night by the evening it
    started, so the record for `day - 1` is last night; `day` itself is accepted too (time zone
    / noon-boundary slack). Returns None (and logs) on any failure."""
    try:
        c = MyAirClient()
        c.connect(interactive=interactive)
        recs = c.sleep_records(day - timedelta(days=MYAIR_RECORD_DAYS), day)
        by_date = {r.get("startDate"): r for r in recs}
        rec = by_date.get((day - timedelta(days=1)).isoformat()) or by_date.get(day.isoformat())
        if rec is None:
            log.warning("myAir: no sleep record for %s (have %s)", day, [r.get("startDate") for r in recs[-3:]])
            return None
        out = _cpap_from_record(rec)
        if not out.get("usage_min"):
            log.warning("myAir: record %s has no usage (CPAP not used / not uploaded yet)", out["date"])
        return out
    except MyAirMFARequired as e:
        log.warning("myAir: %s", e)
    except Exception as e:
        log.warning("myAir fetch failed: %s: %s", type(e).__name__, e)
    return None


def setup_myair() -> int:
    """Interactive one-time login; prints the last 3 nights as proof. Never prints credentials."""
    try:
        c = MyAirClient()
        c.state.pop("access_token", None)          # force a real login so MFA/remember-device is exercised
        c.connect(interactive=True)
        recs = c.sleep_records(date.today() - timedelta(days=MYAIR_RECORD_DAYS), date.today())
        print(f"myAir login OK — tokens stored in {MYAIR_TOKEN_FILE} (DT remembered-device cookie: "
              f"{'yes' if c.state.get('dt') else 'NO — unattended re-login will need MFA'})")
        for r in recs[-3:]:
            print("  ", json.dumps(_cpap_from_record(r)))
        return 0
    except Exception as e:
        print(f"myAir setup FAILED: {type(e).__name__}: {e}")
        return 1


# ───────────────────────────── Sleep Number (SleepIQ) ─────────────────────────────
SLEEPIQ_QUERY_HOUR = 12         # the API works in UTC; noon local on the wake day returns last night


def _to_local_iso(s: Optional[str]) -> Optional[str]:
    """SleepIQ session timestamps -> naive local 'YYYY-MM-DDTHH:MM' (UTC/offset-aware input is converted)."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt.isoformat(timespec="minutes")
    except Exception:
        return str(s)


async def _sleepiq_sleepers(api) -> List[Dict[str, Any]]:
    data = await api.get("sleeper")
    return [{"sleeper_id": s.get("sleeperId"), "bed_id": s.get("bedId"), "side": s.get("side"),
             "name": s.get("firstName"), "active": s.get("active")} for s in data.get("sleepers", [])]


def _pick_sleeper(sleepers: List[Dict[str, Any]], saved: Dict[str, Any], interactive: bool) -> Dict[str, Any]:
    want = os.getenv("SLEEPIQ_SLEEPER") or saved.get("name")
    want_id = saved.get("sleeper_id")
    for s in sleepers:
        if want_id and s["sleeper_id"] == want_id:
            return s
    for s in sleepers:
        if want and str(s.get("name", "")).lower() == str(want).lower():
            return s
    if len(sleepers) == 1:
        return sleepers[0]
    if not interactive:
        raise RuntimeError(f"{len(sleepers)} SleepIQ sleepers and none chosen; run --setup-sleepiq "
                           f"(names: {[s.get('name') for s in sleepers]})")
    print("SleepIQ sleepers on this account:")
    for i, s in enumerate(sleepers, 1):
        side = {0: "left", 1: "right"}.get(s.get("side"), s.get("side"))
        print(f"  {i}. {s.get('name')}  ({side} side, bed {s.get('bed_id')})")
    while True:
        pick = input("Which one is Darren? [number]: ").strip()
        if pick.isdigit() and 1 <= int(pick) <= len(sleepers):
            return sleepers[int(pick) - 1]


async def _sleepiq_fetch(day: date, email: str, password: str, interactive: bool) -> Dict[str, Any]:
    import aiohttp
    from asyncsleepiq import AsyncSleepIQ, SleepIQSleeper
    from asyncsleepiq.consts import Side

    saved = _read_json(SLEEPIQ_SESSION_FILE)
    async with aiohttp.ClientSession() as http:
        api = AsyncSleepIQ(email, password, client_session=http)
        await api.login()
        sleepers = await _sleepiq_sleepers(api)
        if not sleepers:
            raise RuntimeError("SleepIQ account has no sleepers")
        chosen = _pick_sleeper(sleepers, saved, interactive)
        if chosen != saved.get("chosen"):
            _write_json(SLEEPIQ_SESSION_FILE, {**{k: v for k, v in chosen.items() if k != "active"},
                                               "chosen": chosen, "saved_at": datetime.now().isoformat(timespec="seconds")})
        side = Side.LEFT if chosen.get("side") == 0 else Side.RIGHT
        sl = SleepIQSleeper(api, chosen["bed_id"], chosen["sleeper_id"], side)
        sd = await sl.get_sleep_data(datetime.combine(day, datetime.min.time()).replace(hour=SLEEPIQ_QUERY_HOUR))
    if sd is None:
        raise RuntimeError(f"SleepIQ returned no sleepData for {day}")
    mins = lambda sec: round(sec / 60) if isinstance(sec, (int, float)) and not isinstance(sec, bool) else None
    return {
        "sleepiq_score": sd.sleep_score,
        "in_bed_min": mins(sd.duration),
        "restful_min": mins(sd.restful),
        "restless_min": mins(sd.restless),
        "out_of_bed_min": mins(sd.out_of_bed),
        "avg_hr": sd.heart_rate,
        "avg_hrv": sd.hrv,
        "avg_rr": sd.respiratory_rate,
        "bed_entry": _to_local_iso(sd.start_date),
        "bed_exit": _to_local_iso(sd.end_date),
        "sleeper": chosen.get("name"),
        "sessions": sd.session_count,
        "date": day.isoformat(),
    }


def fetch_sleepnumber(day: date, interactive: bool = False) -> Optional[Dict[str, Any]]:
    """Last night's SleepIQ night for the morning `day` (chosen sleeper only). None + log on failure."""
    email = os.getenv("SLEEPIQ_EMAIL") or (input("SleepIQ e-mail: ").strip() if interactive else "")
    password = os.getenv("SLEEPIQ_PASSWORD") or (getpass.getpass("SleepIQ password: ") if interactive else "")
    if not email or not password:
        log.warning("SleepIQ: SLEEPIQ_EMAIL/SLEEPIQ_PASSWORD not set; run --setup-sleepiq")
        return None
    try:
        out = asyncio.run(_sleepiq_fetch(day, email, password, interactive))
        if interactive:
            _env_set("SLEEPIQ_EMAIL", email)
            _env_set("SLEEPIQ_PASSWORD", password)
        if out.get("in_bed_min") is None and out.get("sleepiq_score") is None:
            log.warning("SleepIQ: empty night for %s (not in bed / not uploaded yet)", day)
            return None
        return out
    except Exception as e:
        log.warning("SleepIQ fetch failed: %s: %s", type(e).__name__, e)
        return None


def setup_sleepiq() -> int:
    """Interactive: login, choose the sleeper (asks if more than one), print last night as proof."""
    out = fetch_sleepnumber(date.today(), interactive=True)
    if out is None:
        print("SleepIQ setup FAILED (see log line above)")
        return 1
    print(f"SleepIQ login OK — sleeper saved in {SLEEPIQ_SESSION_FILE}")
    print("  ", json.dumps(out))
    return 0


def sleep_min_from_sleepnumber(sn: Optional[Dict[str, Any]]) -> Optional[int]:
    """Asleep estimate = in_bed - restless - out_of_bed (operator's rule, 2026-10-23)."""
    if not sn or sn.get("in_bed_min") is None:
        return None
    v = sn["in_bed_min"] - (sn.get("restless_min") or 0) - (sn.get("out_of_bed_min") or 0)
    return max(0, int(v))


if __name__ == "__main__":          # quick manual check: python sources_extra.py [YYYY-MM-DD]
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        from dotenv import load_dotenv
        load_dotenv(ENV_FILE, override=False)
    except Exception:
        pass
    d = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date.today()
    print("cpap       :", json.dumps(fetch_cpap(d)))
    print("sleepnumber:", json.dumps(fetch_sleepnumber(d)))
