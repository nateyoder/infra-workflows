"""Answer "is this file one of the ones I check?" from the file, not from its name.

Two checks here discover their own inputs instead of reading a hand-maintained list: the mutation
harness finds every guard under `.github/`, and the fixture SHA checker finds every fixture under
`tests/`. Both then narrowed that discovery with a suffix tuple, which is the list they were
written to remove, one rung up. `.github/scripts/verify-b` carrying a `#!/usr/bin/env bash` line,
and a `tests/fixtures/pin.yml` hard-coding a commit from this repository, were each invisible
while the check that should have caught them reported success (issue #23).

So the predicates ask what the file is:

* `is_script` -- an executable bit or a `#!` line. Either one says something is meant to run this
  file; its name says nothing.
* `is_text` -- no NUL byte in the leading block. If a hex literal can be read out of a fixture it
  can rot, whatever the fixture happens to be written in.

Both walk the index rather than the working tree: a file git does not track is not shipped, and
untracked scratch sitting beside the real thing should not fail anyone's check.

This module is deliberately not a script -- no `#!`, no executable bit -- because it decides
nothing on its own. That keeps it out of `is_script`, and so out of the guard set the mutation
harness demands a case for. Its branches are covered instead by the discovery cases in
`tests/mutation-cases.py`, one per branch.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


# Enough of a file to meet a NUL byte in if there is one; git reads a comparable prefix to decide
# the same question.
TEXT_PROBE_BYTES = 8000


def tracked_files(repo_root: Path, subdir: str) -> list[Path]:
    """Every file git tracks under `subdir`, absolute and sorted."""
    listing = subprocess.run(
        ["git", "ls-files", "-z", "--", subdir],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    paths = (repo_root / name for name in listing.stdout.split("\0") if name)
    # A tracked path can be missing from the working tree, and a submodule entry is a directory
    # here; neither is a file to read.
    return sorted(path for path in paths if path.is_file())


def is_script(path: Path) -> bool:
    """A file something is meant to run: the executable bit, or a `#!` line."""
    if path.stat().st_mode & 0o111:
        return True
    with path.open("rb") as handle:
        return handle.read(2) == b"#!"


def is_text(path: Path) -> bool:
    """A file a hex literal can be read out of: no NUL byte in the leading block."""
    with path.open("rb") as handle:
        return b"\0" not in handle.read(TEXT_PROBE_BYTES)


def scripts_under(repo_root: Path, subdir: str) -> list[Path]:
    """Every tracked script under `subdir`."""
    return [path for path in tracked_files(repo_root, subdir) if is_script(path)]


def text_files_under(repo_root: Path, subdir: str) -> list[Path]:
    """Every tracked text file under `subdir`."""
    return [path for path in tracked_files(repo_root, subdir) if is_text(path)]
