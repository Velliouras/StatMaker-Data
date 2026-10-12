# Real V2 UAT Feed

Consumer: StatMaker/uat Singles. This path is not part of the PROD manifest, SQLite,
odds bundle, canonical recommendation ledger, settlement feed, or Performance.

Initial seed: the real 2026-10-12T15:41:39.903684+00:00 evaluator output.
Candidates are UNVERIFIED. Quotes are from that source bundle, not live prices.

Refresh from the current canonical App-Ready manifest/betting ZIP and cached history:

```powershell
py -3 -m scripts.betting_v2.publish_uat_feed
git add data/statmaker/uat/betting_v2
git commit -m "Refresh real V2 UAT candidates"
git push origin main
```

This invokes the existing offline evaluator and exact bundled-quote binder; no
provider request, training, test cycle, or App-Ready rebuild is performed. Production
inputs are read-only. History comes from the repository's existing Domestic cache.
Failed evaluation leaves latest.json intact. Every payload is immutable and
content-addressed. latest.json points only to a fully written payload.

The existing App-Ready Stats Hot Publish and App-Ready Artifact Publisher jobs also
perform this same host-only step after their normal publication. No new jobs,
triggers, workflow_run fanout, or API requests are added for V2.

Android UAT keeps its last valid feed or packaged seed if refresh fails. Welcome's
existing background updater and Update All read latest.json and its small JSON
payload, never compute recommendations. Singles hide started fixtures, display
source generatedAt/generation and STALE after 24h or installed-generation mismatch.
The timestamp is inference/snapshot time, not a bookmaker observation timestamp.
Actual device rendering/refresh and production promotion require owner validation.
