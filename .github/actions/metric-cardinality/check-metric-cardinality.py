#!/usr/bin/env python3

"""Fail when added diff lines introduce unacknowledged metric cardinality."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path


PATTERNS = (
    (re.compile(r"put_metric_data|PutMetricData"), "publishes a metric directly"),
    (
        re.compile(r'["\']Dimensions["\']\s*:\s*\[\s*[^\]\s]'),
        "adds a non-empty Dimensions list",
    ),
    (
        re.compile(r"_PER_STREAM_METRICS|_PER_SERVICE_METRICS"),
        "adds a per-producer metric tier",
    ),
    (re.compile(r"_ALARM_BOUND"), "adds a published metric name"),
    (re.compile(r"aws_cloudwatch_metric_alarm|MetricName\s*="), "adds a metric or alarm"),
)
DIMENSION_NAME = re.compile(r'["\']Name["\']\s*:')
DIMENSION_VALUE = re.compile(r'["\']Value["\']\s*:')
ACK = re.compile(r"metric-budget:\s*\S", re.IGNORECASE)
ACK_RADIUS = 3
DIMENSION_WINDOW = 5
BLOCK_GAP = 6


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        check=check,
    )


def resolve_base(base: str) -> str:
    subprocess.run(
        ["git", "fetch", "--quiet", "origin", base],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    remote_base = f"origin/{base}"
    if git("rev-parse", "--verify", "--quiet", remote_base, check=False).returncode == 0:
        return remote_base
    return base


def is_acknowledged(path: str, lineno: int) -> bool:
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return False

    start = max(0, lineno - 1 - ACK_RADIUS)
    end = min(len(lines), lineno + ACK_RADIUS)
    return ACK.search("\n".join(lines[start:end])) is not None


def blocks_by_site(sites: set[tuple[str, int, int]]) -> dict[tuple[str, int, int], int]:
    """Number the publication block each (path, hunk, line) site belongs to.

    One publication is several lines -- the call, then the dimensions nested inside its payload
    -- so it yields findings up to BLOCK_GAP lines apart. Treating those as one block is what
    lets a single note clear them all, which is what the failure message promises.

    The hunk is part of the identity, so the run has to be contiguous in the diff and not merely
    close in the file: at --unified=0 a hunk is exactly one unbroken run of added lines, so a
    note on one edit cannot reach a separate edit that happened to land a few lines away.
    """
    block: dict[tuple[str, int, int], int] = {}
    current = 0
    previous: tuple[str, int, int] | None = None
    for site in sorted(sites):
        path, hunk, lineno = site
        if previous is not None and (
            path != previous[0] or hunk != previous[1] or lineno - previous[2] > BLOCK_GAP
        ):
            current += 1
        block[site] = current
        previous = site
    return block


def unacknowledged(found: list[tuple[str, int, str, str, int]]) -> list[tuple[str, int, str, str]]:
    """Drop every finding whose block carries an acknowledgement, and shed the hunk tag."""
    block = blocks_by_site({(path, hunk, lineno) for path, lineno, _r, _s, hunk in found})
    acknowledged = {
        block[(path, hunk, lineno)]
        for path, lineno, _r, _s, hunk in found
        if is_acknowledged(path, lineno)
    }
    return [
        (path, lineno, reason, snippet)
        for path, lineno, reason, snippet, hunk in found
        if block[(path, hunk, lineno)] not in acknowledged
    ]


def scan(diff: str) -> list[tuple[str, int, str, str]]:
    # Tagged with the hunk each finding came from; unacknowledged() groups on it, then sheds it.
    found: list[tuple[str, int, str, str, int]] = []
    path: str | None = None
    hunk_added: list[tuple[int, str]] = []
    hunk = 0

    def flush() -> None:
        if path is None or not hunk_added:
            return

        for lineno, line in hunk_added:
            for pattern, reason in PATTERNS:
                if pattern.search(line):
                    found.append((path, lineno, reason, line.strip()[:120], hunk))
                    break

        for index, (lineno, line) in enumerate(hunk_added):
            if not DIMENSION_NAME.search(line):
                continue
            candidate = "\n".join(
                added_line for _, added_line in hunk_added[index : index + DIMENSION_WINDOW]
            )
            if DIMENSION_VALUE.search(candidate):
                found.append(
                    (path, lineno, "adds a metric dimension entry", line.strip()[:120], hunk)
                )

    lineno = 0
    for raw in diff.splitlines():
        if raw.startswith("+++ b/"):
            flush()
            hunk_added = []
            hunk = 0
            path = raw[6:]
        elif raw.startswith("@@"):
            flush()
            hunk_added = []
            hunk += 1
            match = re.search(r"\+(\d+)", raw)
            lineno = int(match.group(1)) if match else 0
        elif raw.startswith("+") and not raw.startswith("+++"):
            hunk_added.append((lineno, raw[1:]))
            lineno += 1
        elif not raw.startswith(("-", "\\")):
            lineno += 1
    flush()

    return unacknowledged(found)


def main() -> int:
    base = os.environ.get("BASE_REF", "main")
    paths = os.environ.get("SCAN_PATHS", "*.py *.yml *.yaml *.tf *.json").split()
    base_ref = resolve_base(base)
    merge_base = git("merge-base", base_ref, "HEAD", check=False).stdout.strip() or base_ref
    diff = git("diff", "--unified=0", merge_base, "HEAD", "--", *paths).stdout
    findings = scan(diff)

    if not findings:
        print("No new CloudWatch metric cardinality.")
        return 0

    print("This change adds CloudWatch metric cardinality:\n")
    for path, lineno, reason, snippet in findings:
        print(f"  {path}:{lineno}  {reason}")
        print(f"      {snippet}")
    print(
        "\nCloudWatch bills $0.30/month per distinct (namespace, metric name,"
        "\ndimension-value) combination. A dimension that scales with the fleet"
        "\nmultiplies that by the fleet size -- 74 producers makes one metric $22/month."
        "\n"
        "\nBefore adding one, check that an alarm will actually bind to it. If the value is"
        "\nonly read while debugging, put it on a log line instead: Logs Insights queries it"
        "\nfor $0.005/GB scanned and can group by any field, including ones too"
        "\nhigh-cardinality to ever be a dimension."
        "\n"
        "\nIf the cost is intended, say so on the added line or within three committed lines"
        "\nof it:"
        "\n    # metric-budget: 1 fleet series, paged on by <alarm name>"
        "\n"
        "\nOne note clears the whole publication it sits in -- the call and the dimensions"
        "\nnested inside it -- so findings no more than six lines apart in the same edit need"
        "\nonly one note between them. Publications added separately each need their own."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
