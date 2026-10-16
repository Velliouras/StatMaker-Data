# Continuation after the usage reset

User request: expand research to the markets already in the app, using existing data, and run deterministic computation outside the conversation where possible. The reset time supplied by the user is 13:43 Athens on 2026-10-16; it is not independently account-verified.

This job contains no language-model/API calls and no sports-provider requests. It reads all saved receipt files without the old 750-file diagnostic limit, inventories all 32 labels pinned from the UAT app catalogue, records exact join failures, and compares earliest/latest single-receipt decisions using frozen baseline/new models and three existing policies. It adds an offline, explicit FT foul-total mapping and reuses only existing approved provider-ID/canonical bindings with an exact kickoff. Missing prices are never inferred.

This is a bounded research batch. It cannot invent or implement further strategies after completion without another engineering/analysis turn. Check status.json first; a FAILED stage preserves completed evidence. Then read summary.json, coverage.json, app_market_coverage.csv and both replay files. Do not choose a profitable-looking policy from this development sample and describe it as independently validated.

Pending after this batch:

1. Resolve recoverable exact identity mismatches using existing IDs and league-scoped aliases; keep genuine missing results separate from parser failures.
2. Implement and test exact contracts for the remaining app goal markets: correct score, winning margin, goal bands/parity, combined BTTS/result and totals, HT/FT and both-halves events. Match real bookmaker period, line and push rules first. No arbitrary certification gate is required for UAT.
3. For cards/red cards, establish whether archived prices settle yellow/red counts or booking points and compare only to the corresponding actual statistic.
4. Connect historical Football-Data price columns to the research importer. The Android importer currently retains results/stats but drops bookmaker prices. Existing enriched JSON is predominantly API statistics without price fields. The checked-in E0T CSV is a TEST asset and must not be used as a production research dataset without independent source validation. Public historical CSV is a separate potential input, not already audited imported price history.
5. Preserve separate training, prior-window selection and subsequent evaluation. Current inspected history is development evidence; future frozen batches overlap and are not independent samples.

UAT integration is already published: app uat abfca010d77534db25386d09e3ac76c422ba86e0 (0.1.61), Data implementation 236db39a0da207c5b584e129288dadf97330282c, published bundle 5e546bc02b324376b264b69a27125fed75a0573da0fa9aa5a9686d52ba8e4d91, 31,506 forecasts / 386 fixtures. 23 non-UAT tables preserved. Keep PROD and app UI unchanged. Data actions only main; app changes only lowercase uat. No new app ID.
