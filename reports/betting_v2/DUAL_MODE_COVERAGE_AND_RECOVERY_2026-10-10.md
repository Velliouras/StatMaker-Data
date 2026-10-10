# Betting V2 — empirical data coverage with ELO fallback (2026-10-10)

Research-only. The committed Python implementation was NOT executed in this connector-only environment. The counts below are an independent JavaScript replication on real checked-in 2025–26 and 2026–27 domestic_enriched cache data. These are feature-eligibility counts, not winning forecasts or certified picks.

Mode selector: sufficient pregame observed xG -> xG model. Insufficient xG but both teams have >=8 prior completed games, >=3 matching venue games within last 20 and the league has >=20 prior completed fixtures -> isolated ELO + observed goals/form/venue model. Insufficient goals/ELO history -> no forecast.

| 2026–27 league | Finished | xG ready | Extra ELO-only ready | Neither |
|---|---:|---:|---:|---:|
| E0 Premier League | 50 | 16 | 20 | 14 |
| D1 Bundesliga | 37 | 12 | 14 | 11 |
| SP1 La Liga | 70 | 28 | 21 | 21 |
| I1 Serie A | 50 | 22 | 14 | 14 |
| F1 Ligue 1 | 46 | 19 | 17 | 10 |
| N1 Eredivisie | 64 | 8 | 36 | 20 |
| **Total** | **317** | **105** | **122** | **90** |

Feature eligibility rises from 105 to 227. Actual p>=60% calibration, uncertainty/adverse-case checks and positive expected value at original 1.80<odds<3.00 are NOT verified. No STRONG selections are published.

## Understat backfill mechanism

Research-only ingestion from Understat is implemented for E0, D1, SP1, I1 and F1. Import requires matching league, final score, canonical home/away names, close fixture date and a unique Understat match. Existing conflicting canonical xG are never overwritten. The result is a separate research overlay tagged with sourceObservedAtUTC and historicalAsOfVerified=false, so it cannot be silently used as historical information before observed source availability.

**No live Understat xG were actually recovered here**: direct access failed in both the browser and container because of network restrictions. API-Football requests: zero. The script offers one explicit Understat fetch per league-season or imports an already-downloaded Understat JSON. Availability and permissible third-party data use must be verified before executing the fetch locally.

## Offline pipeline

- walk_forward_elo.py: independent chronological ELO + observed-goals model, training split, separate calibration and holdout.
- run_pilot.py: executes both xG primary and ELO-only fallback research, with independent price-calibration report if --with-prices is enabled.
- publish_shadow.py: safe JSON-only default; explicitly approved reports JSONL allowed, addressing a startup failure.

The official full Python pilot and new tests are not confirmed as passing in this GitHub-only environment. No Actions or App-Ready writes were made.
