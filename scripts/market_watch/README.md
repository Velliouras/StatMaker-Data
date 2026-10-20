# StatMaker Market Watch

Operational UAT market routing with a separate GitHub Pages dashboard. No provider
or LLM calls. Verified receipt time establishes quote freshness; provider last-change time stays separate and may be older. This is not an automated betting or profitability guarantee system.

The restored recommendation minimum is 60%, with strict 1.80 < odds < 3.00 and
positive expected return. The 55–60% issues already published remain in historical
Performance but are hidden from current recommendations.

## Lifecycle

Each registered market has one predeclared primary model. Models and their
selection policies are not pooled according to which happens to be profitable.
SHOTS/SOT retain the user's existing UAT baseline admission, explicitly labeled.
CORNERS/FOULS use coupled_conservative paper forecasts. CARDS and GOALS currently
have no connected qualifying model/source and cannot be automatically promoted.

- Activate: >=50 settled fixtures across >=14 days, overall ROI >=3%, last20 ROI >0.
- Pause: current peak-to-trough loss >=10u, or last20 ROI <=-10%.
- Resume: >=20 newly published-and-settled paper fixtures after the pause, their
  ROI >=5%, positive overall and recent ROI, plus seven days since the pause.
- Continue to freeze and settle paper forecasts while paused. No losing records
  are deleted and no live history is replaced with development backtests.
- These numeric settings are explicit operational UAT choices. They are not
  statistically optimized or evidence that future ROI will be positive. The
  dashboard's fixture-cluster interval is descriptive, not an anytime-valid test.

Rules are versioned in config/market_watch.json. A missing/stale decision file
after initial bootstrap stops routing after 36h. Changes apply to recommendations;
issued Performance history stays intact. Price observation and publication
before kickoff remain mandatory. A positive ROI never bypasses identity,
same-book exact-price, probability, EV or settlement requirements.

## Data flow

Shots publisher freezes independent paper_ledger.json records even when paused;
only active markets issue app recommendations. Both ledgers prove publication
after a successful Git push and before kickoff. Paper rows do not enter app
Performance. Paused markets' already published predictions are preserved.

The permanent count paper source reuses the existing frozen coupled architecture
with a rolling seven-day fixture horizon. All 143 dependencies, including the
new runner, are hash-checked. A source change stops new freezes and is displayed
in status.json; it does not silently change the model. The original October trial
is preserved. Its reserved fixture/market packets seed the successor so later
prices cannot be searched for a more attractive retrospective selection. Across
overlapping source packets, the first frozen packet wins, even if it had no pick.

An enabled corner/foul prediction is rechecked against a currently cached exact
event, kickoff, canonical teams, API fixture and same-book same-line same-side
same-price. Team selections also retain the same market favourite. A separately
committed market_ledger.json proves its recommendation publication before kickoff.
Optional marketIssued/marketCandidates sections keep older apps compatible.
UAT 0.1.69 reads the optional sections and keeps each model's identity in
Performance. The active count probabilities have not been refitted by Market Watch.

Market Watch updates every four hours and after the existing source workflows.
It re-filters the public current feed when pausing/resuming without repricing or
rewriting issues. New market issues are handled by the normal Shots publisher,
also triggered after the count source workflow. No new Actions upload-artifact
steps are used. Immutable forecast evidence is kept in the repository.

## Dashboard and GitHub Pages

Generated entry: docs/market-watch/index.html. It shows status/reason, sample,
profit, ROI, last20/50 performance, win rate vs mean prediction, drawdown, curves,
pending selections, distinct development history and transition history.
The page fetches current public state.json when opened and every five minutes;
its embedded snapshot works offline. Data refreshes do not require a Pages rebuild.
Team/provider text is escaped; no secrets or authenticated frontend API is used.

One-time repository owner setup (Pages is currently disabled):

1. Settings > Pages > Build and deployment.
2. Source: Deploy from a branch.
3. Branch: main; folder: /docs; Save.
4. Open https://velliouras.github.io/StatMaker-Data/market-watch/ after GitHub's
   first Pages deployment completes. Before that, the README table is visible at
   https://github.com/Velliouras/StatMaker-Data/tree/main/docs/market-watch.

The installed GitHub connector cannot change Pages administration settings.
GitHub documents the requirement for admin/maintainer access:
https://docs.github.com/en/pages/getting-started-with-github-pages/configuring-a-publishing-source-for-your-github-pages-site

Repeated monitoring differs from a fixed-time confidence statement:
https://arxiv.org/abs/1810.08240

## Checks

```bash
python -m unittest scripts.market_watch.test_policy scripts.shots_uat.test_contract -v
python -m scripts.market_watch.build
```

The workflow owns the two-phase paper publication sequence. Do not manually
mark a local/retrospective forecast as publicly verified.
