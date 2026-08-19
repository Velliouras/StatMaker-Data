#!/usr/bin/env python3
import os
import subprocess
import sys
from pathlib import Path

SOURCE = Path("app/src/main/java/com/statmaker/app/PreparedBettingSnapshotCoordinator.kt")
WELCOME_SOURCE = Path("app/src/main/java/com/statmaker/app/WelcomeDataUpdater.kt")
GRADLE_PROPERTIES = Path("gradle.properties")


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
    compatible_signature = '''    @Suppress("UNUSED_PARAMETER")
    fun refreshAll(
        context: Context,
        onProgress: (percent: Int, label: String) -> Unit,
        onBackgroundPrefetch: (AppReadyPrefetchResult) -> Unit
    ): WelcomeUpdateReport = refreshAll(
        context = context,
        onProgress = onProgress
    )

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
    print("APP_READY_WELCOME_CONTRACT_OK legacy-adapter")


def main() -> None:
    configure_compiler_memory()
    patch_legacy_welcome_contract()

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

    SOURCE.write_text(text, encoding="utf-8")
    print("APP_READY_PREPARED_DIAGNOSTICS_OK")

    bulk_patch = Path(os.environ["GITHUB_WORKSPACE"]) / "scripts/patch_prepared_publisher_bulk.py"
    subprocess.run([sys.executable, str(bulk_patch)], check=True)


if __name__ == "__main__":
    main()
