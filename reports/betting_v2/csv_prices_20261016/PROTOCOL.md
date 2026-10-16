# Archive-price replay protocol, 2026-10-16

Purpose: expand the narrow receipt-based priced study using the public Football-Data archive, while reusing the original frozen predictions. This study does not replace the app's API-Football/Odds-API.io runtime sources. CSV remains `inactive_archive_only`.

## Frozen choices before reading the new ROI

- The existing `full` and `separate_ft_selected_period` arms are compared on identical fixtures. No coefficients, calibration, EV/probability/odds thresholds or policy choices are fitted here.
- Three original monthly windows: fit before July / calibrate July / evaluate August; fit before August / calibrate August / evaluate September; fit before September / calibrate September / evaluate October 1–14, 2026.
- Original raw predictions use completed UTC days, as implemented by `validated_walk_forward.rolling_rows`. No current-day results enter prediction features. This still does not verify historical publication times.
- The recovered source prediction files and original profiles are individually SHA256-bound in `input_manifest.json`. The compressed frozen input is checked again by the runner. Its original training-data hash differs deliberately from the identity metadata snapshot; the latter supplies only explicit aliases and league/date/pair metadata, never recomputed features, means or labels.
- Bet365 is the primary bookmaker. Pinnacle is a separate secondary benchmark, where present. `Max`, `Avg`, exchanges, handicaps and invented market prices are excluded. All three existing selection policies are reported; overlapping results are not pooled as independent bets.
- Import both pre-closing and closing columns. Replay closing columns only. Individual collection times are unknown; no artificial `observedAt` timestamp is assigned. Pre-closing quotes may precede match-day features and are therefore coverage evidence only.
- Price contracts: FT 1X2 and FT O/U 2.5. Missing half, player, corner, card and other price contracts are not synthesized from these prices.
- Unique league + same calendar date + existing country-scoped home/away aliases; require an independent full-time score agreement after selecting the unique candidate. CSV kickoff timezone is unspecified, so this is explicitly not an exact UTC-kickoff join. Ambiguity, test divisions, score conflicts, missing final scores and conflicting duplicate quotes are excluded with reasons.
- One bookmaker and price view per replay. The existing one-result-family and one-goals-family policy, fixed unit staking and fixture-cluster intervals remain unchanged.
- All negative and empty results, bookmaker views, months, market groups, league groups and tiers are retained. No profitable subset is promoted. This is already inspected development evidence, not a new untouched holdout or a profitability certificate.

## Bounded independent batch

38 files: the 22 main 2026/27 divisions plus 16 extra top divisions already in the app catalogue. Each download is limited to 8 MB and a 25-second socket timeout, with at most three concurrent downloads. Successful raw CSV bytes are hash-bound and gzip-cached for resumability. Partial download failures remain visible. The batch runs plain Python in GitHub Actions, with zero LLM calls and zero sports-provider API calls. It publishes only this report directory.

Source documentation: https://football-data.co.uk/notes.txt and https://football-data.co.uk/all_new_data.php.
