#!/usr/bin/env python3

"""Fail when a test fixture hard-codes a commit from this repository's history.

A fixture that names a commit as a literal keeps passing for as long as that commit stays
reachable, and a full fetch drags in ancestors, so it stays green well past the point where it
still proves anything. Then a branch is deleted, the object goes away, and the fixture fails on
an unrelated pull request -- which is how PR #8 R6-F1 arrived (`087da8d` written into the
stale-self-pin case).

The property that matters is not "does this SHA look historical" but "does this SHA name a
commit in *this* repository". A third-party action pin, or a deliberately unreachable sentinel,
is inert here and stays allowed; anything git can resolve to a local commit is not.

Fixtures should build the commit they need and read its SHA back, so the case carries its own
object instead of borrowing one from history.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPO_ROOT / "tests"
FIXTURE_SUFFIXES = (".sh", ".py")
HEX = re.compile(r"\b[0-9a-f]{7,40}\b", re.IGNORECASE)


def git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def names_local_commit(candidate: str) -> bool:
    return git("cat-file", "-e", f"{candidate}^{{commit}}").returncode == 0


def fixture_files() -> list[Path]:
    return sorted(
        path
        for path in FIXTURE_ROOT.rglob("*")
        if path.is_file() and path.suffix in FIXTURE_SUFFIXES
    )


def main() -> int:
    errors: list[str] = []
    checked = 0

    for path in fixture_files():
        location = path.relative_to(REPO_ROOT).as_posix()
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for candidate in HEX.findall(line):
                checked += 1
                if not names_local_commit(candidate):
                    continue
                errors.append(
                    f"{location}:{lineno}: hard-codes a commit from this repository: {candidate}"
                )

    if errors:
        print("Fixture SHA verification failed:\n", file=sys.stderr)
        print("\n".join(errors), file=sys.stderr)
        print(
            "\nBuild the commit inside the fixture and read its SHA back:"
            "\n    stale_sha=$(git rev-parse HEAD)"
            "\nA literal only resolves while some branch still reaches it. Ancestors arrive with"
            "\nany full fetch, so such a fixture keeps passing long after it stopped proving"
            "\nanything -- then fails on whichever pull request follows the branch deletion.",
            file=sys.stderr,
        )
        return 1

    print(f"Checked {checked} hex literal(s) across {len(fixture_files())} fixture files.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
