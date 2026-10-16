# Expanding prior calibration — 2026-10-16

The first 42-fit model revision is preserved in `../model_revision_20261016/`. Its narrow one-month selected calibration had only 24 preceding selected fixtures for the October fold and correctly retained identity. This second experiment fixes that coverage limitation without refitting any core, changing thresholds, selecting a winning arm, or modifying the proper-loss objective.

Use every available preceding frozen prediction month. Seed the August calibrator from July's saved prior calibration cohort. After freezing August calibration, append the original unmodified August out-of-time predictions using their own prior-July base profile. Freeze September calibration before appending September's original out-of-time predictions using their own prior-August profile. Freeze October calibration from that preceding pool. Each fixture/market event is unique; no later evaluation month can enter its own or an earlier calibrator.

The initial July seed is calibration data using a profile estimated in that same prior month; subsequent frames are out-of-time predictions. Preserve and label this distinction. Historical model versions differ because their coefficients are fitted before their respective prior windows; all use the same declared recency/reliability algorithm. The correction transfers observed historical algorithm errors, without mixing bookmaker probabilities into the model.

Price joins for each appended frame use existing country-scoped aliases plus schedule recovery strictly within that prior frame, with two dates/two distinct existing opponents. Labels, prices and prediction values cannot resolve aliases. Only Bet365 closing prices are used. These are retrospective benchmarks with unverified individual quote/publication timestamps.

The same four joint tilts, Brier objective, regularization penalty 10, minimum 30 unique prior selected fixtures, 12 UTC-week bootstrap replicas, conservative percentile, fixed original odds/probability/EV policy and all six comparison arms remain unchanged. Report every negative and zero-pick result. Preserve all input hashes and the complete prior source-frame inventory. No new core fit, provider request, LLM call or download.

This protocol is a development response to insufficient prior calibration coverage. All evaluation periods were already inspected. No out-of-sample profitability certificate or automatic promotion based on this study's ROI is claimed.
