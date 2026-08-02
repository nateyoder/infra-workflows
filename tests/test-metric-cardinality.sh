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

# `scan` is a SCAN_PATHS value, or the literal `default` to leave SCAN_PATHS unset so the
# scanner's own fallback applies. Callers that omit it keep the original *.py behaviour.
run_guard() {
  local repo=$1
  local scan=$2
  local output="$repo/guard-output"

  git -C "$repo" add -A
  git -C "$repo" commit -qm change
  if [ "$scan" = default ]; then
    (cd "$repo" && BASE_REF=main python3 "$guard") >"$output" 2>&1
  else
    (cd "$repo" && BASE_REF=main SCAN_PATHS="$scan" python3 "$guard") >"$output" 2>&1
  fi
}

expect_pass() {
  local repo=$1
  local scan=${2:-'*.py'}
  local output="$repo/guard-output"

  if ! run_guard "$repo" "$scan"; then
    cat "$output" >&2
    echo "expected metric guard to pass in $repo" >&2
    exit 1
  fi
  grep -F 'No new CloudWatch metric cardinality.' "$output" >/dev/null
}

expect_fail() {
  local repo=$1
  local expected=$2
  local scan=${3:-'*.py'}
  local output="$repo/guard-output"

  if run_guard "$repo" "$scan"; then
    echo "expected metric guard to fail in $repo" >&2
    exit 1
  fi
  grep -F "$expected" "$output" >/dev/null || {
    cat "$output" >&2
    echo "missing expected finding: $expected" >&2
    exit 1
  }
}

# The default scan paths exist in three places, and consumers of the reusable workflow get the
# workflow's copy -- the scanner's fallback never runs for them. A drift would therefore scan
# different paths in production than every fixture below exercises, so *.sh could silently stop
# being scanned while this suite stayed green.
extract_default() {
  sed -n 's/.*"\(\*\.py[^"]*\)".*/\1/p' "$1" | head -1
}

scanner_default=$(extract_default "$guard")
action_default=$(extract_default "$repo_root/.github/actions/metric-cardinality/action.yml")
workflow_default=$(extract_default "$repo_root/.github/workflows/metric-cardinality.yml")

if [ -z "$scanner_default" ]; then
  echo "could not extract the scanner's default scan paths" >&2
  exit 1
fi
if [ "$scanner_default" != "$action_default" ] || [ "$scanner_default" != "$workflow_default" ]; then
  echo "the three copies of the default scan paths disagree:" >&2
  echo "  scanner:  $scanner_default" >&2
  echo "  action:   $action_default" >&2
  echo "  workflow: $workflow_default" >&2
  exit 1
fi
case "$scanner_default" in
  *'*.sh'*) ;;
  *)
    echo "default scan paths must include *.sh: $scanner_default" >&2
    exit 1
    ;;
esac

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

# --- AWS CLI shapes (issue #11) -------------------------------------------------------------

# Each source isolates one AWS CLI detector alternative, so removing that alternative makes
# the case pass. Keep them free of the other CLI shapes or the mutations stop biting.
while IFS='|' read -r name source expected; do
  repo=$(new_repo "$name")
  start_change "$repo"
  printf '%s\n' "$source" >"$repo/svc.sh"
  expect_fail "$repo" "svc.sh:1  $expected" '*.sh'
done <<'CLI_DETECTOR_CASES'
cli-put-metric-data|aws cloudwatch put-metric-data --namespace App/Foo --metric-name Uploads --value 1|publishes a metric or alarm with the AWS CLI
cli-put-metric-alarm|aws cloudwatch put-metric-alarm --alarm-name high-latency --threshold 1|publishes a metric or alarm with the AWS CLI
cli-dimension-shorthand|dimensions="Name=StationId,Value=${STATION_ID}"|adds an AWS CLI metric dimension
CLI_DETECTOR_CASES

# The blind spot exactly as reported: a realistic multi-line alarm provisioning command, whose
# --dimensions sits four continuation lines away from its subcommand.
repo=$(new_repo aws-cli-alarm-provisioning)
start_change "$repo"
cat >"$repo/provision.sh" <<'SHELL'
#!/usr/bin/env bash
aws --region "${AWS_REGION}" cloudwatch put-metric-alarm \
  --alarm-name "madis-ldm-per-station" \
  --namespace "KalshiWeather/MADIS" \
  --metric-name PerStationUploads \
  --dimensions Name=StationId,Value="${STATION_ID}"
SHELL
expect_fail "$repo" 'provision.sh:2  publishes a metric or alarm with the AWS CLI' '*.sh'
grep -F 'provision.sh:6  adds an AWS CLI metric dimension' "$repo/guard-output" >/dev/null || {
  cat "$repo/guard-output" >&2
  echo "expected the --dimensions continuation line to be reported as well" >&2
  exit 1
}

# Adding only the flag and shorthand value to an existing multiline command must still bind
# across the continuation. The command line is diff context here, so no other CLI detector can
# make this fixture pass accidentally.
repo=$(new_repo aws-cli-split-dimension-addition)
cat >"$repo/provision.sh" <<'SHELL'
#!/usr/bin/env bash
aws cloudwatch put-metric-alarm \
  --alarm-name "madis-ldm-per-station"
SHELL
start_change "$repo"
cat >"$repo/provision.sh" <<'SHELL'
#!/usr/bin/env bash
aws cloudwatch put-metric-alarm \
  --alarm-name "madis-ldm-per-station" \
  --dimensions \
    Name=StationId,Value="${STATION_ID}"
SHELL
expect_fail "$repo" 'provision.sh:4  adds an AWS CLI metric dimension' '*.sh'

# Shell files must be scanned without the caller naming them, so this one runs on the
# scanner's own default paths rather than an explicit SCAN_PATHS.
repo=$(new_repo shell-scanned-by-default)
start_change "$repo"
cat >"$repo/publish.sh" <<'SHELL'
aws cloudwatch put-metric-data --namespace App/Foo --metric-name Uploads --value 1
SHELL
expect_fail "$repo" 'publish.sh:1  publishes a metric or alarm with the AWS CLI' default

# Reading metrics bills nothing; neither should it fail the guard.
repo=$(new_repo cli-read-only-commands)
start_change "$repo"
cat >"$repo/inspect.sh" <<'SHELL'
aws cloudwatch describe-alarms --alarm-names madis-ldm-per-station
aws cloudwatch get-metric-statistics --namespace App/Foo --metric-name Uploads
SHELL
expect_pass "$repo" '*.sh'

# `Name=x,Value=y` shorthand is not unique to CloudWatch. Adding *.sh to the default paths put
# a lot of ordinary infrastructure shell in front of this guard for the first time, and each of
# these fired before the dimension detector was anchored to the flag.
repo=$(new_repo cli-shorthand-lookalikes)
start_change "$repo"
cat >"$repo/ops.sh" <<'SHELL'
aws ec2 create-tags --resources "$ID" --tags Key=Name,Value=recorder-01
aws cloudformation deploy --parameter-overrides Name=Stack,Value=foo
export PROM_LABELS="Name=host,Value=${HOSTNAME}"
aws elbv2 add-tags --tags Key=Team,Value=infra
SHELL
expect_pass "$repo" '*.sh'

# The acknowledgement works the same way in shell, and covers both findings from one comment.
repo=$(new_repo cli-acknowledged)
start_change "$repo"
cat >"$repo/publish.sh" <<'SHELL'
# metric-budget: 1 series per station, paged on by madis-ldm-stale
aws cloudwatch put-metric-alarm --alarm-name madis-ldm-per-station \
  --dimensions Name=StationId,Value="${STATION_ID}"
SHELL
expect_pass "$repo" '*.sh'
