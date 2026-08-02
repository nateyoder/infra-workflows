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

git init -q --bare "$fixture_root/origin.git"
feature_head=$(git -C "$repo_root" rev-parse HEAD)
base_head=$(git -C "$repo_root" rev-parse origin/main)
feature_tree=$(git -C "$repo_root" rev-parse HEAD^{tree})
fixture_origin="file://$fixture_root/origin.git"
git -C "$repo_root" push -q "$fixture_origin" \
  "$base_head:refs/heads/base"
git -C "$repo_root" push -q "$fixture_origin" \
  "$feature_head:refs/pull/fixture/head"
pin_index=0
while IFS= read -r self_pin; do
  pin_index=$((pin_index + 1))
  git -C "$repo_root" push -q "$fixture_origin" \
    "$self_pin:refs/pull/fixture/pin-$pin_index"
done < <(
  grep -rhoE 'nateyoder/infra-workflows/[^ @]+@[0-9a-f]{40}' \
    "$repo_root/.github" | sed 's/.*@//' | sort -u
)
squash_head=$(
  git -c user.name=Fixture -c user.email=fixture@example.invalid \
    --git-dir="$fixture_root/origin.git" commit-tree "$feature_tree" \
    -p "$base_head" -m 'synthetic squash merge'
)
git --git-dir="$fixture_root/origin.git" update-ref refs/heads/main "$squash_head"
git --git-dir="$fixture_root/origin.git" update-ref -d refs/heads/base

git clone -q --single-branch --branch main "$fixture_origin" \
  "$fixture_root/post-merge"
metric_pin=$(
  sed -n 's|.*metric-cardinality@\([0-9a-f]\{40\}\).*|\1|p' \
    "$fixture_root/post-merge/.github/workflows/metric-cardinality.yml"
)
if git -C "$fixture_root/post-merge" cat-file -e "$metric_pin^{commit}" 2>/dev/null; then
  echo "post-merge fixture unexpectedly contains the self-pinned commit" >&2
  exit 1
fi
(
  cd "$fixture_root/post-merge"
  python3 .github/scripts/verify-action-pins.py
) >"$fixture_root/post-merge.log" 2>&1
grep -F 'Verified 11 immutable external uses entries and 4 self-pins.' \
  "$fixture_root/post-merge.log" >/dev/null
git -C "$fixture_root/post-merge" cat-file -e \
  "$metric_pin:.github/actions/metric-cardinality"

echo "Action pin verifier passed positive and mutation tests."
