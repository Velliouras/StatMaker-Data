#!/usr/bin/env python3
import re
from pathlib import Path


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


harden_download(
    "app/src/main/java/com/statmaker/app/DomesticApiArtifactImporter.kt",
    "Domestic API artifact",
)
harden_download(
    "app/src/main/java/com/statmaker/app/DomesticApiRegistry.kt",
    "Domestic API registry",
)
