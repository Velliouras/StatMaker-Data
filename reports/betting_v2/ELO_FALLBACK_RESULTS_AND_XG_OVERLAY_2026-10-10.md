# Betting V2 — real cached ELO fallback and xG source recovery (2026-10-10)

**RESEARCH ONLY — 0 certified STRONG, 0 new API-Football calls, no Actions or PROD writes.**

## Source-matched missing xG: EPL 2026–27

Canonical fixture cache has 50 finished EPL 2026–27 fixtures: 17 originally have both xG, 33 lack xG. A dated public third-party GitHub mirror of Understat EPL 2026 data contains 50 finished fixtures. Using exact home/away team, final score, source fixture kickoff within 12 hours and two explicit alias mappings (Hull City→Hull and Newcastle→Newcastle United), all **33 of 33** missing-xG canonical fixtures uniquely match.

All 33 recovered overlay rows subsequently passed a separate canonical-fixture integrity check (exact fixture ID, names, score, kickoff agreement within 15 minutes, no duplicate IDs, no overwriting existing canonical xG). Audit is retained in `reports/betting_v2/understat_xg_E0_2026_integrity.json`; values in `reports/betting_v2/understat_xg_E0_2026.json`.

**Caution:** 16 of the 17 already complete API-Football xG fixture entries do not agree to within 0.02 with the mirror. These sources implement different xG models. Mixing them without independent normalization/model retraining and calibration is invalid. The third-party mirror is not a direct Understat attestation or verified licensed production feed. Its referenced Git commit is dated 2026-10-06T20:59:42Z; xG from that snapshot cannot be assumed to have been available before that time. No historical prediction before that date is retroactively changed.

## Elo-only first reality check: six 2026–27 leagues

Independent JS replay of the source's ELO + observed goals/venue Poisson formulas, with frozen exploratory weights (recent=0.50, venue=0.65, Elo=0.50). Uses past-date fixture histories from the 2025–26 and 2026–27 checked-in domestic_enriched cache, strictly updating after each same-day fixture batch. **Not a run of the official Python research pilot and not a certified untouched holdout.**

| League | Fallback-ready matches | Correct raw 1X2 argmax | Raw 1X2 top probability >=60% (won) |
|---|---:|---:|---:|
| Premier League E0 | 20 | 8 | 3 (2) |
| Bundesliga D1 | 14 | 7 | 4 (3) |
| La Liga SP1 | 21 | 10 | 4 (4) |
| Serie A I1 | 14 | 7 | 2 (2) |
| Ligue 1 F1 | 17 | 11 | 0 (0) |
| Eredivisie N1 | 36 | 19 | 13 (9) |
| **Total** | **122** | **62** | **26 (20)** |

Overall ELO-only 1X2 argmax accuracy: **62/122 = 50.82%**. Raw-highest probability subgroup: **20/26 = 76.92%** retrospective successes. **Wilson 95% lower confidence bound for 20/26 = 57.95%, below the mandatory 60% conservative threshold.** This does not certify STRONG picks, because it has no independent price/ROI, reliability per market/league, full injury/lineup adversity, or pre-approved walk-forward model certification. The ELO research branch remains shadow-only.

## Significant code correction

The earlier ELO research tuner optimized parameters on the *whole* ELO-ready sample, including xG-rich matches, then evaluated only xG-missing holdouts. That creates training-population mismatch. `walk_forward_elo.py` now fits, calibrates and holds out using **only the fallback population with insufficient genuine pregame xG history**, with explicit fail-closed behavior when there are fewer than 50 fallback fixture dates or disjoint splits are unavailable. A targeted test was added; the full Python suite has not yet been executed.

### Deployment restrictions

- No xG overwrite in canonical source and no modification of previous recommendations/history.
- No ELO-only probability may inherit the primary xG model's calibration.
- The original 1.80 < odds < 3.00, conservative win >=60%, adverse scenario >=60%, and positive conservative EV requirements still apply.
- No provider calls or Actions needed for these research evaluations.
