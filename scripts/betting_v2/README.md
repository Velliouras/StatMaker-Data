# StatMaker Betting V2 — offline publisher (Data/main)

**Status:** Shadow development, not a live App-Ready betting publisher. This
component has **zero direct API-Football calls** and never modifies existing
production DBs, odds files, manifests or Action workflows.

## Why this exists

The existing StatMaker betting producer has not demonstrated calibrated,
out-of-sample conservative probabilities above 60% at exact decimal odds
strictly between 1.80 and 3.00. Its Elo A/B report used average odds ~1.65
and does **not** certify the user's new STRONG-only policy.

The new app UAT Betting V2 requires:
- ELO, xG/xGA, long/recent form, home/away and market-specific inputs.
- Independent market- and league-aware calibration with actual historical ROI.
- Calibrated **conservative** win probability >=60%, including realistic
  adverse lineup/injury scenarios.
- No SOLID, no Asian/handicap, no old Hybrid fallback, only HIGH / CONFIRMED /
  DEVELOPING subsets of qualified STRONG.
- Price strictly >1.80 and <3.00, exact pre-kickoff quote bound to a
  versioned source generation; reprice on change.

The offline pilot is evidence-gathering only. Its Poisson models, Elo weights,
Wilson bounds and reports are **research**, not certified model parameters.
They do not produce a live recommendation.

## Two-regime feature policy (2026-10-10)

1. **XG_PRIMARY**: use the original strict xG/xGA/ELO/venue model when enough real pre-kickoff xG history exists.
2. **ELO_GOALS_FALLBACK_NO_XG**: otherwise, if enough observed scored/conceded goals, prior ELO and home/away samples exist, use a separately tuned/calibrated ELO + observed-goals research model. Do not synthesize xG.
3. Neither model is allowed to produce STRONG without its own frozen chronological calibration, verified conservative win probability >=60% even under adverse lineup scenarios, independent price coverage and strict 1.80 < odd < 3.00 with positive conservative EV.

The Python offline pilot now writes separate Elo artifacts: pilot_elo_fallback_model.json, pilot_elo_fallback_calibration.jsonl and pilot_elo_fallback_holdout.jsonl. Under explicit --with-prices it also writes pilot_elo_fallback_priced.json. These are research-only; they do not promote Android PROD or modify App-Ready.

## Independent xG recovery (optional, not automatic)

Understat covers E0, D1, SP1, I1 and F1. The recovery script performs strict source-score/team/date reconciliation; for all other leagues, use the ELO fallback when approved. Recovered xG must never be treated as historically available before its source observation timestamp.

Betting V2 has **no HTTP client**. First obtain a league-season JSON
through a separately approved source workflow, with license and provenance
verified. Then import the existing file locally:

```bash
python scripts/betting_v2/recover_understat_xg.py --repository-root . --league E0 --season 2026 --input ./understat_epl_2026.json
```

Only local JSON imports are accepted; there is intentionally **no**
`--fetch-understat` option. The output stays in a separate offline
league-season report (`reports/betting_v2/understat_xg_E0_2026.json`),
never in canonical statistics or App-Ready. The saved overlay needs point-in-time ingestion verification and model backtesting before it can affect STRONG decisions. This session's environment could not access Understat, so **no live missing xG values were retrieved** here.

Independent six-league real-cache readiness check: 317 completed fixtures of 2026-27, 105 primary xG-ready, 122 additional ELO-ready and 90 without adequate prematch sample. See reports/betting_v2/DUAL_MODE_COVERAGE_AND_RECOVERY_2026-10-10.md. The full committed Python tests and pilot remain unexecuted in the available environment.

## Provider quota protection

**V2-specific quota budget: 0 requests.**
The scripts here use the canonical, existing:
- \`data/statmaker/domestic_enriched/index.json\`;
- \`data/statmaker/domestic_enriched/*.json\`;
- historical git objects with immutable, already-stored odds bundles
  (optional explicit priced replay).

They contain no API-Football HTTP client and no automatic API refresh.

The existing provider request guard in
\`scripts/api_football_daily_quota_guard.py\` has a default daily reserve of
**1,500**, but that protects existing provider workflows, not a new V2 run.
Do **not** infer today's account quota balance from the latest stats fetch
report's \`request_count\` or \`max_requests\`: these are per-run values.
Before any *future* provider expansion, confirm real account remaining quota
from provider response headers, keep the existing reserve, and require an
explicit user-approved capped request plan. **This rollout doesn't need one.**

## Fastest local pilot

From the \`StatMaker-Data\` repository root with Python 3.11+:

\`\`\`bash
python scripts/betting_v2/run_pilot.py --repository-root .
\`\`\`

Generated in \`reports/betting_v2/\` locally:
- \`pilot_model.json\`: chronological model tuning and independent holdout;
- \`pilot_calibration.jsonl\`: earlier calibration predictions;
- \`pilot_holdout.jsonl\`: later untouched holdout predictions;
- \`shadow_manifest.json\`: audit of inputs and explicit blocking reasons,
  **certifiedForecasts: []**, **liveRecommendationsPublished: 0**.

This default pilot uses **zero HTTP requests** and requires no local Android
project, no signing keys, no GitHub Actions and no API-Football quota.

Optional local archive-only price comparison (requires full git history,
not a shallow checkout; dates must overlap the untouched holdout):

\`\`\`bash
python scripts/betting_v2/run_pilot.py --repository-root . --with-prices --from-date YYYY-MM-DD --to-date YYYY-MM-DD
\`\`\`

Price replay is restricted to **at most 14 explicit dates per run**.
It reads only old Git blobs and refuses ambiguous IDs, non-pregame quotes
and missing archived bundles. It does not contact the provider. Choose date
limits solely from the frozen holdout in \`pilot_model.json\`, **not**
by optimizing wins/ROI after looking at the holdout.

The individual scripts also remain available:
- \`audit_data.py\`: previously known evidence and xG coverage.
- \`walk_forward_goals.py\`: initial joint 1X2/goals feature model.
- \`export_historical_prices.py\`: legacy-prepared selection prices (NOT proven independently unfiltered bookmaker data).
- \`evaluate_holdout_prices.py\`: price-aligned raw-value evaluation.
- \`calibration_gate.py\`: bin/league Wilson-bound exploratory holdout.

## Offline xG source-to-publication integrity (no provider calls)

Run against the locally cached fixture statistics and the canonical
`domestic_enriched` cache. This does not refetch anything:

```bash
python scripts/betting_v2/xg_source_integrity.py --repository-root . --league-codes E0 D1 SP1 I1 --output reports/betting_v2/xg_source_integrity.json
python -m unittest discover -s tests -p 'test_betting_v2_xg_source_integrity.py' -v
```

For each league/season/month, it compares cached original provider
`raw_statistics.expected_goals`, source `normalized_stats.HxG/AxG`,
and the enriched published `HxG/AxG`, using verified fixture ID,
team names and kickoff within 15 minutes. Missing xG, identity mismatches,
duplicated IDs and normalization discrepancies are counted separately.
If raw team identity cannot be independently verified, the raw xG is NOT
credited as verified. The tool never synthesizes xG or certifies picks.

Locally executed synthetic regression tests: **8 passed (2026-10-10)**.
An additional read-only four-league GitHub-cache check of 207 completed
2026–27 games found **zero raw-to-published xG mismatches**, but substantial
missing September xG at the raw source. A full 94-entry local run and
the official Python model pilot are **still unverified**.
See `reports/betting_v2/REAL_CACHED_SOURCE_DIAGNOSTIC_2026-10-10.md`.

## Archived-price feasibility preflight (before expensive replay)

To audit 1–14 explicitly selected dates **offline** without training a model,
from a **full local Git checkout** of StatMaker-Data:

```bash
python scripts/betting_v2/archive_preflight.py --repository-root . --from-date YYYY-MM-DD --to-date YYYY-MM-DD
```

Output (local, not automatically committed):
`reports/betting_v2/archive_preflight.json`

For each day, it validates the pre-11:00 Athens Git manifest, bundle age,
SHA-256, ZIP entry, prepared SQLite schema, timezone-aware kickoff, odds
inside the strict 1.80–3.00 window, selection identities and presence
of explicit cached fixture IDs. It counts rejection reasons per archive
without calling any provider, running Actions, or touching App-Ready data.

The counts are **before exact fixture joining** and do not claim verified
historical bookmaker observation timestamps or independent/unfiltered odds.
Only run price replay on dates with usable archive coverage, chosen within
the pre-frozen chronological holdout. The preflight does not certify ROI or
generate betting picks.

## Exact pregame source reconciliation

The historical odds exporter resolves the bookmaker match to the **cached,
finished API-Football fixture** only when the league code, normalized home
and away team names and kickoff (within 15 minutes) resolve to exactly one
fixture. An explicit source API fixture ID must agree with this evidence.
Ambiguous, missing or late fixture identities are dropped with rejection
counts. No fuzzy team aliases or guessed fixture IDs are permitted.

Odds dates are grouped by **Athens local day**, while stored training dates
are **UTC**. The research join uses verified fixture ID + league + kickoff
instead of comparing those potentially different dates. The quote-cutoff
timestamp must be strictly before kickoff. This fixes the midnight-rollover
case without accepting incorrectly priced or mismatched fixtures.

**Research limitation:** the historical exporter uses legacy
\`prepared_selections\` from archived App-Ready bundles, not an independently
verified complete bookmaker market. Upstream selection/coverage bias is
unresolved. Replay rows carry \`priceUniverse=LEGACY_PREPARED_SELECTIONS\`, and the
calibration report sets \`independentUnfilteredBookmakerUniverseVerified=false\`.
ROI from these records is diagnostic only and cannot certify V2.

Replay rejects unverifiable, future-dated or >24-hour-old bundle generation
timestamps. ZIP generation time does not prove individual bookmaker quote
observation time; \`quoteCutoff\` is the as-of upper bound, NOT an independently
observed price timestamp. Complete, independently captured quotes remain
a certification prerequisite.

## Prematch feature completeness

The model requires observed attacking AND defensive xG/xGA in its actual
feature windows: 8 in the last 20, all 5 most recent fixtures, and at least
3 venue-specific observations in the latest 8 relevant venue appearances.
Missing xG **never receives a synthetic average of 1.4**. Such fixtures are
rejected before probabilities are generated.

This is intentionally stricter than the earlier *diagnostic* report based on
3,164 qualifying historical rows. The published 10/18 outcome at raw p>=60%
belongs to the **older exploratory model**, not the revised full-data
contract. A fresh holdout on genuinely unseen results is mandatory.

## Explicit local tests (no GitHub Actions)

```bash
python -m unittest discover -s tests -p 'test_betting_v2_*.py' -v
```

Synthetic tests cover missing xG/xGA, ambiguous fixture IDs, UTC/Athens
midnight rollover, quote timing, 1X2, DNB PUSH, strict team-goals identity,
malformed goal lines, quote duplication, archival bundle age, and the
strict 1.80–3.00 price interval. No full Python test-suite pass has yet
been verified in this conversation. Connected GitHub sources alone are
not an executable full local checkout.

## Integration conditions (NOT satisfied yet)

1. The pilot must run successfully and all audit issues resolved.
2. Quoted selections must match the same fixtures/market identities, with
   settlement support, and be numerous enough for meaningful statistics.
3. The model's empirical calibration, model error, priced ROI and leakage
   must be evaluated on proper independent holdout sets.
4. Realistic injuries/lineup scenarios must be covered or the fixture blocked.
5. Required per-market league/history features must actually be present.
6. A versioned immutable certified App-Ready sidecar plus integrity validator
   must be implemented, with exact generation/selection/price binding.
7. Changes to the existing PROD publisher/manifest and UAT consumer require
   a separate explicit rollout review; do not hotpatch them just to show picks.
8. Model Performance/Daily/ROI must preserve all historically issued
   selections and settlements.

## Change-control notes

- Only \`scripts/betting_v2/\` and V2-specific reports/tests are allowed in
  this early shadow implementation.
- Existing \`app-ready-artifact-publisher.yml\` nightly rebuild is untouched.
- No additional scheduled workflow / \`workflow_run\` fan-out.
- **Never publish empty V2 shadow output over an existing PROD bundle.**
- All current Android App/Stats/Simulation/Score Edge/Daily/Performance data
  stay on the current established feed.
