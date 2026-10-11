# StatMaker Betting V2 — real App-Ready UAT diagnostic

This is a **read-only diagnostic runner**, not a new product publisher. It uses **the canonical PROD App-Ready betting ZIP/SQLite** (all fixtures and quoted selection rows), not the 132-match research preview. It uses historical fixture records from `data/statmaker/domestic_enriched/index.json` and the existing frozen xG/Elo coefficients, without fitting or requiring evidence certificates. Unsupported or missing-data markets get an explicit rejection reason; none are silently recast as another market.

## Running on the user's actual StatMaker-Data checkout

Use a **separate clean worktree** for this UAT branch; the Codex Phase 4 checkout has 16 uncommitted changes that must not be overwritten. Open PowerShell in the clean StatMaker-Data worktree and run:

```powershell
python -m scripts.betting_v2.run_real_prod_diagnostic `
  --repository-root . `
  --app-ready data/statmaker/app_ready/app_ready_betting_bundle-4f45d155f3f836aa7f16cf6c97cca8706fa7030ef3106651e105e921bbfdf7f2.zip `
  --generation-id 9ad502ffac81f0578cb5f1d0db81bcf4428700d2bbdf7d1ef15e274ec0de9886 `
  --output "$env:TEMP/statmaker-betting-v2-real-prod-diagnostic.json"
```

Before execution, verify the current canonical `data/statmaker/app_ready/update_manifest.json`: if it references a different betting bundle or generation, replace the ZIP path and generation ID with those of that same exact manifest. Run `python -m unittest scripts.betting_v2.test_real_prod_diagnostic -v` first. Python 3.11+ is sufficient for this runner.

## Output

- Exact raw fixture/selection totals, forecastable fixture count and per-reason rejects.
- For each qualifying **real** quoted selection: market, fixture, model (xG/ELO), price, modelled win/push probabilities and indicative EV.
- Price filters: strictly 1.80 < decimal odd < 3.00; raw win probability >= 60% and raw EV > 0. No Asian/handicap markets.
- Projections use Poisson score/count assumptions and prior-date history. Count markets with insufficient real market history are explicitly rejected. Some labels in raw data or non-domestic competitions may remain unsupported.
- **No scientific certification implied**: historical quote observation times, independent calibration and real adverse scenarios are not established. Never relabel indicative candidates as certified STRONG or publish automatically.

The tool does not call provider APIs, does not invoke GitHub Actions, does not modify existing App-Ready artifacts, and does not touch the Android PROD application or user DB. Output is created as a new file; an existing output file will cause an error rather than be overwritten.

## Limitations

This is the initial UAT computation path, not yet an Android Singles integration. The full 1,800-fixture App-Ready ZIP cannot be executed within the chat's local container (the GitHub connector exposes large binary ZIP contents as unavailable), so **no production-input run or production candidate count is claimed here**. Nine local regression tests passed using synthetic unit fixtures only; the full real-data run must be observed before enabling UAT consumer integration.