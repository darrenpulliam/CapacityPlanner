# Capacity Planner

Standalone project, separate from CrystalBull. Garmin → capacity tier → calendar.

**Flow:** Garmin watch → Garmin Connect → `garmin_capacity.py` (Task Scheduler `\Capacity\Capacity_Morning` 09:00, `Capacity_Retry` 10:15; retimed 2026-10-16 from 07:25/08:40) → Telegram card + `[CAPACITY]` mail → cloud Claude task "Capacity calendar writer" (08:52 ET) → Google Calendar (dppharmd@gmail.com).

**On demand (`--now`, 2026-10-17):** current readings through the day: latest Body Battery with its time, today's high/low, latest stress, latest HR, today's RHR, last watch sync (flagged STALE after 60 min) and this morning's tier for reference. Telegram line `NOW <time>: BB <n> (high/low), stress <n>, HR <n>, synced <time>` plus a `[CAPACITY-NOW] <date> <time> BB<n>` mail with a `<<<CAPACITY_NOW_JSON` block. Always calls Garmin; never touches the morning files (writes `data\now\` and `data\now_latest.json`). Run it with `Start-ScheduledTask -TaskPath '\Capacity\' -TaskName Capacity_Now` (no trigger, S4U, logs to `data\task.log`) or `/capacity` on the CrystalBull Telegram bot, which runs the same script. The planner never polls Telegram, so the bot stays the single `getUpdates` poller on the token.

| Tier | Body Battery at wake | Deep-work blocks | Bedtime shift |
|---|---|---|---|
| GREEN | ≥ 50 | 3 | 0 |
| YELLOW | 25–49 | 2 | −15 min |
| RED | < 25 | 1 | −30 min |

- Drops one tier if sleep score < 60 or HRV status is UNBALANCED/LOW.
- Sleep debt (an average of under 7h15m over at least 2 nights) moves bedtime another 15 min earlier. Bedtime never moves more than 45 min earlier.
- Wake is fixed at **8:30 AM** (shift-work anchor). Sleep need is 8h plus 15 min to fall asleep, so standard bedtime is **12:15 AM**, wind-down starts at 11:15 PM and caffeine cutoff is 3:15 PM. Tier cutoffs were recalibrated on 2026-10-07 from 12 months of Garmin history (median Body Battery at wake about 35). Change these with the constants at the top of `garmin_capacity.py`.

**Layout**
```
CapacityPlanner\
  garmin_capacity.py   install_capacity.ps1   README.md   .env.example   .gitignore
  .env                 (secrets, gitignored)
  data\                (gitignored: YYYY-MM-DD.json, latest.json, history.csv, raw\, task.log)
```

**Secrets:** Windows user environment variables win, then `.env` here, then CrystalBull's `.env` files (read-only fallback for Telegram/Gmail). `GARMIN_EMAIL` / `GARMIN_PASSWORD` live only in the Windows user environment (`setx`, single-quote the password); leave them blank in `.env`. They are used by `--setup` only.

**Auth (shared with HealthAI's `garmin_fetch.py`):** one token store, `%USERPROFILE%\.garminconnect` (`garminconnect` 0.3.x, override with `GARMINTOKENS`). Scheduled runs are token-only: they never attempt a credential login, so a stale password can't trip Garmin's 429 throttle. If the token is missing or rejected the run fails and says to run `--setup`, which is the only path that logs in (email, password, MFA) and writes the token. This is an unofficial API, so if Garmin changes its login, run `pip install --upgrade garminconnect` first.

**One fetch per day:** `data\raw\<date>.json` carries `_meta.fetched_at`. A run less than 20 h after that reuses the file instead of calling Garmin (`--refresh` overrides). `garmin_fetch.py` (HealthAI) reads the same raw file first, so the two projects never fetch twice. The raw pull also includes stress, resting HR, body composition, weigh-ins and hydration for HealthAI.

**Debug:** `python garmin_capacity.py --dry-run` · `--mock` · `--refresh` · `--date YYYY-MM-DD` · `--now --dry-run`
