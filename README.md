# Capacity Planner

Standalone project, separate from CrystalBull. Garmin → capacity tier → calendar.

**Flow:** Garmin watch → Garmin Connect → `garmin_capacity.py` (Task Scheduler `\Capacity\Capacity_Morning` 09:00, `Capacity_Retry` 10:15; retimed 2026-10-16 from 07:25/08:40) → Telegram card + `[CAPACITY]` mail → cloud Claude task "Capacity calendar writer" (08:52 ET) → Google Calendar (dppharmd@gmail.com).

**On demand (`--now`, 2026-10-17):** current readings through the day: latest Body Battery with its time, today's high/low, latest stress, latest HR, today's RHR, last watch sync (flagged STALE after 60 min) and this morning's tier for reference. Telegram line `NOW <time>: BB <n> (high/low), stress <n>, HR <n>, synced <time>` plus a `[CAPACITY-NOW] <date> <time> BB<n>` mail with a `<<<CAPACITY_NOW_JSON` block. Always calls Garmin; never touches the morning files (writes `data\now\` and `data\now_latest.json`). Run it with `Start-ScheduledTask -TaskPath '\Capacity\' -TaskName Capacity_Now` (no trigger, S4U, logs to `data\task.log`) or `/capacity` on the planner's own bot.

**Telegram bot (2026-10-18): `@dp_health_consultant_bot` ("Health Consultant"), `capacity_bot.py`.** Moved off `@crystalbull_ai_bot`; the morning card, the retry and `--now` all post through this bot's token. Long-polls `getUpdates` with stdlib urllib (plain `requests` black-holes on this PC, see `b20bfce`). Owner chat only (`TELEGRAM_CHAT_ID`). Commands, registered with `setMyCommands`: `/capacity` (runs `--now`, which posts the NOW card), `/today` (reposts this morning's card from `data\latest.json`), `/help`; anything else gets a one-line hint. Single instance via `data\capacity_bot.lock`; heartbeat `data\bot_heartbeat`; update offset in `data\capacity_bot_state.json`; commands queued more than 10 min while it was down are skipped with a note. Task `\Capacity\Capacity_Bot`: at logon + at startup, S4U, restart every 1 min on failure (999), no time limit, logs to `data\task.log`. It is the one poller on its token.

| Tier | Body Battery at wake | Deep-work blocks | Bedtime shift |
|---|---|---|---|
| GREEN | ≥ 50 | 3 | 0 |
| YELLOW | 25–49 | 2 | −15 min |
| RED | < 25 | 1 | −30 min |

- Drops one tier if sleep score < 60 or HRV status is UNBALANCED/LOW.
- Sleep debt (an average of under 7h15m over at least 2 nights) moves bedtime another 15 min earlier. Bedtime never moves more than 45 min earlier.
- Wake is fixed at **8:30 AM** (shift-work anchor). Sleep need is 8h plus 15 min to fall asleep, so standard bedtime is **12:15 AM**, wind-down starts at 11:15 PM and caffeine cutoff is 3:15 PM. Tier cutoffs were recalibrated on 2026-10-07 from 12 months of Garmin history (median Body Battery at wake about 35). Change these with the constants at the top of `garmin_capacity.py`.

**Extra sources (2026-10-23, `sources_extra.py`):** ResMed myAir (CPAP) and Sleep Number SleepIQ, both best-effort (a failure logs a WARNING in `data\task.log` and the run continues with Garmin only). One-time setup: `python garmin_capacity.py --setup-myair` (e-mail/password, then the e-mailed MFA code; tokens + Okta remembered-device cookie in `%USERPROFILE%\.myair\tokens.json`) and `--setup-sleepiq` (e-mail/password; asks which sleeper if the bed has two; choice in `%USERPROFILE%\.sleepiq\session.json`). Both write `MYAIR_*` / `SLEEPIQ_*` to `.env` without echoing them. Scheduled runs never prompt: myAir reuses the access token while Okta says it is active, then signs in again with the remembered device (no MFA); if Okta still wants a code the source is skipped. Rules: `sleep_min` falls back Garmin -> SleepIQ (in bed - restless - out of bed) -> CPAP usage (`metrics.sleep_source`); SleepIQ score < 60 only counts when Garmin has no sleep score; AHI >= 5 joins the one-tier downgrade (still max one); AHI >= 15 adds "check mask/pressure"; CPAP < 240 min sets the sleep-debt flag (-15 min, -45 cap); high leak (> 24 L/min 95th pct) or mask seal score < 15 (`cpap.mask_seal_score`, 0-20; `cpap.mask_onoff_score` 0-5 and `cpap.mask_pairs` are informational) adds "mask seal". Watch not worn (no Garmin sleep record AND `bb_charged_overnight` = 0): Body Battery at wake is stale, so the base tier comes from the SleepIQ score (>= 80 GREEN, 60-79 YELLOW, < 60 RED) and the one-tier downgrade OR still applies; `metrics.tier_source` is `garmin` or `sleepiq`; without SleepIQ the Body Battery tier is used as before. The bed-entry-vs-target comparison is done by the cloud calendar writer (shift-aware), not here. Payload / mail JSON gain `cpap` and `sleepnumber` objects (null when missing); subject and `<<<CAPACITY_JSON` markers unchanged. `--no-extra` skips both. `--mock <scenario>` exercises one rule each (see `MOCK_CASES`).

**Weather / barometric pain (2026-10-23, `sources_extra.fetch_weather`):** Open-Meteo hourly sea-level pressure (free, no key) for the configured location. Set `WEATHER_PLACE` (e.g. `Frankfort, KY`; geocoded once, cached in `data\.weather_location.json`) or `WEATHER_LAT` / `WEATHER_LON` in `.env`; unset = the source is skipped with a WARNING and the card shows `Weather: no data`. Pain index = the largest pressure change over any 12 h window (and any 24 h window) from 12 h before to 24 h after the wake anchor (08:30, or the run time when run later today): `pain_level` NONE / LOW (>= 1.5 hPa / 12 h) / MODERATE (>= 3 hPa / 12 h or >= 5 hPa / 24 h) / HIGH (>= 6 hPa / 12 h or >= 10 hPa / 24 h), `pain_index` 0-10. HIGH joins the one-tier downgrade OR (`WEATHER_PAIN_DOWNGRADE_LEVEL`, set to `MODERATE` to be stricter); MODERATE adds a "plan for a pain day" reason only; LOW is card-only. Payload / mail JSON gain a `weather` object (pressure, worst 12 h / 24 h change with its window, pain index/level, temp, humidity, precip chance, condition, location); `history.csv` gains `pressure_hpa`, `pressure_d12_hpa`, `pain_level` for later pain-vs-pressure review; the raw response is kept in `data\raw\weather_<date>.json`. `python garmin_capacity.py --weather` prints today's object; `--no-weather` skips the fetch; `--mock weather-low|weather-moderate|weather-high|weather-high-24h|weather-none` exercise the rule.

**Layout**
```
CapacityPlanner\
  garmin_capacity.py   capacity_bot.py   install_capacity.ps1   README.md   .env.example   .gitignore
  .env                 (secrets, gitignored)
  data\                (gitignored: YYYY-MM-DD.json, latest.json, history.csv, raw\, now\, task.log,
                        bot_heartbeat, capacity_bot.lock, capacity_bot_state.json)
```

**Secrets:** Windows user environment variables win, then `.env` here. There is no CrystalBull fallback any more (removed 2026-10-18): Telegram and Gmail come only from this folder's `.env`. `GARMIN_EMAIL` / `GARMIN_PASSWORD` live only in the Windows user environment (`setx`, single-quote the password); leave them blank in `.env`. They are used by `--setup` only.

**Auth (shared with HealthAI's `garmin_fetch.py`):** one token store, `%USERPROFILE%\.garminconnect` (`garminconnect` 0.3.x, override with `GARMINTOKENS`). Scheduled runs are token-only: they never attempt a credential login, so a stale password can't trip Garmin's 429 throttle. If the token is missing or rejected the run fails and says to run `--setup`, which is the only path that logs in (email, password, MFA) and writes the token. This is an unofficial API, so if Garmin changes its login, run `pip install --upgrade garminconnect` first.

**One fetch per day:** `data\raw\<date>.json` carries `_meta.fetched_at`. A run less than 20 h after that reuses the file instead of calling Garmin (`--refresh` overrides). `garmin_fetch.py` (HealthAI) reads the same raw file first, so the two projects never fetch twice. The raw pull also includes stress, resting HR, body composition, weigh-ins and hydration for HealthAI.

**Debug:** `python garmin_capacity.py --dry-run` · `--mock` · `--refresh` · `--date YYYY-MM-DD` · `--now --dry-run`
