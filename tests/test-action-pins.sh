#!/usr/bin/env bash

set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
verifier="$repo_root/.github/scripts/verify-action-pins.py"
fixture_root=$(mktemp -d "${TMPDIR:-/tmp}/action-pin-test.XXXXXX")
trap 'rm -rf "$fixture_root"' EXIT

python3 "$verifier"

git clone -q --shared "$repo_root" "$fixture_root/content-drift"
printf '\n# simulated unpinned payload change\n' \
  >>"$fixture_root/content-drift/.github/actions/metric-cardinality/action.yml"
if (
  cd "$fixture_root/content-drift"
  python3 .github/scripts/verify-action-pins.py
) >"$fixture_root/content-drift.log" 2>&1; then
  echo "self-pin verifier accepted divergent action content" >&2
  exit 1
fi
grep -F 'self-pin content mismatch' "$fixture_root/content-drift.log" >/dev/null

git clone -q --shared "$repo_root" "$fixture_root/stale-self-pin"
perl -pi -e \
  's|(metric-cardinality@)[0-9a-f]{40}|${1}087da8d7a68bcf2d72ba0cff7674198f53df18cc|' \
  "$fixture_root/stale-self-pin/.github/workflows/metric-cardinality.yml"
if (
  cd "$fixture_root/stale-self-pin"
  python3 .github/scripts/verify-action-pins.py
) >"$fixture_root/stale-self-pin.log" 2>&1; then
  echo "action pin verifier accepted a stale self-pin" >&2
  exit 1
fi
grep -F 'self-pin content mismatch' "$fixture_root/stale-self-pin.log" >/dev/null

git clone -q --shared "$repo_root" "$fixture_root/floating-ref"
perl -pi -e 's|actions/checkout@[0-9a-f]{40}|actions/checkout\@v7|' \
  "$fixture_root/floating-ref/.github/workflows/test-python-ci-contract.yml"
if (
  cd "$fixture_root/floating-ref"
  python3 .github/scripts/verify-action-pins.py
) >"$fixture_root/floating-ref.log" 2>&1; then
  echo "action pin verifier accepted a floating third-party ref" >&2
  exit 1
fi
grep -F 'external action is not pinned by full 40-character SHA' \
  "$fixture_root/floating-ref.log" >/dev/null

echo "Action pin verifier passed positive and mutation tests."
