# Betting V2 — Twelve-league historical replication (2026-10-10)

## Scope and method
Read-only GitHub connector inspection of **12 actual domestic_enriched schema-v3 season files** on `StatMaker-Data/main`. Calculations were made using an independent in-memory JavaScript replication of the existing research-only Poisson/ELO/xG/venue model. **The committed Python scripts were not executed and Python/JavaScript numerical parity has not yet been verified.** No API-Football calls, no GitHub Actions, no app builds.

- Twelve competitions: Belgium Jupiler Pro League; England Championship, League One, Premier League; France Ligue 1; Germany Bundesliga; Italy Serie A, Serie B; Netherlands Eredivisie; Spain La Liga, Segunda División; Turkey Süper Lig.
- Total completed matches checked: **4,658**.
- Fixture xG complete for home and away: **3,963**.
- Historically feature eligible **before kickoff** (8 prior matches and xG observations/team, 3 relevant venue observations and xG per side, prior league scores): **3,164**.
- England League One and Segunda División, especially, have partial xG coverage; global BB-ready metadata must NOT be treated as a Betting V2 certification.

## Strict chronological replication

One global chronology across these 12 competitions, with per-league historical teams/venues/ELO and no same-date future results. Model weights selected on the tuning period **only**, then evaluated on separate calibration and untouched holdout.

| Metric | Result |
|---|---:|
| Eligible pre-match fixtures | 3,164 |
| Fit history cutoff | 2026-02-23 |
| Tuning cutoff | 2026-04-09 |
| Calibration cutoff | 2026-05-14 |
| Tuning fixtures | 668 |
| Calibration fixtures | 615 |
| Untouched final holdout fixtures | 164 |
| Tuning 1X2 log-loss | 1.0179 |
| Calibration 1X2 accuracy | 50.73% |
| Calibration log-loss | 1.0101 |
| Holdout 1X2 accuracy (argmax) | 44.51% |
| Holdout log-loss | 1.0663 |
| Calibration fixtures with **RAW** highest 1X2 p>=60% | 67 |
| Successful among those 67 | 50 (74.63%) |
| **Holdout fixtures with RAW highest 1X2 p>=60%** | **18** |
| **Correct among those 18** | **10 (55.56%)** |
| Mean RAW modeled p for those 18 | 65.74% |
| ROI with real pregame odds 1.80–3.00 | **NOT MEASURED** |
| StatMaker V2 STRONG/High/Confirmed/Developing certified | **0** |

Exploratory winning parameter grid values (NOT production approved): recent weight 0.50; venue weight 0.65; xG weight 0.80; Elo weight 0.50.

**Conclusion:** Even the expanded **RAW p>=60%** subset did NOT achieve 60% observed success on the held-out 18 fixtures and is significantly less reliable than the earlier, much smaller three-league 9/11 observation. This is not sufficient evidence of calibration, conservative >=60% win probability, positive price-value or profits. The new model remains strictly fail-closed. **Do not retune parameters against this seen holdout.** A new unseen time period or genuinely independent dataset is required to evaluate any new architecture revision.

## Next mandatory gates
1. Execute the actual committed offline Python pipeline and verify this independent replication against it.
2. Audit historical bookmaker quote availability for the same exact fixture/market IDs and dates, then produce priced ROI per market/league.
3. Build and freeze a new model architecture using prior training/tuning data only; get future untouched results before declaring any certification.
4. Verify real lineup/rotation adverse-case bounds; use no proxy certification.
5. Keep the official `StatMaker-Data/main` App-Ready publisher, scheduled workflows and API calls unchanged in the shadow phase.

This file documents research-only source facts and replication, **not a validated forecast file**.
