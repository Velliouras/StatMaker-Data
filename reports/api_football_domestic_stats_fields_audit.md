# API-Football Domestic stats fields audit

Generated at: 2026-07-10T07:12:30Z

Live API calls made: 0
Cache files scanned: 16
Fixtures scanned: 2563
Fixtures with raw team statistics: 2242

## Available raw team stat fields

- Ball Possession: 4484 values across cached team rows; fixtures=2242
- Blocked Shots: 4482 values across cached team rows; fixtures=2241
- Corner Kicks: 4484 values across cached team rows; fixtures=2242
- Fouls: 4420 values across cached team rows; fixtures=2210
- Free Kicks: 70 values across cached team rows; fixtures=35
- Goalkeeper Saves: 4420 values across cached team rows; fixtures=2210
- Offsides: 4420 values across cached team rows; fixtures=2210
- Passes %: 4414 values across cached team rows; fixtures=2207
- Passes accurate: 4414 values across cached team rows; fixtures=2207
- Red Cards: 4484 values across cached team rows; fixtures=2242
- Shots insidebox: 4414 values across cached team rows; fixtures=2207
- Shots off Goal: 4484 values across cached team rows; fixtures=2242
- Shots on Goal: 4484 values across cached team rows; fixtures=2242
- Shots outsidebox: 4414 values across cached team rows; fixtures=2207
- Total Shots: 4430 values across cached team rows; fixtures=2215
- Total passes: 4414 values across cached team rows; fixtures=2207
- Yellow Cards: 4484 values across cached team rows; fixtures=2242
- expected_goals: 4414 values across cached team rows; fixtures=2207
- goals_prevented: 4396 values across cached team rows; fixtures=2198

## Available normalized fields

- HS (home total shots): 2211 non-null values
- AS (away total shots): 2211 non-null values
- HST (home shots on target): 2239 non-null values
- AST (away shots on target): 2239 non-null values
- HC (home corners): 2237 non-null values
- AC (away corners): 2237 non-null values
- HF (home fouls): 2207 non-null values
- AF (away fouls): 2207 non-null values
- HY (home yellow cards): 2176 non-null values
- AY (away yellow cards): 2176 non-null values
- HR (home red cards): 471 non-null values
- AR (away red cards): 471 non-null values
- HPossession (home possession): 2236 non-null values
- APossession (away possession): 2236 non-null values
- HSaves (home goalkeeper saves): 2196 non-null values
- ASaves (away goalkeeper saves): 2196 non-null values
- HPasses (home total passes): 2199 non-null values
- APasses (away total passes): 2199 non-null values
- HPassesAccurate (home accurate passes): 2199 non-null values
- APassesAccurate (away accurate passes): 2199 non-null values
- HxG (home xG): 1698 non-null values
- AxG (away xG): 1698 non-null values

## Missing normalized fields

- None among current normalized fields.

## xG

- xG was found in cached /fixtures/statistics payloads.

## Events / lineups / player stats / injuries

Existing yellow coverage rows: 40
Fixture events coverage true rows: 32
Fixture lineups coverage true rows: 28
Fixture player statistics coverage true rows: 15
Injuries are not present in current cache or coverage report; would require explicit /injuries?fixture= audit/fetch if needed.

## API calls / quota impact

- audit_from_existing_cache: 0
- current_fixture_stats_fetch_report_requests_used: 85
- fixture_stats_cache_existing_pattern: 1 /fixtures request per league query plus 1 /fixtures/statistics request per uncached completed fixture, capped by --max-requests default 85
- team_stats_endpoint: GET /fixtures/statistics?fixture={fixture_id}
- events_endpoint_if_added: GET /fixtures/events?fixture={fixture_id} - one request per fixture when coverage.fixtures.events is true
- lineups_endpoint_if_added: GET /fixtures/lineups?fixture={fixture_id} - one request per fixture when coverage.fixtures.lineups is true
- player_stats_endpoint_if_added: GET /fixtures/players?fixture={fixture_id} - one request per fixture when coverage.fixtures.players_statistics is true
- injuries_endpoint_if_added: GET /injuries?fixture={fixture_id} - one request per fixture if needed; not represented in current coverage/cache proof
- quota_note: No live API calls were made for this audit. Future enrichment must keep a request cap and run in small batches.

## Recommended JSON structure

Keep raw provider blocks plus normalized fields. Missing stats must stay null, never fake 0.

See JSON report for full schema and per-league cache summary.
