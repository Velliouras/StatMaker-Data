# Betting V2: broader cached ELO fallback screen (2026-10-10)

**RESEARCH ONLY: zero certified STRONG, zero provider calls, zero Actions, no PROD changes.**

This is an independently coded **JavaScript mirror** of the frozen exploratory Elo + scored-goals + home/away Poisson equations in `walk_forward_elo.py`, with weights recent=0.50, venue=0.65, Elo=0.50. This is **not** the official Python pilot, not a newly tuned/calibrated model and **not an untouched certified holdout**. Dates are processed chronologically; same-date final outcomes are not visible until after the day's predictions. Data comes only from checked-in `domestic_enriched` finished fixtures in StatMaker-Data/main, at the source state observed on 2026-10-10. No historic independent bookmaker offers, no prices, no injury/lineup scenarios were inspected.

This screen follows the strict historical fallback rule: at least 8 previous team matches, 3 previous respective-venue matches in the last 20, and 20 prior league results; only fixtures with insufficient verified **prior** xG history enter the Elo-only fallback population. Canonical historical xG is not replaced or inferred.

## Six 2025–26 + 2026–27 European leagues

| League | Elo fallback eligible | Raw top-1X2 correct | Raw top-1X2 p>=60%: correct/total |
|---|---:|---:|---:|
| England E0 | 25 | 10 | 3/5 |
| Germany D1 | 18 | 9 | 4/6 |
| Spain SP1 | 23 | 11 | 5/5 |
| Italy I1 | 16 | 8 | 3/3 |
| France F1 | 18 | 12 | 1/1 |
| Netherlands N1 | 48 | 27 | 9/15 |
| **Total** | **148** | **77** | **25/35** |

## Six additional league groups with limited xG: 2026, plus Japan 2026–27

| League | Elo fallback eligible | Raw top-1X2 correct | Raw top-1X2 p>=60%: correct/total |
|---|---:|---:|---:|
| Argentina ARG | 298 | 124 | 13/20 |
| Brazil BRA | 60 | 31 | 9/11 |
| China CHN | 127 | 53 | 8/17 |
| Japan JPN | 184 | 93 | 7/12 |
| Norway NOR | 19 | 10 | 5/7 |
| Sweden SWE | 116 | 58 | 18/26 |
| **Total** | **804** | **369** | **60/93** |

**Combined exploratory screen:** 952 fallback matches; 453/952 raw top-1X2 correct. Among 128 raw top-1X2 forecasts with probability >=60%, 85 results correct (**66.41% observed**), but the **95% Wilson lower bound is only 57.85%**. This fails to demonstrate the mandatory conservative 60% criterion even *before* incorporating bookmaker odds or adverse lineup scenarios. The subgroup is not an independently calibrated STRONG selection universe.

## Critical Over/Under source-specific finding

At **raw P(Under 3.5)>=60%** the Elo-only baseline issued 690 observations across ARG/BRA/CHN/JPN/NOR/SWE, of which only 511 succeeded (74.06%). But this aggregate masks unacceptable league-level differences:

- **China CHN:** 42/77 Under 3.5 occurrences actually held (54.55%); the Wilson 95% lower bound is **43.47%**. A bare raw 60% threshold would be dangerously misleading.
- **Argentina ARG:** 245/298 Under 3.5 occurrences held (82.21%). China and Argentina cannot inherit one another's calibration or uncertainty.
- For comparison, raw P(Over 3.5)>=60% produced just 2 observations in those six leagues, both successful; this is far too small to imply reliable Over 3.5 performance.

This is a **statistical model-coverage and calibration warning**, not a retrospective wager screen: none of these events were filtered by verified bookmaker prices within **1.80 < odd < 3.00**. It does not prove that the old app selections won or lost.

## Required next steps / code state

1. Run the **official Python** `scripts/betting_v2/run_pilot.py` end-to-end on the complete Data checkout; do not describe the JavaScript mirror as that run.
2. New `raw_market_holdout.py` diagnostics in the pilot report untouched, chronological observed results for each market/league **separately for primary xG and Elo-only**. Their output is **never certified**.
3. Enforce frozen, minimum-sample **market-and-league calibration** and adverse lineup bounds, then join independently verified full-universe, exact fixture/selection odds observed before kickoff. Legacy prepared selections cannot be treated as this universe.
4. Under-heavy, optimistic or small-sample strata must **fail closed**, not be relaxed to create STRONG picks.
5. Official Python regression suite and device/UAT are pending. No Android main/PROD change is authorized by this report.

**Important data audit note:** `domestic_enriched/index.json` currently labels some still-active 2026 calendar-year league snapshots (including China, Brazil, Argentina, Norway and Sweden) `historical_support` despite the published `lifecycle=active` and in-season end dates. This needs a **separate consumer-filter/season-target audit** before concluding why a country does or does not appear in the application. Do not change these production labels without checking their downstream semantics.
