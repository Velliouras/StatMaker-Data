# Initial worker validation

The new worker includes all 32 labels pinned from the UAT catalogue. Four focused tests passed and an audit-only pass over the locally available 1,413 saved receipts completed successfully. The canonical GitHub batch scans the complete main-branch archive; local counts must not be substituted for its final results.

A concrete parser gap was found: the shared legacy classifier can classify a name such as `Total Fouls` as `MATCH_GOALS` because its generic total-market rule runs without a foul-family case. The new offline worker handles explicit full-time match/team foul totals before that fallback, and rejects player, period, handicap and invalid-line cases. The global classifier/live feed path still needs a scoped correction and regression test before using it for new foul-market publication. This finding alone does not establish that any previously selected priced bet was affected; trace affected raw receipts and picks before changing prior ROI reports.

The local audit retained 10,496 earliest-packet quotes / 70 fixtures and 11,326 latest-packet quotes / 70 fixtures across goals, corners, shots, SOT and fouls. These are overlapping development samples. Many existing app markets still need an explicit exact settlement contract. Larger statistical history cannot manufacture missing historical prices.

The background worker contains no AI calls and no sports-provider requests. It saves stage status, coverage, rejected exact joins, both packet universes and model/policy replay outputs. It does not publish app data or select a winning strategy automatically.
