# Elo Betting A/B Backtest

- Window: **2026-09-05 → 2026-10-04**
- Historical snapshot policy: **latest App-Ready manifest at/before 11:00 Europe/Athens per day**
- Monte Carlo runs per historical snapshot: **10000**
- Days processed: **27** / 30
- Days skipped: **3**

## Result

| Metric | Legacy simulation | Elo-backed simulation |
|---|---:|---:|
| Strong picks | 638 | 633 |
| Graded | 592 | 586 |
| Won | 359 | 356 |
| Lost | 231 | 228 |
| Void | 2 | 2 |
| Hit rate | 60.8% | 61.0% |
| Average odds | 1.65 | 1.65 |
| ROI (1 unit/pick) | -0.6% | -0.3% |
| Avg final probability | 73.7% | 73.7% |
| Settlement coverage | 92.8% | 92.6% |

## Decision changes

- Same pick: **620**
- Different selection on same match: **12**
- Legacy-only pick: **6**
- Elo-only pick: **1**
- Legacy losing picks removed/changed by Elo: **8**
- Legacy winning picks removed/changed by Elo: **10**
- Elo added/changed picks that won: **7**
- Elo added/changed picks that lost: **5**

## Notes

- Both variants use the **same current Hybrid Strong thresholds and 70/30 base/simulation weight**.
- The only A/B difference is the simulation input: legacy prepared_simulations versus the Elo-backed Match Explorer overlay where available.
- Each historical simulation is rebuilt from the repository state at that historical manifest commit, then filtered by the existing pre-kickoff safety cutoff.
- ROI assumes one unit staked on every graded pick. VOID returns one unit.
- Markets that cannot be deterministically graded from retained score/stat artifacts remain unresolved and are excluded from hit-rate/ROI.

## First changed decisions

| Date | Match | Legacy | Elo |
|---|---|---|---|
| 2026-09-05 | 2026-09-05 / Leicester / Oxford United | Oxford United Over 0.5 @1.50 (WON) | — |
| 2026-09-05 | 2026-09-05 / Metz / Rodez | Rodez Over 3.5 @1.67 (WON) | Metz Under 1.5 @1.77 (LOST) |
| 2026-09-05 | 2026-09-05 / VfL Wolfsburg / Energie Cottbus | VfL Wolfsburg Under 2.5 @1.73 (LOST) | Energie Cottbus Over 0.5 @1.50 (WON) |
| 2026-09-05 | 2026-09-05 / Wealdstone / Kidderminster Harriers | Under @1.50 (LOST) | — |
| 2026-09-06 | 2026-09-06 / SpVgg Greuther Fürth / 1. FC Heidenheim | SpVgg Greuther Fürth Over 1.5 @2.10 (LOST) | — |
| 2026-09-12 | 2026-09-12 / Olympiakos Piraeus / OFI | Under @1.57 (WON) | Olympiakos Piraeus Under 2.5 @1.83 (WON) |
| 2026-09-12 | 2026-09-12 / Wolfsberger AC / Rapid Vienna | Rapid Vienna Corners Over 4.5 @1.60 (WON) | Under @1.57 (WON) |
| 2026-09-13 | 2026-09-13 / HNK Hajduk Split / NK Slaven Belupo | HNK Hajduk Split Under 2.5 @1.63 (WON) | HNK Hajduk Split Corners Under 6.5 @1.73 (WON) |
| 2026-09-13 | 2026-09-13 / WSG Wattens / TSV Hartberg | WSG Wattens Under 1.5 @1.77 (LOST) | — |
| 2026-09-18 | 2026-09-18 / Rudes / NK Slaven Belupo | Over @1.82 (WON) | Corners Under 9.5 @1.67 (LOST) |
| 2026-09-19 | 2026-09-19 / Ascoli / Avellino | Ascoli Under 1.5 @1.68 (LOST) | — |
| 2026-09-19 | 2026-09-19 / Grazer AK / Austria Vienna | — | Austria Vienna Under 1.5 @1.50 (LOST) |
| 2026-09-19 | 2026-09-19 / Grimsby / Crawley Town | Grimsby Under 2.5 @1.56 (WON) | Grimsby Corners Under 7.5 @1.60 (LOST) |
| 2026-09-19 | 2026-09-19 / Peterborough / Doncaster | Under @1.53 (WON) | Under @1.57 (WON) |
| 2026-09-20 | 2026-09-20 / Energie Cottbus / FC St. Pauli | Energie Cottbus Over 1.5 @2.00 (LOST) | FC St. Pauli Under 1.5 @2.10 (WON) |
| 2026-09-20 | 2026-09-20 / Hannover 96 / VfL Bochum | Under @1.61 (WON) | — |
| 2026-09-20 | 2026-09-20 / Manchester City / Sunderland | Manchester City Under 2.5 @1.61 (LOST) | Yellow Cards Over 2.5 @1.50 (LOST) |
| 2026-09-20 | 2026-09-20 / Parma / Genoa | Parma Over 0.5 @1.50 (WON) | Over @1.50 (WON) |
| 2026-09-25 | 2026-09-25 / Girona / Albacete | Girona Under 1.5 @2.38 (LOST) | Albacete Corners Over 3.5 @2.10 (?) |
