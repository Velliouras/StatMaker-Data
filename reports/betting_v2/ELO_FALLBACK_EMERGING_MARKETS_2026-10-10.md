# V2 ELO fallback: real historical coverage and outcomes in six additional leagues

**Date:** 2026-10-10. **RESEARCH ONLY / NOT CERTIFIED.**

This report uses actual committed domestic_enriched cached match outcomes, retrospectively replayed in a strictly prior-UTC-date independent JavaScript translation of the current ELO+observed-goals formulas. ELO weights were **fixed before this evaluation** (recent=0.50, venue=0.65, ELO=0.50). These are NOT output of the official Python pilot and NOT an untouched model-certification holdout. No bookmaker odds, ex-ante quote provenance, ROI, lineups or adverse scenarios were evaluated.

Calendar-year source data: ARG 2026, BRA 2026, CHN 2026, NOR 2026, SWE 2026; JPN uses 2026 historical support and 2026-2027 current target. Histories and ELO update only after every fixture in a given UTC date batch. ELO readiness requires at least 8 prior matches per side, three prior home/away matches in each last-20 window, and twenty earlier league match results. Primary xG readiness additionally requires xG/xGA history meeting last-20, recent-five and last-eight home/away gates. No missing xG is inferred.

| League | Completed | Primary xG ready | Extra ELO-only ready | Neither | ELO-only raw 1X2 correct | Raw top-1X2 p≥60% correct |
|---|---:|---:|---:|---:|---:|---:|
| Argentina (ARG) | 423 | 3 | 298 | 122 | 124/298 | 13/20 |
| Brazil (BRA) | 289 | 147 | 60 | 82 | 31/60 | 9/11 |
| China (CHN) | 211 | 22 | 125 | 64 | 52/125 | 8/17 |
| Japan (JPN) | 285 | 22 | 183 | 80 | 92/183 | 7/12 |
| Norway (NOR) | 169 | 85 | 17 | 67 | 9/17 | 4/5 |
| Sweden (SWE) | 177 | 0 | 113 | 64 | 57/113 | 18/26 |
| **Total** | **1554** | **279** | **796** | **479** | **365/796** | **59/91** |

Thus feature-eligible cases rise from **279 to 1,075** when ELO-only *research* mode is permitted. They are not picks. **The ELO-only historical 1X2 argmax correctly identified 365/796 = 45.85%**. Among 91 retrospectively observed cases where the raw maximum probability was at least 60%, 59 were correct (**64.84%**). The corresponding 95% Wilson lower bound is only **54.61%**, well below the user's required 60% conservative minimum. China is 8/17 in this raw subset. These preliminary data do not substantiate STRONG, and any re-tuning based on them must use new independent, later holdout periods.

IMPORTANT CORRECTION: An earlier exploratory JS coverage count reported primary=249, additional ELO=762 and neither=543. That calculation failed to store the earliest fixture of some teams when populating its history map. The corrected map-retention replay yielded **279 / 796 / 479** and independently agrees with the 796 ELO-only result-event count. The Python make_elo_rows history uses defaultdict and was not shown to share the exploratory JS map bug; full Python parity still needs to be established.

## Model release guardrails

- ELO-only cases must maintain a separate fixed training, calibration and holdout regime. Per-league and per-market lower bounds and an independent bona fide bookmaker price universe are required; global raw 1X2 accuracy cannot certify a league.
- Actual quotes must have independently verified observation times; old prepared_selections snapshot cutoffs are not quote timestamps.
- Continue to reject countries with inadequate score and venue history; do not silently substitute missing xG or widen price limits.
- No Android PROD changes, GitHub Actions, API-Football calls or forecasts certified by this report.
