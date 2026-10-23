"""Bounded job-output transfer for AI evidence, without persistent Actions artifacts."""
import argparse
import base64
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath

MAX_OUTPUT = 400_000  # ASCII output stays below GitHub's 1 MB UTF-16 per-job limit.
MAX_CONTENT = 20_000_000


def pack(source):
    source = Path(source)
    if not source.is_dir():
        raise ValueError('Missing evidence directory')
    files = {}
    for path in sorted(source.rglob('*')):
        if path.is_symlink():
            raise ValueError('Evidence symlink is not allowed')
        if path.is_file():
            files[path.relative_to(source).as_posix()] = base64.b64encode(path.read_bytes()).decode('ascii')
    raw = json.dumps(files, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode()
    if len(raw) > MAX_CONTENT:
        raise ValueError('Evidence exceeds uncompressed transfer limit')
    compressed = gzip.compress(raw, mtime=0)
    encoded = base64.b64encode(compressed).decode('ascii')
    if len(encoded) > MAX_OUTPUT:
        raise ValueError('Evidence exceeds bounded job-output limit')
    return encoded, hashlib.sha256(compressed).hexdigest()


def unpack(encoded, digest, target):
    if len(encoded) > MAX_OUTPUT:
        raise ValueError('Evidence exceeds bounded job-output limit')
    compressed = base64.b64decode(encoded, validate=True)
    if hashlib.sha256(compressed).hexdigest() != digest:
        raise ValueError('Evidence SHA mismatch')
    with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
        raw = stream.read(MAX_CONTENT + 1)
    if len(raw) > MAX_CONTENT:
        raise ValueError('Evidence exceeds uncompressed transfer limit')
    files = json.loads(raw)
    target = Path(target).resolve()
    entries = []
    for name, content in files.items():
        relative = PurePosixPath(name)
        if relative.is_absolute() or '..' in relative.parts or not relative.parts or '\\' in name:
            raise ValueError('Invalid evidence path')
        destination = (target / name).resolve()
        if not destination.is_relative_to(target):
            raise ValueError('Evidence escapes destination')
        entries.append((destination, base64.b64decode(content, validate=True)))
    for path, content in entries:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return len(entries)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['pack', 'unpack'])
    parser.add_argument('directory')
    args = parser.parse_args()
    if args.mode == 'pack':
        encoded, digest = pack(args.directory)
        with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
            output.write(f'evidence={encoded}\nevidence_sha256={digest}\n')
        print('AI_JOB_EVIDENCE_PACKED', len(encoded), digest)
    else:
        count = unpack(os.environ['AI_EVIDENCE'], os.environ['AI_EVIDENCE_SHA256'], args.directory)
        print('AI_JOB_EVIDENCE_RESTORED', count)


if __name__ == '__main__':
    main()
