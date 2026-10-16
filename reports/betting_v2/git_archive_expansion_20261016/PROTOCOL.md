# Historical Git archive expansion — 16 October 2026

The aim is to increase the genuinely priced count-market sample using already stored provider responses. No provider API, LLM, live app or prospective-candidate changes are authorized by this experiment.

## Fixed source selection

Use the most recent commit changing odds/odds_api_io/domestic_provider_markets.json before each 12:00 and 23:59:59 UTC slot from 2 to 15 October 2026. The 28 fixed slots produce 27 distinct snapshots from 61 path-changing commits in the queried window. Selection is independent of odds, results and ROI. All commit candidates and selected timestamps are retained in manifest.json. Download only exact SHA-pinned public GitHub raw files; preserve content SHA-256 and Git blob SHA-1 in source_bindings.json. Raw files remain recoverable from those commit URLs.

## Time, identity and settlement

An offer must come from an explicitly exact provider payload and an approved canonical team mapping. Require an absolute generatedAt at or before commit time, commit time strictly before kickoff, and a market updatedAt at or before commit time and no more than 24 hours old. Use the later commit time as the conservative quote observation, never the earlier provider timestamp. Keep at least 24 hours of feature lag and reject observations preceding the frozen October model state. Only unique exact canonical pair plus kickoff joins are accepted. Numeric cross-provider fixture IDs are not independently verified. Conflicting prices for an identical contract/bookmaker/snapshot are excluded permanently.

Retain all supported non-card contracts for coverage and complete favourite triplets. Replay the four predeclared count targets: CORNERS, SHOTS, SOT and FOULS. Cards require a separate verified bookmaker-unit target and eligible player/period events; no HY+HR shortcut. No missing prices are created. Both earliest and latest whole-fixture snapshots are retained, including all bookmakers from that snapshot. They overlap and are not independent tests.

## Frozen model comparison

Compare control point, control conservative, coupled point and coupled conservative with unchanged odds >1.80 and <3.00, probability >=60%, positive EV, favourite support and one selection per fixture/family. Use the previously bound rate coefficients and coupled profiles, with lagged rolling features only; no refitting, ROI optimization or removal of losing targets. Select before reading actual count labels. Missing labels remain unscored; integer-line pushes retain their stake return.

The period is previously inspected development data. Git commit metadata and provider timestamps are useful archive evidence but are not independent historical publication verification or proof of executable odds. These results cannot replace the separate public-before-kickoff prospective cohort.

All 27 downloaded payloads were independently checked against Git tree blob SHA-1 and byte length, in addition to their SHA-256. Collection now bounds in-flight payloads and retains whole-fixture packets incrementally. A process lock prevents duplicate writers and cache writes are atomic. Eleven tests cover timing, exact-source integrity, conflict permanence, whole-packet selection, unique identity joins, missing labels and push accounting.

The recovered universe contains 589 unique fixtures, including all 150 fixtures in the prior receipt universe and 439 additional fixtures. At least one snapshot contains corners for 576 fixtures, shots for 141, SOT for 206 and fouls for 31. Whole-packet choice changes availability: earliest has 385/5/42/0 count fixtures respectively; latest has 533/114/154/31. These are coverage counts before model selection and do not imply 589 bets. The frozen prediction/settlement history contains 17,950 deduplicated fixtures and is archived as history.json.gz with its content digest, separately from the original frozen model training inputs.
