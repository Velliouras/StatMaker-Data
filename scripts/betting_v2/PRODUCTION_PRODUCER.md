# Certified Betting V2 Production Source

Source implementation only, 2026-10-11. NOT RUN, NOT TRAINED, NOT TESTED,
NOT VALIDATED, NOT PUBLISHED. This is not a claim of profitable models or
currently usable certified recommendations. Existing research pilots and
`reports/betting_v2/betting_v2_precompute.json` cannot supply certificates.

## Existing Production Inputs

The producer reads the SAME canonical App-Ready SQLite and API-Football caches
used by StatMaker-Data/main. It does not contact any provider. There is no UAT
dataset, old `prepared_selections` discovery join, fixture cap, odds shortlist,
default day scope, or hidden market truncation.

`production_sources.full_inventory` LEFT JOINs all `prepared_fixture_matches`
with `prepared_fixture_markets`, retaining unquoted/unsupported/malformed rows
and explicit blocker counts. Ready `prepared_snapshot_meta` versions bind each
row; `id` must be an actual immutable fixture identity, not a synthesized name.
The inventory includes every competition present in the canonical tables.

The history adapter follows schema-3
`data/statmaker/domestic_enriched/index.json` and every `cache_path` it names.
It reads the actual API-Football `fixture_stats.json` schema, including canonical
fixture/team IDs, regulation-time FT scores, provider statistics, normalized
counts and real `expected_goals`. It does not use readiness thresholds in that
index as Betting V2 policy. Other competitions remain in discovery, but cannot
borrow Domestic calibration or claim cached Domestic rows are their own league
history. Missing exact crosswalks/history/context/models block those rows.

Old cache `generated_at` is NOT historical observation time. Old cached rows
without a genuine per-response receipt are rejected for point-in-time evidence.
No absence of a join, missing team response, invented zero, research timestamp,
or later rebuild timestamp establishes xG unavailability.

Existing fetched-statistics success path can capture lineage only when
`STATMAKER_BETTING_V2_CAPTURE_LINEAGE=1`. Default behavior is unchanged. Only an
actually received response creates `betting_v2_provenance`; cached old rows are
never backdated. Existing request caps, caching and quota accounting stay intact.

Existing bounded Domestic context calls can archive their actual successful
`/injuries` and `/fixtures` responses only when
`STATMAKER_BETTING_V2_CONTEXT_RECEIPTS` names an approved immutable receipt
directory outside `data`, `reports`, `.github`. No extra call is made. Old public
`squadContext.generatedAt` does not constitute a pre-match receipt.

Context adaptation uses first actual pre-match injury observation and explicit
home/away team IDs. Features are counts of provider-reported Missing Fixture
players, not a claim that all real absences are known. A grounded adverse scenario
adds actual provider Questionable players. Unknown types/player IDs, absent
receipts and fixtures without an observable doubtful-player scenario are blocked.
Historical adverse calibration additionally requires a genuine subsequent
pre-kickoff response containing both actual 11-player starting XIs, showing those
players did not start. Realization is outcome evidence, NOT an earlier feature.
Unsupported tactical/motivation scenarios are not synthesized.

## Explicit Offline Phases

The module entry point is `python -m scripts.betting_v2.produce_certified_app_ready`.
These phases are IMPLEMENTED FOR FUTURE EXPLICIT AUTHORIZATION, not executed by
the implementation assignment. No CLI automatically runs the next phase.
Outputs are exclusive-create immutable artifacts outside checked-in production
or research paths. Python >=3.11 and optional `requirements-producer.txt`
dependencies are required for future fitting/evaluation; none was installed now.

1. `inventory --repository-root ROOT --prepared-db DB --generation-id ID
   --workspace DIR` writes `source.json`: full raw inventory, source DB SHA256,
   raw inventory SHA256/counts, cached history/schedules and exact blockers.
   DB is opened read-only. This is not a new published dataset or migration.
2. `quotes --source SOURCE --receipts RECEIPT.gz [...] --attestations REVIEW
   --output FILE` adapts original cached bounded quote receipts. Those receipts
   remain research-only on their own. Exact original outcome ID, decimal price,
   observedAt, validUntil, event/bookmaker identity, independent reviewed complete
   universe and an exhaustive mutually exclusive decisive price partition are
   compulsory. Missing provider timestamps/IDs/expiry are NOT inferred from
   client receipt time, bundle time or a chosen TTL. No historical API fetch.
3. `contexts --source SOURCE --receipts CONTEXT.json [...] --maximum-age-ms AGE
   --output FILE` binds context values to real provider receipt fields. AGE is an
   explicit freshness policy input, not a default; model parameter candidates
   also declare and evaluate historical feature ages. Unproven availability is
   rejected, never converted to zero. Source and receipt artifacts must preserve
   earlier observations rather than overwrite them.
4. `freeze --source SOURCE --contexts CONTEXTS --plan PLAN
   --calibration-quotes QUOTES --output FILE` selects candidate count-model/window/
   strength parameters on earlier walk-forward folds, fits the training period,
   calibrates distribution rates on an earlier calibration-fit subperiod, then
   freezes independent calibration-policy error bins and tier criteria. It must
   actually finish BEFORE untouched holdout begins. It does not read holdout
   prices/outcomes. A later historical file cannot pretend the freeze was earlier.
5. `evaluate --source SOURCE --contexts CONTEXTS --frozen FROZEN
   --holdout-quotes QUOTES --output FILE` applies that frozen model/policy without
   tuning. Actual available labels, independent fixture blocks, comparable bins,
   base and separately realized-adverse errors, conservative interval coverage,
   drift, empirical baseline/no-vig comparisons and positive lower-bound priced
   profitability are required. Failure creates no certificate. Reusing failed
   holdout results for tuning is prohibited; a new prospective plan/holdout is
   required. Price artifacts describe retrospective evaluation cases, NEVER
   actually issued historical picks.
6. `produce --source SOURCE --contexts CONTEXTS --quotes CURRENT_QUOTES
   --evaluated-models EVALUATED [...] --output RELEASE` attempts every raw quoted
   row, with per-row reasons, and constructs only currently valid supported
   forecasts/documents/quotes from genuine evaluated models. No source is cut
   because a model or quote is missing. Unquoted fixtures are counted as blockers.
   Empty output may explain blockers but cannot be installed as a certified
   release. No phase writes to the Android ledger or replays historical issues.

Prospective inventory and receipts must be captured incrementally before the
corresponding predictions. Current raw fixtures alone cannot reconstruct an old
quote universe. Historical canonicalOffer/generation/snapshot/price bindings stay
attached to their actually captured quote artifact; future production is checked
against the current full raw inventory again before staging.

## Evaluation Plan Contract

`contract = statmaker-betting-v2-evaluation-plan-v1`. No default successful plan,
thresholds, samples or fixtures are supplied. A real precommitted plan must include:

- scope: competitionId, leagueCode, season, family, marketModelKey, mode,
  oddsBandFrom (>=1.8), oddsBandTo (<=3), historicalQuoteDecisionPolicy
  (`FIRST_OBSERVED` only), and contextColumns
  (`homeUnavailablePlayers`, `awayUnavailablePlayers`).
- real absolute numeric epoch-ms boundaries: planObservedAtMs, trainingStartMs,
  trainingEndMs, calibrationStartMs, calibrationFitEndMs,
  calibrationPolicyStartMs, calibrationEndMs, policyFreezeAtMs, holdoutStartMs,
  holdoutEndMs, evaluateAtMs, validUntilMs. Plan must precede calibration; train,
  calibration-fit, calibration-policy and untouched holdout cannot overlap.
- walkForwardFolds: each fitStartMs/fitEndMs/testStartMs/testEndMs inside training,
  with point-in-time labels. Earlier labels cannot depend on later cache receipts.
- parameterCandidates: explicit recentWindow, longWindow, venueWindow,
  minimumHistory, maximumFeatureAgeMs, eloScale, eloK, regularization,
  maximumFitIterations. Window/strength parameters are selected by earlier
  empirical count loss, not by current prices or future ROI.
- minimumTrainCases, minimumCalibrationCases, minimumHoldoutCases,
  minimumCalibrationBinCases, minimumHoldoutBinCases, minimumHoldoutStrongCases,
  minimumIndependentFixtures, minimumIndependentCalibrationFixtures,
  minimumIndependentHoldoutBinFixtures, calibrationBinCount.
- confidenceAlpha, bootstrapSeed, bootstrapReplicates,
  maximumHoldoutCalibrationError, maximumDistributionCount, tailMassTolerance,
  calibrationLogScaleBounds (two [lower, upper] pairs), correlationThreshold,
  tierQualityQuantiles for HIGH/CONFIRMED/DEVELOPING in descending quality order.
  These are frozen prospective research design/search settings; they are NOT
  certificates or arbitrary Kotlin production thresholds. They must be justified
  before a real run, and surviving tiers require independent holdout evidence.
- CARDS only: cardCountContract with contract
  `statmaker-verified-bookmaker-card-count-v1`, bookmaker, actual providerRule,
  providerRuleSha256 and its exact yellowWeight/redWeight. No default card points
  or assumptions about second-yellow/red counting are supplied.

The independently evaluated mode is XG_PRIMARY or
ELO_GOALS_FALLBACK_NO_XG. The latter requires real source-verified absence across
its required form windows and its OWN fitting, calibration and holdout evidence.
Partial xG coverage is neither complete xG evidence nor proof of provider absence.

## Models, Uncertainty And Boundaries

Fitted home/away Poisson count regressions with empirical dispersion and residual
Gaussian copula generate common joint count cells. FT goals/result/DC/DNB/BTTS
share the same actual family model hash; differing family fits are rejected.
Corners/shots/SOT/yellow cards/documented card counts have their own historical
targets and fits, never a disguised goal-model probability. 1H/2H require actual
cached period counts; absent period fields are not fabricated. ELO uses available
real earlier scores, opponent strength and venue/form windows; no unobserved
equal prior counts as verified team history.

Rate calibration and uncertainty/policy estimation use disjoint calibration
subperiods. Empirical intervals resample whole fixture blocks, not correlated
selections as independent matches. Uncertainty includes observed model error,
sample uncertainty, period drift, misspecification, season-transition quality and
explicit unresolved numerical tail mass. Conservative WIN/PUSH/LOSE is one
coherent mixture towards loss. Adverse bins/errors come only from independently
observed realizations, never base-confidence copying or feature ablations.

Numerical integration, finite support and model-family assumptions are explicit
and still require genuine independent validation. Overflow cells are tagged;
their possible WIN error is debited conservatively, not claimed as exact scores.
Forecasts outside fitted/calibrated scope, freshness, cohort or verified data fail
closed. Coherent probabilities and SHA256 alone do not prove a good model.

Calibration-supported tier criteria are frozen before holdout. All three tiers
require 1.80 < current odd < 3.00, conservative base/adverse WIN >=0.60, positive
base/adverse `WIN*odd+PUSH-1`, and independently priced profitable evidence.
Historical PUSH refunds are separate from losses. Asian/Handicap are retired;
unimplemented settlement/model contracts are counted, not promoted.

Correlation groups use event-indicator dependence under the shared fitted family
distribution; within-family alternatives are not blanket family-collapsed.
Cross-family joint dependence, BB and Paroli joint calibration are NOT established
by multiplying these marginal forecasts. Android joint BB/Paroli blockers remain.

## Exact Kotlin-Compatible Population

Contracts:

- statmaker-betting-v2-certification-v1
- statmaker-betting-v2-evidence-v1
- statmaker-betting-v2-forecast-v1

Exact SQLite tables/columns, in the same canonical prepared DB:

- prepared_betting_v2_certificates(certification_id,payload,sha256)
- prepared_betting_v2_evidence(sha256,payload)
- prepared_betting_v2_quotes(generation_id,competition_id,snapshot_version,
  fixture_id,selection_id,bookmaker,provider_quote_id,observed_at_ms,valid_until_ms,odd)
- prepared_betting_v2_forecasts(generation_id,competition_id,snapshot_version,
  fixture_id,selection_id,payload)

Payload strings are canonical UTF-8 JSON; SHA256 hashes the EXACT stored bytes.
Selection identity matches Kotlin: competitionId, immutable preparedMatchKey,
original trimmed market/selection/team and SIGNED Java Double bit-pattern for
line, separated by U+0001. Market aliases affect model identity only, never this
exact raw identity. Quote/event/bookmaker/generation/snapshot identity is exact.

Certificates bind model/calibration/mode/cohort, training fingerprint, chronological
split, actual freeze/evaluation times, empirical diagrams/errors/adverse/drift/
profitability/price documents, comparableCases and identical frozen tierCriteria.
Forecasts bind exact quote/fixture/selection, coherent settlement/uncertainty,
actual feature lineage/validity, distribution, adverse and scenario group, plus
verified no-xG provenance only when applicable. Runtime Kotlin reconstructs
domain fixtureId as competitionId + '|' + immutable fixture ID; JSON/SQL retain
the raw immutable fixture ID. No UI, issue time, historical odds or ledger rewrite.

The existing `materialize_prepared_fixture_index.py` CLI invokes
`production_app_ready.install_release` ONLY when BOTH explicit env paths are set:
`STATMAKER_BETTING_V2_RELEASE_PATH` and `STATMAKER_BETTING_V2_RELEASE_APPROVAL`.
Default CLI execution is unchanged. No workflow/trigger was added or changed.
The existing publisher still owns file hashes, manifests, bundle/package and
atomic publish. Optional insertion happens before those existing operations.

Approval contract is `statmaker-betting-v2-owner-release-approval-v1`: approvedBy,
approvedForCanonicalAppReady=true, releaseSha256 (exact immutable file bytes),
generationId and stagingRoot. This assignment does NOT grant that future release
approval. Only a disposable staging DB under approved stagingRoot, outside
repository/data, may be populated. Installed/user/published DBs are prohibited.
The full raw inventory and existing generation/source/rules fingerprints must
match, nonempty genuine forecasts/certificates and all dependencies must be
valid. Inserts are transactional and immutable; conflicting prior evidence is
rejected, not replaced or deleted. user_version remains unchanged. Empty CREATE
TABLEs alone cannot satisfy the release gate.

## Present Blockers And Execution Ledger

No model, evaluation, quote attestation, receipt archive, certificate, forecast
release or approved publication was produced by this source assignment. Current
PROD input coverage/freshness is unmeasured. Old cached statistics/context/quote
receipts often lack the required original observation/expiry/completeness proof;
their absence blocks the appropriate stage, not a synthetic replacement.
The source adapter currently normalizes Domestic API cache history; WC/UEFA
rows in full discovery need their own verified history/context/crosswalk/cohort
before certification. No fake provider, copied Domestic cert or fallback is wired.

NO TESTS / NO VALIDATION / NO BUILD / NO ACTIONS / NO PROVIDER API / NO PUBLISH.
Source reading and Git delivery are the only executed operations. Nothing here
claims compile success, empirical acceptance, positive ROI or a ready release.
