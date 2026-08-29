#!/usr/bin/env python3
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

MAIN_RAW = "https://raw.githubusercontent.com/Velliouras/StatMaker-Data/main"
UEFA_RAW = "https://raw.githubusercontent.com/Velliouras/StatMaker-Data/build/uefa-qualifier-feed-20260720"
LOCAL_MAIN = "http://127.0.0.1:8765"
LOCAL_UEFA = "http://127.0.0.1:8765/__uefa__"


def replace_repository_urls() -> None:
    root = Path("app/src/main/java/com/statmaker/app")
    main_replacements = 0
    uefa_replacements = 0
    for source in root.glob("*.kt"):
        text = source.read_text(encoding="utf-8")
        updated = text.replace(UEFA_RAW, LOCAL_UEFA)
        uefa_replacements += text.count(UEFA_RAW)
        main_replacements += updated.count(MAIN_RAW)
        updated = updated.replace(MAIN_RAW, LOCAL_MAIN)
        if updated != text:
            source.write_text(updated, encoding="utf-8")
    if main_replacements == 0:
        raise SystemExit("Expected at least one StatMaker-Data main URL in producer sources")
    if uefa_replacements == 0:
        raise SystemExit("Expected at least one StatMaker-Data UEFA URL in producer sources")
    print(f"APP_READY_LOCAL_URLS_OK main={main_replacements} uefa={uefa_replacements}")


def tune_runner_manifest() -> None:
    manifest = Path("app/src/main/AndroidManifest.xml")
    text = manifest.read_text(encoding="utf-8")
    needle = '        android:allowBackup="true"\n'
    additions = []
    if 'android:usesCleartextTraffic="true"' not in text:
        additions.append('        android:usesCleartextTraffic="true"\n')
    if 'android:largeHeap="true"' not in text:
        additions.append('        android:largeHeap="true"\n')
    if additions:
        if text.count(needle) != 1:
            raise SystemExit("Could not locate Android application allowBackup attribute")
        text = text.replace(needle, needle + "".join(additions), 1)
        manifest.write_text(text, encoding="utf-8")
    print("APP_READY_RUNNER_MANIFEST_OK")



def register_recommendation_publisher_activity() -> None:
    manifest = Path("app/src/main/AndroidManifest.xml")
    text = manifest.read_text(encoding="utf-8")
    if 'android:name=".AppReadyPatternPublisherActivity"' in text:
        print("APP_READY_PATTERN_ACTIVITY_OK already-registered")
        return

    marker = '''        <activity
            android:name=".StatMakerWelcomeActivity"
'''
    addition = '''        <activity
            android:name=".AppReadyPatternPublisherActivity"
            android:exported="true"
            android:noHistory="true"
            android:theme="@android:style/Theme.NoDisplay" />

        <activity
            android:name=".StatMakerWelcomeActivity"
'''
    if text.count(marker) != 1:
        raise SystemExit("Could not locate Welcome activity manifest marker")
    manifest.write_text(text.replace(marker, addition, 1), encoding="utf-8")
    print("APP_READY_PATTERN_ACTIVITY_OK registered")



def bundle_normalized_snapshot() -> None:
    workspace = Path(os.environ["GITHUB_WORKSPACE"])
    source = workspace / "data/api_football/domestic_normalized_fixture_stats.json"
    builder = workspace / "scripts/build_domestic_normalized_snapshot.py"
    target = Path("app/src/main/assets/app_ready/domestic_normalized_stats_v2.bin")
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, str(builder), str(source), str(target)], check=True)
    if not target.is_file() or target.stat().st_size <= 12:
        raise SystemExit("Prebuilt Domestic normalized snapshot is missing/empty")
    print(f"APP_READY_NORMALIZED_ASSET_OK bytes={target.stat().st_size}")


def patch_normalized_repository() -> None:
    source = Path("app/src/main/java/com/statmaker/app/DomesticNormalizedStatsRepository.kt")
    text = source.read_text(encoding="utf-8")
    marker = '''        val prefs = context.applicationContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        val storedHash = prefs.getString(HASH_KEY, "").orEmpty()
'''
    replacement = '''        val prefs = context.applicationContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        if (!target.isFile || target.length() <= 12L) {
            target.parentFile?.mkdirs()
            context.applicationContext.assets.open("app_ready/domestic_normalized_stats_v2.bin").use { input ->
                target.outputStream().buffered().use { output -> input.copyTo(output) }
            }
            check(prefs.edit().putString(HASH_KEY, expectedHash).commit()) {
                "Could not persist prebuilt Domestic normalized stats hash"
            }
        }
        val storedHash = prefs.getString(HASH_KEY, "").orEmpty()
'''
    if text.count(marker) != 1:
        raise SystemExit("Could not locate normalized-stats preferences block")
    text = text.replace(marker, replacement, 1)
    old_fast_path = '''        if (!force && target.isFile && target.length() > 12L &&
            (expectedHash.isBlank() || expectedHash == storedHash)
'''
    new_fast_path = '''        if (target.isFile && target.length() > 12L &&
            (expectedHash.isBlank() || expectedHash == storedHash)
'''
    if text.count(old_fast_path) != 1:
        raise SystemExit("Could not locate normalized-stats fast path")
    text = text.replace(old_fast_path, new_fast_path, 1)
    source.write_text(text, encoding="utf-8")
    print("APP_READY_NORMALIZED_REPOSITORY_OK")


def harden_download(path: str, label: str) -> None:
    source = Path(path)
    text = source.read_text(encoding="utf-8")
    pattern = re.compile(
        r"    private fun downloadText\(urlString: String\): String \{.*?^    \}\n",
        re.MULTILINE | re.DOTALL,
    )
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        raise SystemExit(f"Expected exactly one downloadText block in {source}; found {len(matches)}")
    replacement = '''    private fun downloadText(urlString: String): String {
        var lastFailure: Throwable? = null
        repeat(3) { attempt ->
            val connection = (URL(urlString).openConnection() as HttpURLConnection).apply {
                connectTimeout = 30000
                readTimeout = 60000
                requestMethod = "GET"
                useCaches = false
                setRequestProperty("User-Agent", "StatMaker AppReady Publisher")
                setRequestProperty("Connection", "close")
            }
            try {
                val code = connection.responseCode
                if (code !in 200..299) throw IllegalStateException("HTTP $code while downloading __LABEL__")
                return BufferedReader(InputStreamReader(connection.inputStream)).use { it.readText() }
            } catch (error: Throwable) {
                lastFailure = error
                if (attempt < 2) Thread.sleep(2000L * (attempt + 1))
            } finally {
                connection.disconnect()
            }
        }
        throw IllegalStateException("Failed to download __LABEL__ after 3 attempts: $urlString", lastFailure)
    }
'''.replace("__LABEL__", label)
    source.write_text(pattern.sub(replacement, text, count=1), encoding="utf-8")
    print(f"APP_READY_PRODUCER_PATCH_OK {source}")


def patch_domestic_multi_season_index() -> None:
    source = Path("app/src/main/java/com/statmaker/app/DomesticApiRegistry.kt")
    text = source.read_text(encoding="utf-8")
    old = '''        val duplicateCodes = index.leagues
            .groupBy { it.leagueCode }
            .filterValues { it.size > 1 }
            .keys
            .sorted()
        if (duplicateCodes.isNotEmpty()) {
            errors += "Duplicate Domestic league codes: ${duplicateCodes.joinToString(", ")}"
        }
'''
    new = '''        val duplicateScopes = index.leagues
            .groupBy { "${it.leagueCode}|${it.appSeason}" }
            .filterValues { it.size > 1 }
            .keys
            .sorted()
        if (duplicateScopes.isNotEmpty()) {
            errors += "Duplicate Domestic league code+season rows: ${duplicateScopes.joinToString(", ")}"
        }
'''
    if text.count(old) == 1:
        source.write_text(text.replace(old, new, 1), encoding="utf-8")
        print("APP_READY_MULTI_SEASON_DOMESTIC_INDEX_OK patched")
        return
    if "Duplicate Domestic league code+season rows:" in text:
        print("APP_READY_MULTI_SEASON_DOMESTIC_INDEX_OK source-already-multi-season")
        return
    raise SystemExit("Could not locate a supported Domestic registry duplicate validation contract")


def patch_empty_uefa_ready_snapshots() -> None:
    source = Path("app/src/main/java/com/statmaker/app/PreparedBettingSnapshotCoordinator.kt")
    text = source.read_text(encoding="utf-8")
    old = '                val availableFeed = feed?.takeIf { it.matches.isNotEmpty() } ?: return\n'
    new = '''                // Production requires a READY snapshot for every UEFA competition.
                // An empty canonical feed is still a valid immutable 0/0 snapshot.
                val availableFeed = feed ?: return
'''
    if text.count(old) != 1:
        raise SystemExit("Could not locate UEFA empty-feed skip")
    source.write_text(text.replace(old, new, 1), encoding="utf-8")
    print("APP_READY_EMPTY_UEFA_READY_OK")



def install_prepared_pattern_bridge() -> None:
    workspace = Path(os.environ["GITHUB_WORKSPACE"])
    source = workspace / "scripts/app_ready_v10/AppReadyPatternPublisherBridge.kt"
    target = Path("app/src/main/java/com/statmaker/app/AppReadyPatternPublisherBridge.kt")
    if not source.is_file() or source.stat().st_size <= 0:
        raise SystemExit(f"Missing prepared recommendation bridge: {source}")
    shutil.copy2(source, target)
    print(f"APP_READY_PATTERN_BRIDGE_OK bytes={target.stat().st_size}")


def patch_prepared_store_v10() -> None:
    source = Path("app/src/main/java/com/statmaker/app/PreparedBettingSnapshotStore.kt")
    text = source.read_text(encoding="utf-8")

    if "AppReadyPatternSchema.create(db)" not in text:
        on_create_marker = '''        db.execSQL(
            "CREATE INDEX idx_prepared_selections_builder ON prepared_selections(competition_id, snapshot_version, qualifies_builder, local_date, match_key)"
        )
    }
'''
        on_create_replacement = '''        db.execSQL(
            "CREATE INDEX idx_prepared_selections_builder ON prepared_selections(competition_id, snapshot_version, qualifies_builder, local_date, match_key)"
        )
        AppReadyPatternSchema.create(db)
    }
'''
        if text.count(on_create_marker) != 1:
            raise SystemExit("Could not locate PreparedBettingSnapshotStore onCreate tail")
        text = text.replace(on_create_marker, on_create_replacement, 1)

        upgrade_marker = '''        if (oldVersion < 8) {
            db.execSQL("ALTER TABLE prepared_selections ADD COLUMN evidence_home_outcomes_bits TEXT")
            db.execSQL("ALTER TABLE prepared_selections ADD COLUMN evidence_away_outcomes_bits TEXT")
        }
        createPerformanceIndexes(db)
'''
        upgrade_replacement = '''        if (oldVersion < 8) {
            db.execSQL("ALTER TABLE prepared_selections ADD COLUMN evidence_home_outcomes_bits TEXT")
            db.execSQL("ALTER TABLE prepared_selections ADD COLUMN evidence_away_outcomes_bits TEXT")
        }
        if (oldVersion < APP_READY_PATTERN_SCHEMA_VERSION) {
            AppReadyPatternSchema.create(db)
        }
        createPerformanceIndexes(db)
'''
        if text.count(upgrade_marker) != 1:
            raise SystemExit("Could not locate PreparedBettingSnapshotStore upgrade tail")
        text = text.replace(upgrade_marker, upgrade_replacement, 1)

    old_version = "        private const val DATABASE_VERSION = 8"
    new_version = "        private const val DATABASE_VERSION = APP_READY_PATTERN_SCHEMA_VERSION"
    if old_version in text:
        text = text.replace(old_version, new_version, 1)
    elif new_version not in text:
        raise SystemExit("Could not locate PreparedBettingSnapshotStore database version")

    publisher_load_marker = '''    /**
     * Returns null only when the requested immutable snapshot is unavailable. An empty but ready
     * snapshot returns a non-null result with an empty selections list.
     */
    fun loadForFeed(
'''
    publisher_load_method = '''    /**
     * Publisher-only full PATTERN read that avoids prepared_snapshot_meta.catalog_payload.
     *
     * The compact catalogue can exceed Android CursorWindow limits after league expansion.
     * Final recommendation materialization only needs the persisted prepared match rows plus
     * PATTERN selections, so hydrate those rows directly and never read the giant catalogue blob.
     */
    fun loadAllPatternSelectionsForPublisher(
        competitionId: String,
        snapshotVersion: String
    ): PreparedSnapshotLoadResult? {
        if (!hasReadySnapshot(competitionId, snapshotVersion)) return null

        val requestedMatches = readableDatabase.rawQuery(
            """
            SELECT match_key, payload
            FROM prepared_matches
            WHERE competition_id=? AND snapshot_version=?
            ORDER BY local_date, match_key
            """.trimIndent(),
            arrayOf(competitionId, snapshotVersion)
        ).use { cursor ->
            buildMap<String, OddsMatch> {
                while (cursor.moveToNext()) {
                    val matchKey = cursor.getString(0)
                    val match = runCatching {
                        JSONObject(cursor.getString(1)).toPreparedOddsMatch()
                    }.getOrNull() ?: continue
                    put(matchKey, match)
                }
            }
        }

        if (requestedMatches.isEmpty()) {
            return PreparedSnapshotLoadResult(
                selections = emptyList(),
                selectionCount = 0,
                snapshotVersion = snapshotVersion
            )
        }

        val dates = requestedMatches.values
            .asSequence()
            .map(::bettingLocalDate)
            .filter(String::isNotBlank)
            .toSet()

        val selections = loadSelections(
            competitionId = competitionId,
            snapshotVersion = snapshotVersion,
            dates = dates,
            requestedMatches = requestedMatches,
            purpose = PreparedSelectionPurpose.PATTERN
        )

        return PreparedSnapshotLoadResult(
            selections = selections,
            selectionCount = selections.size,
            snapshotVersion = snapshotVersion
        )
    }

    /**
     * Returns null only when the requested immutable snapshot is unavailable. An empty but ready
     * snapshot returns a non-null result with an empty selections list.
     */
    fun loadForFeed(
'''
    if publisher_load_method not in text:
        if text.count(publisher_load_marker) != 1:
            raise SystemExit("Could not locate PreparedBettingSnapshotStore loadForFeed marker")
        text = text.replace(publisher_load_marker, publisher_load_method, 1)

    source.write_text(text, encoding="utf-8")
    print("APP_READY_PREPARED_STORE_V10_OK")



def add_producer_diagnostics() -> None:
    source = Path("app/src/main/java/com/statmaker/app/WelcomeDataUpdater.kt")
    text = source.read_text(encoding="utf-8")
    if "import android.util.Log" not in text:
        text = text.replace("import android.content.Context\n", "import android.content.Context\nimport android.util.Log\n", 1)
    old = '                result.error?.let { warnings += "${result.label}: ${it.message.orEmpty()}" }\n'
    new = '''                result.error?.let {
                    Log.e("StatMakerAppReady", "${result.label}: ${it.message.orEmpty()}", it)
                    warnings += "${result.label}: ${it.message.orEmpty()}"
                }
'''
    if text.count(old) != 1:
        raise SystemExit("Could not locate Welcome task error handler")
    text = text.replace(old, new, 1)
    replacements = [
        ("            val domestic = resolve(domesticFuture)\n", "            val domestic = resolve(domesticFuture)\n" + '            Log.i("StatMakerAppReady", "stage=domestic_resolved matches=${db.totalMatchCount()} ok=${domestic != null}")\n'),
        ("            val normalizedResult = resolve(normalizedFuture)\n", "            val normalizedResult = resolve(normalizedFuture)\n" + '            Log.i("StatMakerAppReady", "stage=normalized_resolved refreshed=${normalizedResult?.refreshed == true}")\n'),
        ("            val domesticOdds = resolve(domesticOddsFuture)\n", "            val domesticOdds = resolve(domesticOddsFuture)\n" + '            Log.i("StatMakerAppReady", "stage=domestic_odds_resolved matches=${domesticOdds?.matches?.size ?: 0}")\n'),
        ("            val champions = resolve(championsFuture)\n", "            val champions = resolve(championsFuture)\n" + '            Log.i("StatMakerAppReady", "stage=champions_odds_resolved matches=${champions?.matches?.size ?: 0}")\n'),
        ("            val europa = resolve(europaFuture)\n", "            val europa = resolve(europaFuture)\n" + '            Log.i("StatMakerAppReady", "stage=europa_odds_resolved matches=${europa?.matches?.size ?: 0}")\n'),
        ("            val conference = resolve(conferenceFuture)\n", "            val conference = resolve(conferenceFuture)\n" + '            Log.i("StatMakerAppReady", "stage=conference_odds_resolved matches=${conference?.matches?.size ?: 0}")\n'),
        ("            val supportResults = supportFutures.map(::resolve)\n", "            val supportResults = supportFutures.map(::resolve)\n" + '            Log.i("StatMakerAppReady", "stage=support_resolved ready=${supportResults.count { it != null }}")\n'),
        ("            val logosUpdated = resolve(logosFuture) == true\n", "            val logosUpdated = resolve(logosFuture) == true\n" + '            Log.i("StatMakerAppReady", "stage=all_futures_resolved")\n'),
        ('            val prepared = trace.measure("prepared_coordinator") {\n', '            Log.i("StatMakerAppReady", "stage=prepared_begin")\n            val prepared = trace.measure("prepared_coordinator") {\n'),
        ("            warnings += prepared.warnings\n", '''            Log.i("StatMakerAppReady", "stage=prepared_complete ready=${prepared.readyCompetitions.size} requested=${prepared.requestedCompetitions.size}")
            check(prepared.requestedCompetitions.size == 4 && prepared.readyCompetitions.size == 4) {
                "App-ready publisher requires 4/4 prepared source snapshots"
            }
            Log.i("StatMakerAppReady", "stage=recommendations_begin")
            val recommendationReport = trace.measure("prepared_recommendations") {
                AppReadyPatternPublisher.publish(appContext, db)
            }
            Log.i(
                "StatMakerAppReady",
                "stage=recommendations_complete generation=${recommendationReport.generationId} " +
                    "candidates=${recommendationReport.candidateCount} reused=${recommendationReport.reused} " +
                    "elapsedMs=${recommendationReport.elapsedMs}"
            )
            warnings += prepared.warnings
'''),
    ]
    for old_marker, new_marker in replacements:
        if text.count(old_marker) != 1:
            raise SystemExit(f"Could not locate producer diagnostic marker: {old_marker.strip()}")
        text = text.replace(old_marker, new_marker, 1)
    source.write_text(text, encoding="utf-8")
    print("APP_READY_DIAGNOSTICS_OK")


replace_repository_urls()
tune_runner_manifest()
register_recommendation_publisher_activity()
bundle_normalized_snapshot()
patch_normalized_repository()
harden_download("app/src/main/java/com/statmaker/app/DomesticApiArtifactImporter.kt", "Domestic API artifact")
harden_download("app/src/main/java/com/statmaker/app/DomesticApiRegistry.kt", "Domestic API registry")
patch_domestic_multi_season_index()
patch_empty_uefa_ready_snapshots()
install_prepared_pattern_bridge()
patch_prepared_store_v10()
add_producer_diagnostics()
