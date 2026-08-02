#!/usr/bin/env bash

set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

python3 "$repo_root/.github/scripts/s3-request-audit.py" validate \
  --config "$repo_root/audits/s3-request-attribution.json"
python3 "$repo_root/tests/test-s3-request-audit.py" -v
