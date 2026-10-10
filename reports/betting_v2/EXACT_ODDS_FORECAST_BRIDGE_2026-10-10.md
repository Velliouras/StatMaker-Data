# Betting V2 — Real bookmaker odds vs historical xG/ELO crosswalk

**Verified:** [GitHub Actions offline run 38084919366](https://github.com/Velliouras/StatMaker-Data/actions/runs/38084919366), Data/main commit `26ed7dab107a094e10de1fbce2334d6b566cbe63`. **143/143 tests passed** and no new Odds-API.io or API-Football requests.

## Evidence from 13 existing Brazil receipts

| Measure | Count |
|---|---:|
| Receipts scanned | 13 |
| Integrity-validated receipts (including explicitly partial snapshots) | 12 |
| Pre-kickoff raw price observations from stored partial/full snapshots | 22,876 |
| Supported full-time 1X2, double chance, draw no bet, match/team goal O/U *in research odds contract* (1.80 < odd < 3.00) | 140 |
| Price observations with exact same-provider event ID + both source teams + UTC kickoff in canonical Odds-API.io schedule | 140 |
| Distinct verified same-provider scheduled events | **11** |
| Matches to completed API-Football historical fixture cache, uniquely verified by exact teams/UTC kickoff | **0** |
| Priced, forecast-as-of-quote-certified xG/ELO outcomes | **0** |
| Independently certified positive EV or net ROI | **Not available** |
| Certified STRONG / PROD V2 | **0 / BLOCKED** |

### What is verified and what is not

The schedule crosswalk matches the **same Odds-API.io event** against the latest independently persisted schedule from that provider with identical provider event ID, exact raw team names and UTC kickoff. If ambiguous or missing, the join fails closed. It **does not** verify the independent API-Football fixture ID or produce a historical score settlement, so it is not a substitute for the xG/ELO model fixture join.

The completed-fixtures model cache does not contain future scheduled games. Therefore zero completed-API fixture matches here is not a defect in the market mapper. The final historical holdout is separate from the October 2026 newly collected receipt sample. One must **not** use already settled results to create retroactive predictions, quote observation time or fictitious ROI.

**Required before priced certification:**
1. Exact independent API-Football future fixture crosswalk from already acquired scheduled fixture IDs (reject ambiguous/missing matches).
2. Freeze a genuinely pre-kickoff xG or ELO-fallback forecast with evidence of feature as-of time, model version, independent calibration population, and source content hash. No fabricated xG.
3. Match the frozen forecast to the independently recorded dated bookmaker selection, including a verified price-observation time and completeness/selection-universe gates; exclude stale offers, half-time, Asian/handicap and unsupported markets.
4. Settle only after a confirmed FT score, deduplicate correlated selections per fixture and calculate reproducible out-of-time EV/ROI with uncertainty and adverse lineup stress.
5. Keep certification fail-closed and Android PROD untouched until independently reviewed.

Implemented code:
- `scripts/betting_v2/receipt_forecast_bridge.py`
- `tests/test_betting_v2_receipt_forecast_bridge.py`
- `.github/workflows/betting-v2-offline-qa-manual.yml`

All analysis is offline and research-only; raw receipt/probability counts are **not** betting recommendations.
