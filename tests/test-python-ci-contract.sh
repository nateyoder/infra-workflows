#!/usr/bin/env bash

set -euo pipefail

workflow=.github/workflows/python-ci.yml

require() {
  grep -F -- "$1" "$workflow" >/dev/null || {
    echo "missing workflow contract: $1" >&2
    exit 1
  }
}

forbid() {
  grep -F -- "$1" "$workflow" >/dev/null && {
    echo "forbidden workflow behavior: $1" >&2
    exit 1
  }
}

# Defaults preserve the existing full-quality gate for callers that do not
# opt in to the faster draft mode.
require 'coverage:'
require 'default: true'
require 'test-workers:'
require 'default: 0'
require 'pytest-args:'
require 'Run linter and formatter'
require 'Run type checker'
require 'Run tests'
require 'if: ${{ inputs.run-tests || inputs.run-lint || inputs.run-type-check }}'

# Draft callers must be able to omit coverage while retaining the test suite.
require '[ "${{ inputs.coverage }}" = "true" ]'
require 'if: ${{ always() && inputs.run-tests && inputs.coverage }}'

# Workers are opt-in, so projects without pytest-xdist retain serial pytest.
require 'if [ "${{ inputs.test-workers }}" -gt 0 ]'
require '--dist=worksteal'

# A shared workflow should not need write permission or a third-party PR bot.
forbid 'pull-requests: write'
forbid 'pytest-coverage-comment'
