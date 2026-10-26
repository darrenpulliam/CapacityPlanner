# Bedtime: total vs direct effect, work-day control, reverse causation — 2026-10-26

READ-ONLY follow-up to the "sleep before midnight" run. No code, config or task changes. Report file only.

## 0. Source, samples, definitions (unchanged from the prior run)

- Source: `OneDrive\Desktop\Garmin\garmin_daily_2019-2026.csv` (file dated 2026-10-07; last wake date 2026-10-05).
- Row = wake date d. `bed_h` = sleep_start minus midnight of d, in hours (23:30 = −0.5, 01:58 = +1.97). `sleep_h` = Garmin
  asleep hours. `asleep_before_mid` = minutes in bed before 00:00 × sleep efficiency. `prev_stress` = stress on d−1.
  `rhr_next` = RHR on d+1. Lags use calendar dates, not row order.
- MAIN = wake dates 2025-10-06..2026-10-05 (309 nights). CHECK = 2025-01-01..2026-10-05 (407 nights; only 98 of them
  fall before MAIN, because the watch was worn less in 2025).
- Outcomes: bb_charged, bb_sleepend (BB at wake), hrv, rhr_next. Sleep score and readiness skipped (the composites
  include timing).
- Models are OLS. 95% CIs come from HC1 SEs, and also from Newey-West HAC SEs (lag 7). Indirect-effect CIs come from a
  moving-block bootstrap (7-night blocks, B = 2000, seed 20261026). Every mediation runs on one complete-case sample per
  outcome, so TOTAL = DIRECT + a×b holds exactly (checked: the gap is ≤ 2e-14 in every row).
- HAC caveat: the lag counts rows, not calendar days, and nights without data are dropped. Lag 7 therefore covers about
  7 to 9 calendar days.
- Scripts are in the session scratchpad and are not committed: `mediation.py`, `fmt.py`, `q5_pre.py`, `parse_shifts.py`.
  The prior script is `bedtime_analysis.py` in the 10/25 CapacityPlanner session scratchpad.

## 1. Work-day control

### 1a. Google Calendar pull: shift source FOUND
- The first calls returned `Resource has been exhausted (check quota)`, 4 times in all. Retries after backing off worked.
- Source: primary calendar (dppharmd@gmail.com), events whose description contains "Pharmacist". There are two formats:
  "Your work schedule" (`HH:MM am : Pharmacist Work - Reg. Shift`) and "Shift in <store>" (`Pharmacist:  Work`).
  The window 2024-12-31..2026-10-07 returned 229 shifts, from 2025-01-03 to 2026-10-06. One date had two shifts
  (2025-07-15); the earlier one was kept.
- **Coverage gaps (no shift events at all):**
  - 2025-08-28..2025-11-20. All 171 events in 2025-08-25..2025-12-01 were listed: only routine and medical items, no
    shifts.
  - 2026-08-26..2026-09-27, except 09-09.
  - "No event" cannot be told apart from "off work" (leave? vacation?). The "HVAC Related Dates" calendar (described as
    Sedgwick, i.e. possible leave dates) has **no events** in the window. These months are treated as **uncovered** and
    left out of the calendar models: 2025-09, 2025-10, 2025-11 and 2026-09. Covered = 2025-01..08 and 2025-12..2026-08.
    A night counts only if both d and d−1 are in a covered month.
- Flags for wake date d:
  - `early_shift` = a shift on d that starts ≤ 10:00, so the wake is alarm-bound.
  - `late_shift_prev` = a shift on d−1 that ends ≥ 21:00, so bedtime is pushed late.
  - `shift_today` = any shift on d.

| sample | nights | calendar-covered | shift on d | early shift on d | late shift on d−1 |
|---|---|---|---|---|---|
| MAIN | 309 | 222 (72%) | 81 | 48 (22% of covered) | 28 |
| CHECK | 407 | 288 (71%) | 110 | 68 (24%) | 33 |

Nights by schedule (MAIN, covered nights; means):

| night type | n | bed_h | wake h | sleep_h | BB charged |
|---|---|---|---|---|---|
| early shift on d | 48 | +0.60 | 6.99 | 6.23 | 25.5 |
| later shift on d | 33 | +0.73 | 9.50 | 8.63 | 36.1 |
| off on d | 141 | +1.56 | 10.32 | 8.38 | 33.5 |
| after a late shift (d−1) | 28 | +1.96 | 10.02 | 7.60 | 32.1 |

### 1b. Proxy (spec fallback; reported alongside, clearly a PROXY)
- `cw_proxy` = wake before 08:00 on a weekday (wake date Mon–Fri).
- **The "alarm-like wake" part (no awake period >10 min in the last hour) could NOT be computed.** The CSV has nightly
  totals only. Intra-night timelines (`sleepLevels`) exist only in `CapacityPlanner\data\raw\`, and only from 2026-10-07,
  which is after both samples end.
- Proxy = yes on 60/309 MAIN nights (19.4%) and 85/407 CHECK nights (20.9%).
- Agreement with the calendar `early_shift` (MAIN, covered n = 222): both yes 34, both no 168, proxy-only 14,
  calendar-only 6. The proxy catches 34 of 40 early-shift nights but also flags 14 weekday early wakes that had no early
  shift.

## 2–3. TOTAL vs DIRECT vs INDIRECT (per 1 h later bedtime)

- TOTAL: `y ~ bed_h + prev_stress + work controls`.
- DIRECT: TOTAL + `sleep_h`.
- a-path: `sleep_h ~ bed_h + same controls`.
- b = the sleep_h coefficient in DIRECT.
- INDIRECT = a × b.
- Primary spec = **calendar**: controls prev_stress + early_shift + late_shift_prev, covered nights only. Base (prior
  run's controls) and proxy specs are given for comparison.
- Cells show coef [95% CI] with HC1 / HAC7.

### MAIN — calendar spec

| outcome | n | TOTAL HC1 | TOTAL HAC7 | DIRECT HC1 | DIRECT HAC7 | INDIRECT [block boot] |
|---|---|---|---|---|---|---|
| BB charged | 222 | −2.58 [−4.12, −1.05] | [−4.05, −1.12] | −0.77 [−2.13, +0.59] | [−1.87, +0.33] | −1.81 [−3.09, −1.08] |
| BB at wake | 222 | −3.24 [−4.60, −1.87] | [−4.55, −1.92] | −1.18 [−2.29, −0.07] | [−2.25, −0.10] | −2.06 [−3.54, −1.36] |
| HRV | 222 | −0.25 [−1.00, +0.50] | [−0.95, +0.44] | +0.14 [−0.70, +0.99] | [−0.53, +0.82] | −0.40 [−0.79, −0.14] |
| next-day RHR | 218 | −0.03 [−0.17, +0.12] | [−0.15, +0.10] | +0.07 [−0.10, +0.25] | [−0.08, +0.23] | −0.10 [−0.25, +0.08] |

- a-path: −0.63 h of sleep per 1 h later bedtime [−0.86, −0.40], n = 222.
- b: BB charged +2.88 [+1.70, +4.06] per sleep hour; BB wake +3.27 [+2.16, +4.38]; HRV +0.63 [+0.07, +1.19];
  RHR_next +0.16 [−0.03, +0.35].

### MAIN — base spec (prior run's controls, all 309 nights) and proxy spec

| outcome | n | TOTAL base | DIRECT base | INDIRECT base | TOTAL proxy | DIRECT proxy | INDIRECT proxy |
|---|---|---|---|---|---|---|---|
| BB charged | 309 | −2.08 [−2.85, −1.31] | −0.33 [−1.06, +0.39] | −1.75 [−2.44, −1.19] | −2.42 [−3.27, −1.56] | −0.20 [−1.04, +0.64] | −2.22 [−3.07, −1.52] |
| BB at wake | 309 | −2.52 [−3.25, −1.79] | −0.65 [−1.39, +0.09] | −1.87 [−2.58, −1.31] | −2.81 [−3.61, −2.02] | −0.32 [−1.14, +0.49] | −2.49 [−3.42, −1.79] |
| HRV | 309 | −0.28 [−0.73, +0.16] | +0.03 [−0.46, +0.51] | −0.31 [−0.50, −0.15] | −0.30 [−0.78, +0.17] | +0.16 [−0.42, +0.73] | −0.46 [−0.72, −0.22] |
| next-day RHR | 304 | −0.01 [−0.17, +0.15] | +0.03 [−0.16, +0.22] | −0.05 [−0.13, +0.05] | +0.02 [−0.14, +0.17] | +0.15 [−0.05, +0.35] | −0.13 [−0.26, +0.02] |

(HC1 CIs shown. HAC7 CIs are within ±0.15 of HC1 on every row, and no conclusion changes. Full HAC columns are in the
scratchpad `tables_v1.txt`.)

### CHECK — calendar spec

| outcome | n | TOTAL HC1 | TOTAL HAC7 | DIRECT HC1 | DIRECT HAC7 | INDIRECT [block boot] |
|---|---|---|---|---|---|---|
| BB charged | 286 | −1.69 [−2.48, −0.90] | [−2.68, −0.71] | −0.35 [−1.06, +0.36] | [−0.93, +0.23] | −1.34 [−2.32, −0.80] |
| BB at wake | 282 | −1.94 [−2.68, −1.19] | [−2.95, −0.92] | −0.41 [−1.05, +0.24] | [−1.05, +0.24] | −1.53 [−2.55, −0.90] |
| HRV | 222 | −0.25 [−1.00, +0.50] | [−0.95, +0.44] | +0.14 [−0.70, +0.99] | [−0.53, +0.82] | −0.40 [−0.78, −0.15] |
| next-day RHR | 282 | +0.01 [−0.30, +0.31] | [−0.22, +0.24] | +0.05 [−0.32, +0.41] | [−0.21, +0.30] | −0.04 [−0.18, +0.08] |

- a-path: −0.42 [−0.54, −0.30], n = 286.
- CHECK base and proxy specs give the same picture. TOTAL BB charged is −1.62 (base) and −1.92 (proxy); DIRECT is
  −0.24 and −0.07, both with CIs spanning 0.
- HRV exists only from late 2025, so the CHECK HRV rows have the same 222 nights as MAIN.

### 2c. a-path split by constrained wake (sleep_h lost per 1 h later bedtime)

| sample / flag source | flag = no: n, a [95% CI] HC1 / HAC | flag = yes: n, a [95% CI] HC1 / HAC |
|---|---|---|
| MAIN / calendar early_shift | 174, −0.77 [−0.93, −0.61] / [−0.94, −0.60] | 48, −0.34 [−0.55, −0.12] / [−0.55, −0.13] |
| MAIN / proxy | 249, −0.69 [−0.83, −0.54] / same | 60, −0.90 [−1.07, −0.72] / [−1.13, −0.66] |
| CHECK / calendar early_shift | 219, −0.46 [−0.60, −0.31] / [−0.70, −0.21] | 67, −0.28 [−0.41, −0.15] / same |
| CHECK / proxy | 320, −0.55 [−0.68, −0.43] / [−0.75, −0.36] | 85, −0.65 [−0.86, −0.44] / [−0.89, −0.41] |

Indirect effect per stratum, using b from the pooled DIRECT model (BB charged, MAIN calendar):
- not early shift: −2.22 [−3.48, −1.39]
- early shift: −0.98 [−3.06, −0.59]

The early-shift bootstrap CI is skewed: the point estimate sits near its upper edge, and the stratum has n = 48. Treat
it as imprecise.

## 4. Reverse causation: does bedtime follow prior-day signals?

The model is `bed_h ~ prev_rhr_high + prev_stress + bb_bed + schedule`:
- `prev_rhr_high`: RHR on d−1 above its 30-day median (days d−31..d−2).
- `prev_stress`: stress on d−1.
- `bb_bed`: BB at sleep onset = BB at wake − BB charged.

| term | MAIN all (n = 308; + weekday) | MAIN calendar (n = 221; + shifts) | CHECK all (n = 385) | CHECK calendar (n = 266) |
|---|---|---|---|---|
| prev RHR > 30-d median (h) | −0.22 [−0.70, +0.27] | −0.31 [−0.96, +0.34] | −0.36 [−0.84, +0.13] | −0.49 [−1.12, +0.15] |
| prev-day stress (h / point) | +0.003 [−0.028, +0.034] | −0.014 [−0.050, +0.023] | +0.006 [−0.023, +0.034] | −0.003 [−0.036, +0.031] |
| BB at bedtime (h / BB point) | −0.059 [−0.103, −0.014] | −0.059 [−0.121, +0.003] | −0.040 [−0.071, −0.009] | −0.028 [−0.070, +0.014] |
| weekday wake / early shift | −1.99 [−2.61, −1.37] | −0.65 [−1.44, +0.14] | −1.88 [−2.45, −1.31] | −0.74 [−1.48, −0.00] |
| late shift d−1 | — | +0.64 [−0.18, +1.45] | — | +0.62 [−0.14, +1.37] |
| R² | 0.177 | 0.074 | 0.151 | 0.053 |

(HC1 shown; HAC7 CIs are within ±0.06 and no sign or significance call changes, except BB at bedtime in MAIN calendar:
HAC [−0.116, −0.002].)

Prior-day signals by bedtime bin (MAIN):

| bin | n | share with prev RHR > median | prev stress | BB at bedtime |
|---|---|---|---|---|
| before 12 | 95 | 0.44 | 49.7 | 9.0 |
| 12–1 | 72 | 0.48 | 48.0 | 7.7 |
| 1–2 | 63 | 0.51 | 46.6 | 7.8 |
| after 2 | 77 | 0.40 | 53.6 | 5.7 |

**The RHR level is what separates them.** On before-midnight nights:
- PRE (2025-01-01..2025-10-05): prior-day RHR 70.1 vs 68.3 on other nights (n = 19 vs 76).
- MAIN: 71.9 vs 71.0 (n = 95 vs 214).

## 5. The CHECK RHR result (+0.90 bpm per hour asleep before midnight), re-run

The model is `rhr_next ~ sleep_h + hours asleep before midnight + controls`.

| spec | sample | n (before-mid nights) | coef HC1 | coef HAC7 |
|---|---|---|---|---|
| original (prev_stress) | CHECK | 399 (110) | +0.90 [+0.48, +1.32] | [+0.44, +1.35] |
| + proxy | CHECK | 399 (110) | +0.99 [+0.55, +1.44] | [+0.44, +1.54] |
| same covered nights, no shift ctrl | CHECK | 282 (71) | +0.95 [+0.52, +1.37] | [+0.48, +1.41] |
| **+ calendar shifts** | CHECK | 282 (71) | **+1.01 [+0.57, +1.44]** | [+0.47, +1.55] |
| + calendar + proxy | CHECK | 282 (71) | +1.12 [+0.65, +1.59] | [+0.54, +1.70] |
| **+ prior-day RHR (d−1)** | CHECK | 397 (110) | **−0.11 [−0.30, +0.08]** | [−0.28, +0.06] |
| + calendar + prior-day RHR | CHECK | 280 (71) | −0.12 [−0.33, +0.08] | [−0.30, +0.06] |
| original | PRE only (2025-01..2025-10-05) | 95 (17) | +1.30 [+0.77, +1.83] | [+0.63, +1.97] |
| + prior-day RHR | PRE only | 94 (17) | −0.17 [−0.42, +0.09] | [−0.42, +0.08] |
| original | MAIN | 304 (93) | +0.48 [−0.36, +1.32] | [−0.35, +1.31] |
| + calendar shifts | MAIN | 218 (64) | −0.00 [−0.66, +0.65] | [−0.53, +0.52] |

- Leave-one-month-out on the original CHECK spec: the coefficient ranges +0.72..+0.96, so no single month drives it.
- In the MAIN calendar model, `early_shift` on d raises next-day RHR by +0.95 [+0.03, +1.87].

## 6. Alcohol

`data\alcohol.csv` starts on 2026-10-23 (2 rows: 10/23 = 2 drinks, 10/24 = 4). That is after the CSV ends (2026-10-05).
Note only; no model.

---

## OBSERVATIONS (numbers only)

1. A shift calendar exists. It covers 71–72% of sample nights, with two blank stretches: 2025-08-28..11-20 and
   2026-08-26..09-27. Early-shift mornings: wake 6.99, sleep 6.23 h, BB charged 25.5 (n = 48). Off days: wake 10.32,
   sleep 8.38 h, BB charged 33.5 (n = 141).
2. In both samples and in all 3 control specs, a later bedtime has a clear **TOTAL** cost on BB charged (−1.6 to −2.6 per
   hour) and on BB at wake (−1.9 to −3.2 per hour). The CIs exclude 0 under both HC1 and HAC7.
3. The **DIRECT** effect, holding sleep_h fixed, is 0 within the CI in 11 of 12 BB rows. The exception is MAIN calendar,
   BB at wake: −1.18 [−2.29, −0.07], HAC [−2.25, −0.10].
4. The **INDIRECT** path (later bed → less sleep → less BB) carries most of the total, about 70–100% of it. For BB charged
   it is −1.3 to −2.2 per hour, and every bootstrap CI excludes 0. HRV has a small indirect effect (−0.3 to −0.5 ms per
   hour, CIs exclude 0) and a null total and direct effect. Next-day RHR is null on all three.
5. The a-path is −0.42 to −0.90 h of sleep per hour later. On calendar early-shift mornings it is smaller (−0.34 MAIN,
   −0.28 CHECK) than on other nights (−0.77, −0.46). With the proxy flag the ordering reverses (−0.90 vs −0.69 in MAIN).
6. Prior-day stress and the RHR-above-median flag do not predict bedtime; all CIs span 0. Higher BB at sleep onset goes
   with an earlier bedtime: −0.06 h per BB point in MAIN, CI excludes 0. The after-2 AM bin has the highest prior-day
   stress (53.6 vs 49.7 before midnight).
7. The CHECK +0.90 RHR result survives the shift controls (+1.01 [+0.57, +1.44]). It disappears when prior-day RHR is
   controlled (−0.11 [−0.30, +0.08]). It comes almost entirely from the 95 nights in 2025-01..10-05 (+1.30, only 17
   before-midnight nights) and is 0 in MAIN.

## INTERPRETATION (judgment; observational, n = 1)

- **Bedtime matters for recovery, but through sleep length, not clock time.** Moving bedtime 1 h later costs about 2
  BB points by morning because it costs about 0.4–0.8 h of sleep. With sleep length held fixed, the clock time adds
  little or nothing; the one exception (MAIN BB at wake, about −1.2 per hour) is marginal. In practice: protect total
  sleep. An earlier bedtime helps when the wake time is fixed, because that is exactly when it buys sleep.
- **The split by work day mostly says that on early-shift mornings sleep is short whatever the bedtime.** The a-path
  is flatter there (−0.34) because both bedtime and the hard wake are clustered: an early night and a 7 AM alarm still
  give about 6.2 h. The proxy shows the opposite ordering because it mixes non-work early wakes in. The calendar split
  is the trustworthy one; the proxy is not.
- **No reverse causation of the feared kind.** Early bedtimes do NOT follow "run-down" days by stress or by the
  RHR-above-median flag. The BB-at-bedtime link is at least partly mechanical: a later bedtime means more awake drain
  before sleep onset, so BB at onset is lower. It should not be read as "low battery causes late nights".
- **One reverse-causation exception, and it matters: the CHECK RHR result.** The +0.90 bpm per hour asleep before
  midnight is NOT a harm from early sleep. Before-midnight nights in early 2025 followed days when RHR was already
  higher. Hold prior-day RHR fixed and the effect is −0.11 [−0.30, +0.08]. The likely story is "went to bed early
  because already unwell / elevated", and the elevation carried into the next day. Work schedule does not explain it.
  Verdict: **withdraw the CHECK RHR finding**. Treat it as RHR persistence plus going to bed early when unwell, not as
  a bedtime effect.
- Caveats:
  - The calendar gaps were treated as unknown, not "off"; if they were leave, adding them would change the off-day
    means.
  - The alarm-like-wake definition could not be built for these samples.
  - The HAC lag counts rows. Its CIs were nearly identical to HC1 anyway, so serial correlation is not changing the
    calls.
