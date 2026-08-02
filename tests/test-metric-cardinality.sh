#!/usr/bin/env bash

set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
guard="$repo_root/.github/actions/metric-cardinality/check-metric-cardinality.py"
fixture_root=$(mktemp -d "${TMPDIR:-/tmp}/metric-cardinality-test.XXXXXX")
trap 'rm -rf "$fixture_root"' EXIT

new_repo() {
  local name=$1
  local repo="$fixture_root/$name"

  mkdir -p "$repo"
  git -C "$repo" init -q -b main
  git -C "$repo" config user.email test@example.com
  git -C "$repo" config user.name "Metric Guard Test"
  git -C "$repo" config commit.gpgsign false
  printf '%s\n' "$repo"
}

start_change() {
  local repo=$1
  git -C "$repo" add .
  git -C "$repo" commit -qm base --allow-empty
  git -C "$repo" switch -qc change
}

expect_pass() {
  local repo=$1
  local output="$repo/guard-output"

  git -C "$repo" add -A
  git -C "$repo" commit -qm change
  if ! (cd "$repo" && BASE_REF=main SCAN_PATHS='*.py' python3 "$guard") >"$output" 2>&1; then
    cat "$output" >&2
    echo "expected metric guard to pass in $repo" >&2
    exit 1
  fi
  grep -F 'No new CloudWatch metric cardinality.' "$output" >/dev/null
}

expect_fail() {
  local repo=$1
  local expected=$2
  local output="$repo/guard-output"

  git -C "$repo" add -A
  git -C "$repo" commit -qm change
  if (cd "$repo" && BASE_REF=main SCAN_PATHS='*.py' python3 "$guard") >"$output" 2>&1; then
    echo "expected metric guard to fail in $repo" >&2
    exit 1
  fi
  grep -F "$expected" "$output" >/dev/null || {
    cat "$output" >&2
    echo "missing expected finding: $expected" >&2
    exit 1
  }
}

repo=$(new_repo single-line-dimension)
start_change "$repo"
cat >"$repo/svc.py" <<'PYTHON'
dimensions = [{"Name": "Producer", "Value": producer_id}]
PYTHON
expect_fail "$repo" 'svc.py:1  adds a metric dimension entry'

repo=$(new_repo acknowledged-added-line)
start_change "$repo"
cat >"$repo/svc.py" <<'PYTHON'
dimensions = [{"Name": "Producer", "Value": producer_id}]  # metric-budget: 74 fleet series
PYTHON
expect_pass "$repo"

repo=$(new_repo unrelated-change)
start_change "$repo"
cat >"$repo/svc.py" <<'PYTHON'
print("ordinary change")
PYTHON
expect_pass "$repo"

repo=$(new_repo removal-only)
cat >"$repo/svc.py" <<'PYTHON'
cw.put_metric_data(Namespace="Recorder")
PYTHON
start_change "$repo"
: >"$repo/svc.py"
expect_pass "$repo"

repo=$(new_repo multiline-dimension)
cat >"$repo/svc.py" <<'PYTHON'
def dimensions():
    return [
    ]
PYTHON
start_change "$repo"
cat >"$repo/svc.py" <<'PYTHON'
def dimensions():
    return [
        {
            "Name": "DeploySha",
            "Value": os.environ["DEPLOY_SHA"],
        },
    ]
PYTHON
expect_fail "$repo" 'svc.py:4  adds a metric dimension entry'

# Each source isolates one PATTERNS alternative so removing that alternative makes its case pass.
while IFS='|' read -r name source expected; do
  repo=$(new_repo "$name")
  start_change "$repo"
  printf '%s\n' "$source" >"$repo/svc.py"
  expect_fail "$repo" "svc.py:1  $expected"
done <<'DETECTOR_CASES'
put-metric-data-client|cloudwatch.PutMetricData(MetricData=[])|publishes a metric directly
dimensions-literal|payload = {"Dimensions": [dimension]}|adds a non-empty Dimensions list
per-stream-metrics|metrics = _PER_STREAM_METRICS|adds a per-producer metric tier
per-service-metrics|metrics = _PER_SERVICE_METRICS|adds a per-producer metric tier
alarm-bound-metrics|metrics = _ALARM_BOUND|adds a published metric name
cloudwatch-metric-alarm|resource "aws_cloudwatch_metric_alarm" "latency" {}|adds a metric or alarm
metric-name-assignment|MetricName = "Latency"|adds a metric or alarm
DETECTOR_CASES

repo=$(new_repo committed-acknowledgement)
cat >"$repo/svc.py" <<'PYTHON'
# metric-budget: 1 fleet series, paged on by recorder-data-loss
cw.put_metric_data(Namespace="Recorder")
PYTHON
start_change "$repo"
cat >"$repo/svc.py" <<'PYTHON'
# metric-budget: 1 fleet series, paged on by recorder-data-loss
cw.put_metric_data(Namespace="RecorderV2")
PYTHON
expect_pass "$repo"

repo=$(new_repo distant-acknowledgement)
start_change "$repo"
cat >"$repo/svc.py" <<'PYTHON'
"""metric-budget: one documented module."""





cw.put_metric_data(Namespace="Recorder")
cw.put_metric_data(Namespace="Recorder", MetricData=[{"Name": "DeploySha", "Value": sha}])
PYTHON
expect_fail "$repo" 'svc.py:7  publishes a metric directly'

# An acknowledged non-publication finding must not clear a later bare dimension entry. Without
# its own block, the deploy-SHA dimension would inherit the acknowledgement beyond ACK_RADIUS.
repo=$(new_repo bare-dimension-opens-own-block)
start_change "$repo"
cat >"$repo/svc.py" <<'PYTHON'
# metric-budget: 1 fleet series, paged on by RecorderDepthStall
RECORDER_ALARM_BOUND = "book_depth"
pad_a = 1
pad_b = 2
pad_c = 3
pad_d = 4
EXTRA_DIMS = [
    {"Name": "deploy_sha", "Value": sha},
]
PYTHON
expect_fail "$repo" 'svc.py:8  adds a metric dimension entry'

# A realistic dimensioned boto3 datum: the call and dimensions are eight lines apart once the
# API-required Value and ordinary Unit/Timestamp fields are present. Their shared publication,
# rather than a tuned line gap, lets one note acknowledge both.
repo=$(new_repo dimensioned-block-one-acknowledgement)
start_change "$repo"
cat >"$repo/svc.py" <<'PYTHON'
# metric-budget: 1 series per recorder (~74), paged on by RecorderDepthStall
cw.put_metric_data(
    Namespace="xp/recorder",
    MetricData=[
        {
            "MetricName": "book_depth",
            "Unit": "Count",
            "Value": depth,
            "Timestamp": now,
            "Dimensions": [{"Name": "recorder_id", "Value": rid}],
        }
    ],
)
PYTHON
expect_pass "$repo"

# The same block with no note at all still fails, on every line of it.
repo=$(new_repo dimensioned-block-unacknowledged)
start_change "$repo"
cat >"$repo/svc.py" <<'PYTHON'
cw.put_metric_data(
    Namespace="xp/recorder",
    MetricData=[
        {
            "MetricName": "book_depth",
            "Dimensions": [{"Name": "recorder_id", "Value": rid}],
        }
    ],
)
PYTHON
expect_fail "$repo" 'svc.py:6  adds a non-empty Dimensions list'

# A chain of short gaps must not let one note flow through later publications. Every call opens a
# new block even when the calls are closer than the old six-line threshold.
repo=$(new_repo acknowledgement-block-is-bounded)
start_change "$repo"
cat >"$repo/svc.py" <<'PYTHON'
# metric-budget: 1 fleet series, paged on by recorder-data-loss
cw.put_metric_data(Namespace="Recorder")
x = 1
y = 2
cw.put_metric_data(Namespace="RecorderV2")
x = 3
y = 4
cw.put_metric_data(Namespace="RecorderV3")
PYTHON
expect_fail "$repo" 'svc.py:5  publishes a metric directly'

# A nested dimension added in a later hunk cannot join a publication from an earlier edit.
repo=$(new_repo acknowledgement-does-not-cross-edits)
cat >"$repo/svc.py" <<'PYTHON'
import os
x = 1
y = 2
payload = {
}
print("tail")
PYTHON
start_change "$repo"
cat >"$repo/svc.py" <<'PYTHON'
import os
# metric-budget: 1 fleet series, paged on by recorder-data-loss
cw.put_metric_data(Namespace="Recorder")
x = 1
y = 2
payload = {
    "Dimensions": [{"Name": "recorder_id", "Value": rid}],
}
print("tail")
PYTHON
expect_fail "$repo" 'svc.py:7  adds a non-empty Dimensions list'

repo=$(new_repo no-trailing-newline-acknowledged)
printf '%s' '# metric-budget: 1 fleet series, paged on by recorder-data-loss


cw.put_metric_data(Namespace="Recorder")' >"$repo/svc.py"
start_change "$repo"
printf '%s' '# metric-budget: 1 fleet series, paged on by recorder-data-loss


cw.put_metric_data(Namespace="RecorderV2")' >"$repo/svc.py"
expect_pass "$repo"

repo=$(new_repo no-trailing-newline-line-number)
printf '%s' '# unacknowledged metric


cw.put_metric_data(Namespace="Recorder")' >"$repo/svc.py"
start_change "$repo"
printf '%s' '# unacknowledged metric


cw.put_metric_data(Namespace="RecorderV2")' >"$repo/svc.py"
expect_fail "$repo" 'svc.py:4  publishes a metric directly'
