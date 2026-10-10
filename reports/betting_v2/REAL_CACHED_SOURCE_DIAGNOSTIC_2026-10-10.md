# Betting Engine V2 — read-only cached source diagnostic (2026-10-10)

## Status and scope

**Research / NOT CERTIFIED.** Analysis on the real checked-in
`domestic_enriched` and original `data/api_football/fixture_stats` cache,
retrieved read-only via the connected GitHub repository. No network provider
calls, GitHub Actions, App-Ready modifications, official Python CLI or
historical bookmaker-price settlement were involved.

**Important limitation:** prediction metrics below are produced by an
independent in-memory JavaScript translation of the strict source-window
rules and frozen *exploratory* parameters, **NOT** by execution of the
committed Python `run_pilot.py`. Exact Python/JavaScript parity remains
unverified. These outcomes must **not** be called a model certificate or
independently validated profit evidence.

Exploratory, previously chosen weights: recent 0.50; venue 0.65; xG 0.80;
ELO 0.50. Walk-forward processing updates ELO and form after all games
on the same UTC fixture date; no same-date final results are used when
generating earlier features. Feature gates: both teams at least eight
earlier matches, three relevant venue games, eight observed xG and xGA
in the last 20, complete recent-five xG and xGA, three observed
home/away xG and xGA within the latest 20 games, and 20 prior league
scores. No invented xG substitutions.

## Eight 2025–26 cached domestic competition files

| League | Finished matches inspected | Both xG observed | Strict feature eligible |
|---|---:|---:|---:|
| England Premier League E0 | 380 | 380 | 300 |
| Germany Bundesliga D1 | 308 | 306 | 234 |
| Spain La Liga SP1 | 380 | 380 | 300 |
| Italy Serie A I1 | 380 | 380 | 300 |
| France Ligue 1 F1 | 307 | 305 | 233 |
| Netherlands Eredivisie N1 | 309 | 304 | 226 |
| England Championship E1 | 557 | 557 | 460 |
| Belgium Jupiler Pro League B1 | 321 | 314 | 249 |
| **Total** | **2,942** | **2,926** | **2,302** |

These are coverage counts, not predictions satisfying calibrated
conservative >=60% and positive EV at original odds. In particular, the
2025–26 score evaluation is not an independently frozen holdout study.

## Subsequent 2026–27 fixture observations, four leagues

Original 2025–26 and 2026–27 cache files were processed together, with
historical features carried across season boundaries but not imputed.

| League | Finished 2026–27 matches | Both xG observed | Strict eligible | Raw 1X2 argmax correct | Raw argmax p>=60% count (correct) |
|---|---:|---:|---:|---:|---:|
| E0 | 50 | 17 | 16 | 6/16 | 0 (0) |
| D1 | 37 | 10 | 12 | 8/12 | 5 (5) |
| SP1 | 70 | 31 | 28 | 14/28 | 4 (4) |
| I1 | 50 | 40 | 22 | 18/22 | 1 (1) |
| **Total** | **207** | **98** | **78** | **46/78** | **10 (10)** |

These outcomes are **descriptive only**. The apparent 10/10 among raw
argmax p>=60% is a very small, retrospectively inspected subgroup,
without conservative probability bounds, uncertainty, bookmaker odds,
model certificates, injury/lineup stress tests, or independent evidence of
EV/ROI. It is **not** a reason to issue STRONG picks.

The latest strict-eligible fixture across these four 2026–27 competition
samples is **2026-09-07**. Missing later xG systematically blocks
current strict-feature readiness.

## Source-level xG gap confirmed against provider raw_statistics

Checked original archived `raw_statistics` for **both** team entries,
requiring a non-null provider `expected_goals` observation; counts agree
with `normalized_stats.HxG/AxG` in every month checked.

| League | August 2026 provider xG / FT | September 2026 provider xG / FT | October 2026 provider xG / FT |
|---|---|---|---|
| E0 | 17/20 | **0/30** | no completed source fixtures |
| D1 | 9/9 | **0/27** | 1/1 |
| SP1 | 30/30 | **0/39** | 1/1 |
| I1 | 20/20 | 20/30 | no completed source fixtures |

Source cache files are generated on **2026-10-10 at ~09:48 UTC**
and expose this gap directly. The V2 no-imputation rule must remain.
September is **not** a failure of the enriched `HxG/AxG`
normalizer when the upstream raw `expected_goals` is absent.

## Blocking conditions

1. The actual committed Python unit suite and full `run_pilot.py` must
   still be executed from a full locally readable Data checkout. This
   connector-only analysis cannot establish a Python test pass.
2. Historical prices from `prepared_selections` are a
   legacy-prepared subset, not proven unfiltered bookmaker offers;
   individual quote observation timestamps, bias and full pricing
   coverage remain unverified.
3. No verified conservative calibration >=60%, realistic adverse
   lineup probability, independent ROI, or true future immutable
   forecast certificate exists.
4. New season xG coverage is insufficient for many late fixtures;
   no synthetic substitution and no quota-spending provider refresh.
5. No production promotions or modifications. V2 certified STRONG
   recommendations: **0**. V2 provider API requests in this investigation: **0**.

Research only — not a prediction feed.
