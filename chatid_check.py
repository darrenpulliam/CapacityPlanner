"""Find the chat ID that messaged the check-in bot and compare it to .env. Never prints the token."""
import requests
import checkin_bot as c

e = c.load_env()
tok = e.get("CAPACITY_BOT_TOKEN")
if not tok:
    raise SystemExit("CAPACITY_BOT_TOKEN missing from .env")

me = requests.get(f"https://api.telegram.org/bot{tok}/getMe", timeout=20).json()
print("Bot:", "@" + me["result"]["username"] if me.get("ok") else f"TOKEN REJECTED ({me.get('description')})")

configured = e.get("CAPACITY_CHAT_ID") or e.get("TELEGRAM_CHAT_ID")
print("CAPACITY_CHAT_ID in .env:", e.get("CAPACITY_CHAT_ID") or "(missing)")
print("TELEGRAM_CHAT_ID in .env:", e.get("TELEGRAM_CHAT_ID") or "(missing)")

r = requests.get(f"https://api.telegram.org/bot{tok}/getUpdates", timeout=20).json()
if not r.get("ok"):
    raise SystemExit(f"getUpdates failed: {r.get('description')}  (stop the task first)")
ids = {}
for u in r["result"]:
    m = u.get("message") or u.get("callback_query", {}).get("message")
    if m:
        ch = m["chat"]
        ids[str(ch["id"])] = ch.get("first_name") or ch.get("title") or ""
if not ids:
    print("Chat IDs that messaged the bot: NONE -> open the bot above in Telegram, tap Start, send 'hi', run again")
else:
    for i, name in ids.items():
        print(f"Chat ID that messaged the bot: {i} ({name})")
    if configured in ids:
        print("MATCH -> .env is right; run: python checkin_bot.py --test")
    else:
        fix = next(iter(ids))
        print(f"NO MATCH -> set CAPACITY_CHAT_ID={fix} in .env")
