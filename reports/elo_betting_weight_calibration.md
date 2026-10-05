# Elo Betting Weight Calibration

- Window: **2026-09-05 → 2026-10-04**
- Historical snapshot: **latest App-Ready manifest at/before 11:00 Europe/Athens**
- Monte Carlo runs: **10000** per processed day
- Processed days: **27** / 30
- Calibration days: **20**
- Holdout days: **7**

## Calibration window

| Variant | Picks | Graded | Won | Lost | Hit rate | ROI | Avg odds |
|---|---:|---:|---:|---:|---:|---:|---:|
| Legacy simulation @30% | 624 | 578 | 351 | 225 | 60.9% | -0.5% | 1.65 |
| Elo 0% | 638 | 598 | 360 | 235 | 60.5% | -0.8% | 1.65 |
| Elo 15% | 615 | 571 | 347 | 222 | 61.0% | -0.3% | 1.65 |
| Elo 20% | 616 | 569 | 345 | 222 | 60.8% | -0.4% | 1.65 |
| Elo 25% | 619 | 572 | 348 | 222 | 61.1% | -0.2% | 1.65 |
| Elo 30% | 619 | 572 | 348 | 222 | 61.1% | -0.2% | 1.65 |
| Elo 35% | 617 | 571 | 345 | 224 | 60.6% | -0.9% | 1.65 |
| Elo 40% | 618 | 572 | 344 | 226 | 60.4% | -1.4% | 1.65 |

## Holdout window

| Variant | Picks | Graded | Won | Lost | Hit rate | ROI | Avg odds |
|---|---:|---:|---:|---:|---:|---:|---:|
| Legacy simulation @30% | 14 | 14 | 8 | 6 | 57.1% | -4.7% | 1.69 |
| Elo 0% | 16 | 16 | 10 | 6 | 62.5% | 7.3% | 1.72 |
| Elo 15% | 15 | 15 | 9 | 6 | 60.0% | -1.1% | 1.68 |
| Elo 20% | 14 | 14 | 8 | 6 | 57.1% | -4.7% | 1.69 |
| Elo 25% | 14 | 14 | 8 | 6 | 57.1% | -4.7% | 1.69 |
| Elo 30% | 14 | 14 | 8 | 6 | 57.1% | -4.7% | 1.69 |
| Elo 35% | 14 | 14 | 8 | 6 | 57.1% | -4.7% | 1.69 |
| Elo 40% | 14 | 14 | 8 | 6 | 57.1% | -2.6% | 1.71 |

## Selection

- Training-best Elo weight: **25%**
- Holdout result for that weight: **57.1% hit rate**, **-4.7% ROI**
- Current 30% Elo holdout: **57.1% hit rate**, **-4.7% ROI**
- Legacy holdout: **57.1% hit rate**, **-4.7% ROI**
- Promote weight change: **NO**
- Reason: holdout does not confirm a robust improvement on both hit-rate and ROI.

## Guardrails

- Strong thresholds, ranking coefficients, odd limits and one-pick-per-match logic are unchanged.
- Only the base/simulation blend weight changes across Elo variants.
- 0% Elo is a true base-only control: simulation is removed from both blend and simulation gate.
- The weight is selected only on earlier calibration dates; the final seven processed dates are untouched holdout.
- No Android or PROD Betting configuration is changed by this workflow.
