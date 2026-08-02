#!/usr/bin/env bash

# Requires every guard branch to have a fixture that fails when the branch stops working.
# See tests/mutation-cases.py for what is covered and why.

set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
scratch=$(mktemp -d "${TMPDIR:-/tmp}/mutation-coverage-test.XXXXXX")
trap 'rm -rf "$scratch"' EXIT

# The two structural checks are what stop the table rotting, so they need fixtures of their own.
# --checks-only reads the working tree and skips the per-case mutations, which these do not need.
expect_coverage_failure() {
  local name=$1 expected=$2

  if (
    cd "$scratch/$name"
    python3 tests/mutation-cases.py --checks-only
  ) >"$scratch/$name.log" 2>&1; then
    echo "mutation coverage accepted $name" >&2
    exit 1
  fi
  grep -F "$expected" "$scratch/$name.log" >/dev/null || {
    cat "$scratch/$name.log" >&2
    echo "missing expected coverage failure: $expected" >&2
    exit 1
  }
}

# A guard script can be added under .github/ tomorrow; nothing used to require covering it.
git clone -q --shared "$repo_root" "$scratch/new-guard"
printf '#!/usr/bin/env python3\nraise SystemExit(0)\n' \
  >"$scratch/new-guard/.github/scripts/verify-nothing.py"
expect_coverage_failure new-guard 'no mutation case breaks this guard'

# A new detector alternative must arrive with the case that disables it.
git clone -q --shared "$repo_root" "$scratch/new-detector"
perl -pi -e 's|r"_ALARM_BOUND"|r"_ALARM_BOUND\|_BUDGET_BOUND"|' \
  "$scratch/new-detector/.github/actions/metric-cardinality/check-metric-cardinality.py"
expect_coverage_failure new-detector "detector alternative '_BUDGET_BOUND' has no mutation case"

# The hole that counting left open: the right number of cases disabling the wrong set of
# alternatives. After this rewrite two cases disable put_metric_data and none disables
# PutMetricData, which the old count check accepted.
git clone -q --shared "$repo_root" "$scratch/miscovered-detector"
python3 - "$scratch/miscovered-detector/tests/mutation-cases.py" <<'PYTHON'
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
source = path.read_text(encoding="utf-8")
old = """    ("PutMetricData", "detector", METRIC_SUITE, SCANNER,
     'r"put_metric_data|PutMetricData"', 'r"put_metric_data"'),"""
new = """    ("PutMetricData", "detector", METRIC_SUITE, SCANNER,
     'r"put_metric_data|PutMetricData"', 'r"PutMetricData"'),"""
if old not in source:
    raise SystemExit("the case this fixture rewrites has moved; update the fixture")
path.write_text(source.replace(old, new, 1), encoding="utf-8")
PYTHON
expect_coverage_failure miscovered-detector \
  "detector alternative 'PutMetricData' has no mutation case"

python3 "$repo_root/tests/mutation-cases.py"
