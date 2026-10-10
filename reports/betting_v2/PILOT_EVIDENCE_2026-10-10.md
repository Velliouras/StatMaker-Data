# StatMaker Betting V2 — official offline Python pilot and cached bookmaker audit

**Date:** 2026-10-10 (Europe/Athens)  
**Repository:** `Velliouras/StatMaker-Data/main`  
**Official verified QA:** [run 38080856362](https://github.com/Velliouras/StatMaker-Data/actions/runs/38080856362), trigger SHA `9943f8bac1d5f32690609a2366c0c5c5d1e93d68`. The GitHub commit status `betting-v2/offline-qa=success` confirms **116/116 Python stdlib tests passed**, both independent offline Python pilots completed, the fail-closed manifest was verified, and the cached odds audit scanned the entire source. GitHub artifact: `betting-v2-offline-qa` (7-day retention; preserve relevant metrics here).

**This is not an authorization to publish STRONG.** Zero API-Football calls during QA, zero App-Ready / Android PROD changes, zero priced value certificates.

## Primary xG model — real chronological walk-forward Python run

- 4,106 eligible historical prematch rows.
- Tune: 1,167 fixtures; independent calibration: 193; final untouched holdout: 409.
- Tuned parameters: recent=0.25, venue=0.65, xG=0.80, Elo=0.50.
- Final holdout 1X2 argmax accuracy: 50.61% overall.
- Raw top-1X2 probability >=60%: **30 wins / 32 fixtures** (93.75%). A small selected subgroup, **NOT a conservative calibrated STRONG certificate**.
- Frozen market-and-league pre-price readiness: **1,276 market evaluations in 140 distinct fixtures** (markets per fixture are correlated).
- E.g., no 1X2 selections passed the frozen market/league Wilson gate; match Over 2.5 goals also passed zero evaluations.

## ELO + observed-goals fallback — independent no-xG population

- 11,368 fixtures ready for historical Elo model in total; **7,262 specifically require the Elo fallback** due to insufficient genuine prior xG evidence.
- Tune: 1,233; separate calibration: 1,237; final untouched holdout: 1,360.
- Tuned parameters: recent=0.50, venue=0.35, Elo=0.50. **Never substitute fake xG.**
- Final holdout raw argmax 1X2 accuracy: **660/1,360** (48.53%).
- Raw top-1X2 probability >=60%: **155/233** (66.52%), pooled descriptive 95% Wilson lower bound **60.24%**. This overall descriptive bound **does not** prove the market-and-league, quote-priced, lineup-adverse criteria.
- Frozen market/league pre-price readiness: **5,989 market evaluations in 688 distinct fixtures**. Most are very likely short-price selections such as team Under 4.5 or Under 5.5.
- Over 2.5 match goals: 18 pre-price eligible evaluations. 1X2: zero pre-price eligible.
- **No independent bookmaker offers were joined**, so none is an actual bet or certified STRONG.

## Cached odds source — full-file offline audit

Source: `odds/odds_api_io/domestic_odds.json`, size **47,508,733 bytes**, SHA256 `1aa89db601dd5387b882ef384de0c77f61d8f6ec6ea23e10d2db4cf8d48b678f`.

The complete container-only traversal examined **129,625 structural nodes**, `scanTruncated=false`:

- **125,872** objects containing price/odds-like field keys.
- **125,841** price-like objects containing bookmaker identity and **125,841** containing selection identity in the same object.
- Only **25** price-like objects with an `updatedAt`-style timestamp candidate and absolute timestamp (counts are field-shape observations, **not validated bookmaker quote timestamps**).
- **Zero self-contained objects observed** containing the full same-object bookmaker + selection + absolute timestamp combination in this structural scan.
- Generation time and provider-archive timestamps are **not** individually verified quote observation times.
- No claim of unfiltered independent offers, bookmaker-specific quote time, timestamp provenance, source-generation evidence, unbiased quote universe, or historical ROI is supported.

**Consequence:** No historically valid priced backtest is currently certifiable using these cached odds. Existing `LEGACY_PREPARED_SELECTIONS` remain barred by `quote_provenance.py`. Do not set `priceObservationTimestampVerified=true` by copying `generatedAt` or unrelated `updatedAt`.

## Prioritized implementation work remaining

1. Extend the **existing bookmaker ingestion** with an append-only, generation-bound receipt collected when the *actual* bookmaker offer is observed: observation instant, provider/bookmaker IDs, exact market/selection/line/odd, event identity, provider-supplied update metadata where available, full-universe completeness and immutable source hash. **Do not retrofit timestamps on existing legacy archives.** Preserve normal provider quota; do not trigger extra fetches merely for tests.
2. Build independently timestamp-verifiable, non-selected full-universe historical quote archives over future real games. Reject any quote lacking exact provenance or pre-kickoff identity.
3. Evaluate frozen calibrated xG vs Elo models separately on genuinely independent priced holdout, odds strictly `1.80 < odd < 3.00`, positive conservative EV, adverse lineup bounds, and actual net ROI. Deduplicate correlated alternatives in each fixture. Counter small bins and under/over overconfidence.
4. Add remaining market models (such as corners, shots, cards) **only with sufficient observed features and independent calibration/validation**; do not silently label unsupported markets STRONG.
5. Authorize Android UAT -> verified tests -> PROD only after independent certification; do not overwrite existing PROD Betting engine meanwhile.

## Safety/commitment

Research and shadow-only. `publish_shadow.py` deliberately blocks promotion (`certificationStatus=BLOCKED`, `realStrongSelections=0`). The most recent offline Python workflow returned SUCCESS **for research QA**, not an economic forecast certification. Zero API calls, no Android PROD writes, no automatic published betting recommendations.
