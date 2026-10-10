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
- \`export_historical_prices.py\`: pre-kickoff original bookmaker quotes.
- \`evaluate_holdout_prices.py\`: price-aligned raw-value evaluation.
- \`calibration_gate.py\`: bin/league Wilson-bound exploratory holdout.

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
