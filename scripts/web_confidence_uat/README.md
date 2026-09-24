# Web confidence export — isolated UAT

Status: exporter and exact Android classifier validated; real device parity is NOT yet verified.
No production feed, engine, Android source, workflow, schema migration or Site is changed.

## Why a device capture is required

Android classifies ALL locally settled canonical Model Performance entries. The repository ledger
contains recommendation-time context only (30-day retention), while live results retain 14 days.
Reconstructing a shorter history would change HIGH hysteresis. This tool reads the existing Android
performance SQLite database without regenerating outcomes. It compiles the pinned Android classifier,
context function, default-history predicate and retirement policy directly from the private source
checkout. The production formulas and thresholds are not reimplemented.

The eight existing Android confidence tests run on every build. Input source and dependency hashes
are locked. Missing prepared opponent context is reported as unavailable, never guessed.

## Capture on the PC connected to the Android phone

Prerequisites: Python 3.9+, Android SDK platform-tools/adb, USB debugging authorized, a debuggable
StatMaker installation. No root, APK replacement, schema change, data clearing or Android code edit.
The explicit `--stop-app` option closes StatMaker so both databases can be captured coherently;
reopen it normally afterwards. WAL recovery happens only on copies on the PC.

1. Update the Android data and let Model Performance finish settling. Note the displayed tiers and date.
2. From this repository branch, run:

```sh
python scripts/web_confidence_uat/collect_device.py --stop-app --output confidence-capture
```

Use `--adb "PATH/TO/adb"` if adb is not on PATH; use `--serial SERIAL` if multiple devices are connected.
Only the performance and prepared betting databases are copied. Do not commit captures, output JSON,
private Android source or compiled artifacts to this Data repository. Captures are review inputs.
If run-as is denied, collection stops; it never attempts root or modifies app permissions.

## Export locally

Needs Java 17 on PATH and an existing StatMaker source checkout at the commit in source-lock.json.
Download the matching production App-Ready manifest into `confidence-capture/update_manifest.json`.
The exporter rejects a manifest whose generation is absent or not ready in the captured prepared DB.
Use the Athens date on which the Android tiers were observed (do not compare across midnight).

```sh
python scripts/web_confidence_uat/export_confidence.py \
  --performance-db confidence-capture/statmaker_model_performance.db \
  --prepared-db confidence-capture/statmaker_prepared_betting.db \
  --manifest confidence-capture/update_manifest.json \
  --android-source ../StatMaker \
  --as-of-date 2026-09-24 \
  --output confidence-review \
  --build-dir /tmp/statmaker-confidence-build \
  --dependencies /tmp/statmaker-confidence-deps
```

On Windows use one line and writable Windows paths for build-dir/dependencies. The first build downloads
hash-locked Kotlin compiler/JUnit jars from Maven Central. Later builds reuse them. No Android SDK build
or emulator is needed. No Odds API or API-Football requests are made.

## Output contract

- `settled-history.json`: exact canonical history rows consumed (including pending/void rows, which the
  unchanged classifier filters), input fingerprints, date, generation and classifier source identity.
- `confidence.json`: one annotation per eligible prepared pick, keyed by competition_id + snapshot_version
  + selection_key. Classified rows include tier, score, support, positiveSignals and negativeSignals.
  Unavailable rows have a reason and no tier.
- `productionReady` is always false; `parityStatus` explicitly requires device comparison.

No automatic publishing is performed. No output is written under production or UAT App-Ready paths.
The review output should remain private.

## Parity gate before Web activation

Compare the same captured device, Athens date, generation and exact selections against Android.
The Kotlin classifier is unchanged, but prepared evidence may not cover Android's additional runtime
opponent context; those selections are explicitly unavailable. A real-device comparison is needed
before activating these annotations. Do not relabel unavailable entries as DEVELOPING.

The future Web reader must reject mismatched generation/contentVersion/asOfDate and join on all three
identity fields. It must never change recommendation eligibility, prices, markets or default filters.
A repeatable shared publication contract requires a separate design after this UAT parity proof; a
single phone capture is a test input, not a continuously updating production confidence source.

## Validation

```sh
python -m unittest discover -s tests -p test_web_confidence_export.py -v
python scripts/web_confidence_uat/build_classifier.py --android-source ../StatMaker --output /tmp/statmaker-confidence-build --dependencies /tmp/statmaker-confidence-deps
```

Validated: 9 export tests, 8 unchanged Android confidence tests, and a full synthetic SQLite → Kotlin →
JSON export. Synthetic output was temporary, not committed or deployed. Real device capture has not
been executed in the remote workspace because no phone is connected.
