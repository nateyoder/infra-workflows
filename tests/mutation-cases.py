#!/usr/bin/env python3

"""Prove the guard suites bite, by breaking each guard and requiring a suite to notice.

Four separate times in PR #8 a guard shipped with a branch no fixture exercised (R1-F4, R2-F1,
R3-F2, R5-F2). Every one was found by hand, by editing the guard and watching the suite stay
green. A fixture existing is not the property that matters -- a fixture *failing when the guard
stops working* is -- and only running it can tell you which you have.

Each case edits one guard and requires the named suite to fail. A case that survives means the
suite cannot see that branch of that guard.

Two structural checks run before any of that, because the table's own gaps are what kept
reproducing:

* every guard script under `.github/` must be named by some case, so a new guard cannot arrive
  with no coverage and nothing complaining;
* every detector alternative must be disabled by exactly one case, established by running the
  mutation and seeing which alternative disappears rather than by counting rows.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCANNER = ".github/actions/metric-cardinality/check-metric-cardinality.py"
METRIC_WORKFLOW = ".github/workflows/metric-cardinality.yml"
PINS = ".github/scripts/verify-action-pins.py"
FIXTURE_SHAS = ".github/scripts/verify-fixture-shas.py"
S3_AUDIT = ".github/scripts/s3-request-audit.py"
INSTALL = ".github/actions/setup-python-env/install-dependencies.sh"
DISCOVERY = ".github/scripts/discovery.py"
HARNESS = "tests/mutation-cases.py"
METRIC_SUITE = "tests/test-metric-cardinality.sh"
PINS_SUITE = "tests/test-action-pins.sh"
FIXTURE_SHA_SUITE = "tests/test-fixture-shas.sh"
S3_AUDIT_SUITE = "tests/test-s3-request-audit.sh"
INSTALL_SUITE = "tests/test-private-git-auth.sh"
DISCOVERY_SUITE = "tests/test-discovery.sh"

# Guards live here; every script under it must be covered. Found rather than declared -- and
# recognised by shape rather than by suffix, because a suffix tuple is the same hand-maintained
# list one rung up and went stale the same way (issue #23).
GUARD_ROOT = ".github"

# Shared helpers under GUARD_ROOT that reach no verdict of their own, so guard discovery leaves
# them out of the set above. Naming them does two things the accident of a missing shebang did
# not: the exemption becomes reviewable when a second helper arrives, and -- because these decide
# what the guards can *see* -- they carry the same file-level coverage requirement a guard does,
# so the cases below cannot quietly be deleted (PR #26 R1-F2).
COVERED_HELPERS = (DISCOVERY,)

# (label, kind, suite, file, old, new). `kind` is "detector" for one alternative of a
# metric-cardinality detector; those are matched against the scanner's alternatives below.
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
    ("aws cli put-metric-data", "detector", METRIC_SUITE, SCANNER,
     'r"put-metric-data|put-metric-alarm"', 'r"put-metric-alarm"'),
    ("aws cli put-metric-alarm", "detector", METRIC_SUITE, SCANNER,
     'r"put-metric-data|put-metric-alarm"', 'r"put-metric-data"'),
    ("aws cli dimension shorthand", "detector", METRIC_SUITE, SCANNER,
     r'''CLI_DIMENSION = re.compile(r"[Dd]imensions(?:[\s=\\]*[\"']?)Name=[^,\s]+,Value=\S")''',
     'CLI_DIMENSION = re.compile(r"(?!x)x")'),

    # Scanner behaviour that is not a detector alternative.
    ("dimension structural scope", "scanner", METRIC_SUITE, SCANNER,
     r"""DIMENSIONS_START = re.compile(r'["\']Dimensions["\']\s*:\s*\[')""",
     'DIMENSIONS_START = re.compile(r"(?s).*")'),
    ("dimension Name half", "scanner", METRIC_SUITE, SCANNER,
     r"""DIMENSION_NAME = re.compile(r'["\']Name["\']\s*:')""",
     'DIMENSION_NAME = re.compile(r"(?!x)x")'),
    ("dimension Value half", "scanner", METRIC_SUITE, SCANNER,
     r"""DIMENSION_VALUE = re.compile(r'["\']Value["\']\s*:')""",
     'DIMENSION_VALUE = re.compile(r"(?!x)x")'),
    ("split aws cli dimension continuation", "scanner", METRIC_SUITE, SCANNER,
     '"dimensions" in line.lower()\n                    and not CLI_DIMENSION.search(line)',
     'False\n                    and not CLI_DIMENSION.search(line)'),
    ("acknowledgement is honoured", "scanner", METRIC_SUITE, SCANNER,
     'ACK = re.compile(r"metric-budget:\\s*\\S", re.IGNORECASE)', 'ACK = re.compile(r"(?!x)x")'),
    ("acknowledgement is local", "scanner", METRIC_SUITE, SCANNER,
     "ACK_RADIUS = 3", "ACK_RADIUS = 8"),
    ("one note clears a whole publication", "scanner", METRIC_SUITE, SCANNER,
     "elif pattern_index == 1 and active_publication is not None:", "elif False:"),
    ("a block stops at the next publication", "scanner", METRIC_SUITE, SCANNER,
     "next_block += 1  # Every publication opens a new block.",
     "next_block += active_publication is None  # Reuse the prior publication's block."),
    ("blocks do not span separate edits", "scanner", METRIC_SUITE, SCANNER,
     "active_publication = None  # A publication block never crosses an added hunk.",
     "pass  # Keep the preceding hunk's publication active."),
    ("a bare dimension opens its own block", "scanner", METRIC_SUITE, SCANNER,
     """if active_publication is None:
                    next_block += 1
                    dimension_block = next_block
                else:
                    dimension_block = active_publication""",
     "dimension_block = next_block  # Reuse the prior finding's block."),
    ("only added lines are scanned", "scanner", METRIC_SUITE, SCANNER,
     'elif raw.startswith("+") and not raw.startswith("+++"):',
     'elif raw[:1] in "+-" and not raw.startswith(("+++", "---")):'),
    ("line numbers skip the no-newline marker", "scanner", METRIC_SUITE, SCANNER,
     'elif not raw.startswith(("-", "\\\\")):', 'elif True:'),
    # Drops *.sh from the paths the scanner actually uses while leaving all three copies of the
    # literal identical, so only the fixture that runs on the default paths can notice.
    ("shell files are scanned by default", "scanner", METRIC_SUITE, SCANNER,
     '"*.py *.yml *.yaml *.tf *.json *.sh").split()',
     '"*.py *.yml *.yaml *.tf *.json *.sh").split()[:5]'),
    # Consumers get the workflow's copy of the default, never the scanner's fallback, so a
    # drift here would stop shell scanning in production with every runtime fixture still green.
    ("default scan paths stay in sync", "scanner", METRIC_SUITE, METRIC_WORKFLOW,
     'default: "*.py *.yml *.yaml *.tf *.json *.sh"',
     'default: "*.py *.yml *.yaml *.tf *.json"'),

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
    ("fixture SHA errors fail the run", "fixtures", FIXTURE_SHA_SUITE, FIXTURE_SHAS,
     "if errors:", "if False:"),

    # Shared discovery. Not a guard itself -- it reaches no verdict, so `is_script` leaves it out
    # of the set above -- but each of its branches decides what one of the two checks can see, so
    # each gets the case that removes it. The suffix tuples the mutations reinstate are the exact
    # shape this replaced (issue #23); a narrowing that silently shrinks coverage now fails here.
    ("guards are found by their executable bit", "discovery", DISCOVERY_SUITE, DISCOVERY,
     "    if path.stat().st_mode & 0o111:", "    if False:"),
    ("guards are found by their shebang", "discovery", DISCOVERY_SUITE, DISCOVERY,
     '        return handle.read(2) == b"#!"', '        return path.suffix in (".py", ".sh")'),
    ("fixtures are found by content, not suffix", "discovery", DISCOVERY_SUITE, DISCOVERY,
     '        return b"\\0" not in handle.read(TEXT_PROBE_BYTES)',
     '        return path.suffix in (".py", ".sh")'),
    # Nested files used to be a decision of the fixture checker's own; it is `git ls-files` now,
    # and this is the pathspec that would stop it recursing. tests/lib holds the fixture.
    ("discovery recurses into subdirectories", "discovery", FIXTURE_SHA_SUITE, DISCOVERY,
     '"git", "ls-files", "-z", "--", subdir',
     '"git", "ls-files", "-z", "--", f":(glob){subdir}/*"'),
    # The two halves of "the switch to shape never sees less": the helper has to report what the
    # old predicate would have found, and the harness has to fail on it. Shape is not a superset
    # of suffix, so without these a decorative shebang is all that holds a guard in coverage.
    ("a shrunk guard set is reported", "discovery", DISCOVERY_SUITE, DISCOVERY,
     "        if path.suffix in LEGACY_SCRIPT_SUFFIXES", "        if False"),
    ("a shrunk guard set fails the run", "discovery", DISCOVERY_SUITE, HARNESS,
     "        for path in load_discovery().undiscovered_scripts("
     "REPO_ROOT, GUARD_ROOT, COVERED_HELPERS)",
     "        for path in ()"),
    # And the requirement that keeps the four cases above alive: drop COVERED_HELPERS from the
    # comparison and every one of them could be deleted with the build still green.
    #
    # A case whose target is this file must never spell its `old` string contiguously, because
    # `run_case` replaces the first match and the first match would be the case itself -- which
    # mutates the table and leaves the code untouched, so the case passes while proving nothing.
    # Splitting the literal keeps the text out of the source; both HARNESS cases do it.
    ("shared helpers keep their cases", "discovery", DISCOVERY_SUITE, HARNESS,
     '(("guard", guard_scripts()), '
     '("shared helper", COVERED_HELPERS))',
     '(("guard", guard_scripts()),)'),
    # S3 request-attribution audit safety guards.
    ("restore schedules precede enable schedules", "s3-audit", S3_AUDIT_SUITE, S3_AUDIT,
     "for index, source in enumerate(source_states):",
     "for index, source in enumerate([]):"),
    ("sample window has setup margin", "s3-audit", S3_AUDIT_SUITE, S3_AUDIT,
     "if starts_at < now + dt.timedelta(minutes=15):", "if False:"),
    ("destination blocks public access", "s3-audit", S3_AUDIT_SUITE, S3_AUDIT,
     "if not all(public.get(key) for key in (\"BlockPublicAcls\", \"IgnorePublicAcls\", \"BlockPublicPolicy\", \"RestrictPublicBuckets\")):",
     "if False:"),
    ("teardown rejects recursive logging", "s3-audit", S3_AUDIT_SUITE, S3_AUDIT,
     'if enabled.get("TargetBucket") == destination:', "if False:"),
    ("expected audit volume fits hard cap", "s3-audit", S3_AUDIT_SUITE, S3_AUDIT,
     'if expected_bytes > config["max_log_bytes"]:', "if False:"),
    ("schedule execution time is verified", "s3-audit", S3_AUDIT_SUITE, S3_AUDIT,
     'if actual.get("ScheduleExpression") != expression:', "if False:"),
    ("schedule input and state are verified", "s3-audit", S3_AUDIT_SUITE, S3_AUDIT,
     'if actual_input != expected_input or actual.get("State") != "ENABLED":', "if False:"),
    ("retrieved log volume stays under the hard cap", "s3-audit", S3_AUDIT_SUITE, S3_AUDIT,
     'if total_bytes > state["max_log_bytes"]:', "if False:"),
    ("retrieval cost stays under the audit budget", "s3-audit", S3_AUDIT_SUITE, S3_AUDIT,
     'if projected_cost >= state["budget_usd"]:', "if False:"),
    # Dependency installer. Guard discovery found this one uncovered: it decides whether a failed
    # `uv` run is a missing credential or a stale lockfile, and every branch of that decision
    # reaches a developer as an error message telling them what to go fix.
    ("private dependency detection", "install", INSTALL_SUITE, INSTALL,
     "  if ! has_github_git_dependency; then", "  if true; then"),
    ("authentication failures are recognised", "install", INSTALL_SUITE, INSTALL,
     "'Authentication failed|could not read Username|Invalid username or token|"
     "Repository not found|terminal prompts disabled|returned error: (401|403)'",
     "'ZZZ_NEVER_MATCHES_ANY_UV_OUTPUT'"),
    ("missing and invalid tokens are told apart", "install", INSTALL_SUITE, INSTALL,
     '  if [ -z "${REPO_READ_TOKEN:-}" ]; then', "  if true; then"),
    ("a token installs a credential helper", "install", INSTALL_SUITE, INSTALL,
     'if [ -n "${REPO_READ_TOKEN:-}" ]; then', "if false; then"),
    ("git is never allowed to prompt", "install", INSTALL_SUITE, INSTALL,
     "export GIT_TERMINAL_PROMPT=0", "export GIT_TERMINAL_PROMPT=1"),
    ("the credential helper only answers github.com", "install", INSTALL_SUITE, INSTALL,
     '  "Password for \'https://github.com\'"* | '
     '"Password for \'https://x-access-token@github.com\'"*)',
     '  "Password for "*)'),
    ("the lockfile is checked before syncing", "install", INSTALL_SUITE, INSTALL,
     "if ! run_uv uv lock --check; then", "if false; then"),
    ("the credential helper is cleaned up", "install", INSTALL_SUITE, INSTALL,
     "trap cleanup EXIT", "trap - EXIT"),
]


def detector_alternatives(scanner: Path) -> list[str]:
    """Every alternative of every metric-cardinality detector, straight from the scanner."""
    spec = importlib.util.spec_from_file_location("scanner", scanner)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    alternatives: list[str] = []
    for pattern, _reason in module.PATTERNS:
        # Top-level alternation only; these patterns nest none.
        alternatives += re.split(r"\|(?![^\[]*\])", pattern.pattern)
    return alternatives


def alternatives_disabled_by(target: str, old: str, new: str) -> list[str]:
    """Which detector alternatives a case actually removes, by applying it and looking."""
    source = (REPO_ROOT / target).read_text(encoding="utf-8")
    before = Counter(detector_alternatives(REPO_ROOT / target))
    with tempfile.TemporaryDirectory(prefix="alternatives-") as tmp:
        mutated = Path(tmp) / "scanner.py"
        mutated.write_text(source.replace(old, new, 1), encoding="utf-8")
        after = Counter(detector_alternatives(mutated))
    return sorted((before - after).elements())


def check_detector_coverage() -> list[str]:
    """Every detector alternative needs the case that disables *it*.

    Counting rows only proved the table was the right size. Eight alternatives and eight cases
    passed even when two cases disabled the same one and a third alternative went untested --
    which is the shape R2-F1 and R5-F2 both had. Identity comes from running each mutation and
    seeing which alternative disappears, so it cannot drift out of step with the labels.
    """
    errors: list[str] = []
    covered: dict[str, list[str]] = {}

    for label, kind, _suite, target, old, new in CASES:
        if kind != "detector":
            continue
        if target != SCANNER:
            errors.append(f"{label}: a detector case must mutate {SCANNER}, not {target}")
            continue
        disabled = alternatives_disabled_by(target, old, new)
        if len(disabled) != 1:
            errors.append(
                f"{label}: a detector case must disable exactly one alternative; "
                f"this one disables {len(disabled)} ({', '.join(disabled) or 'none'})"
            )
            continue
        covered.setdefault(disabled[0], []).append(label)

    for alternative, required in sorted(Counter(detector_alternatives(REPO_ROOT / SCANNER)).items()):
        labels = covered.pop(alternative, [])
        if len(labels) < required:
            errors.append(
                f"detector alternative {alternative!r} has no mutation case that disables it"
            )
        elif len(labels) > required:
            errors.append(
                f"detector alternative {alternative!r} is disabled by {len(labels)} cases "
                f"({', '.join(labels)}); some other alternative is going untested"
            )
    for alternative, labels in sorted(covered.items()):
        errors.append(
            f"{', '.join(labels)}: disables {alternative!r}, which the scanner no longer has"
        )
    return errors


def load_discovery():
    """The shared discovery helper. Loaded by path: it ships beside the guards, not on sys.path."""
    spec = importlib.util.spec_from_file_location("discovery", REPO_ROOT / DISCOVERY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def guard_scripts() -> list[str]:
    """Every guard script shipped under .github/, found rather than declared."""
    return [
        path.relative_to(REPO_ROOT).as_posix()
        for path in load_discovery().scripts_under(REPO_ROOT, GUARD_ROOT)
    ]


def check_guard_coverage() -> list[str]:
    """A guard script added under .github/ must arrive with a case, or this fails.

    The table used to name its guards and nothing else, so a third one could be added tomorrow,
    ship a pass/fail decision, and never be mutated. Discovering them from the tree instead means
    the omission fails the build rather than waiting to be noticed in review.

    Discovery by shape sees things a suffix list could not, but it is not a superset of one: three
    guards here are mode 644 and held in the set by a shebang that CI does not need, so deleting
    that line used to drop a live guard out of coverage with every suite still green. Anything the
    old predicate would have found and this one does not is reported before the coverage check
    itself, because a set that quietly got smaller makes the rest of this meaningless.
    """
    mutated = {case[3] for case in CASES}
    errors = [
        f"{path.relative_to(REPO_ROOT).as_posix()}: reads as a guard by name but has neither an "
        f"executable bit nor a shebang, so discovery no longer finds it and its coverage would go "
        f"quiet; give it one, or name it in COVERED_HELPERS if it decides nothing"
        for path in load_discovery().undiscovered_scripts(REPO_ROOT, GUARD_ROOT, COVERED_HELPERS)
    ]
    return errors + [
        f"{path}: no mutation case breaks this {role}, so nothing proves a suite would notice"
        for role, paths in (("guard", guard_scripts()), ("shared helper", COVERED_HELPERS))
        for path in paths
        if path not in mutated
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
    failures = check_guard_coverage() + check_detector_coverage()

    # The structural checks read the working tree and cost nothing; the mutations below clone and
    # run a suite each. --checks-only lets the fixtures that prove the checks bite skip that.
    if "--checks-only" in sys.argv[1:]:
        if failures:
            print("\nMutation coverage failed:\n", file=sys.stderr)
            print("\n".join(f"  {failure}" for failure in failures), file=sys.stderr)
            return 1
        print(f"Coverage checks passed for {len(guard_scripts())} guard scripts.")
        return 0

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
