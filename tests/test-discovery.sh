#!/usr/bin/env bash

# The two checks that discover their own inputs must find files by shape, not by name.
#
# Both narrowed discovery with a suffix tuple, which is the hand-maintained list they were written
# to replace, one rung up: an extensionless guard under .github/ and a .yml fixture hard-coding a
# repository commit were each invisible while the check reported success (issue #23). One fixture
# per branch of the shared predicate, so re-narrowing discovery fails here instead of in review.

set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
scratch=$(mktemp -d "${TMPDIR:-/tmp}/discovery-test.XXXXXX")
trap 'rm -rf "$scratch"' EXIT

# The baseline the cases below are measured against: on an untouched tree both checks pass, so a
# failure there is the file that was added and not a predicate that now flags everything.
python3 "$repo_root/tests/mutation-cases.py" --checks-only
python3 "$repo_root/.github/scripts/verify-fixture-shas.py"

# Each case adds a file the old suffix allowlist could not see and requires the check to see it.
# The file is committed to the index, because that is where discovery looks and an untracked file
# is not shipped.
expect_discovered() {
  local name=$1 expected=$2
  shift 2

  if (cd "$scratch/$name" && "$@") >"$scratch/$name.log" 2>&1; then
    echo "discovery missed $name" >&2
    exit 1
  fi
  grep -F "$expected" "$scratch/$name.log" >/dev/null || {
    cat "$scratch/$name.log" >&2
    echo "missing expected failure for $name: $expected" >&2
    exit 1
  }
}

# A guard whose name carries no suffix at all, run by its shebang. This is the verified miss:
# `.github/scripts/verify-b` shipped a pass/fail decision and coverage reported exit 0.
git clone -q --shared "$repo_root" "$scratch/shebang-guard"
printf '#!/usr/bin/env bash\nexit 0\n' >"$scratch/shebang-guard/.github/scripts/verify-b"
git -C "$scratch/shebang-guard" add .github/scripts/verify-b
expect_discovered shebang-guard 'verify-b: no mutation case breaks this guard' \
  python3 tests/mutation-cases.py --checks-only

# The other half of "is a script": the executable bit, with no shebang to fall back on.
git clone -q --shared "$repo_root" "$scratch/executable-guard"
printf 'exit 0\n' >"$scratch/executable-guard/.github/scripts/verify-c"
chmod +x "$scratch/executable-guard/.github/scripts/verify-c"
git -C "$scratch/executable-guard" add .github/scripts/verify-c
expect_discovered executable-guard 'verify-c: no mutation case breaks this guard' \
  python3 tests/mutation-cases.py --checks-only

# A fixture is whatever a fixture is written in. A borrowed commit rots in YAML exactly as it does
# in shell, and the checker used to walk straight past this file.
git clone -q --shared "$repo_root" "$scratch/yaml-fixture"
mkdir -p "$scratch/yaml-fixture/tests/fixtures"
printf 'pin: %s\n' "$(git -C "$scratch/yaml-fixture" rev-parse HEAD)" \
  >"$scratch/yaml-fixture/tests/fixtures/pin.yml"
git -C "$scratch/yaml-fixture" add tests/fixtures/pin.yml
expect_discovered yaml-fixture 'tests/fixtures/pin.yml' \
  python3 .github/scripts/verify-fixture-shas.py

echo "Discovery finds guards by shape and fixtures by content."
