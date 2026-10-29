"""Reject persistent artifact uploads and Gradle build caches in this repository."""
import re
import sys
from pathlib import Path

UPLOAD = re.compile(r'''^\s*(?:-\s*)?uses:\s*["']?actions/(?:upload-artifact|upload-pages-artifact)@''', re.I)
GRADLE = re.compile(r'''^\s*(?:-\s*)?uses:\s*["']?gradle/actions/setup-gradle@''', re.I)
ITEM = re.compile(r'^(\s*)-\s')
CACHE_DISABLED = re.compile(r'^\s*cache-disabled:\s*true\s*(?:#.*)?$')


def gradle_cache_disabled(lines, index):
    """Check this step only; a later step cannot make an unsafe step pass."""
    start = next(i for i in range(index, -1, -1) if ITEM.match(lines[i]))
    indent = len(ITEM.match(lines[start]).group(1))
    end = len(lines)
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if line.strip() and not line.lstrip().startswith('#'):
            spaces = len(line) - len(line.lstrip())
            if spaces <= indent:
                end = i
                break
    return any(CACHE_DISABLED.match(line) for line in lines[index + 1:end])


def violations(directory):
    bad = []
    for path in sorted(Path(directory).glob('*.y*ml')):
        lines = path.read_text().splitlines()
        for index, line in enumerate(lines):
            if UPLOAD.search(line) or (GRADLE.search(line) and not gradle_cache_disabled(lines, index)):
                bad.append((str(path), index + 1))
    return bad


if __name__ == '__main__':
    directory = sys.argv[1] if len(sys.argv) > 1 else '.github/workflows'
    bad = violations(directory)
    for path, number in bad:
        print(f'::error file={path},line={number}::Artifact uploads are disabled; setup-gradle must set cache-disabled: true in the same step.')
    if bad:
        raise SystemExit(1)
    print('ACTIONS_ARTIFACT_POLICY_OK')
