#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

HISTORICAL_DATA_COMMIT = "17fa84485df5e1f46d9a35c919b1a33255a69961"


def run(*args: str, env: dict[str, str] | None = None) -> None:
    subprocess.run(list(args), check=True, env=env)

# Preserve the historical provider-identity validator that accompanied the known-good v5 snapshot.
# Run it against the current repository data while resolving its helper imports from ./scripts.
url = f"https://raw.githubusercontent.com/Velliouras/StatMaker-Data/{HISTORICAL_DATA_COMMIT}/scripts/validate_domestic_cache_provider_identity.py"
with urllib.request.urlopen(url, timeout=60) as response:
    historical = response.read().decode("utf-8")

# The historical script derives ROOT from __file__. Because we execute an immutable copy from /tmp,
# bind ROOT explicitly to the checked-out repository root without changing validation semantics.
root_anchor = 'ROOT = Path(__file__).resolve().parents[1]'
if historical.count(root_anchor) != 1:
    raise SystemExit("Historical validator ROOT anchor mismatch")
historical = historical.replace(root_anchor, 'ROOT = Path.cwd()', 1)

with tempfile.NamedTemporaryFile(prefix="statmaker-v5-validator-", suffix=".py", mode="w", encoding="utf-8", delete=False) as tmp:
    tmp.write(historical)
    tmp_path = Path(tmp.name)
try:
    env = os.environ.copy()
    scripts_path = str((Path.cwd() / "scripts").resolve())
    env["PYTHONPATH"] = scripts_path + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    run(sys.executable, str(tmp_path), env=env)
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
