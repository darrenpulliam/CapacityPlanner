#!/usr/bin/env python3
"""CapacityPlanner nightly check-in bot.

- Any time: text the bot "beer", "2 wine", "bourbon", "/drink" -> logged with a timestamp.
- At wind-down (default 23:15): a button check-in -> drinks, new open wounds
  (count + where), older wounds, exercise (types + minutes).
- Each morning (default 08:35, just after the 8:30 wake): one button question about
  night sweats for the night just ended -> none / damp / soaked the sheets.
- One nudge if unanswered after 45 min; logged as skipped at the 4 AM day rollover.
- Everything is appended to data/checkins.jsonl (data/ is gitignored).
- garmin_capacity.py can import last_night_summary() / last_night_record()
  to put last night's answers on the morning card.

Usage:
  python checkin_bot.py                 run the listener (what the scheduled task runs)
  python checkin_bot.py --test          send a test message and exit
  python checkin_bot.py --checkin-now   run the listener and send a check-in immediately
  python checkin_bot.py --summary [YYYY-MM-DD]   print a day's summary (default: last night)
"""
import argparse
import datetime as dt
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
LOG_FILE = DATA / "checkins.jsonl"
STATE_FILE = DATA / "checkin_state.json"
BOT_LOG = DATA / "checkin_bot.log"

DAY_ROLLOVER_HOUR = 4          # answers before 4 AM count toward the previous day
DEFAULT_CHECKIN_TIME = "23:15"  # standard wind-down (bed 12:15 AM - 60 min)
DEFAULT_MORNING_TIME = "08:35"  # just after the 8:30 AM wake anchor, before the 9:00 Garmin pull
NUDGE_AFTER_MIN = 45

WOUND_SITES = [("face", "Face/mouth"), ("scalp", "Scalp"), ("arms", "Hands/arms"),
               ("legs", "Legs/feet"), ("torso", "Torso/back"), ("other", "Other")]
OLD_WOUNDS = [("none", "None open"), ("healing", "Healing"), ("worse", "Getting worse")]
EXERCISES = [("strength", "Strength training"), ("treadmill", "Treadmill"),
             ("walk", "Walking outside"), ("elliptical", "Elliptical"),
             ("bike", "Biking"), ("hike", "Hiking")]
EX_MINUTES = [("lt15", "<15 min"), ("15-30", "15–30 min"),
              ("30-60", "30–60 min"), ("60plus", "60+ min")]
SWEAT = [("none", "No sweating"), ("damp", "Damp (clothes/pillow)"),
         ("soaked", "Soaked the sheets")]
SWEAT_SHORT = {"none": "no night sweat", "damp": "night sweat: damp",
               "soaked": "NIGHT SWEAT: soaked sheets"}
LABEL = dict(WOUND_SITES + OLD_WOUNDS + EXERCISES + EX_MINUTES + SWEAT)

DRINK_RE = re.compile(r"\b(beers?|wines?|drinks?|shots?|cocktails?|bourbons?|whiske?y|"
                      r"liquor|seltzers?|margaritas?|ciders?)\b")

log = logging.getLogger("checkin")


# ---------------------------------------------------------------- config / io
def load_env():
    env = {}
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    env.update({k: v for k, v in os.environ.items()
                if k.startswith(("CAPACITY_", "TELEGRAM_", "CHECKIN_"))})
    return env


def log_day(now=None):
    now = now or dt.datetime.now()
    return (now - dt.timedelta(hours=DAY_ROLLOVER_HOUR)).date().isoformat()


def _last_night(now=None):
    """The evening/night that ended this morning (same key as that evening's check-in)."""
    return (dt.date.fromisoformat(log_day(now)) - dt.timedelta(days=1)).isoformat()


def pretty_day(day):
    d = dt.date.fromisoformat(day)
    return d.strftime("%a %b ") + str(d.day)


def read_log():
    if not LOG_FILE.exists():
        return []
    out = []
    for line in LOG_FILE.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def append_log(rec):
    DATA.mkdir(exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def load_state():
    state = {"offset": 0, "checkin": None, "last_sent_day": None,
             "morning": None, "last_morning_day": None}
    if STATE_FILE.exists():
        try:
            state.update(json.loads(STATE_FILE.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            log.warning("state file unreadable; starting fresh")
    return state


def save_state(state):
    DATA.mkdir(exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    tmp.replace(STATE_FILE)


def drinks_logged(day, recs=None):
    recs = recs if recs is not None else read_log()
    return sum(r.get("count", 0) for r in recs if r.get("type") == "drink" and r.get("day") == day)


# ---------------------------------------------------------------- telegram
class TG:
    def __init__(self, token):
        self.base = f"https://api.telegram.org/bot{token}/"

    def call(self, method, **params):
        r = requests.post(self.base + method, json=params, timeout=70)
        try:
            data = r.json()
        except ValueError:
            raise RuntimeError(f"{method}: HTTP {r.status_code}")
        if not data.get("ok"):
            raise RuntimeError(f"{method}: {data.get('error_code')} {data.get('description')}")
        return data["result"]


def kb(rows):
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}


def chunk(items, n):
    return [items[i:i + n] for i in range(0, len(items), n)]


# ---------------------------------------------------------------- evening check-in flow
def render(ci):
    a, step = ci["answers"], ci["step"]
    head = f"🌙 Check-in for {pretty_day(ci['day'])}\n\n"
    if step == "drinks":
        so_far = drinks_logged(ci["day"])
        q = "Any drinks today?" + (f" (logged so far: {so_far})" if so_far else "")
        return head + q, kb([[("None", "d:0"), ("1", "d:1"), ("2", "d:2")],
                             [("3", "d:3"), ("4+", "d:4")]])
    if step == "new_wounds":
        return head + "Any NEW open wounds today?", kb([[("No", "w:n"), ("Yes", "w:y")]])
    if step == "wound_count":
        return head + "How many new ones?", kb([[("1", "wc:1"), ("2", "wc:2"), ("3+", "wc:3")]])
    if step == "wound_where":
        sel = a.get("wound_where", [])
        btns = [(("✅ " if k in sel else "") + lbl, f"wl:{k}") for k, lbl in WOUND_SITES]
        return head + "Where? Tap all that apply, then Done.", kb(chunk(btns, 2) + [[("Done", "wl:done")]])
    if step == "old_wounds":
        return head + "Older wounds?", kb([[(lbl, f"ow:{k}") for k, lbl in OLD_WOUNDS]])
    if step == "exercise":
        return head + "Did you exercise today?", kb([[("No", "x:n"), ("Yes", "x:y")]])
    if step == "ex_types":
        sel = a.get("ex_types", [])
        btns = [(("✅ " if k in sel else "") + lbl, f"xt:{k}") for k, lbl in EXERCISES]
        return head + "What type? Tap all that apply, then Done.", kb(chunk(btns, 2) + [[("Done", "xt:done")]])
    if step == "ex_minutes":
        return head + "About how long, total?", kb([[(lbl, f"xm:{k}") for k, lbl in EX_MINUTES]])
    raise ValueError(step)


def toggle(lst, val):
    lst.remove(val) if val in lst else lst.append(val)


def apply_answer(ci, data):
    """Advance the state machine. Returns True if the tap was valid for the current step."""
    a, step = ci["answers"], ci["step"]
    key, _, val = data.partition(":")
    if key == "d" and step == "drinks":
        a["drinks"] = int(val)
        ci["step"] = "new_wounds"
    elif key == "w" and step == "new_wounds":
        if val == "y":
            a["new_wounds"] = True
            ci["step"] = "wound_count"
        else:
            a.update(new_wounds=False, wound_count=0, wound_where=[])
            ci["step"] = "old_wounds"
    elif key == "wc" and step == "wound_count":
        a["wound_count"] = int(val)
        a.setdefault("wound_where", [])
        ci["step"] = "wound_where"
    elif key == "wl" and step == "wound_where":
        if val == "done":
            ci["step"] = "old_wounds"
        elif val in LABEL:
            toggle(a["wound_where"], val)
    elif key == "ow" and step == "old_wounds":
        a["old_wounds"] = val
        ci["step"] = "exercise"
    elif key == "x" and step == "exercise":
        if val == "y":
            a.update(exercise=True, ex_types=[])
            ci["step"] = "ex_types"
        else:
            a.update(exercise=False, ex_types=[], ex_minutes=None)
            ci["step"] = "done"
    elif key == "xt" and step == "ex_types":
        if val == "done":
            if a["ex_types"]:
                ci["step"] = "ex_minutes"
        elif val in LABEL:
            toggle(a["ex_types"], val)
    elif key == "xm" and step == "ex_minutes":
        a["ex_minutes"] = val
        ci["step"] = "done"
    else:
        return False
    return True


def checkin_record(ci, now=None):
    a = ci["answers"]
    return {"type": "checkin", "ts": (now or dt.datetime.now()).isoformat(timespec="seconds"),
            "day": ci["day"], "skipped": False,
            "drinks": a.get("drinks"), "drinks_logged_by_text": drinks_logged(ci["day"]),
            "new_wounds": a.get("new_wounds"), "wound_count": a.get("wound_count", 0),
            "wound_where": a.get("wound_where", []), "old_wounds": a.get("old_wounds"),
            "exercise": a.get("exercise"), "ex_types": a.get("ex_types", []),
            "ex_minutes": a.get("ex_minutes")}


def send_checkin(tg, chat, state, now):
    day = log_day(now)
    ci = {"day": day, "step": "drinks", "answers": {}, "msg_id": None,
          "sent_at": now.isoformat(timespec="seconds"), "nudged": False}
    text, markup = render(ci)
    msg = tg.call("sendMessage", chat_id=chat, text=text, reply_markup=markup)
    ci["msg_id"] = msg["message_id"]
    state["checkin"], state["last_sent_day"] = ci, day
    save_state(state)
    log.info("check-in sent for %s", day)


# ---------------------------------------------------------------- morning sweat question
def render_morning(m):
    text = f"☀️ Morning check: night of {pretty_day(m['day'])}\n\nDid you sweat in your sleep?"
    return text, kb([[(lbl, f"sw:{k}")] for k, lbl in SWEAT])


def send_morning(tg, chat, state, now):
    day = _last_night(now)
    m = {"day": day, "msg_id": None, "sent_at": now.isoformat(timespec="seconds"), "nudged": False}
    text, markup = render_morning(m)
    msg = tg.call("sendMessage", chat_id=chat, text=text, reply_markup=markup)
    m["msg_id"] = msg["message_id"]
    state["morning"], state["last_morning_day"] = m, day
    save_state(state)
    log.info("morning check sent for night of %s", day)


def on_morning_tap(tg, chat, state, cq, val):
    m = state.get("morning")
    msg_id = cq.get("message", {}).get("message_id")
    if not m or m["msg_id"] != msg_id or val not in dict(SWEAT):
        tg.call("answerCallbackQuery", callback_query_id=cq["id"], text="That question is closed.")
        return
    rec = {"type": "morning", "ts": dt.datetime.now().isoformat(timespec="seconds"),
           "day": m["day"], "skipped": False, "sweat": val}
    append_log(rec)
    state["morning"] = None
    save_state(state)
    tg.call("answerCallbackQuery", callback_query_id=cq["id"])
    tg.call("editMessageText", chat_id=chat, message_id=msg_id,
            text=f"✅ Night of {pretty_day(m['day'])}: {LABEL[val]}")
    log.info("morning check saved for night of %s: %s", m["day"], val)


# ---------------------------------------------------------------- callbacks / timing
def on_callback(tg, chat, state, cq):
    data = cq.get("data", "")
    if data.startswith("sw:"):
        on_morning_tap(tg, chat, state, cq, data[3:])
        return
    ci = state.get("checkin")
    msg_id = cq.get("message", {}).get("message_id")
    if not ci or ci["msg_id"] != msg_id:
        tg.call("answerCallbackQuery", callback_query_id=cq["id"], text="That check-in is closed.")
        return
    changed = apply_answer(ci, data)
    tg.call("answerCallbackQuery", callback_query_id=cq["id"])
    if not changed:
        return
    if ci["step"] == "done":
        rec = checkin_record(ci)
        append_log(rec)
        state["checkin"] = None
        save_state(state)
        tg.call("editMessageText", chat_id=chat, message_id=msg_id,
                text=f"✅ Check-in saved for {pretty_day(rec['day'])}\n{summarize(rec, [])}")
        log.info("check-in saved for %s", rec["day"])
    else:
        save_state(state)
        text, markup = render(ci)
        tg.call("editMessageText", chat_id=chat, message_id=msg_id, text=text, reply_markup=markup)


def _close_skipped(tg, chat, state, key, rec_type, label, now):
    item = state[key]
    rec = {"type": rec_type, "ts": now.isoformat(timespec="seconds"),
           "day": item["day"], "skipped": True}
    if "answers" in item:
        rec["partial"] = item["answers"]
    append_log(rec)
    try:
        tg.call("editMessageText", chat_id=chat, message_id=item["msg_id"],
                text=f"⏭ {label} for {pretty_day(item['day'])} skipped.")
    except Exception as e:  # message may be too old to edit
        log.warning("could not edit skipped message: %s", e)
    state[key] = None
    save_state(state)


def _nudge(tg, chat, state, item, text, now):
    if item and not item["nudged"]:
        if now - dt.datetime.fromisoformat(item["sent_at"]) >= dt.timedelta(minutes=NUDGE_AFTER_MIN):
            tg.call("sendMessage", chat_id=chat, text=text, reply_to_message_id=item["msg_id"])
            item["nudged"] = True
            save_state(state)


def housekeeping(tg, chat, state, now, checkin_time, morning_time, force=False):
    # ---- evening check-in
    ci = state.get("checkin")
    if ci and log_day(now) != ci["day"]:
        _close_skipped(tg, chat, state, "checkin", "checkin", "Check-in", now)
        ci = None
    _nudge(tg, chat, state, ci, "Check-in still open ☝️ (takes 20 seconds)", now)
    if ci is None:
        day = log_day(now)
        hh, mm = map(int, checkin_time.split(":"))
        target = dt.datetime.combine(dt.date.fromisoformat(day), dt.time(hh, mm))
        if force or (state.get("last_sent_day") != day and now >= target):
            send_checkin(tg, chat, state, now)

    # ---- morning sweat question (about the night that just ended)
    m = state.get("morning")
    if m and _last_night(now) != m["day"]:
        _close_skipped(tg, chat, state, "morning", "morning", "Morning check", now)
        m = None
    _nudge(tg, chat, state, m, "Quick one ☝️ night sweats?", now)
    if m is None:
        night = _last_night(now)
        hh, mm = map(int, morning_time.split(":"))
        target = dt.datetime.combine(dt.date.fromisoformat(log_day(now)), dt.time(hh, mm))
        if state.get("last_morning_day") != night and now >= target:
            send_morning(tg, chat, state, now)


# ---------------------------------------------------------------- free-text drinks
def parse_drink(text):
    t = text.lower().strip()
    if t.startswith("/drink"):
        t = t[len("/drink"):].strip() or "drink"
    m = DRINK_RE.search(t)
    if not m:
        return None
    n = re.search(r"\b(\d{1,2})\b", t)
    count = int(n.group(1)) if n else 1
    if not 1 <= count <= 12:
        return None
    what = m.group(1)
    if what.endswith("s") and what != "whiskys":
        what = what[:-1]
    return count, what


def on_message(tg, chat, state, msg):
    text = (msg.get("text") or "").strip()
    now = dt.datetime.now()
    day = log_day(now)
    low = text.lower()
    if low in ("/start", "/help", "help"):
        reply = ("Text a drink any time: beer · 2 wine · bourbon · /drink\n"
                 "/undo removes the last drink · /status shows today · /checkin starts the check-in now\n"
                 "/sweat asks the night-sweat question for last night")
    elif low == "/checkin":
        if state.get("checkin"):
            reply = "A check-in is already open ☝️"
        else:
            send_checkin(tg, chat, state, now)
            return
    elif low in ("/sweat", "/morning"):
        if state.get("morning"):
            reply = "The night-sweat question is already open ☝️"
        else:
            send_morning(tg, chat, state, now)
            return
    elif low == "/status":
        recs = read_log()
        n = drinks_logged(day, recs)
        done = any(r.get("type") == "checkin" and r.get("day") == day and not r.get("skipped") for r in recs)
        night = _last_night(now)
        sw = next((r for r in reversed(recs) if r.get("type") == "morning"
                   and r.get("day") == night and not r.get("skipped")), None)
        reply = (f"{pretty_day(day)}: {n} drink(s) logged · check-in {'done' if done else 'not done'}\n"
                 f"Night of {pretty_day(night)}: {LABEL[sw['sweat']] if sw else 'sweat not answered'}")
    elif low == "/undo":
        recs = read_log()
        drinks = [r for r in recs if r.get("type") == "drink" and r.get("day") == day]
        total = sum(r["count"] for r in drinks)
        last = next((r for r in reversed(drinks) if r["count"] > 0), None)
        if not last or total <= 0:
            reply = "Nothing to undo today."
        else:
            n = min(last["count"], total)
            append_log({"type": "drink", "ts": now.isoformat(timespec="seconds"), "day": day,
                        "count": -n, "what": last["what"], "undo": True})
            reply = f"Removed {n} {last['what']}. Today: {total - n}."
    else:
        parsed = parse_drink(text)
        if not parsed:
            reply = "Didn't catch that. Try: beer · 2 wine · bourbon · /help"
        else:
            count, what = parsed
            append_log({"type": "drink", "ts": now.isoformat(timespec="seconds"), "day": day,
                        "count": count, "what": what})
            reply = (f"Logged {count} {what} at {now.strftime('%I:%M %p').lstrip('0')}. "
                     f"Today: {drinks_logged(day)}.")
    tg.call("sendMessage", chat_id=chat, text=reply)


# ---------------------------------------------------------------- morning-card helpers
def _sweat_part(morning_rec):
    if morning_rec and not morning_rec.get("skipped") and morning_rec.get("sweat"):
        return SWEAT_SHORT[morning_rec["sweat"]]
    return "night sweat ?"


def summarize(ci_rec, drink_recs, morning_rec=None):
    """One line for the morning card. ci_rec / morning_rec may be None."""
    parts = []
    last = max((r["ts"] for r in drink_recs if r.get("count", 0) > 0), default=None)
    text_total = sum(r.get("count", 0) for r in drink_recs)
    if ci_rec and not ci_rec.get("skipped") and ci_rec.get("drinks") is not None:
        d = ci_rec["drinks"]
        drinks = "4+" if d >= 4 else str(d)
    else:
        drinks = str(text_total) if drink_recs else "?"
    s = f"Drinks {drinks}"
    if last:
        s += f" (last {dt.datetime.fromisoformat(last).strftime('%I:%M %p').lstrip('0')})"
    parts.append(s)
    if not ci_rec or ci_rec.get("skipped"):
        parts.append("check-in skipped" if ci_rec else "no check-in")
    else:
        if ci_rec.get("new_wounds"):
            where = ", ".join(LABEL.get(k, k) for k in ci_rec.get("wound_where", []))
            n = ci_rec.get("wound_count", 0)
            parts.append(f"NEW wounds {'3+' if n >= 3 else n}" + (f" ({where})" if where else ""))
        else:
            parts.append("no new wounds")
        if ci_rec.get("old_wounds") and ci_rec["old_wounds"] != "none":
            parts.append(f"older: {LABEL[ci_rec['old_wounds']].lower()}")
        if ci_rec.get("exercise"):
            types = ", ".join(LABEL.get(k, k) for k in ci_rec.get("ex_types", []))
            parts.append(f"exercise: {types} {LABEL.get(ci_rec.get('ex_minutes'), '')}".rstrip())
        else:
            parts.append("no exercise")
    if morning_rec is not None or ci_rec is not None:
        parts.append(_sweat_part(morning_rec))
    return "📝 " + " · ".join(parts)


def day_record(day):
    recs = read_log()

    def pick(kind):
        rows = [r for r in recs if r.get("type") == kind and r.get("day") == day]
        return next((r for r in reversed(rows) if not r.get("skipped")), rows[-1] if rows else None)

    drinks = [r for r in recs if r.get("type") == "drink" and r.get("day") == day]
    return pick("checkin"), drinks, pick("morning")


def last_night_summary(now=None):
    """Card line for the previous day, e.g. '📝 Drinks 2 (last 10:40 PM) · no new wounds · ... · no night sweat'"""
    return summarize(*day_record(_last_night(now)))


def last_night_record(now=None):
    """Dict for the [CAPACITY] JSON block (key: 'checkin')."""
    day = _last_night(now)
    ci, drinks, m = day_record(day)
    return {"day": day, "checkin": ci,
            "drinks_logged_by_text": sum(r.get("count", 0) for r in drinks),
            "drink_times": [r["ts"] for r in drinks if r.get("count", 0) > 0],
            "night_sweat": m.get("sweat") if m and not m.get("skipped") else None}


# ---------------------------------------------------------------- main loop
def run(tg, chat, checkin_time, morning_time, force_first=False):
    state = load_state()
    force = force_first
    log.info("listening (check-in at %s, morning check at %s)", checkin_time, morning_time)
    while True:
        try:
            housekeeping(tg, chat, state, dt.datetime.now(), checkin_time, morning_time, force=force)
            force = False
            updates = tg.call("getUpdates", offset=state["offset"], timeout=50,
                              allowed_updates=["message", "callback_query"])
            for u in updates:
                state["offset"] = u["update_id"] + 1
                save_state(state)
                if "callback_query" in u:
                    cq = u["callback_query"]
                    if str(cq.get("message", {}).get("chat", {}).get("id")) == chat:
                        on_callback(tg, chat, state, cq)
                elif "message" in u:
                    m = u["message"]
                    if str(m.get("chat", {}).get("id")) == chat:
                        on_message(tg, chat, state, m)
        except KeyboardInterrupt:
            log.info("stopped")
            return
        except RuntimeError as e:
            if "409" in str(e):
                log.error("409 Conflict: another program is polling this bot token. "
                          "Use a dedicated bot for check-ins (see README).")
                time.sleep(60)
            else:
                log.error("%s", e)
                time.sleep(10)
        except requests.RequestException as e:
            log.warning("network: %s", e)
            time.sleep(15)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--checkin-now", action="store_true")
    ap.add_argument("--summary", nargs="?", const="last")
    args = ap.parse_args()

    DATA.mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(BOT_LOG, encoding="utf-8"),
                                  logging.StreamHandler(sys.stdout)])

    if args.summary:
        day = _last_night() if args.summary == "last" else args.summary
        print(summarize(*day_record(day)))
        return

    env = load_env()
    token = env.get("CAPACITY_BOT_TOKEN")
    chat = env.get("CAPACITY_CHAT_ID") or env.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        sys.exit("Set CAPACITY_BOT_TOKEN and CAPACITY_CHAT_ID (or TELEGRAM_CHAT_ID) in .env")
    tg = TG(token)
    checkin_time = env.get("CHECKIN_TIME", DEFAULT_CHECKIN_TIME)
    morning_time = env.get("CHECKIN_MORNING_TIME", DEFAULT_MORNING_TIME)

    if args.test:
        tg.call("sendMessage", chat_id=chat,
                text="✅ Check-in bot connected. Text 'beer' to test drink logging, /help for commands.")
        print("Test message sent.")
        return
    run(tg, str(chat), checkin_time, morning_time, force_first=args.checkin_now)


if __name__ == "__main__":
    main()
