# Integration status

This package is offline research, not a completed Android UAT deployment.

The previously inspected Android direct recommendation path is
`PreparedPatternRecommendationRepository` →
`PreparedFixtureReadModelRepository.directBettingV2Estimates`.
The reader expected model `betting-v2-monotone-matchup-scenarios-v3` and
10,000 runs. The new exact adapter returns model `betting-v2-joint-count-v4`
and zero simulated runs. It must not impersonate the old contract.

Before normal UAT integration:

1. Pair a frozen fitted model with its own earlier-window calibration profile;
   retain source/data fingerprints and model provenance.
2. Materialize an immutable UAT candidate database through the exact adapter.
3. Update the actual Android direct reader to accept and explain exact
   probabilities, without a second app ID or unrelated UI changes.
4. Verify selection and displayed explanation against the same distribution,
   including pushes, halves and full-time-winner conditional scenarios.
5. Exercise normal Android UAT. Python policy replay is not Kotlin parity.

HIGH/CONFIRMED/DEVELOPING remain raw probability bands in the old direct path,
not empirically certified confidence tiers. The inspected historical periods
cannot serve as an untouched holdout. Publication-time provenance of cached
statistics remains unverified, and in-sample goal-strength stacking needs
separate out-of-fold work. These are substantive limitations, not just a
publication approval issue.

PROD, Singles maintenance, existing non-betting app behavior, and UI are not
changed by this research package. No provider API calls are needed to run it.

## Publication blocker

Automatic approval review rejected the earlier push to `StatMaker-Data/main`
because explicit approval for that branch mutation was required. No alternative
transport was used to bypass it. Request approval for the concrete reviewed
patch before pushing. The included workflow is manual, main-only, read-only,
and uploads research artifacts; it does not publish candidate feeds.
