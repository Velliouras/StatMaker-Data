# Betting V2 — Independent future fixture ID verification

Verified in the [offline QA run 38086315649](https://github.com/Velliouras/StatMaker-Data/actions/runs/38086315649), with **156/156 tests passing** and **zero new API-Football or Odds-API.io calls**.

The immutable original research forecast archive contains **153** ELO-only future match forecasts frozen before kickoff. These were retrospectively *identity checked*, not recomputed, using the independent `schedule_fixtures` records already saved in the API-Football fixture_stats caches.

| Measure | Result |
|---|---:|
| Independently checked original shadow forecast rows | 153 |
| Exact source-scope, two-team, UTC kickoff, league and season matches | **25** |
| Without an exact independent cached fixture match | **128** |
| Newly fetched provider records | **0** |
| Certified expected value or ROI | None |
| Certified STRONG or Android production recommendations | **0** |

Only an **exact and unique** API-Football future fixture ID qualifies. Source cache must declare provider=api-football, matching API league ID and season, a matching exact-season query and not-started (NS) status, both canonical team names and a kickoff within 15 minutes. Differences, duplicates and unknown fixtures are rejected.

The crosswalk is performed **after the immutable shadow snapshot was written**, with its independent checking timestamp. No evidence is invented retroactively about the availability of API-Football identity when historical bookmaker odds were observed. These 25 links do **not** yet qualify as priced bets and are not a value or hit-rate certificate.

Persisted implementation:
- `scripts/betting_v2/future_fixture_crosswalk.py`
- `scripts/betting_v2/verify_forward_fixture_ids.py`
- `tests/test_betting_v2_future_fixture_crosswalk.py`
- `tests/test_betting_v2_verify_forward_fixture_ids.py`

The standalone zero-provider-call forward freeze Action and the standard Domestic odds-refresh Action now invoke the crosswalk and write the research report `reports/betting_v2/pilot_forward_identity_crosswalk.json`, without modifying app-facing odds schemas or Android PROD. The **successful second archive publication** must still be checked independently; a passing offline QA does not prove publication.

Next blockers: capture and verify historical bookmaker price updates, exact market+selection identities, freeze matching actual as-of forecast/quote source receipts, prospective settlement on completed scores, and unbiased out-of-time EV/ROI plus lineup adversity tests. Until then `certifiedStrong=0` is mandatory.
