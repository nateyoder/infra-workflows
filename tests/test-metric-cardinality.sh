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

# The canonical dimensioned boto3 shape: the call and the dimensions nested in its payload are
# five lines apart, so before block grouping one note cleared the call and left the dimensions
# behind, and the failure message asked for a note that could not exist.
repo=$(new_repo dimensioned-block-one-acknowledgement)
start_change "$repo"
cat >"$repo/svc.py" <<'PYTHON'
# metric-budget: 1 series per recorder (~74), paged on by RecorderDepthStall
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

# A block ends where the publications stop being adjacent: nine lines apart is two blocks, and
# the note on the first one says nothing about the second.
repo=$(new_repo acknowledgement-block-is-bounded)
start_change "$repo"
cat >"$repo/svc.py" <<'PYTHON'
# metric-budget: 1 fleet series, paged on by recorder-data-loss
cw.put_metric_data(Namespace="Recorder")








cw.put_metric_data(Namespace="RecorderV2")
PYTHON
expect_fail "$repo" 'svc.py:11  publishes a metric directly'

# Adjacency is measured in the diff, not the file. These two publications are four lines apart
# but arrive as separate hunks, so the note on the first cannot clear the second.
repo=$(new_repo acknowledgement-does-not-cross-edits)
cat >"$repo/svc.py" <<'PYTHON'
import os




print("tail")
PYTHON
start_change "$repo"
cat >"$repo/svc.py" <<'PYTHON'
import os
# metric-budget: 1 fleet series, paged on by recorder-data-loss
cw.put_metric_data(Namespace="Recorder")



cw.put_metric_data(Namespace="RecorderV2")
print("tail")
PYTHON
expect_fail "$repo" 'svc.py:7  publishes a metric directly'

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
