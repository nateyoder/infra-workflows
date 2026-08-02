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


def unacknowledged(found: list[tuple[str, int, str, str, int]]) -> list[tuple[str, int, str, str]]:
    """Drop every finding whose publication block carries an acknowledgement."""
    acknowledged = {
        block
        for path, lineno, _reason, _snippet, block in found
        if is_acknowledged(path, lineno)
    }
    return [
        (path, lineno, reason, snippet)
        for path, lineno, reason, snippet, block in found
        if block not in acknowledged
    ]


def scan(diff: str) -> list[tuple[str, int, str, str]]:
    # The final field identifies the publication block; unacknowledged() sheds it.
    found: list[tuple[str, int, str, str, int]] = []
    path: str | None = None
    hunk_added: list[tuple[int, str]] = []
    next_block = 0
    active_publication: int | None = None

    def flush() -> None:
        nonlocal active_publication, next_block
        if path is None or not hunk_added:
            return

        active_publication = None  # A publication block never crosses an added hunk.
        for index, (lineno, line) in enumerate(hunk_added):
            for pattern_index, (pattern, reason) in enumerate(PATTERNS):
                if pattern.search(line):
                    if pattern_index == 0:
                        next_block += 1  # Every publication opens a new block.
                        active_publication = next_block
                        finding_block = active_publication
                    elif pattern_index == 1 and active_publication is not None:
                        finding_block = active_publication
                    else:
                        next_block += 1
                        finding_block = next_block
                    found.append(
                        (
                            path,
                            lineno,
                            reason,
                            line.strip()[:120],
                            finding_block,
                        )
                    )
                    break

            if not DIMENSION_NAME.search(line):
                continue
            candidate = "\n".join(
                added_line for _, added_line in hunk_added[index : index + DIMENSION_WINDOW]
            )
            if DIMENSION_VALUE.search(candidate):
                if active_publication is None:
                    next_block += 1
                    dimension_block = next_block
                else:
                    dimension_block = active_publication
                found.append(
                    (
                        path,
                        lineno,
                        "adds a metric dimension entry",
                        line.strip()[:120],
                        dimension_block,
                    )
                )

    lineno = 0
    for raw in diff.splitlines():
        if raw.startswith("+++ b/"):
            flush()
            hunk_added = []
            path = raw[6:]
        elif raw.startswith("@@"):
            flush()
            hunk_added = []
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
        "\nnested inside it -- regardless of how many fields the payload carries. The next"
        "\npublication, or a separate edit, needs its own note."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
