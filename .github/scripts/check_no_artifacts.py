"""Fail CI if a workflow reintroduces persistent Actions artifact uploads."""
import re
import sys
from pathlib import Path

UPLOAD = re.compile(r'''^\s*(?:-\s*)?uses:\s*["']?actions/(?:upload-artifact|upload-pages-artifact)@''', re.I)


def violations(directory):
    return [(str(path), number) for path in sorted(Path(directory).glob('*.y*ml'))
            for number, line in enumerate(path.read_text().splitlines(), 1) if UPLOAD.search(line)]


if __name__ == '__main__':
    directory = sys.argv[1] if len(sys.argv) > 1 else '.github/workflows'
    bad = violations(directory)
    for path, number in bad:
        print(f'::error file={path},line={number}::Persistent Actions artifact uploads are disabled by repository policy.')
    if bad:
        raise SystemExit(1)
    print('ACTIONS_ARTIFACT_POLICY_OK')
