#!/usr/bin/env python3

"""Verify immutable action references and self-pin content equality."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SELF_REPO = "nateyoder/infra-workflows"
FULL_SHA = re.compile(r"[0-9a-fA-F]{40}")
DOCKER_DIGEST = re.compile(r".+@sha256:[0-9a-fA-F]{64}")
USES = re.compile(r'^\s*(?:-\s*)?uses:\s*["\']?([^"\'\s#]+)')


def git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def ensure_commit(ref: str) -> tuple[bool, str]:
    """Make a pinned commit available without assuming it is in clone history."""
    if git("cat-file", "-e", f"{ref}^{{commit}}").returncode == 0:
        return True, ""

    fetch = git("fetch", "--quiet", "--no-tags", "--depth=1", "origin", ref)
    if fetch.returncode != 0:
        detail = fetch.stderr.strip() or fetch.stdout.strip() or "git fetch failed"
        return False, detail
    if git("cat-file", "-e", f"{ref}^{{commit}}").returncode != 0:
        return False, "git fetch succeeded but the commit is still unavailable"
    return True, ""


def uses_entries() -> list[tuple[Path, int, str]]:
    entries: list[tuple[Path, int, str]] = []
    yaml_files = sorted(REPO_ROOT.joinpath(".github").rglob("*.yml"))
    yaml_files += sorted(REPO_ROOT.joinpath(".github").rglob("*.yaml"))
    for path in yaml_files:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            match = USES.match(line)
            if match:
                entries.append((path.relative_to(REPO_ROOT), lineno, match.group(1)))
    return entries


def main() -> int:
    errors: list[str] = []
    external_count = 0
    self_count = 0

    for source, lineno, target in uses_entries():
        location = f"{source}:{lineno}"
        if target.startswith("./"):
            continue
        if target.startswith("docker://"):
            if not DOCKER_DIGEST.fullmatch(target.removeprefix("docker://")):
                errors.append(f"{location}: Docker action is not pinned by sha256 digest: {target}")
            continue
        if "@" not in target:
            errors.append(f"{location}: external action has no immutable ref: {target}")
            continue

        action, ref = target.rsplit("@", 1)
        external_count += 1
        if not FULL_SHA.fullmatch(ref):
            errors.append(f"{location}: external action is not pinned by full 40-character SHA: {target}")
            continue
        if not action.startswith(f"{SELF_REPO}/"):
            continue

        self_count += 1
        action_path = Path(action.removeprefix(f"{SELF_REPO}/"))
        if action_path.is_absolute() or ".." in action_path.parts:
            errors.append(f"{location}: invalid self-referencing action path: {action_path}")
            continue
        if not REPO_ROOT.joinpath(action_path).exists():
            errors.append(f"{location}: self-referencing action path is missing: {action_path}")
            continue
        available, detail = ensure_commit(ref)
        if not available:
            errors.append(
                f"{location}: unable to verify self-pin {ref}; "
                f"the commit is unavailable after fetching from origin: {detail}"
            )
            continue
        if git("cat-file", "-e", f"{ref}:{action_path.as_posix()}").returncode != 0:
            errors.append(f"{location}: self-pin does not contain {action_path}: {ref}")
            continue

        comparison = git("diff", "--no-ext-diff", "--quiet", ref, "--", action_path.as_posix())
        if comparison.returncode == 0:
            continue
        if comparison.returncode > 1:
            errors.append(
                f"{location}: unable to compare self-pin {ref} with {action_path}: "
                f"{comparison.stderr.strip()}"
            )
            continue

        diff = git("diff", "--no-ext-diff", ref, "--", action_path.as_posix()).stdout.rstrip()
        errors.append(
            f"{location}: self-pin content mismatch for {action_path} at {ref}\n{diff}"
        )

    if errors:
        print("Action pin verification failed:\n", file=sys.stderr)
        print("\n\n".join(errors), file=sys.stderr)
        return 1

    print(f"Verified {external_count} immutable external uses entries and {self_count} self-pins.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
