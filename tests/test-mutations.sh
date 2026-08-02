#!/usr/bin/env bash

# Requires every guard branch to have a fixture that fails when the branch stops working.
# See tests/mutation-cases.py for what is covered and why.

set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
python3 "$repo_root/tests/mutation-cases.py"
