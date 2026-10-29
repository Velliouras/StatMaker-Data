#!/usr/bin/env python3
"""Publish exactly one generated commit, without replaying shallow checkout history."""
from __future__ import annotations

import argparse
import subprocess
import time


def git(*args, check=True):
    result = subprocess.run(["git", *args], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and result.returncode:
        raise RuntimeError(f"git {args[0]} failed: {result.stderr.strip()}")
    return result


def publish(remote="origin", branch="main", attempts=4, prefixes=(), pause=time.sleep):
    """Retry publication only; provider work must never be repeated by this helper."""
    head = git("rev-parse", "HEAD").stdout.strip()
    parents = git("show", "-s", "--format=%P", head).stdout.split()
    if len(parents) != 1:
        raise RuntimeError("Publication requires exactly one local generated commit")
    changed = set(git("diff-tree", "--no-commit-id", "--name-only", "-r", head).stdout.splitlines())
    if not changed:
        raise RuntimeError("Generated commit contains no changed files")
    if prefixes and any(not any(path.startswith(prefix) for prefix in prefixes) for path in changed):
        raise RuntimeError("Generated commit contains files outside the publisher's owned paths")

    for attempt in range(1, attempts + 1):
        # Capture the actual parent of this one commit before fetching. Never use
        # a moving remote-tracking ref as the range to replay, nor --depth=N:
        # both can make Git replay someone else's older commit in a shallow repo.
        parent = git("rev-parse", "HEAD^").stdout.strip()
        git("fetch", "--no-tags", remote, f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}")
        upstream = git("rev-parse", f"refs/remotes/{remote}/{branch}").stdout.strip()
        if git("merge-base", "--is-ancestor", "HEAD", upstream, check=False).returncode == 0:
            print("GENERATED_PUBLISH_ALREADY_VISIBLE", flush=True)
            return
        rebased = git("rebase", "--onto", upstream, parent, check=False)
        if rebased.returncode:
            git("rebase", "--abort", check=False)
            raise RuntimeError("Generated output conflicts with newer canonical output; publication stopped: " + rebased.stderr.strip())
        ahead = int(git("rev-list", "--count", f"{upstream}..HEAD").stdout.strip())
        if ahead == 0:
            print("GENERATED_PUBLISH_ALREADY_CURRENT", flush=True)
            return
        if ahead != 1:
            raise RuntimeError(f"Refusing to publish {ahead} commits instead of one generated commit")
        actual = set(git("diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").stdout.splitlines())
        if not actual.issubset(changed):
            raise RuntimeError("Publication unexpectedly includes unrelated files")
        pushed = git("push", remote, f"HEAD:refs/heads/{branch}", check=False)
        if pushed.returncode == 0:
            print(f"GENERATED_PUBLISH_OK attempt={attempt} files={len(actual)}", flush=True)
            return
        print(f"GENERATED_PUBLISH_RETRY attempt={attempt}: {pushed.stderr.strip()}", flush=True)
        if attempt < attempts:
            pause(min(attempt * 2, 8))
    raise RuntimeError(f"Generated publication could not complete after {attempts} attempts")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--attempts", type=int, default=4)
    parser.add_argument("--path-prefix", action="append", default=[])
    args = parser.parse_args()
    if not 1 <= args.attempts <= 8:
        parser.error("attempts must be between 1 and 8")
    publish(args.remote, args.branch, args.attempts, args.path_prefix)
