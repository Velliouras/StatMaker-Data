#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

HISTORICAL_DATA_COMMIT = "17fa84485df5e1f46d9a35c919b1a33255a69961"


def run(*args: str) -> None:
    subprocess.run(list(args), check=True)

# Preserve the historical provider-identity validator that accompanied the known-good v5 snapshot.
url = f"https://raw.githubusercontent.com/Velliouras/StatMaker-Data/{HISTORICAL_DATA_COMMIT}/scripts/validate_domestic_cache_provider_identity.py"
with urllib.request.urlopen(url, timeout=60) as response:
    historical = response.read()
with tempfile.NamedTemporaryFile(prefix="statmaker-v5-validator-", suffix=".py", delete=False) as tmp:
    tmp.write(historical)
    tmp_path = Path(tmp.name)
try:
    run(sys.executable, str(tmp_path))
finally:
    tmp_path.unlink(missing_ok=True)

# The only intentional product delta from that historical baseline:
# retire Asian scopes and all Asian/Handicap markets before the exact v5 engine sees them.
retirement = Path("scripts/app_ready_input_retirement.py")
if not retirement.is_file():
    raise SystemExit("Missing scripts/app_ready_input_retirement.py")
run(sys.executable, str(retirement), "--root", ".")
run(sys.executable, str(retirement), "--root", ".", "--check-only")

print("APP_READY_PRE_V6_INPUT_PREFLIGHT_OK retirement=asian+handicap")
