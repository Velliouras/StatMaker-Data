"""Merge only the AI artifact namespace; preserve all existing immutable records."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ai_analyst.contracts import within
from ai_analyst.run_shadow import OUTPUT

IMMUTABLE = {'inputs', 'attempts', 'reviews', 'failures'}
MUTABLE = {'status.json', 'summary.json', 'RESULTS_GR.md'}


def merge(root, artifact):
    count = 0
    for incoming in sorted(artifact.rglob('*')):
        if incoming.is_symlink():
            raise ValueError('Symlink in AI artifact')
        if not incoming.is_file():
            continue
        relative = incoming.relative_to(artifact)
        if len(relative.parts) == 1 and relative.name in MUTABLE:
            continue  # Recompute status/score from the merged authoritative state.
        if len(relative.parts) != 2 or relative.parts[0] not in IMMUTABLE or incoming.suffix != '.json':
            # Publication proofs belong to the publisher, never the inference job.
            if relative.as_posix() == 'publication_evidence.json':
                continue
            raise ValueError('Unexpected path in AI artifact')
        target = within(root, OUTPUT+'/'+relative.as_posix())
        raw = incoming.read_bytes()
        if target.exists() and target.read_bytes() != raw:
            raise ValueError('AI immutable file collision')
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('xb') as f:
                f.write(raw)
            count += 1
    status = artifact/'status.json'
    if status.exists():
        target = within(root, OUTPUT+'/status.json')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(status.read_bytes())
    return count


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--artifact', type=Path, required=True)
    p.add_argument('--repository-root', type=Path, default=Path(__file__).resolve().parents[2])
    a = p.parse_args()
    print('AI artifact files merged:', merge(a.repository_root.resolve(), a.artifact.resolve()))
