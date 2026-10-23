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
  /help      list the commands
Anything else gets a one-line hint. The commands are registered with setMyCommands at start.

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
COMMANDS = [
    {"command": "capacity", "description": "Current Body Battery, stress, HR (live Garmin pull)"},
    {"command": "today", "description": "This morning's capacity card"},
    {"command": "drinks", "description": "Log alcohol: /drinks N [M/D] (0-10; before 2 PM = last night)"},
    {"command": "help", "description": "List the commands"},
]
HINT = "Unknown command. Try /capacity, /today, /drinks or /help."
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


def acquire_lock() -> bool:
    """True if this process now holds the lock. A lock held by a live PID -> False."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
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
                log.info("another capacity_bot holds the lock (PID %s); exiting", holder)
                return False
            log.warning("stale lock (PID %s not running); taking it over", holder)
            try:
                LOCK_FILE.unlink()
            except FileNotFoundError:
                pass
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


def handle(message: Dict[str, Any], owner: str, now: Optional[float] = None) -> str:
    """Process one message. Returns an outcome tag (for the heartbeat and tests)."""
    chat_id = (message.get("chat") or {}).get("id")
    sender = (message.get("from") or {}).get("id", chat_id)
    if str(chat_id) != str(owner) or str(sender) != str(owner):
        log.warning("ignored message from non-owner chat=%s from=%s", chat_id, sender)
        return "ignored_non_owner"
    cmd = parse_command(message.get("text") or "")
    if cmd is None:
        return "ignored_text"
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
    if cmd in ("help", "start"):
        send(chat_id, help_text())
        return "help"
    send(chat_id, HINT)
    return "unknown"


# ───────────────────────────── main loop ─────────────────────────────
def poll_once(offset: Optional[int], owner: str) -> Optional[int]:
    params: Dict[str, Any] = {"timeout": POLL_TIMEOUT_S, "allowed_updates": ["message"]}
    if offset is not None:
        params["offset"] = offset
    updates: List[dict] = api("getUpdates", params, timeout=POLL_TIMEOUT_S + 15) or []
    last_cmd = "-"
    for upd in updates:
        uid = upd.get("update_id")
        msg = upd.get("message") or {}
        try:
            if msg:
                last_cmd = handle(msg, owner)
                log.info("update %s -> %s", uid, last_cmd)
        except Exception as e:
            log.exception("update %s failed: %s", uid, e)
        offset = uid + 1
        save_offset(offset)          # after each update: a crash never replays a handled command
    write_heartbeat(offset, last_cmd)
    return offset


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="one poll cycle, then exit")
    a = ap.parse_args()
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
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)
        log.info("started as @%s (pid %s), owner chat set, %d commands registered",
                 me.get("username"), os.getpid(), len(COMMANDS))
        offset = load_offset()
        backoff = 5
        while True:
            try:
                offset = poll_once(offset, owner)
                backoff = 5
            except TelegramError as e:
                if e.status == 409:
                    log.error("409 Conflict: another poller is using this token; retrying in 30s")
                    time.sleep(30)
                elif e.status in (401, 404):
                    log.error("token rejected (%s); exiting so the task retries", e)
                    return 3
                else:
                    log.warning("Telegram error %s; retry in %ss", e, backoff)
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 300)
            except Exception as e:
                log.warning("poll failed: %s: %s; retry in %ss", type(e).__name__, e, backoff)
                write_heartbeat(offset, f"error {type(e).__name__}")
                time.sleep(backoff)
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
