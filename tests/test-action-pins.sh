#!/usr/bin/env bash

set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
verifier="$repo_root/.github/scripts/verify-action-pins.py"
fixture_root=$(mktemp -d "${TMPDIR:-/tmp}/action-pin-test.XXXXXX")
trap 'rm -rf "$fixture_root"' EXIT

# shellcheck source=tests/lib/post-merge-clone.sh
. "$repo_root/tests/lib/post-merge-clone.sh"

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

# The stale commit is built here rather than hard-coded, so the case does not depend on any
# particular SHA still being reachable. A hard-coded historical SHA passes on the branch that
# happens to contain it and fails everywhere else.
git clone -q --shared "$repo_root" "$fixture_root/stale-self-pin"
(
  cd "$fixture_root/stale-self-pin"
  printf '\n# divergent payload present only in the pinned commit\n' \
    >>.github/actions/metric-cardinality/action.yml
  git add -A
  git -c user.name=Fixture -c user.email=fixture@example.invalid commit -qm 'divergent action'
  stale_sha=$(git rev-parse HEAD)
  git reset -q --hard HEAD~1
  perl -pi -e "s|(metric-cardinality\@)[0-9a-f]{40}|\${1}$stale_sha|" \
    .github/workflows/metric-cardinality.yml
)
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

git clone -q --shared "$repo_root" "$fixture_root/missing-ref"
perl -pi -e 's|actions/checkout@[0-9a-f]{40}|actions/checkout|' \
  "$fixture_root/missing-ref/.github/workflows/test-python-ci-contract.yml"
if (
  cd "$fixture_root/missing-ref"
  python3 .github/scripts/verify-action-pins.py
) >"$fixture_root/missing-ref.log" 2>&1; then
  echo "action pin verifier accepted a third-party action without a ref" >&2
  exit 1
fi
grep -F 'external action has no immutable ref' "$fixture_root/missing-ref.log" >/dev/null

git clone -q --shared "$repo_root" "$fixture_root/short-sha"
perl -pi -e 's|actions/checkout@[0-9a-f]{40}|actions/checkout\@9c091bb|' \
  "$fixture_root/short-sha/.github/workflows/test-python-ci-contract.yml"
if (
  cd "$fixture_root/short-sha"
  python3 .github/scripts/verify-action-pins.py
) >"$fixture_root/short-sha.log" 2>&1; then
  echo "action pin verifier accepted an abbreviated third-party SHA" >&2
  exit 1
fi
grep -F 'external action is not pinned by full 40-character SHA' \
  "$fixture_root/short-sha.log" >/dev/null

git clone -q --shared "$repo_root" "$fixture_root/unavailable-self-pin"
perl -pi -e \
  's|(metric-cardinality@)[0-9a-f]{40}|${1}0000000000000000000000000000000000000000|' \
  "$fixture_root/unavailable-self-pin/.github/workflows/metric-cardinality.yml"
if (
  cd "$fixture_root/unavailable-self-pin"
  python3 .github/scripts/verify-action-pins.py
) >"$fixture_root/unavailable-self-pin.log" 2>&1; then
  echo "action pin verifier accepted an unavailable self-pin" >&2
  exit 1
fi
grep -F 'unable to verify self-pin' "$fixture_root/unavailable-self-pin.log" >/dev/null
if grep -F 'self-pin content mismatch' \
  "$fixture_root/unavailable-self-pin.log" >/dev/null; then
  echo "unavailable self-pin was misreported as a content mismatch" >&2
  exit 1
fi

build_post_merge_clone "$repo_root" "$fixture_root"
metric_pin=$(
  sed -n 's|.*metric-cardinality@\([0-9a-f]\{40\}\).*|\1|p' \
    "$post_merge_clone/.github/workflows/metric-cardinality.yml"
)
if git -C "$post_merge_clone" cat-file -e "$metric_pin^{commit}" 2>/dev/null; then
  echo "post-merge fixture unexpectedly contains the self-pinned commit" >&2
  exit 1
fi
(
  cd "$post_merge_clone"
  python3 .github/scripts/verify-action-pins.py
) >"$fixture_root/post-merge.log" 2>&1
grep -F 'Verified 11 immutable external uses entries and 4 self-pins.' \
  "$fixture_root/post-merge.log" >/dev/null
git -C "$post_merge_clone" cat-file -e \
  "$metric_pin:.github/actions/metric-cardinality"

echo "Action pin verifier passed positive and mutation tests."
