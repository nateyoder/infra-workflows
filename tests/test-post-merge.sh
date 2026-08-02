#!/usr/bin/env bash

# Runs every other suite against a squash-merged clone of this repository.
#
# A suite that only ever runs on the branch it was written on cannot observe the state it will
# meet on `main`. This runs them where it counts, so "green here, red after merge" fails in the
# PR that introduces it instead of in the next unrelated one.

set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
scratch=$(mktemp -d "${TMPDIR:-/tmp}/post-merge-test.XXXXXX")
trap 'rm -rf "$scratch"' EXIT

# shellcheck source=tests/lib/post-merge-clone.sh
. "$repo_root/tests/lib/post-merge-clone.sh"

build_post_merge_clone "$repo_root" "$scratch"

# The premise of the whole exercise: the pins this repo depends on are not in this clone.
absent=0
while IFS= read -r pin; do
  [ -n "$pin" ] || continue
  git -C "$post_merge_clone" cat-file -e "${pin}^{commit}" 2>/dev/null || absent=$((absent + 1))
done < <(
  grep -rhoE 'nateyoder/infra-workflows/[^ @]+@[0-9a-f]{40}' "$post_merge_clone/.github" \
    | sed 's/.*@//' | sort -u
)
if [ "$absent" -eq 0 ]; then
  echo "post-merge fixture is not representative: every self-pin is already present" >&2
  exit 1
fi
echo "Post-merge clone built; $absent self-pin(s) unreachable from main, as after a squash merge."

failed=0
for suite in "$repo_root"/tests/test-*.sh; do
  name=$(basename "$suite")
  # Skip self: this harness builds the environment, it does not run inside it.
  [ "$name" = "test-post-merge.sh" ] && continue
  # Skip mutation testing: whether a guard's fixture bites does not depend on the checkout, and
  # re-running it here roughly doubles the job for no new signal.
  [ "$name" = "test-mutations.sh" ] && continue
  [ -f "$post_merge_clone/tests/$name" ] || continue
  printf '  %-34s' "$name"
  if (cd "$post_merge_clone" && bash "tests/$name") >"$scratch/$name.log" 2>&1; then
    echo "pass"
  else
    echo "FAIL"
    failed=$((failed + 1))
    sed 's/^/      /' "$scratch/$name.log" >&2
  fi
done

if [ "$failed" -ne 0 ]; then
  echo "$failed suite(s) fail on a squash-merged checkout" >&2
  exit 1
fi

echo "All suites pass on a squash-merged checkout."
