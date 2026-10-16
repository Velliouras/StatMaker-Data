# Historical CSV price research

Status: COMPLETE. Joined 1289 fixtures from 65115 CSV rows.

| Bookmaker / model / policy | Bets | Fixtures | ROI | 95% fixture interval |
|---|---:|---:|---:|---|
| Bet365::full::current_preference | 101 | 94 | -25.14% | -45.89% to -3.68% |
| Bet365::full::probability_ranking | 101 | 94 | -25.14% | -45.89% to -3.68% |
| Bet365::full::neutral_btts | 101 | 94 | -25.14% | -45.89% to -3.68% |
| Bet365::separate_ft_selected_period::current_preference | 59 | 52 | -21.66% | -49.10% to 5.64% |
| Bet365::separate_ft_selected_period::probability_ranking | 59 | 52 | -21.66% | -49.10% to 5.64% |
| Bet365::separate_ft_selected_period::neutral_btts | 59 | 52 | -21.66% | -49.10% to 5.64% |
| Pinnacle::full::current_preference | 0 | 0 | 0.00% | insufficient clusters |
| Pinnacle::full::probability_ranking | 0 | 0 | 0.00% | insufficient clusters |
| Pinnacle::full::neutral_btts | 0 | 0 | 0.00% | insufficient clusters |
| Pinnacle::separate_ft_selected_period::current_preference | 0 | 0 | 0.00% | insufficient clusters |
| Pinnacle::separate_ft_selected_period::probability_ranking | 0 | 0 | 0.00% | insufficient clusters |
| Pinnacle::separate_ft_selected_period::neutral_btts | 0 | 0 | 0.00% | insufficient clusters |

- Already inspected development months, not an untouched holdout.
- Source-labelled closing prices have no individual quote timestamps or executable-fill verification.
- CSV kickoff timezone is unspecified; date/pair joins are explicit and are not exact UTC kickoff joins.
- Original daily features exclude match-day results but historical publication times remain unverified.
- Pre-closing prices retained for coverage only: match-day features may postdate their collection.
- Only 1X2 and total 2.5 are priced here. Missing markets and prices are not synthesized.
- Bookmakers, policies, months, tiers and league subsets overlap; no winning subset is selected.
- Intervals resample fixture clusters, without a serial-dependence or multiple-comparison adjustment.
- No commission, staking optimization or guaranteed profitability is assumed.

0 LLM calls; 0 sports-provider requests; no app publication or model fitting.
