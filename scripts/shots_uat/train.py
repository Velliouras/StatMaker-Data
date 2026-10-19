"""Fit once from cached history, never from prices or future outcomes."""
import argparse
import gzip
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from .features import training_frame
from .model import fit_bank

ROOT = Path(__file__).resolve().parents[2]
HISTORY = ROOT / "reports/betting_v2/prior_market_calibration_20261018/history.json.gz"


def timestamp(day):
    return datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "data/statmaker/uat/shots_v1/model")
    parser.add_argument("--train-before", default="2026-09-01")
    parser.add_argument("--calibrate-before", default="2026-10-01")
    args = parser.parse_args()
    raw = HISTORY.read_bytes(); rows = json.loads(gzip.decompress(raw))
    x, shots, sot, times = training_frame(rows)
    print("FEATURES", len(rows), len(x), flush=True)
    report = fit_bank(x, shots, sot, times, timestamp(args.train_before), timestamp(args.calibrate_before), args.output)
    report["historySha256"] = hashlib.sha256(raw).hexdigest()
    report["historyFixtures"] = len(rows)
    report["trainingFixtures"] = report["trainingRows"] // 2
    report["calibrationFixtures"] = report["calibrationRows"] // 2
    report["calibrationMethod"] = "Prior month count mean/accuracy scaling and NB2 dispersion; no ROI threshold search"
    (args.output / "bank.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k not in ("features", "replicas")}), flush=True)


if __name__ == "__main__":
    main()
