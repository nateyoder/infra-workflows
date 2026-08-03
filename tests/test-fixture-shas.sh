#!/usr/bin/env bash

set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
checker="$repo_root/.github/scripts/verify-fixture-shas.py"
fixture_root=$(mktemp -d "${TMPDIR:-/tmp}/fixture-sha-test.XXXXXX")
trap 'rm -rf "$fixture_root"' EXIT

python3 "$checker"

# A literal naming a commit in this repository is what rots: it resolves today because some
# branch still reaches it, and stops resolving the moment that branch is deleted.
git clone -q --shared "$repo_root" "$fixture_root/hard-coded"
(
  cd "$fixture_root/hard-coded"
  printf '\n# stale_sha=%s\n' "$(git rev-parse HEAD)" >>tests/test-action-pins.sh
)
if (
  cd "$fixture_root/hard-coded"
  python3 .github/scripts/verify-fixture-shas.py
) >"$fixture_root/hard-coded.log" 2>&1; then
  echo "fixture SHA checker accepted a hard-coded repository commit" >&2
  exit 1
fi
grep -F 'hard-codes a commit from this repository' "$fixture_root/hard-coded.log" >/dev/null

# The same shape one directory down, because fixtures live in tests/lib too.
git clone -q --shared "$repo_root" "$fixture_root/nested"
(
  cd "$fixture_root/nested"
  printf '\n# stale_sha=%s\n' "$(git rev-parse HEAD)" >>tests/lib/post-merge-clone.sh
)
if (
  cd "$fixture_root/nested"
  python3 .github/scripts/verify-fixture-shas.py
) >"$fixture_root/nested.log" 2>&1; then
  echo "fixture SHA checker did not scan tests/lib" >&2
  exit 1
fi
grep -F 'tests/lib/post-merge-clone.sh' "$fixture_root/nested.log" >/dev/null

# Git resolves uppercase hexadecimal abbreviations too, so pasted SHAs cannot bypass discovery.
git clone -q --shared "$repo_root" "$fixture_root/uppercase"
uppercase_sha=$(git -C "$fixture_root/uppercase" rev-parse HEAD | tr '[:lower:]' '[:upper:]')
printf '\n# stale_sha=%s\n' "$uppercase_sha" \
  >>"$fixture_root/uppercase/tests/test-action-pins.sh"
if (
  cd "$fixture_root/uppercase"
  python3 .github/scripts/verify-fixture-shas.py
) >"$fixture_root/uppercase.log" 2>&1; then
  echo "fixture SHA checker accepted an uppercase repository commit" >&2
  exit 1
fi
grep -F "hard-codes a commit from this repository: $uppercase_sha" \
  "$fixture_root/uppercase.log" >/dev/null

# A hex literal git cannot resolve here -- a third-party action pin, an unreachable sentinel --
# carries none of that risk and must stay allowed, or every pin fixture becomes unwritable.
git clone -q --shared "$repo_root" "$fixture_root/foreign"
{
  printf '\n# third-party pin: 9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0\n'
  printf '# sentinel: 0000000000000000000000000000000000000000\n'
} >>"$fixture_root/foreign/tests/test-action-pins.sh"
if ! (
  cd "$fixture_root/foreign"
  python3 .github/scripts/verify-fixture-shas.py
) >"$fixture_root/foreign.log" 2>&1; then
  cat "$fixture_root/foreign.log" >&2
  echo "fixture SHA checker rejected a hex literal that is not a commit here" >&2
  exit 1
fi

echo "Fixture SHA checker passed positive and negative tests."
