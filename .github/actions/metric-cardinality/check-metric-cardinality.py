#!/usr/bin/env python3

"""Fail when added diff lines introduce unacknowledged metric cardinality."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path


CLI_DIMENSION = re.compile(r"[Dd]imensions(?:[\s=\\]*[\"']?)Name=[^,\s]+,Value=\S")

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
    # AWS CLI. The hyphenated subcommands cannot collide with the boto3 spellings above.
    # These fire on the subcommand alone, like put_metric_data does, rather than only when
    # --dimensions is present: a dimensionless publish still mints one billed series, and
    # shelling out must not be a softer path to a metric than the SDK.
    (
        re.compile(r"put-metric-data|put-metric-alarm"),
        "publishes a metric or alarm with the AWS CLI",
    ),
    # `--dimensions Name=StationId,Value=$ID` shorthand, which carries the cardinality and is
    # often a continuation line away from its subcommand. The JSON form of the same flag is
    # already covered by DIMENSION_NAME/DIMENSION_VALUE.
    #
    # Anchored on the flag rather than matching `Name=,Value=` alone: that shorthand is not
    # unique to CloudWatch, and unanchored it fires on `cloudformation deploy
    # --parameter-overrides Name=Stack,Value=foo` and on Prometheus label strings.
    (CLI_DIMENSION, "adds an AWS CLI metric dimension"),
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


def scan(diff: str) -> list[tuple[str, int, str, str]]:
    findings: list[tuple[str, int, str, str]] = []
    path: str | None = None
    hunk_added: list[tuple[int, str]] = []

    def flush() -> None:
        if path is None or not hunk_added:
            return

        for lineno, line in hunk_added:
            for pattern, reason in PATTERNS:
                if pattern.search(line):
                    findings.append((path, lineno, reason, line.strip()[:120]))
                    break

        for index, (lineno, line) in enumerate(hunk_added):
            if not DIMENSION_NAME.search(line):
                candidate = "\n".join(
                    added_line for _, added_line in hunk_added[index : index + DIMENSION_WINDOW]
                )
                if (
                    "dimensions" in line.lower()
                    and not CLI_DIMENSION.search(line)
                    and CLI_DIMENSION.search(candidate)
                ):
                    findings.append(
                        (path, lineno, "adds an AWS CLI metric dimension", line.strip()[:120])
                    )
                continue
            candidate = "\n".join(
                added_line for _, added_line in hunk_added[index : index + DIMENSION_WINDOW]
            )
            if DIMENSION_VALUE.search(candidate):
                findings.append((path, lineno, "adds a metric dimension entry", line.strip()[:120]))

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

    return [finding for finding in findings if not is_acknowledged(finding[0], finding[1])]


def main() -> int:
    base = os.environ.get("BASE_REF", "main")
    paths = os.environ.get("SCAN_PATHS", "*.py *.yml *.yaml *.tf *.json *.sh").split()
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
        "\nIf the cost is intended, say so on the added line or a nearby committed line:"
        "\n    # metric-budget: 1 fleet series, paged on by <alarm name>"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
