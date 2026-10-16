# CSV identity expansion, 2026-10-16

Inherits every model, price, selection, chronology and development-evidence limitation in `../csv_prices_20261016/PROTOCOL.md`. The first CSV batch and every result remain preserved in their original directory. This second batch addresses coverage failures only; it does not choose names, models or thresholds from profit.

Changes:

- Normalize the repository's `ROM` competition identifier to `ROU`, as Android `DomesticApiRegistry.normalizeLeagueCode` already does. This restores 61 paired Romanian predictions that were omitted by the initial source-code spelling mismatch. No probability changes.
- For an unrecognized CSV team name, use exact league/date/home-away schedule constraints against opponents that already have explicit aliases. Require at least two distinct dates and two distinct existing opponents. Every available nonempty constraint must agree on exactly one canonical country-scoped identity. Conflicting or insufficient evidence is retained and rejected.
- Newly inferred aliases cannot anchor another inference. No fuzzy string similarity, score, result, probability, odds or ROI is read by alias recovery. Tests mutate all scores/prices/probabilities and require identical recovered aliases and evidence.
- Score agreement is checked only after a unique schedule/pair candidate has been selected. A result cannot resolve an ambiguous fixture. Fixtures with missing or conflicting scores remain excluded.
- Preserve full evidence for accepted and rejected aliases in `schedule_alias_evidence.json`. These are schedule-derived identities, distinct from the original explicitly configured aliases.
- Reuse the 38 hash-recorded CSV source files committed by the first batch. The replay performs no fresh public downloads when the cache is complete, and still performs zero sports-provider requests and zero LLM calls.
- Keep Bet365 primary and Pinnacle secondary, closing view only, all three unchanged policies, both unchanged models, all months/markets/tiers/leagues, fixed unit stakes. No profitable subset is promoted. These are overlapping development comparisons, not independent holdouts.

The new frozen input has 2,601 paired fixtures before CSV matching. The original training data SHA256 remains `1653f6779eece3b7fef7dd5265db1978ffe566ec61e312f5866374739e14e9c0`; source predictions, profiles and test windows remain unchanged. Only the Romanian code and archive identity coverage differ. No app source, model weights, UAT data bundle, PROD behavior or publication gate is changed.
