#!/usr/bin/env python3
import re
from pathlib import Path

MAIN_RAW = "https://raw.githubusercontent.com/Velliouras/StatMaker-Data/main"
UEFA_RAW = "https://raw.githubusercontent.com/Velliouras/StatMaker-Data/build/uefa-qualifier-feed-20260720"
LOCAL_MAIN = "http://10.0.2.2:8765"
LOCAL_UEFA = "http://10.0.2.2:8765/__uefa__"


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
    print(
        f"APP_READY_LOCAL_URLS_OK main={main_replacements} uefa={uefa_replacements}"
    )


def allow_runner_cleartext() -> None:
    manifest = Path("app/src/main/AndroidManifest.xml")
    text = manifest.read_text(encoding="utf-8")
    if 'android:usesCleartextTraffic="true"' not in text:
        needle = '        android:allowBackup="true"\n'
        if text.count(needle) != 1:
            raise SystemExit("Could not locate Android application allowBackup attribute")
        text = text.replace(
            needle,
            needle + '        android:usesCleartextTraffic="true"\n',
            1,
        )
        manifest.write_text(text, encoding="utf-8")
    print("APP_READY_CLEARTEXT_OK")


def harden_download(path: str, label: str) -> None:
    source = Path(path)
    text = source.read_text(encoding="utf-8")
    pattern = re.compile(
        r"    private fun downloadText\(urlString: String\): String \{.*?^    \}\n",
        re.MULTILINE | re.DOTALL,
    )
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        raise SystemExit(
            f"Expected exactly one downloadText block in {source}; found {len(matches)}"
        )

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


def add_producer_diagnostics() -> None:
    source = Path("app/src/main/java/com/statmaker/app/WelcomeDataUpdater.kt")
    text = source.read_text(encoding="utf-8")
    if "import android.util.Log" not in text:
        text = text.replace(
            "import android.content.Context\n",
            "import android.content.Context\nimport android.util.Log\n",
            1,
        )
    old = '                result.error?.let { warnings += "${result.label}: ${it.message.orEmpty()}" }\n'
    new = '''                result.error?.let {
                    Log.e("StatMakerAppReady", "${result.label}: ${it.message.orEmpty()}", it)
                    warnings += "${result.label}: ${it.message.orEmpty()}"
                }
'''
    if text.count(old) != 1:
        raise SystemExit("Could not locate Welcome task error handler")
    text = text.replace(old, new, 1)
    marker = "            val domestic = resolve(domesticFuture)\n"
    if text.count(marker) != 1:
        raise SystemExit("Could not locate domestic producer resolution")
    text = text.replace(
        marker,
        marker
        + '            Log.i("StatMakerAppReady", "domestic_matches=${db.totalMatchCount()} domestic_ok=${domestic != null}")\n',
        1,
    )
    source.write_text(text, encoding="utf-8")
    print("APP_READY_DIAGNOSTICS_OK")


replace_repository_urls()
allow_runner_cleartext()
harden_download(
    "app/src/main/java/com/statmaker/app/DomesticApiArtifactImporter.kt",
    "Domestic API artifact",
)
harden_download(
    "app/src/main/java/com/statmaker/app/DomesticApiRegistry.kt",
    "Domestic API registry",
)
add_producer_diagnostics()
