# Shots / SOT UAT v1

Independent numerical UAT model, not KickoffScore's proprietary model. Only
full-time match/team SHOTS and SHOTS_ON_TARGET Over/Under integer and half lines.
No production app model changes, paid provider calls, LLM calls or automatic promotion.

The app reads `latest.json`, verifies the content-addressed candidate file and imports
`issued` into its separate `UAT_SHOTS_CATBOOST_V1` Performance source. The same records
drive Daily Outcomes. A Performance setting switches back to the previous model's
history; no old entries are deleted. Singles filtering does not create or remove issues.

## Initial frozen bank

- Cached history: 17,950 fixtures, SHA256
  `a9f43dca5f0741c20dc8a3de5fcb9b8a05a4a3d9efeda4266758f4afea5693bb`.
- Training: 7,321 fixtures whose kickoff plus 24h precedes 2026-09-01 UTC.
- Calibration: 1,271 separate September fixtures available before 2026-10-01 UTC.
- Three fixed CatBoost seeds, 220 trees, depth 5. No ROI parameter search.
- Poisson objective for expected SHOTS; exposure-weighted CrossEntropy for SOT/SHOTS.
  A negative-binomial count distribution and binomial thinning give a valid joint
  where SOT is a subset of SHOTS. The lower envelope across three fits is a model
  sensitivity measure, not a statistical confidence interval.
- Inputs: lagged team/opponent counts, venue, rest, goals, ELO and available xG.
  Actual results enter features only at kickoff +24h. Missing xG stays missing.
- Frozen models are reused by the scheduled publisher. Retraining/version changes
  require a deliberate model revision; no automatic refits from observed ROI.

## Selection and publication

- Exact raw provider event, kickoff, team names, bookmaker, market, line, direction
  and price are bound together. All supported bookmaker lines are considered,
  including offers hidden by the normalized feed's best-price merge.
- Verified quote observation age <=24h; API-Football fixture ID required for grading.
  Provider `updatedAt` is the last price change, not a fetch clock. The legacy
  Android field `quoteUpdatedAt` carries the verified observation clock when
  available; `providerMarketUpdatedAt` separately preserves the provider clock.
  Verified client receipts, fresh UEFA snapshots and reported domestic whole-league
  polls are accepted. Retained rotating batches inherit no new clock. The domestic
  observation ledger is reused only when its exact payload hash still agrees.
  Without observation evidence the provider last-change time is a conservative
  lower bound; stale or future evidence remains rejected.
  Published read models separately add `latestQuoteObservedAt` when the identical
  first-issued event/book/market/line/side/price is observed again. This annotation
  does not modify `ledger.json`, original odds/probability/time or Performance.
  Changed, missing or stale prices are removed from current Singles candidates.
  UAT Android 0.1.66 uses this clock for current visibility, keeping the original
  snapshot clock in Performance; older clients retain their original 24h display rule.
- `1.80 < odd < 3.00`, conservative win probability >=0.55 and
  `EV = pWin*odd + pPush - 1 > 0`. Minimum 8 prior fixtures for both teams.
- Team props: same-book 1X2 favourite must agree with ELO and have >=60% count
  dominance across every fit, with favourite-Over or underdog-Under direction.
  Match totals do not assert which team wins or dominates the counts, so this
  directional gate does not apply to match totals (policy SHOTS_SOT_SCOPE_GUARD_V2).
- Maximum one first-issued selection per fixture and metric. SHOTS and SOT are
  separate families. No quota and no SOLID picks; the app labels all as Developing.
- Two-phase publication: commit/push the immutable issues, then verify the public
  commit's timestamp before kickoff and publish the app feed. Late issues never
  enter the prospective Performance ledger. New issues need >=15min to kickoff.
- First-issued ID, price, probability, publication time and line never change.
  Only pending outcomes can become WON/LOST/VOID using cached FT statistics with
  the same API fixture ID and kickoff. Missing or inconsistent target statistics
  stay pending. Integer-line pushes return one unit.

## Operation and checks

`Shots SOT UAT Feed` runs every four hours and after the existing odds/statistics
workflows finish successfully. It reads existing repository caches. Android checks
the feed at startup, on Singles opening and on its existing 15-minute Performance
poll, with a five-minute network throttle. Settlement timing depends on the
existing statistics collectors; it is not a live-results service.

Coverage includes domestic caches plus a read-only checkout of the existing UEFA
odds branch (Champions League, Europa League, Conference League). UEFA observations
require a successful current odds request, no rate-limit skip and no preserved
market rows. Country-scoped explicit aliases and exact cached kickoff/team/API
fixture identities are required; ambiguous mappings are rejected. No new provider
request is made. `coverage.json` reports source/identity exclusions and Athens
calendar-day coverage, including zero-selection days. Existing first-issued rows
are preserved when coverage expands; forecast parameters and thresholds are unchanged.

`python -m unittest scripts.shots_uat.test_contract -v`

`python -m scripts.shots_uat.feed --preview --cached-history-only`

`python -m scripts.shots_uat.replay`

`validation.json` contains the October development replay for policy
SHOTS_SOT_SCOPE_GUARD_V2, using the unchanged frozen numerical bank and the same
archived quote inputs: earliest 37 selections (+17.166u), latest 38 (+7.328u).
These are overlapping quote variants, not an untouched holdout or proof of
sustainable profitability. They are never imported into prospective Performance.

`validation-directional-guard-v1.json` preserves the earlier 12/14-selection replay.
That policy incorrectly required directional favourite dominance for match totals,
although a sum-of-shots bet makes no claim about the dominant team. The correction
removes this unrelated gate from totals; odds, probability, EV, quote age, exact
identity and all team-prop safeguards remain unchanged. No model was refitted and
no odds/probability threshold was lowered to fill a daily quota.

## UAT P55 policy (20 October 2026)

SHOTS_SOT_P55_UAT_V3 changes only admission pWin from .60 to .55. Exact odds
1.80<odd<3.00, EV>0, history/identity/freshness/favourite rules and frozen fits
remain unchanged. A fixed-grid post-hoc diagnostic on the same 19 October
snapshot gives 19/0 Saturday picks at .60, 25/1 at .58, and 37/7 at .55. These
are comparison counts, not a forecast of the next refreshed feed. The existing
October development replay gives .55: 82 (+21.495u) earliest / 86 (+20.279u) latest;
variants overlap and were inspected during development, so profitability is not
certified. No retrospective pick is imported to prospective Performance.

UAT 0.1.67 reads p55/latest.json. The root latest.json keeps only p>=.60 rows for
0.1.66 and earlier. Both are read models of one immutable first-issued ledger.
High >=.70, Confirmed >=.60, Developing >=.55 are explicitly model probability
bands, not historical confirmation. Developing below .60 is not called Strong.
Existing stored constant Developing labels are corrected when read, keeping all
original prediction, price, date and outcome fields intact. Performance and Daily
include all issued P55 model rows, including the below-.60 Developing band.

## Game and market intelligence (UAT 0.1.68)

A separate read-only intelligence block reconstructs factual historical summaries
using each issue's original feature cutoff, plus timestamped current contextual
information and exact same-line market comparisons. It never updates a numerical
prediction, first-issued price, selection, policy or outcome. Facts are not causal
feature attributions. No LLM/provider call, invented motivation or lineup
assumption is used. Missing xG and unconfirmed/stale squad information are visible.
Same-book/same-line opposite prices give a proportional no-margin probability
conditional on no push; no cross-book combination is used to estimate margin.
Break-even win probability and model fair price account for pushes. Separate
book prices show recent cached observations, not executable price guarantees.
Injuries/lineups are explicitly informational and have not been incorporated
into the frozen numerical bank. This is a first explainability/market-comparison
implementation, not a clone of OddysAI's proprietary engine or evidence of better
predictive accuracy/profitability.
