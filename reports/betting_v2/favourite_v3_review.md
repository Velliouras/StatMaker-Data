# Favourite matchup review — UAT 0.1.52

App commit: ed51717c141db7d195fea157ab351682dd46cea0 (uat only).
Data model source: ff7dddbc6f3d25eaaae158c58c6230da6ce79ffc.
Validated publish: https://github.com/Velliouras/StatMaker-Data/actions/runs/38640828918
Immutable V3 bundle SHA256: e6a886682677a3be68e24f2b945dc226eb86c49fc5af8d6700efb7cf3e06fcd0.
PROD/main unchanged: e770747eb91b6591397874cd16536a46e4d1b701. Singles remains under maintenance.

The earlier Bayern–Gladbach preference recovered from conversation history was Bayern Over 4.5 team goals @2.50 versus Gladbach Over 0.5 team goals @2.50, preferring the favourite. A later example used Over 3.5 and compared a different opponent (Dortmund). These are paraphrases of retrieved history, not verbatim quotations.

The unconstrained V2 fit gave GOALS_FT elo_gap a negative coefficient (about -0.367), and SOT elo_gap about -0.170. This inverted the intended meaning with other inputs fixed. V3 uses bounded Poisson fitting for semantically increasing attack/strength inputs. Learned coefficients may be zero: actual V3 GOALS_FT and SOT elo_gap are zero, not forcibly positive; corners is about +0.251. Club attack, opponent defence and competitive matchup evidence still determine the rates. No bookmaker price fabricates model probabilities.

V3 raw P(WIN), PUSH and EV remain unchanged by selection policy. Goal period offers are checked against the same sampled FT winner; other attacking counts use their own team-count dominance, without invented goal/corner correlation. Clear favourites use complete verified FT 1X2 prices. Contradictory matchup/count roles or insufficient scenario support reject offers. Ranking within one fixture/broad family prefers supported favourite alternatives; no club-specific exceptions or blanket Under prohibition.

Actual new Bayern–Gladbach goal means: 4.20713 / 0.783723.
Bayern–Dortmund: 2.43856 / 1.25999.
Bayern Over 4.5 is approximately 41.2% under this model, below the existing 60% eligibility threshold; do not assert that it automatically becomes a STRONG recommendation. Prefer the favourite among eligible alternatives, never manufacture higher probability.

All 14 screenshot offers were compared by exact fixture, market, selection and line against the published SQLite. 13 reject; KuPS–Trabzonspor second-half total Under 1.5 @1.833 remains: raw 0.6943 and FT-Trabzonspor-winner support 0.614939. Detailed numerical replay: favourite_v3_screenshot_review.json. Replay applies the reviewed policy to actual published rows; it is not a running Android UI test.

Validation: 11 Python contracts; parser checks for all four reviewed UAT Kotlin/Gradle files; compilation and execution of the actual new Kotlin policy against minimal domain bindings, 8 contracts; SQLite quick_check; 36395 forecasts / 449 fixtures; old fixture table (1815 rows) and old market table (45289 rows) unchanged. No full Android application build, emulator or APK was run. No new football/bookmaker provider API requests.

User pull commands, always:
```powershell
cd C:\Users\gvell\StudioProjects\StatMaker-UAT
git fetch origin
git switch uat
git pull --ff-only origin uat
```
Run in Android Studio, then Update All, then Find Bets.
