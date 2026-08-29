#!/usr/bin/env python3
import os
import subprocess
import sys
from pathlib import Path

SOURCE = Path("app/src/main/java/com/statmaker/app/PreparedBettingSnapshotCoordinator.kt")
WELCOME_SOURCE = Path("app/src/main/java/com/statmaker/app/WelcomeDataUpdater.kt")
GRADLE_PROPERTIES = Path("gradle.properties")
PATTERN_MATCHER_SOURCE = Path("app/src/main/java/com/statmaker/app/PatternOddsMatcher.kt")


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"Could not locate {label}; found {count} matches")
    return text.replace(old, new, 1)


def configure_compiler_memory() -> None:
    managed = {
        "org.gradle.jvmargs": "-Xmx4g -XX:MaxMetaspaceSize=1g -Dfile.encoding=UTF-8",
        "kotlin.compiler.execution.strategy": "in-process",
        "org.gradle.workers.max": "1",
        "kotlin.incremental": "false",
    }
    lines = GRADLE_PROPERTIES.read_text(encoding="utf-8").splitlines() if GRADLE_PROPERTIES.is_file() else []
    keys = set(managed)
    kept = [
        line for line in lines
        if not any(line.lstrip().startswith(f"{key}=") for key in keys)
    ]
    if kept and kept[-1].strip():
        kept.append("")
    kept.append("# App-ready publisher compile guardrails (staged checkout only)")
    kept.extend(f"{key}={value}" for key, value in managed.items())
    GRADLE_PROPERTIES.write_text("\n".join(kept) + "\n", encoding="utf-8")
    print("APP_READY_GRADLE_MEMORY_OK heap=4g workers=1 kotlin=in-process incremental=false")


def patch_legacy_welcome_contract() -> None:
    text = WELCOME_SOURCE.read_text(encoding="utf-8")
    callback_contract = "onBackgroundPrefetch: (AppReadyPrefetchResult) -> Unit"
    if callback_contract in text:
        print("APP_READY_WELCOME_CONTRACT_OK current")
        return

    legacy_signature = '''    fun refreshAll(
        context: Context,
        onProgress: (percent: Int, label: String) -> Unit
    ): WelcomeUpdateReport {
'''
    compatible_signature = '''    private var appReadyPublisherResult: WelcomeUpdateReport? = null

    @Suppress("UNUSED_PARAMETER")
    @Synchronized
    fun refreshAll(
        context: Context,
        onProgress: (percent: Int, label: String) -> Unit,
        onBackgroundPrefetch: (AppReadyPrefetchResult) -> Unit
    ): WelcomeUpdateReport {
        appReadyPublisherResult?.let { return it }
        return refreshAll(
            context = context,
            onProgress = onProgress
        ).also { appReadyPublisherResult = it }
    }

    @Synchronized
    fun refreshAll(
        context: Context,
        onProgress: (percent: Int, label: String) -> Unit
    ): WelcomeUpdateReport {
'''
    text = replace_once(
        text,
        legacy_signature,
        compatible_signature,
        "legacy Welcome refreshAll contract",
    )
    WELCOME_SOURCE.write_text(text, encoding="utf-8")
    print("APP_READY_WELCOME_CONTRACT_OK legacy-adapter single-flight")



def patch_pattern_matcher_regex_reuse() -> None:
    text = PATTERN_MATCHER_SOURCE.read_text(encoding="utf-8")

    old_body = '''        text = Normalizer.normalize(text, Normalizer.Form.NFD).replace("\\\\p{Mn}+".toRegex(), "")
        text = text.replace("oe", "o").replace("aa", "a")
        return text.replace("[^a-z0-9]+".toRegex(), " ").trim().replace("\\\\s+".toRegex(), " ")
'''
    new_body = '''        text = Normalizer.normalize(text, Normalizer.Form.NFD).replace(PUBLISHER_COMBINING_MARKS_REGEX, "")
        text = text.replace("oe", "o").replace("aa", "a")
        return text.replace(PUBLISHER_NON_ALNUM_REGEX, " ").trim().replace(PUBLISHER_WHITESPACE_REGEX, " ")
'''
    if old_body in text:
        text = text.replace(old_body, new_body, 1)
    elif "PUBLISHER_COMBINING_MARKS_REGEX" not in text:
        raise SystemExit("Could not locate PatternOddsMatcher normalizeTeamName regex block")

    class_marker = "class PatternOddsMatcher("
    class_index = text.find(class_marker)
    if class_index < 0:
        raise SystemExit("Could not locate PatternOddsMatcher class")

    body_index = text.find("{", class_index)
    if body_index < 0:
        raise SystemExit("Could not locate PatternOddsMatcher class body")

    if "private val PUBLISHER_COMBINING_MARKS_REGEX" not in text:
        constants = '''
    // Publisher-only staged optimization. These regexes were previously compiled on every
    // normalizeTeamName call. Under parallel Domestic generation Android ICU eventually failed
    // native allocation (U_MEMORY_ALLOCATION_ERROR). Reusing compiled Regex objects preserves
    // exact normalization semantics while removing the allocation storm.
    private val PUBLISHER_COMBINING_MARKS_REGEX = Regex("\\\\p{Mn}+")
    private val PUBLISHER_NON_ALNUM_REGEX = Regex("[^a-z0-9]+")
    private val PUBLISHER_WHITESPACE_REGEX = Regex("\\\\s+")

'''
        text = text[:body_index + 1] + constants + text[body_index + 1:]

    PATTERN_MATCHER_SOURCE.write_text(text, encoding="utf-8")
    print("APP_READY_PATTERN_REGEX_REUSE_OK")


def limit_publisher_domestic_parallelism() -> None:
    text = SOURCE.read_text(encoding="utf-8")
    old = "    private const val MAX_DOMESTIC_WORKERS = 4"
    new = "    private const val MAX_DOMESTIC_WORKERS = 2"
    if old in text:
        text = text.replace(old, new, 1)
    elif new not in text:
        raise SystemExit("Could not locate MAX_DOMESTIC_WORKERS")
    SOURCE.write_text(text, encoding="utf-8")
    print("APP_READY_DOMESTIC_WORKERS_OK workers=2")



def main() -> None:
    configure_compiler_memory()
    patch_legacy_welcome_contract()
    patch_pattern_matcher_regex_reuse()
    limit_publisher_domestic_parallelism()

    text = SOURCE.read_text(encoding="utf-8")

    if "import android.util.Log" not in text:
        text = replace_once(
            text,
            "import android.content.Context\n",
            "import android.content.Context\nimport android.util.Log\n",
            "PreparedBetting Log import",
        )

    text = replace_once(
        text,
        "            requested += competitionId\n",
        "            requested += competitionId\n"
        '            Log.i("StatMakerAppReady", "stage=prepared_${competitionId}_check matches=${warmFeed.matches.size}")\n',
        "prepared competition start",
    )

    can_patch = '''                val canPatch = !fullSourceChanged && previousVersion != null && previousCatalog != null &&
                    previousFeed != null
'''
    text = replace_once(
        text,
        can_patch,
        can_patch
        + '                Log.i("StatMakerAppReady", "stage=prepared_${competitionId}_${if (canPatch) "patch" else "full"}_begin")\n',
        "prepared mode marker",
    )

    old_full = '''                } else {
                    store.replaceSnapshot(
                        competitionId = competitionId,
                        snapshotVersion = version,
                        catalogFeed = warmFeed,
                        selections = buildAll(warmFeed)
                    )
                }
'''
    new_full = '''                } else {
                    Log.i("StatMakerAppReady", "stage=prepared_${competitionId}_build_begin matches=${warmFeed.matches.size}")
                    val builtSelections = buildAll(warmFeed)
                    Log.i("StatMakerAppReady", "stage=prepared_${competitionId}_build_complete selections=${builtSelections.size}")
                    Log.i("StatMakerAppReady", "stage=prepared_${competitionId}_write_begin")
                    val replaced = store.replaceSnapshot(
                        competitionId = competitionId,
                        snapshotVersion = version,
                        catalogFeed = warmFeed,
                        selections = builtSelections
                    )
                    Log.i("StatMakerAppReady", "stage=prepared_${competitionId}_write_complete selections=${replaced.selectionCount}")
                    replaced
                }
'''
    text = replace_once(text, old_full, new_full, "full prepared snapshot build")

    text = replace_once(
        text,
        "                rebuilt += competitionId\n                counts[competitionId] = write.selectionCount\n",
        "                rebuilt += competitionId\n"
        "                counts[competitionId] = write.selectionCount\n"
        '                Log.i("StatMakerAppReady", "stage=prepared_${competitionId}_complete selections=${write.selectionCount}")\n',
        "prepared completion marker",
    )

    text = replace_once(
        text,
        '''            }.onFailure { error ->
                warnings += "$competitionId: ${error.message.orEmpty()}"
            }
''',
        '''            }.onFailure { error ->
                Log.e("StatMakerAppReady", "stage=prepared_${competitionId}_failed ${error.message.orEmpty()}", error)
                warnings += "$competitionId: ${error.message.orEmpty()}"
            }
''',
        "prepared failure marker",
    )

    # Publisher-only progress heartbeat around the expensive Domestic matcher. This
    # does not change matcher/engine semantics; it only exposes real progress so the
    # emulator watchdog can distinguish a slow healthy build from a stalled one.
    text = replace_once(
        text,
        '''        if (leagueFeeds.isEmpty()) return emptyList()

        val workerCount = minOf(
''',
        '''        if (leagueFeeds.isEmpty()) return emptyList()

        fun buildPublisherLeague(
            source: LeagueSource,
            leagueFeed: OddsFeed
        ): List<PatternBackedSelection> {
            val startedAt = System.nanoTime()
            Log.i(
                "StatMakerAppReady",
                "stage=prepared_domestic_league_${source.code}_begin " +
                    "matches=${leagueFeed.matches.size} " +
                    "markets=${leagueFeed.matches.sumOf { it.markets.size }}"
            )
            return matcher.findPatternBackedSelections(
                league = source,
                oddsFeed = leagueFeed,
                selectedFilters = "prepared-snapshot"
            ).also { selections ->
                val elapsedMs = (System.nanoTime() - startedAt) / 1_000_000L
                Log.i(
                    "StatMakerAppReady",
                    "stage=prepared_domestic_league_${source.code}_complete " +
                        "selections=${selections.size} elapsedMs=$elapsedMs"
                )
            }
        }

        val workerCount = minOf(
''',
        "prepared Domestic per-league heartbeat helper",
    )

    text = replace_once(
        text,
        '''        if (workerCount <= 1) {
            return leagueFeeds.flatMap { (source, leagueFeed) ->
                matcher.findPatternBackedSelections(
                    league = source,
                    oddsFeed = leagueFeed,
                    selectedFilters = "prepared-snapshot"
                )
            }
        }
''',
        '''        if (workerCount <= 1) {
            return leagueFeeds.flatMap { (source, leagueFeed) ->
                buildPublisherLeague(source, leagueFeed)
            }
        }
''',
        "prepared Domestic sequential heartbeat",
    )

    text = replace_once(
        text,
        '''            leagueFeeds.map { (source, leagueFeed) ->
                executor.submit(Callable {
                    matcher.findPatternBackedSelections(
                        league = source,
                        oddsFeed = leagueFeed,
                        selectedFilters = "prepared-snapshot"
                    )
                })
            }.flatMap { future -> future.get() }
''',
        '''            leagueFeeds.map { (source, leagueFeed) ->
                executor.submit(Callable {
                    buildPublisherLeague(source, leagueFeed)
                })
            }.flatMap { future -> future.get() }
''',
        "prepared Domestic parallel heartbeat",
    )

    SOURCE.write_text(text, encoding="utf-8")
    print("APP_READY_PREPARED_DIAGNOSTICS_OK")

    bulk_patch = Path(os.environ["GITHUB_WORKSPACE"]) / "scripts/patch_prepared_publisher_bulk.py"
    subprocess.run([sys.executable, str(bulk_patch)], check=True)


if __name__ == "__main__":
    main()
