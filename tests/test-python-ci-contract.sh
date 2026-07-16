#!/usr/bin/env bash

set -euo pipefail

workflow=.github/workflows/python-ci.yml

require() {
  grep -F -- "$1" "$workflow" >/dev/null || {
    echo "missing workflow contract: $1" >&2
    exit 1
  }
}

require_default() {
  local input=$1
  local expected=$2

  awk -v input="      $input:" -v expected="        default: $expected" '
    $0 == input { in_input = 1; next }
    in_input && /^      [[:alnum:]-]+:$/ { in_input = 0 }
    in_input && $0 == expected { found = 1 }
    END { exit !found }
  ' "$workflow" || {
    echo "missing default for $input: $expected" >&2
    exit 1
  }
}

require_count() {
  local expected=$1
  local text=$2
  local count
  count=$(grep -F -c -- "$text" "$workflow" || true)
  [ "$count" -eq "$expected" ] || {
    echo "expected $expected occurrences of: $text (found $count)" >&2
    exit 1
  }
}

require_file() {
  local file=$1
  local text=$2
  grep -F -- "$text" "$file" >/dev/null || {
    echo "missing contract in $file: $text" >&2
    exit 1
  }
}

forbid() {
  if grep -F -- "$1" "$workflow" >/dev/null; then
    echo "forbidden workflow behavior: $1" >&2
    exit 1
  fi
}

# Defaults preserve the existing full-quality gate for callers that do not
# opt in to the faster draft mode.
require 'coverage:'
require 'test-workers:'
require 'pytest-args:'
require_default coverage true
require_default test-workers 0
require_default pytest-args '""'
require_default test-timeout-minutes 15
require 'Run linter and formatter'
require 'Run type checker'
require 'Run tests'
require 'if: ${{ inputs.run-tests || inputs.run-lint || inputs.run-type-check }}'
require 'timeout-minutes: ${{ inputs.test-timeout-minutes }}'

# Draft callers must be able to omit coverage while retaining the test suite.
require '[ "${{ inputs.coverage }}" = "true" ]'
require 'if: ${{ always() && inputs.run-tests && inputs.coverage }}'
require 'args+=(--no-cov)'

# Workers are opt-in, so projects without pytest-xdist retain serial pytest.
require 'if [ "${{ inputs.test-workers }}" -gt 0 ]'
require '--dist=worksteal'
grep -F -- 'pytest-xdist>=3.2.0' README.md >/dev/null || {
  echo 'missing pytest-xdist worksteal requirement' >&2
  exit 1
}

# Tests-only projects must still be linted and type checked.
require_count 2 'if [ -d "${{ inputs.tests-path }}" ] && { [ -z "$src_real" ] ||'

# The reusable execution chain is immutable and uses the declared uv version.
forbid '@v1'
require_count 2 'nateyoder/infra-workflows/.github/actions/setup-python-env@789efce74cd52d896c6ab4c64214b737283c29d9'
require_file .github/actions/setup-python-env/action.yml 'actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0 # v7'
require_file .github/actions/setup-python-env/action.yml 'nateyoder/infra-workflows/.github/actions/checkout-editable-deps@a326b975a3ea40c809ad1d9e46b2cab584445491'
require_file .github/actions/setup-python-env/action.yml 'astral-sh/setup-uv@fac544c07dec837d0ccb6301d7b5580bf5edae39 # v8.2.0'
require_file .github/actions/setup-python-env/action.yml 'version: "0.11.28"'

# A shared workflow should not need write permission or a third-party PR bot.
forbid 'pull-requests: write'
forbid 'pytest-coverage-comment'
