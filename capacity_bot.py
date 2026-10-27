#!/usr/bin/env python3
"""
Capacity Planner — Telegram bot (@dp_health_consultant_bot, "Health Consultant")
================================================================================
Standalone, separate from CrystalBull (moved off @crystalbull_ai_bot 2026-10-18).

Long-polls getUpdates with stdlib urllib and the pre-built TLS context from garmin_capacity.py: on this PC plain
`requests` black-holes because it sends the ClientHello late (see garmin_capacity.py "HTTP hardening", b20bfce).

Commands (owner chat only; TELEGRAM_CHAT_ID in this folder's .env, never anyone else):
  /capacity  run `garmin_capacity.py --now`; it posts the NOW card itself (and mails [CAPACITY-NOW])
  /today     repost this morning's card from data\\latest.json
  /drinks N  log last night's / tonight's alcohol (0-10, halves OK) to data\\alcohol.csv — record-only.
             Before 14:00 the entry is for LAST night (yesterday's date), from 14:00 on for TONIGHT (today's
             date); an explicit date wins: /drinks 2 10/22. Re-logging a night appends a new row; the
             morning run takes the last row (garmin_capacity.alcohol_for_night). Never assumes 0.
  /checkin   start the nightly check-in now (see below)
  /help      list the commands
Anything else gets a one-line hint. The commands are registered with setMyCommands at start.

Nightly check-in (2026-10-25, display only): at CHECKIN_TIME (default 23:15, .env CHECKIN_TIME=HH:MM) the bot
asks five inline-button questions: drinks today (None/1/2/3/4+; shows what /drinks already logged; the answer
sets the day's total and is written to data\\alcohol.csv with source "checkin"), any gluten today (No/Yes/Not sure
-> no|yes|unsure, 2026-10-27), NEW open wounds (No/Yes ->
1/2/3+ -> sites multi-select -> Done), older wounds (None open/Healing/Getting worse), exercise (No/Yes -> types
multi-select -> Done -> <15/15-30/30-60/60+). One nudge CHECKIN_NUDGE_MIN after the last prompt; still silent
CHECKIN_SKIP_MIN after it -> the row is logged as "skipped" with the answers given so far. The day follows the
/drinks rule (checkin_day: before 14:00 = yesterday). Rows: data\\checkin.csv (garmin_capacity.CHECKIN_FIELDS,
append-only, last row per day wins). The in-flight session is persisted in data\\checkin_state.json so a bot
restart resumes it. A missed 23:15 start is made up until CHECKIN_WINDOW_H later. Backfill / manual rows:
python capacity_bot.py --checkin-log 2026-10-24 new_wounds=yes new_wound_count=unknown new_wound_sites=Scalp exercise=no

Safety and liveness:
  * single instance: data\\capacity_bot.lock holds the PID; a second copy exits 0 if the holder is alive.
  * heartbeat: data\\bot_heartbeat (epoch, ISO time, last update id, last command) every poll cycle.
  * the getUpdates offset is persisted in data\\capacity_bot_state.json; commands queued while the bot was down
    are answered if they are less than STALE_S old, older ones are acknowledged and skipped.
  * this process never touches CrystalBull's bot token; it is the ONE poller on its own token.

Run:  python capacity_bot.py            (Task Scheduler: \\Capacity\\Capacity_Bot, at logon + at startup, S4U)
      python capacity_bot.py --once     (one poll cycle, for a manual check)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import garmin_capacity as gc   # load_env, card(), _TLS_CTX, DATA_DIR

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = gc.DATA_DIR
LOCK_FILE = DATA_DIR / "capacity_bot.lock"
HEARTBEAT_FILE = DATA_DIR / "bot_heartbeat"
STATE_FILE = DATA_DIR / "capacity_bot_state.json"
POLL_TIMEOUT_S = 30
STALE_S = 600                 # a command older than this when first seen is skipped (bot was down)
NOW_TIMEOUT_S = 240
LOCK_STALE_S = 120           # a live lock holder whose last sign of life (heartbeat / lock write) is older -> taken over
LOCK_WAIT_S = 15             # a live, fresh holder gets this long to exit (Stop-ScheduledTask returns before it is gone)
HEARTBEAT_EVERY_S = 30       # backoff sleeps still beat this often, so a waiting bot never looks stale
COMMANDS = [
    {"command": "capacity", "description": "Current Body Battery, stress, HR (live Garmin pull)"},
    {"command": "today", "description": "This morning's capacity card"},
    {"command": "drinks", "description": "Log alcohol: /drinks N [M/D] (0-10; before 2 PM = last night)"},
    {"command": "checkin", "description": "Nightly check-in now: drinks, gluten, new/older wounds, exercise (buttons)"},
    {"command": "sweat", "description": "Night-sweat question for last night (buttons)"},
    {"command": "drink", "description": "Log a drink now (or just text: beer, 2 wine, bourbon)"},
    {"command": "undo", "description": "Remove the last drink you texted"},
    {"command": "help", "description": "List the commands"},
]
HINT = "Unknown command. Try /capacity, /today, /drinks, /drink, /undo, /checkin, /sweat or /help."
DRINKS_USAGE = ("Usage: /drinks N [M/D]  — N = 0-10 drinks (halves OK, e.g. 1.5). "
                "Before 2 PM it counts for last night, from 2 PM on for tonight; add a date to override: /drinks 2 10/22")

log = logging.getLogger("capacity_bot")
TASK_LOG = DATA_DIR / "task.log"


class AppendPerRecordHandler(logging.Handler):
    """Append one record to data\\task.log and close the file at once.

    The bot runs for days. A long-lived handle on task.log (e.g. the task action's
    `cmd /c ... >> task.log` redirect) locks the file, and then Capacity_Morning /
    Retry / Now, which append through their own cmd redirect, fail with "file in use"
    (observed 2026-10-18 16:58, Capacity_Now LastTaskResult 1). So the task runs
    python directly and the bot never holds the log open between lines."""

    def emit(self, record):
        try:
            line = self.format(record) + "\n"
            for _ in range(3):
                try:
                    with open(TASK_LOG, "a", encoding="utf-8") as f:
                        f.write(line)
                    return
                except PermissionError:      # another task is appending this instant
                    time.sleep(0.2)
        except Exception:
            self.handleError(record)


# ───────────────────────────── Telegram (stdlib urllib) ─────────────────────────────
class TelegramError(Exception):
    def __init__(self, status: int, desc: str):
        super().__init__(f"HTTP {status}: {desc}")
        self.status = status


def api(method: str, payload: Optional[dict] = None, timeout: float = 15) -> Any:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    body = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=gc._TLS_CTX) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            desc = json.loads(e.read().decode("utf-8")).get("description", "")
        except Exception:
            desc = ""
        raise TelegramError(e.code, desc) from None
    if not data.get("ok"):
        raise TelegramError(200, data.get("description", "ok:false"))
    return data.get("result")


def send(chat_id: int, text: str) -> bool:
    try:
        api("sendMessage", {"chat_id": chat_id, "text": text[:4000]})
        return True
    except Exception as e:
        log.warning("sendMessage failed: %s", e)
        return False


# ───────────────────────────── single instance + heartbeat + state ─────────────────────────────
def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return bool(ok) and code.value == 259                         # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _holder_last_seen(holder: int) -> float:
    """Epoch of the lock holder's last sign of life: the lock file's mtime (a just-started holder has no heartbeat
    yet) or its own heartbeat line (a heartbeat written by another PID does not count). 0 when neither exists."""
    seen = 0.0
    try:
        seen = LOCK_FILE.stat().st_mtime
    except OSError:
        pass
    try:
        lines = HEARTBEAT_FILE.read_text(encoding="utf-8").splitlines()
        m = re.search(r"\bpid=(\d+)", lines[2]) if len(lines) >= 3 else None
        if m and int(m.group(1)) == holder:
            seen = max(seen, float(lines[0]))
    except Exception:
        pass
    return seen


def acquire_lock(wait_s: float = LOCK_WAIT_S) -> bool:
    """True if this process now holds the lock. Taken over when the holder PID is dead, or alive but silent for more
    than LOCK_STALE_S (hung, or a reused PID). A live holder with a fresh heartbeat gets wait_s to exit (a restart's
    Start can fire while the stopped bot is still exiting, 2026-10-27); still there -> False (this copy exits)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + wait_s
    waited = False
    for _ in range(4 * int(wait_s + 1) + 3):
        try:
            fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                holder = int((LOCK_FILE.read_text().strip() or "0"))
            except Exception:
                holder = 0
            if holder != os.getpid() and _pid_alive(holder):
                age = time.time() - _holder_last_seen(holder)
                if age <= LOCK_STALE_S:
                    if time.time() < deadline:
                        if not waited:
                            log.info("lock held by live PID %s; waiting up to %ss for it to exit", holder, wait_s)
                            waited = True
                        time.sleep(0.5)
                        continue
                    log.info("another capacity_bot holds the lock (PID %s, last seen %.0fs ago); exiting", holder, age)
                    return False
                log.warning("lock holder PID %s is alive but silent for %.0fs (> %ss); taking the lock over",
                            holder, age, LOCK_STALE_S)
            else:
                log.warning("stale lock (PID %s not running); taking it over", holder)
            try:
                LOCK_FILE.unlink()
            except FileNotFoundError:
                pass
            except Exception as e:
                log.error("cannot remove the stale lock (%s); exiting", e)
                return False
    return False


def release_lock() -> None:
    try:
        if LOCK_FILE.read_text().strip() == str(os.getpid()):
            LOCK_FILE.unlink()
    except Exception:
        pass


def write_heartbeat(last_update_id: Optional[int], last_cmd: str) -> None:
    try:
        tmp = HEARTBEAT_FILE.with_suffix(".tmp")
        tmp.write_text(f"{time.time():.0f}\n{datetime.now().isoformat(timespec='seconds')}\n"
                       f"pid={os.getpid()} last_update_id={last_update_id} last_cmd={last_cmd}\n", encoding="utf-8")
        os.replace(tmp, HEARTBEAT_FILE)
    except Exception as e:
        log.warning("heartbeat write failed: %s", e)


def _sleep_beating(seconds: float, last_update_id: Optional[int], last_cmd: str) -> None:
    """time.sleep that keeps the heartbeat fresh (a 300 s backoff must not make the bot look stale to acquire_lock)."""
    end = time.time() + seconds
    while (left := end - time.time()) > 0:
        time.sleep(min(HEARTBEAT_EVERY_S, left))
        write_heartbeat(last_update_id, last_cmd)


def load_offset() -> Optional[int]:
    try:
        return int(json.loads(STATE_FILE.read_text(encoding="utf-8"))["offset"])
    except Exception:
        return None


def save_offset(offset: int) -> None:
    try:
        STATE_FILE.write_text(json.dumps({"offset": offset, "saved_at": datetime.now().isoformat(timespec="seconds")}),
                              encoding="utf-8")
    except Exception as e:
        log.warning("state write failed: %s", e)


# ───────────────────────────── commands ─────────────────────────────
def parse_command(text: str) -> Optional[str]:
    """'/Capacity@dp_health_consultant_bot now' -> 'capacity'; non-commands -> None."""
    t = (text or "").strip()
    if not t.startswith("/"):
        return None
    return t[1:].split()[0].split("@")[0].lower() if len(t) > 1 else ""


def cmd_capacity(chat_id: int) -> str:
    send(chat_id, "Pulling current Garmin readings…")
    try:
        r = subprocess.run([sys.executable, str(BASE_DIR / "garmin_capacity.py"), "--now"], cwd=str(BASE_DIR),
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=NOW_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        send(chat_id, f"⚠️ Garmin pull timed out after {NOW_TIMEOUT_S}s")
        return "timeout"
    out, err = (r.stdout or "").strip(), (r.stderr or "").strip()
    if r.returncode != 0:
        tail = "\n".join((err or out).splitlines()[-4:])
        send(chat_id, f"🔴 Capacity pull failed (exit {r.returncode})\n{tail}"[:1500])
        return f"exit {r.returncode}"
    if "telegram=True" not in err and out:
        send(chat_id, out + "\n(card relayed: the planner's own send failed)")   # fallback only
    return "ok"


def today_card() -> str:
    f = DATA_DIR / "latest.json"
    if not f.exists():
        return "No morning card yet (data\\latest.json missing)."
    try:
        payload = json.loads(f.read_text(encoding="utf-8"))
        text = gc.card(payload)
    except Exception as e:
        return f"Morning card unreadable: {type(e).__name__}: {e}"[:300]
    if payload.get("date") != str(date.today()):
        text = f"(last card is from {payload.get('date')}; today's 09:00 pull has not run yet)\n" + text
    return text


def parse_drinks(args: str, now: datetime) -> tuple:
    """'2' / '1.5 10/22' / '0 2026-10-22' -> (night_date, drinks). ValueError on anything else.
    Night attribution without a date: before ALCOHOL_CUTOVER_H (14:00) = last night (yesterday's date),
    from 14:00 on = tonight (today's date). M/D without a year = the most recent such date not after today."""
    parts = (args or "").split()
    if not parts or len(parts) > 2:
        raise ValueError("need N [date]")
    try:
        n = float(parts[0])
    except ValueError:
        raise ValueError("N must be a number") from None
    if not (0 <= n <= gc.ALCOHOL_MAX_DRINKS) or (n * 2) != int(n * 2):
        raise ValueError("N must be 0-10 in halves")
    if len(parts) == 2:
        night = _parse_night(parts[1], now.date())
    else:
        night = now.date() - timedelta(days=1) if now.hour < gc.ALCOHOL_CUTOVER_H else now.date()
    return night, n


def _parse_night(txt: str, today: date) -> date:
    t = txt.strip().lower()
    if t in ("yesterday", "lastnight", "last-night"):
        return today - timedelta(days=1)
    if t in ("today", "tonight"):
        return today
    try:
        return date.fromisoformat(t)
    except ValueError:
        pass
    bits = t.split("/")
    if len(bits) in (2, 3) and all(b.isdigit() for b in bits):
        m, d = int(bits[0]), int(bits[1])
        if len(bits) == 3:
            y = int(bits[2]) + (2000 if int(bits[2]) < 100 else 0)
            return date(y, m, d)
        cand = date(today.year, m, d)
        return cand if cand <= today else date(today.year - 1, m, d)
    raise ValueError("date must be M/D, M/D/YYYY or YYYY-MM-DD")


def cmd_drinks(args: str, now: Optional[datetime] = None) -> str:
    """Reply text for /drinks. Logs to data\\alcohol.csv; bad input -> usage hint (nothing logged)."""
    now = now or datetime.now()
    try:
        night, n = parse_drinks(args, now)
    except ValueError as e:
        return f"{DRINKS_USAGE}\n({e})"
    try:
        gc.alcohol_log(night, n, source="telegram", at=now)
    except Exception as e:
        log.warning("alcohol log write failed: %s: %s", type(e).__name__, e)
        return f"⚠️ Could not write data\\alcohol.csv ({type(e).__name__}); nothing logged."
    unit = "drink" if n == 1 else "drinks"
    return f"Logged {n:g} {unit} for {night.strftime('%a')} {night.month}/{night.day}."


def help_text() -> str:
    return "Health Consultant — capacity planner\n" + "\n".join(f"/{c['command']} - {c['description']}" for c in COMMANDS)


# ───────────────────────────── nightly check-in (inline buttons) ─────────────────────────────
CHECKIN_TIME = os.getenv("CHECKIN_TIME", "23:15")   # wind-down start, HH:MM local (.env CHECKIN_TIME overrides)
CHECKIN_NUDGE_MIN = 45                               # one nudge this long after the last prompt / answer ...
CHECKIN_SKIP_MIN = 90                                # ... and logged as skipped this long after it (45 min past the nudge)
CHECKIN_WINDOW_H = 6                                 # a missed start (bot down) is still made up this long after CHECKIN_TIME
CHECKIN_STATE_FILE = DATA_DIR / "capacity_checkin_state.json"   # this bot only (checkin_state.json belonged to the retired checkin_bot)
SWEAT_NUDGE_MIN = 45                                  # one nudge this long after the morning night-sweat prompt
SWEAT_LABEL = {"none": "No sweating", "damp": "Damp (clothes/pillow)", "soaked": "Soaked the sheets"}
WOUND_SITES = ["Face/mouth", "Scalp", "Hands/arms", "Legs/feet", "Torso/back", "Other"]
EXERCISE_TYPES = ["Strength training", "Treadmill", "Walking outside", "Elliptical", "Biking", "Hiking"]
EXERCISE_MINUTES = ["<15", "15-30", "30-60", "60+"]
CHECKIN_NEXT = {"drinks": "gluten", "gluten": "wounds_new", "wounds_new": {"no": "wounds_older", "yes": "wounds_count"},
                "wounds_count": "wounds_sites", "wounds_sites": "wounds_older", "wounds_older": "exercise",
                "exercise": {"no": "done", "yes": "exercise_types"}, "exercise_types": "exercise_minutes",
                "exercise_minutes": "done"}
CHECKIN_MULTI = {"wounds_sites": ("new_wound_sites", WOUND_SITES), "exercise_types": ("exercise_types", EXERCISE_TYPES)}
CHECKIN_FIELD = {"drinks": "drinks", "gluten": "gluten", "wounds_new": "new_wounds", "wounds_count": "new_wound_count",
                 "wounds_older": "older_wounds", "exercise": "exercise", "exercise_minutes": "exercise_minutes"}
CHECKIN_LABEL = {"0": "None", "no": "No", "yes": "Yes", "none": "None open", "healing": "Healing", "worse": "Getting worse",
                 "unsure": "Not sure"}
CHECKIN_CLI_FIELDS = gc.CHECKIN_FIELDS[2:10] + ["gluten"]   # --checkin-log answer fields

_session: Optional[Dict[str, Any]] = None   # the one in-flight evening check-in (owner chat only)
_sweat: Optional[Dict[str, Any]] = None     # the one in-flight morning night-sweat question


def checkin_day(now: datetime) -> date:
    """Same rule as /drinks: before 14:00 the check-in belongs to yesterday (the evening that just ended),
    from 14:00 on to today — so the check-in's drinks and /drinks always name the same night."""
    return now.date() - timedelta(days=1) if now.hour < gc.ALCOHOL_CUTOVER_H else now.date()


def _day_label(d: date) -> str:
    return f"{d.strftime('%a')} {d.month}/{d.day}"


def _kb(rows) -> Dict[str, Any]:
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}


def _multi_rows(step: str, options: List[str], chosen: List[str]):
    rows = [[(("☑ " if o in chosen else "☐ ") + o, f"ci|{step}|{j}") for j, o in list(enumerate(options))[i:i + 2]]
            for i in range(0, len(options), 2)]
    rows.append([("Done ✓", f"ci|{step}|done")])
    return rows


def checkin_question(s: Dict[str, Any]):
    """(text, button rows) for the session's current step."""
    step, a, d = s["step"], s["answers"], date.fromisoformat(s["day"])
    if step == "drinks":
        prior = gc.alcohol_for_night(d)
        note = f"(/drinks logged {prior:g} so far)" if prior is not None else "(nothing logged via /drinks yet)"
        return (f"Check-in {_day_label(d)} — 1/5  Drinks today? {note}",
                [[("None", "ci|drinks|0"), ("1", "ci|drinks|1"), ("2", "ci|drinks|2"), ("3", "ci|drinks|3"), ("4+", "ci|drinks|4+")]])
    if step == "gluten":
        return "2/5  Any gluten today?", [[("No", "ci|gluten|no"), ("Yes", "ci|gluten|yes"), ("Not sure", "ci|gluten|unsure")]]
    if step == "wounds_new":
        return "3/5  NEW open wounds today?", [[("No", "ci|wounds_new|no"), ("Yes", "ci|wounds_new|yes")]]
    if step == "wounds_count":
        return "How many new wounds?", [[("1", "ci|wounds_count|1"), ("2", "ci|wounds_count|2"), ("3+", "ci|wounds_count|3+")]]
    if step == "wounds_sites":
        return "Where? Tap all that apply, then Done.", _multi_rows("wounds_sites", WOUND_SITES, a.get("new_wound_sites") or [])
    if step == "wounds_older":
        return "4/5  Older wounds?", [[("None open", "ci|wounds_older|none"), ("Healing", "ci|wounds_older|healing"),
                                      ("Getting worse", "ci|wounds_older|worse")]]
    if step == "exercise":
        return "5/5  Exercise today?", [[("No", "ci|exercise|no"), ("Yes", "ci|exercise|yes")]]
    if step == "exercise_types":
        return "What kind? Tap all that apply, then Done.", _multi_rows("exercise_types", EXERCISE_TYPES, a.get("exercise_types") or [])
    if step == "exercise_minutes":
        return "How long?", [[(m, f"ci|exercise_minutes|{m}") for m in EXERCISE_MINUTES]]
    raise ValueError(f"no question for step {step}")


def _save_session() -> None:
    try:
        CHECKIN_STATE_FILE.write_text(json.dumps({"evening": _session, "sweat": _sweat}), encoding="utf-8")
    except Exception as e:
        log.warning("check-in state write failed: %s", e)


def _load_session() -> None:
    global _session, _sweat
    _session = _sweat = None
    try:
        s = json.loads(CHECKIN_STATE_FILE.read_text(encoding="utf-8")) if CHECKIN_STATE_FILE.exists() else {}
        ev, sw = s.get("evening"), s.get("sweat")
        _session = ev if ev and ev.get("step") else None
        _sweat = sw if sw and sw.get("day") else None
        if _session:
            log.info("check-in for %s resumed at step %s", _session["day"], _session["step"])
        if _sweat:
            log.info("night-sweat question for %s resumed", _sweat["day"])
    except Exception as e:
        log.warning("check-in state unreadable (%s); starting without one", e)
        _session = _sweat = None


def _ask(s: Dict[str, Any], now: datetime, reset_clock: bool = True) -> None:
    text, rows = checkin_question(s)
    s["q_text"] = text
    try:
        r = api("sendMessage", {"chat_id": s["chat_id"], "text": text[:4000], "reply_markup": _kb(rows)})
        s["msg_id"] = (r or {}).get("message_id")
    except Exception as e:
        log.warning("check-in question failed: %s", e)
        s["msg_id"] = None
    if reset_clock:
        s["last_prompt_at"] = now.isoformat(timespec="seconds")
        s["nudged"] = False
    _save_session()


def _edit(chat_id, message_id, text: Optional[str] = None, rows=None) -> None:
    if not message_id:
        return
    try:
        if text is not None:
            body = {"chat_id": chat_id, "message_id": message_id, "text": text[:4000]}
            if rows is not None:
                body["reply_markup"] = _kb(rows)
            api("editMessageText", body)
        elif rows is not None:
            api("editMessageReplyMarkup", {"chat_id": chat_id, "message_id": message_id, "reply_markup": _kb(rows)})
    except Exception as e:
        log.warning("edit failed: %s", e)


def _answer_cb(cq_id, text: Optional[str] = None) -> None:
    try:
        api("answerCallbackQuery", {"callback_query_id": cq_id, **({"text": text} if text else {})})
    except Exception as e:
        log.warning("answerCallbackQuery failed: %s", e)


def start_checkin(chat_id, now: Optional[datetime] = None, trigger: str = "command") -> None:
    global _session
    now = now or datetime.now()
    _session = {"day": checkin_day(now).isoformat(), "chat_id": chat_id, "started_at": now.isoformat(timespec="seconds"),
                "step": "drinks", "answers": {}, "msg_id": None, "q_text": "", "last_prompt_at": now.isoformat(timespec="seconds"),
                "nudged": False, "trigger": trigger}
    log.info("check-in %s started (%s)", _session["day"], trigger)
    _ask(_session, now)


def apply_answer(s: Dict[str, Any], step: str, value: str) -> bool:
    """Record one button press. Returns True when the step is complete (False = a multi-select toggle)."""
    a = s["answers"]
    if step in CHECKIN_MULTI:
        key, opts = CHECKIN_MULTI[step]
        lst = a.setdefault(key, [])
        if value != "done":
            o = opts[int(value)]
            lst.remove(o) if o in lst else lst.append(o)
            return False
    else:
        a[CHECKIN_FIELD[step]] = value
    nxt = CHECKIN_NEXT[step]
    s["step"] = nxt[value] if isinstance(nxt, dict) else nxt
    return True


def finish_checkin(s: Dict[str, Any], now: datetime, status: str) -> Dict[str, Any]:
    """Write the checkin.csv row (+ the drinks total to alcohol.csv when answered) and clear the session."""
    global _session
    day = date.fromisoformat(s["day"])
    row = gc.checkin_row(day, s["answers"], started_at=datetime.fromisoformat(s["started_at"]), finished_at=now,
                         status=status, trigger=s.get("trigger", ""), source="telegram")
    try:
        gc.checkin_log(row)
    except Exception as e:
        log.warning("check-in log write failed: %s: %s", type(e).__name__, e)
    n = gc.checkin_drinks_value(s["answers"].get("drinks"))
    if n is not None:
        try:
            gc.alcohol_log(day, n, source="checkin", at=now)
        except Exception as e:
            log.warning("alcohol log write failed: %s: %s", type(e).__name__, e)
    log.info("check-in %s %s: %s", s["day"], status, gc.checkin_line(gc.checkin_for_day(day)))
    _session = None
    _save_session()
    return row


def handle_callback(cq: Dict[str, Any], owner: str, now: Optional[datetime] = None) -> str:
    """One inline-button press. Owner only; a press for a finished / other step is acknowledged and ignored."""
    now = now or datetime.now()
    msg = cq.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    sender = (cq.get("from") or {}).get("id")
    if str(chat_id) != str(owner) or str(sender) != str(owner):
        log.warning("ignored callback from non-owner chat=%s from=%s", chat_id, sender)
        _answer_cb(cq.get("id"))
        return "ignored_non_owner"
    parts = (cq.get("data") or "").split("|")
    if len(parts) != 3 or parts[0] != "ci":
        _answer_cb(cq.get("id"))
        return "cb_unknown"
    step, value = parts[1], parts[2]
    if step == "sweat":
        return _sweat_callback(cq, chat_id, value, now)
    s = _session
    if not s or str(s.get("chat_id")) != str(chat_id) or s.get("step") != step:
        _answer_cb(cq.get("id"), "That check-in step is already finished — /checkin to redo it.")
        return "cb_stale"
    try:
        advanced = apply_answer(s, step, value)
    except (ValueError, IndexError, KeyError):
        _answer_cb(cq.get("id"))
        return "cb_bad"
    _answer_cb(cq.get("id"))
    if not advanced:
        _edit(chat_id, s.get("msg_id"), rows=checkin_question(s)[1])
        _save_session()
        return "cb_toggle"
    if step in CHECKIN_MULTI:
        label = ", ".join(s["answers"].get(CHECKIN_MULTI[step][0]) or []) or "none"
    else:
        label = CHECKIN_LABEL.get(value, value)
    _edit(chat_id, s.get("msg_id"), text=f"{s.get('q_text', '')}\n✓ {label}")
    if s["step"] == "done":
        finish_checkin(s, now, "done")
        send(chat_id, "Saved. " + gc.checkin_line(gc.checkin_for_day(date.fromisoformat(s["day"]))))
        return "checkin_done"
    _ask(s, now)
    return f"cb_{step}"


def _scheduled_start(day: date) -> datetime:
    """The wall-clock moment the check-in for `day` should start (an evening time belongs to that date,
    a CHECKIN_TIME before 14:00 to the following morning)."""
    h, m = map(int, CHECKIN_TIME.split(":"))
    at = datetime.combine(day, datetime.min.time()).replace(hour=h, minute=m)
    return at + timedelta(days=1) if h < gc.ALCOHOL_CUTOVER_H else at


def checkin_tick(owner: str, now: Optional[datetime] = None) -> str:
    """Called every poll cycle: nudge / skip an idle session, or start the scheduled one. Never raises."""
    now = now or datetime.now()
    try:
        s = _session
        if s:
            idle_min = (now - datetime.fromisoformat(s["last_prompt_at"])).total_seconds() / 60
            if not s.get("nudged") and idle_min >= CHECKIN_NUDGE_MIN:
                send(s["chat_id"], "Still up? One tap finishes tonight's check-in (it logs as skipped in 45 min).")
                _ask(s, now, reset_clock=False)
                s["nudged"] = True
                _save_session()
                return "nudged"
            if s.get("nudged") and idle_min >= CHECKIN_SKIP_MIN:
                finish_checkin(s, now, "skipped")
                send(s["chat_id"], "Check-in logged as skipped (no answer). /checkin any time to redo it.")
                return "skipped"
            return "waiting"
        day = checkin_day(now)
        start = _scheduled_start(day)
        if start <= now < start + timedelta(hours=CHECKIN_WINDOW_H) and not (gc.checkin_for_day(day) or {}).get("status"):
            start_checkin(int(owner), now, trigger="scheduled")
            return "started"
        return "idle"
    except Exception as e:
        log.warning("check-in tick failed: %s: %s", type(e).__name__, e)
        return "error"


def cmd_checkin(chat_id, now: Optional[datetime] = None) -> str:
    now = now or datetime.now()
    if _session and str(_session.get("chat_id")) == str(chat_id):
        _ask(_session, now)
        return "checkin_resend"
    start_checkin(chat_id, now, trigger="command")
    return "checkin_start"


# ───────────────────────────── morning night-sweat question ─────────────────────────────
def morning_time() -> str:
    return os.getenv("MORNING_TIME", "08:35")           # HH:MM local; the prompt for the night that just ended


def sweat_night(now: datetime) -> date:
    """The evening date of the night that just ended: yesterday (before 04:00, still up from the prior evening: two days ago)."""
    return now.date() - timedelta(days=2 if now.hour < 4 else 1)


def sweat_for_day(day: date) -> str:
    return (gc.checkin_for_day(day) or {}).get("night_sweat") or ""


def _sweat_rows():
    return [[(lbl, f"ci|sweat|{k}")] for k, lbl in SWEAT_LABEL.items()]


def _sweat_ask(s: Dict[str, Any], now: datetime, reset_clock: bool = True) -> None:
    d = date.fromisoformat(s["day"])
    s["q_text"] = f"Morning check - night of {_day_label(d)}: did you sweat in your sleep?"
    try:
        r = api("sendMessage", {"chat_id": s["chat_id"], "text": s["q_text"], "reply_markup": _kb(_sweat_rows())})
        s["msg_id"] = (r or {}).get("message_id")
    except Exception as e:
        log.warning("night-sweat question failed: %s", e)
        s["msg_id"] = None
    if reset_clock:
        s["last_prompt_at"] = now.isoformat(timespec="seconds")
        s["nudged"] = False
    _save_session()


def start_sweat(chat_id, now: Optional[datetime] = None, trigger: str = "command") -> None:
    global _sweat
    now = now or datetime.now()
    _sweat = {"day": sweat_night(now).isoformat(), "chat_id": chat_id, "started_at": now.isoformat(timespec="seconds"),
              "msg_id": None, "q_text": "", "last_prompt_at": now.isoformat(timespec="seconds"), "nudged": False,
              "trigger": trigger}
    log.info("night-sweat question %s started (%s)", _sweat["day"], trigger)
    _sweat_ask(_sweat, now)


def finish_sweat(s: Dict[str, Any], now: datetime, value: str) -> None:
    """Write a sweat-only row (trigger "sweat"): checkin_for_day merges it, the evening answers stay untouched."""
    global _sweat
    day = date.fromisoformat(s["day"])
    row = gc.checkin_row(day, {"night_sweat": value}, started_at=datetime.fromisoformat(s["started_at"]), finished_at=now,
                         status="", trigger="sweat", source="telegram")
    try:
        gc.checkin_log(row)
    except Exception as e:
        log.warning("night-sweat log write failed: %s: %s", type(e).__name__, e)
    log.info("night sweat %s: %s", s["day"], value)
    _sweat = None
    _save_session()


def _sweat_callback(cq: Dict[str, Any], chat_id, value: str, now: datetime) -> str:
    s = _sweat
    if not s or str(s.get("chat_id")) != str(chat_id) or value not in SWEAT_LABEL:
        _answer_cb(cq.get("id"), "That question is already closed - /sweat to redo it.")
        return "cb_stale"
    _answer_cb(cq.get("id"))
    _edit(chat_id, s.get("msg_id"), text=f"{s.get('q_text', '')}\n✓ {SWEAT_LABEL[value]}")
    day = date.fromisoformat(s["day"])
    finish_sweat(s, now, value)
    send(chat_id, f"Saved: {_day_label(day)} night sweat - {SWEAT_LABEL[value].lower()}.")
    return "sweat_done"


def sweat_tick(owner: str, now: Optional[datetime] = None) -> str:
    """Every poll cycle: nudge / skip the open morning question, or start it at MORNING_TIME (before 14:00 only,
    and only while that night has no answer yet). Never raises."""
    now = now or datetime.now()
    try:
        s = _sweat
        if s:
            deadline = datetime.combine(date.fromisoformat(s["day"]) + timedelta(days=1), datetime.min.time()).replace(hour=gc.ALCOHOL_CUTOVER_H)
            if now >= deadline:
                _edit(s["chat_id"], s.get("msg_id"), text=f"{s.get('q_text', '')}\n(no answer - logged as skipped)")
                finish_sweat(s, now, "skipped")
                return "sweat_skipped"
            idle_min = (now - datetime.fromisoformat(s["last_prompt_at"])).total_seconds() / 60
            if not s.get("nudged") and idle_min >= SWEAT_NUDGE_MIN:
                send(s["chat_id"], "Quick one: night sweats last night? One tap.")
                _sweat_ask(s, now, reset_clock=False)
                s["nudged"] = True
                _save_session()
                return "sweat_nudged"
            return "sweat_waiting"
        h, m = map(int, morning_time().split(":"))
        if (now.replace(hour=h, minute=m, second=0, microsecond=0) <= now and now.hour < gc.ALCOHOL_CUTOVER_H
                and not sweat_for_day(sweat_night(now))):
            start_sweat(int(owner), now, trigger="scheduled")
            return "sweat_started"
        return "idle"
    except Exception as e:
        log.warning("night-sweat tick failed: %s: %s", type(e).__name__, e)
        return "error"


def cmd_sweat(chat_id, now: Optional[datetime] = None) -> str:
    now = now or datetime.now()
    if _sweat and str(_sweat.get("chat_id")) == str(chat_id):
        _sweat_ask(_sweat, now)
        return "sweat_resend"
    start_sweat(chat_id, now, trigger="command")
    return "sweat_start"


# ───────────────────────────── free-text drinks ─────────────────────────────
DRINK_RE = re.compile(r"\b(beers?|wines?|drinks?|shots?|cocktails?|bourbons?|whiske?y|"
                      r"liquor|seltzers?|margaritas?|ciders?)\b")


def parse_drink_text(text: str, command: bool = False):
    """'beer' / '2 wine' / 'bourbon' -> (count, what); '/drink' -> (1, 'drink'); anything else -> None."""
    t = (text or "").lower().strip()
    if command:
        t = t.split(None, 1)[1].strip() if len(t.split(None, 1)) > 1 else ""
        t = t or "drink"
    m = DRINK_RE.search(t)
    n = re.search(r"\b(\d{1,2})\b", t)
    if not m and not (command and n):
        return None
    count = int(n.group(1)) if n else 1
    if not 1 <= count <= 12:
        return None
    what = m.group(1) if m else "drink"
    if what.endswith("s") and what != "whiskys":
        what = what[:-1]
    return count, what


def cmd_drink_text(text: str, now: Optional[datetime] = None, command: bool = False) -> Optional[str]:
    """Log one free-text drink event + bump that night's total in alcohol.csv. None = not a drink message."""
    now = now or datetime.now()
    parsed = parse_drink_text(text, command)
    if not parsed:
        return None
    count, what = parsed
    night = checkin_day(now)
    try:
        gc.drink_event_log(night, count, what, at=now)
        total = min(gc.ALCOHOL_MAX_DRINKS, (gc.alcohol_for_night(night) or 0) + count)
        gc.alcohol_log(night, total, source="text", at=now)
    except Exception as e:
        log.warning("drink event write failed: %s: %s", type(e).__name__, e)
        return f"⚠️ Could not log the drink ({type(e).__name__})."
    return f"Logged {count} {what} at {now.strftime('%I:%M %p').lstrip('0')}. {_day_label(night)}: {total:g} total."


def cmd_undo(now: Optional[datetime] = None) -> str:
    now = now or datetime.now()
    try:
        last = gc.drink_undo(now)
        if not last:
            return "Nothing to undo."
        night = date.fromisoformat(last["night_date"])
        total = max(0.0, (gc.alcohol_for_night(night) or 0) - last["count"])
        gc.alcohol_log(night, total, source="undo", at=now)
    except Exception as e:
        log.warning("undo failed: %s: %s", type(e).__name__, e)
        return f"⚠️ Could not undo ({type(e).__name__})."
    return f"Removed {last['count']:g} {last['what']}. {_day_label(night)}: {total:g} total."


# ───────────────────────────── one-time import from the retired checkin_bot ─────────────────────────────
def import_checkins_jsonl(path: Optional[Path] = None, archive: bool = True) -> Dict[str, int]:
    """data/checkins.jsonl -> night_sweat rows in checkin.csv + drink_events.csv. Idempotent (a night that already has a
    sweat answer, or a drink event with the same ts, is skipped). Then moves the file to data/archive/."""
    path = path or DATA_DIR / "checkins.jsonl"
    out = {"sweat": 0, "drinks": 0, "skipped": 0}
    if not path.exists():
        return out
    have_ts = {e["ts"] for e in gc.drink_events()}
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
            day = date.fromisoformat(r["day"])
            ts = datetime.fromisoformat(r["ts"])
        except Exception:
            out["skipped"] += 1
            continue
        if r.get("type") == "morning":
            val = "skipped" if r.get("skipped") else r.get("sweat")
            if val and not sweat_for_day(day):
                gc.checkin_log(gc.checkin_row(day, {"night_sweat": val}, finished_at=ts, status="", trigger="sweat", source="import"))
                out["sweat"] += 1
        elif r.get("type") == "drink" and r.get("count", 0) > 0 and r["ts"] not in have_ts:
            gc.drink_event_log(day, r["count"], r.get("what", "drink"), at=ts)
            out["drinks"] += 1
    if archive:
        dest_dir = DATA_DIR / "archive"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / path.name
        if dest.exists():
            dest = dest_dir / f"{path.stem}.{datetime.now():%Y%m%d%H%M%S}{path.suffix}"
        shutil.move(str(path), str(dest))
    return out


def checkin_log_cli(args: List[str]) -> int:
    """--checkin-log DAY key=value ...  (backfill / manual row; unknown fields are refused)."""
    try:
        day = date.fromisoformat(args[0])
    except Exception:
        print("usage: --checkin-log YYYY-MM-DD field=value ...  fields:", ", ".join(CHECKIN_CLI_FIELDS))
        return 2
    answers: Dict[str, Any] = {}
    for kv in args[1:]:
        k, _, v = kv.partition("=")
        if k not in CHECKIN_CLI_FIELDS:
            print(f"unknown field {k!r}; allowed: {', '.join(CHECKIN_CLI_FIELDS)}")
            return 2
        answers[k] = v.split(";") if k in gc.CHECKIN_LIST_FIELDS else v
    row = gc.checkin_row(day, answers, status="done", trigger="manual", source="backfill")
    gc.checkin_log(row)
    print("logged:", json.dumps(row))
    print(gc.checkin_line(gc.checkin_for_day(day)))
    return 0


def handle(message: Dict[str, Any], owner: str, now: Optional[float] = None) -> str:
    """Process one message. Returns an outcome tag (for the heartbeat and tests)."""
    chat_id = (message.get("chat") or {}).get("id")
    sender = (message.get("from") or {}).get("id", chat_id)
    if str(chat_id) != str(owner) or str(sender) != str(owner):
        log.warning("ignored message from non-owner chat=%s from=%s", chat_id, sender)
        return "ignored_non_owner"
    cmd = parse_command(message.get("text") or "")
    if cmd is None:
        if not parse_drink_text(message.get("text") or ""):
            return "ignored_text"
        cmd = "drink_text"
    age = (now or time.time()) - float(message.get("date") or 0)
    if age > STALE_S:
        send(chat_id, f"Skipped /{cmd} sent {int(age // 60)} min ago while I was offline. Send it again.")
        return f"stale_{cmd}"
    if cmd == "capacity":
        return "capacity_" + cmd_capacity(chat_id)
    if cmd == "today":
        send(chat_id, today_card())
        return "today"
    if cmd == "drinks":
        args = (message.get("text") or "").strip().split(None, 1)
        send(chat_id, cmd_drinks(args[1] if len(args) > 1 else ""))
        return "drinks"
    if cmd in ("drink", "drink_text"):
        reply = cmd_drink_text(message.get("text") or "", datetime.fromtimestamp(now) if now else None, command=(cmd == "drink"))
        send(chat_id, reply or "Try: /drink, or /drink 2 wine (or just text: beer, 2 wine, bourbon).")
        return "drink_text"
    if cmd == "undo":
        send(chat_id, cmd_undo(datetime.fromtimestamp(now) if now else None))
        return "undo"
    if cmd == "sweat":
        return cmd_sweat(chat_id, datetime.fromtimestamp(now) if now else None)
    if cmd == "checkin":
        return cmd_checkin(chat_id, datetime.fromtimestamp(now) if now else None)
    if cmd in ("help", "start"):
        send(chat_id, help_text())
        return "help"
    send(chat_id, HINT)
    return "unknown"


# ───────────────────────────── main loop ─────────────────────────────
def poll_once(offset: Optional[int], owner: str) -> Optional[int]:
    params: Dict[str, Any] = {"timeout": POLL_TIMEOUT_S, "allowed_updates": ["message", "callback_query"]}
    if offset is not None:
        params["offset"] = offset
    updates: List[dict] = api("getUpdates", params, timeout=POLL_TIMEOUT_S + 15) or []
    last_cmd = "-"
    for upd in updates:
        uid = upd.get("update_id")
        msg = upd.get("message") or {}
        cq = upd.get("callback_query") or {}
        try:
            if msg:
                last_cmd = handle(msg, owner)
                log.info("update %s -> %s", uid, last_cmd)
            elif cq:
                last_cmd = handle_callback(cq, owner)
                log.info("update %s (button) -> %s", uid, last_cmd)
        except Exception as e:
            log.exception("update %s failed: %s", uid, e)
        offset = uid + 1
        save_offset(offset)          # after each update: a crash never replays a handled command
    tick = checkin_tick(owner)
    if tick not in ("idle", "waiting"):
        last_cmd = f"checkin_{tick}"
    stick = sweat_tick(owner)
    if stick not in ("idle", "sweat_waiting"):
        last_cmd = stick
    write_heartbeat(offset, last_cmd)
    return offset


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="one poll cycle, then exit")
    ap.add_argument("--checkin-log", nargs="+", metavar="ARG",
                    help="append a check-in row and exit: --checkin-log YYYY-MM-DD field=value ... (backfill)")
    ap.add_argument("--import-checkins", action="store_true",
                    help="one-time: import data/checkins.jsonl (retired checkin_bot) and archive it, then exit")
    a = ap.parse_args()
    if a.import_checkins:
        print("imported:", import_checkins_jsonl())
        return 0
    if a.checkin_log:
        return checkin_log_cli(a.checkin_log)
    fmt = logging.Formatter("%(asctime)s %(levelname)s capacity_bot %(message)s")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    handlers: List[logging.Handler] = [AppendPerRecordHandler()]
    if sys.stdout and sys.stdout.isatty():
        handlers.append(logging.StreamHandler())
    for h in handlers:
        h.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)
    gc.load_env()
    owner = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not os.getenv("TELEGRAM_BOT_TOKEN") or not owner:
        log.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing in %s\\.env", BASE_DIR)
        return 2
    if not acquire_lock():
        return 0
    try:
        # Startup calls retry like the poll loop: a TLS-handshake timeout (seen 2026-10-18 17:00) must not
        # crash the bot; only a rejected token exits (the task then retries every minute).
        backoff = 5
        while True:
            try:
                me = api("getMe")
                api("deleteWebhook", {"drop_pending_updates": False})
                api("setMyCommands", {"commands": COMMANDS})
                break
            except TelegramError as e:
                if e.status in (401, 404):
                    log.error("token rejected at startup (%s); exiting so the task retries", e)
                    return 3
                log.warning("startup call failed: %s; retry in %ss", e, backoff)
            except Exception as e:
                log.warning("startup call failed: %s: %s; retry in %ss", type(e).__name__, e, backoff)
            write_heartbeat(None, "starting")
            _sleep_beating(backoff, None, "starting")
            backoff = min(backoff * 2, 300)
        log.info("started as @%s (pid %s), owner chat set, %d commands registered, check-in at %s",
                 me.get("username"), os.getpid(), len(COMMANDS), CHECKIN_TIME)
        offset = load_offset()
        write_heartbeat(offset, "started")
        _load_session()
        backoff = 5
        while True:
            try:
                offset = poll_once(offset, owner)
                backoff = 5
            except TelegramError as e:
                if e.status == 409:
                    log.error("409 Conflict: another poller is using this token; retrying in 30s")
                    _sleep_beating(30, offset, "409")
                elif e.status in (401, 404):
                    log.error("token rejected (%s); exiting so the task retries", e)
                    return 3
                else:
                    log.warning("Telegram error %s; retry in %ss", e, backoff)
                    _sleep_beating(backoff, offset, f"error {e.status}")
                    backoff = min(backoff * 2, 300)
            except Exception as e:
                log.warning("poll failed: %s: %s; retry in %ss", type(e).__name__, e, backoff)
                write_heartbeat(offset, f"error {type(e).__name__}")
                _sleep_beating(backoff, offset, f"error {type(e).__name__}")
                backoff = min(backoff * 2, 300)
            if a.once:
                return 0
    finally:
        release_lock()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as exc:          # no console under the task: get the crash into task.log
        log.exception("capacity_bot crashed: %s", exc)
        sys.exit(1)
