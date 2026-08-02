#!/usr/bin/env python3

"""Prove the guard suites bite, by breaking each guard and requiring a suite to notice.

Four separate times in PR #8 a guard shipped with a branch no fixture exercised (R1-F4, R2-F1,
R3-F2, R5-F2). Every one was found by hand, by editing the guard and watching the suite stay
green. A fixture existing is not the property that matters -- a fixture *failing when the guard
stops working* is -- and only running it can tell you which you have.

Each case edits one guard and requires the named suite to fail. A case that survives means the
suite cannot see that branch of that guard.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCANNER = ".github/actions/metric-cardinality/check-metric-cardinality.py"
PINS = ".github/scripts/verify-action-pins.py"
FIXTURE_SHAS = ".github/scripts/verify-fixture-shas.py"
METRIC_SUITE = "tests/test-metric-cardinality.sh"
PINS_SUITE = "tests/test-action-pins.sh"
FIXTURE_SHA_SUITE = "tests/test-fixture-shas.sh"

# (label, kind, suite, file, old, new). `kind` is "detector" for one alternative of a
# metric-cardinality detector; those are counted against the scanner below.
CASES = [
    ("put_metric_data", "detector", METRIC_SUITE, SCANNER,
     'r"put_metric_data|PutMetricData"', 'r"PutMetricData"'),
    ("PutMetricData", "detector", METRIC_SUITE, SCANNER,
     'r"put_metric_data|PutMetricData"', 'r"put_metric_data"'),
    ("Dimensions literal", "detector", METRIC_SUITE, SCANNER,
     r"""r'["\']Dimensions["\']\s*:\s*\[\s*[^\]\s]'""", 'r"(?!x)x"'),
    ("_PER_STREAM_METRICS", "detector", METRIC_SUITE, SCANNER,
     'r"_PER_STREAM_METRICS|_PER_SERVICE_METRICS"', 'r"_PER_SERVICE_METRICS"'),
    ("_PER_SERVICE_METRICS", "detector", METRIC_SUITE, SCANNER,
     'r"_PER_STREAM_METRICS|_PER_SERVICE_METRICS"', 'r"_PER_STREAM_METRICS"'),
    ("_ALARM_BOUND", "detector", METRIC_SUITE, SCANNER, 'r"_ALARM_BOUND"', 'r"(?!x)x"'),
    ("aws_cloudwatch_metric_alarm", "detector", METRIC_SUITE, SCANNER,
     r'r"aws_cloudwatch_metric_alarm|MetricName\s*="', r'r"MetricName\s*="'),
    ("MetricName assignment", "detector", METRIC_SUITE, SCANNER,
     r'r"aws_cloudwatch_metric_alarm|MetricName\s*="', 'r"aws_cloudwatch_metric_alarm"'),

    # Scanner behaviour that is not a detector alternative.
    ("dimension Name half", "scanner", METRIC_SUITE, SCANNER,
     r"""DIMENSION_NAME = re.compile(r'["\']Name["\']\s*:')""",
     'DIMENSION_NAME = re.compile(r"(?!x)x")'),
    ("dimension Value half", "scanner", METRIC_SUITE, SCANNER,
     r"""DIMENSION_VALUE = re.compile(r'["\']Value["\']\s*:')""",
     'DIMENSION_VALUE = re.compile(r"(?!x)x")'),
    ("acknowledgement is honoured", "scanner", METRIC_SUITE, SCANNER,
     'ACK = re.compile(r"metric-budget:\\s*\\S", re.IGNORECASE)', 'ACK = re.compile(r"(?!x)x")'),
    ("acknowledgement is local", "scanner", METRIC_SUITE, SCANNER,
     "ACK_RADIUS = 3", "ACK_RADIUS = 8"),
    ("only added lines are scanned", "scanner", METRIC_SUITE, SCANNER,
     'elif raw.startswith("+") and not raw.startswith("+++"):',
     'elif raw[:1] in "+-" and not raw.startswith(("+++", "---")):'),
    ("line numbers skip the no-newline marker", "scanner", METRIC_SUITE, SCANNER,
     'elif not raw.startswith(("-", "\\\\")):', 'elif True:'),

    # Pin verifier.
    ("self-pin content equality", "pins", PINS_SUITE, PINS,
     "if comparison.returncode == 0:\n            continue", "if True:\n            continue"),
    ("full-SHA enforcement", "pins", PINS_SUITE, PINS,
     "if not FULL_SHA.fullmatch(ref):", "if False:"),
    ("abbreviated SHA rejection", "pins", PINS_SUITE, PINS,
     'FULL_SHA = re.compile(r"[0-9a-fA-F]{40}")', 'FULL_SHA = re.compile(r"[0-9a-fA-F]{7,40}")'),
    ("missing ref rejection", "pins", PINS_SUITE, PINS, 'if "@" not in target:', "if False:"),
    # Bypasses the whole availability step. Mutating only the fetch-failure branch instead would
    # survive, and correctly so: the check immediately after it catches the same condition, so
    # dropping one changes the diagnostic wording and nothing else.
    ("unavailable pin is detected", "pins", PINS_SUITE, PINS,
     "        available, detail = ensure_commit(ref)", '        available, detail = (True, "")'),
    ("errors fail the run", "pins", PINS_SUITE, PINS, "if errors:", "if False:"),

    # Fixture SHA checker. The two directions of the "is it a commit here" test are separate
    # cases because different fixtures hold them: dropping it lets hard-coded history through,
    # and inverting it condemns every third-party pin a fixture legitimately names.
    ("hex literals are scanned", "fixtures", FIXTURE_SHA_SUITE, FIXTURE_SHAS,
     'HEX = re.compile(r"\\b[0-9a-f]{7,40}\\b", re.IGNORECASE)',
     'HEX = re.compile(r"(?!x)x")'),
    ("uppercase hex literals are scanned", "fixtures", FIXTURE_SHA_SUITE, FIXTURE_SHAS,
     'HEX = re.compile(r"\\b[0-9a-f]{7,40}\\b", re.IGNORECASE)',
     'HEX = re.compile(r"\\b[0-9a-f]{7,40}\\b")'),
    ("local commits are rejected", "fixtures", FIXTURE_SHA_SUITE, FIXTURE_SHAS,
     '    return git("cat-file", "-e", f"{candidate}^{{commit}}").returncode == 0',
     "    return False"),
    ("foreign hex is left alone", "fixtures", FIXTURE_SHA_SUITE, FIXTURE_SHAS,
     '    return git("cat-file", "-e", f"{candidate}^{{commit}}").returncode == 0',
     "    return True"),
    ("nested fixture files are scanned", "fixtures", FIXTURE_SHA_SUITE, FIXTURE_SHAS,
     "for path in FIXTURE_ROOT.rglob(\"*\")", "for path in FIXTURE_ROOT.glob(\"*\")"),
    ("fixture SHA errors fail the run", "fixtures", FIXTURE_SHA_SUITE, FIXTURE_SHAS,
     "if errors:", "if False:"),
]


def detector_alternatives() -> list[str]:
    """Every alternative of every metric-cardinality detector, straight from the scanner."""
    spec = importlib.util.spec_from_file_location("scanner", REPO_ROOT / SCANNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    alternatives: list[str] = []
    for pattern, _reason in module.PATTERNS:
        # Top-level alternation only; these patterns nest none.
        alternatives += re.split(r"\|(?![^\[]*\])", pattern.pattern)
    return alternatives


def check_detector_coverage() -> list[str]:
    """A new detector alternative must arrive with a case, or this fails."""
    declared = sum(1 for case in CASES if case[1] == "detector")
    actual = len(detector_alternatives())
    if declared == actual:
        return []
    return [
        f"the scanner advertises {actual} detector alternatives but "
        f"{declared} have a mutation case; every alternative needs one that bites"
    ]


def run_case(label: str, suite: str, target: str, old: str, new: str, workdir: Path) -> str | None:
    checkout = workdir / "checkout"
    # A clone, not a copy: a copied worktree keeps a .git *file* pointing at the original, so
    # commits made "inside the copy" land in the real repository instead.
    subprocess.run(["git", "clone", "--quiet", "--shared", str(REPO_ROOT), str(checkout)],
                   check=True, capture_output=True)
    path = checkout / target
    source = path.read_text(encoding="utf-8")
    if old not in source:
        return f"{label}: the code this case mutates is gone; update or drop the case"
    path.write_text(source.replace(old, new, 1), encoding="utf-8")
    # Commit, because fixtures clone the checkout and would otherwise see the original.
    subprocess.run(["git", "-C", str(checkout), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(checkout), "-c", "user.name=Mutation",
         "-c", "user.email=mutation@example.invalid", "commit", "--quiet", "-m", label],
        check=True, capture_output=True,
    )
    result = subprocess.run(["bash", suite], cwd=checkout, capture_output=True, text=True)
    if result.returncode == 0:
        return f"{label}: {suite} still passes with this guard broken"
    return None


def main() -> int:
    failures = check_detector_coverage()

    for label, _kind, suite, target, old, new in CASES:
        workdir = Path(tempfile.mkdtemp(prefix="mutation-"))
        try:
            failure = run_case(label, suite, target, old, new, workdir)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        print(f"  {'FAIL' if failure else 'ok  '}  {label}")
        if failure:
            failures.append(failure)

    if failures:
        print("\nMutation testing failed:\n", file=sys.stderr)
        print("\n".join(f"  {failure}" for failure in failures), file=sys.stderr)
        return 1

    print(f"\nAll {len(CASES)} guard mutations were caught by their suites.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
