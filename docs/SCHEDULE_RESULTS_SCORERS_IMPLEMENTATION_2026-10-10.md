# StatMaker Schedule — Results by matchday and top scorers

Status: Android **UAT implementation**, separate local player-scorer Data contract; **not deployed to Android PROD**.

## User interface

- The existing daily Schedule remains untouched in purpose; two extra action bars appear below League Tables: Matchday results / Αποτελέσματα αγωνιστικών and League top scorers / Πρώτοι σκόρερ πρωταθλημάτων.
- Results popup: select country → league → round. Shows final scores and kickoffs in Europe/Athens time for any available completed domestic fixture, using existing StatMaker-Data/main domestic_enriched index and league files. Round labels preserve upstream segments (Regular Season, Championship/Playoffs, etc.). No predictions, odds, betting, or quota.
- Verified goal events (scorer name, minute/stoppage, team, own goal, penalty) appear beneath each result **only** when a provider-validated corresponding event archive exists. Otherwise the UI explicitly says scorer names are unavailable; it does not invent them.
- Top scorers popup: Premier League (E0), La Liga (SP1), Serie A (I1), Bundesliga (D1), Ligue 1 (F1), Greek Super League (G1). Ordered by goals, includes player and team (and source supports assists). The on-screen list is constrained to the same season as the selected league; no stale former-season table.

## Data availability as of October 10, 2026

All current cached `domestic_enriched/*.json` include a fixture `round`, `home_goals`, `away_goals` and completed status; they do **not** include goal-event players or official league player rankings.

`data/statmaker/schedule_scorers/season_snapshot.json` is intentionally empty. Do not claim that scorers have been published until the **official** provider endpoint responses have been fetched. The app handles this with a clear unavailable state.

## Offline verified importer

Script `scripts/build_statmaker_schedule_scorers.py` takes already-downloaded response JSON files from `data/api_football/schedule_scorers/events` and `.../leaders` and emits the bounded snapshot. It accepts an event timeline only if exact fixture ID/league and *total number of credited goal events equals the match scoreline total*. A top scorer list is accepted only when exact league/season and official request identity match. Never synthesize scorer names or goal totals.

Input event JSON must carry `sourceRequest: /fixtures/events?fixture=FIXTURE_ID`, `fixtureId`, `leagueCode`, `retrievedAtUTC`, `response` (the API's response array). Top scorers must carry `sourceRequest: /players/topscorers?league=LEAGUE_ID&season=SEASON`, `leagueCode`, `apiFootballLeagueId`, `season`, `retrievedAtUTC`, `response` (player objects with statistics).

## Optional provider collection — quota-safe

`scripts/fetch_schedule_scorers_capped.py` is **dry-run by default** and spends 0 quota unless `--execute` is explicitly specified with a configured `API_FOOTBALL_KEY`:

```bash
python scripts/fetch_schedule_scorers_capped.py --phase leaders
python scripts/fetch_schedule_scorers_capped.py --execute --phase leaders --max-requests 6
python scripts/fetch_schedule_scorers_capped.py --execute --phase events --max-requests 12 --lookback 14
python scripts/fetch_schedule_scorers_capped.py --phase build
```

The script caps each explicitly executed run at at most 40 calls and refuses a reserve below 1,500. It stops if the provider does not return a usable quota remainder or falls below reserve. One initial request might be necessary to discover the provider's actual remaining quota. Always confirm a real account balance before any --execute. It is NOT connected to GitHub Actions and does not run when Android opens Schedule or when Update All runs.

## Verification and release

Tests: `python -m unittest discover -s tests -p 'test_schedule_scorers_offline_builder.py' -v` (no network). Tests are committed but full Android Gradle build / device check not yet executed in this environment.

App version should be bumped for an actual UAT build; PROD cherry-pick only after on-device testing. Android UAT currently uses `applicationId=com.statmaker.app.uat`.

Release blockers: provider events and league scorer snapshots must be populated and verified; small-screen UI should be tested on the target Android devices. No Apps are signed or installed by this change.
