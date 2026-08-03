#!/usr/bin/env python3
"""Run a bounded S3 server-access-log attribution sample.

The setup command arms AWS-owned one-time schedules for an exact two-day UTC
window. It never waits in CI and never enables logging before every restore
schedule and destination-bucket invariant has been verified.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Iterable


UTC = dt.timezone.utc
REQUIRED_TAGS = {"Project", "Repository", "Environment", "Owner"}
REVIEWED_BUCKETS = {
    "kalshi-weather-evidence-303529433772-us-east-2",
    "kalshi-weather-madis-hfmetar-303529433772-us-east-2",
    "kalshi-weather-pipeline-303529433772-us-east-2",
    "pmkt-recorder-v21-canary-303529433772-us-east-2",
}
LOG_TOKEN = re.compile(r'("[^"]*"|\[[^]]+\]|\S+)')
TIER1_METHODS = {"PUT", "POST", "COPY", "LIST"}
TIER2_METHODS = {"GET", "HEAD"}
MULTIPART_MARKERS = ("MULTI", "UPLOAD", "PART")


class AuditError(RuntimeError):
    """A safety check or AWS operation failed."""


class AwsCli:
    def __init__(self, region: str) -> None:
        self.region = region

    def call(self, *args: str, optional_codes: Iterable[str] = ()) -> dict[str, Any]:
        # Cost Explorer exposes its API through us-east-1 even when the sampled
        # resources live elsewhere.
        region = "us-east-1" if args[0] == "ce" else self.region
        command = ["aws", *args, "--region", region, "--output", "json", "--no-cli-pager"]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode:
            if any(f"({code})" in result.stderr for code in optional_codes):
                return {}
            detail = result.stderr.strip() or result.stdout.strip()
            raise AuditError(f"AWS command failed: {' '.join(command[:3])}: {detail}")
        return json.loads(result.stdout or "{}")

    def download(self, bucket: str, key: str, destination: Path) -> None:
        command = [
            "aws", "s3api", "get-object", "--bucket", bucket, "--key", key,
            str(destination), "--region", self.region, "--no-cli-pager",
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode:
            raise AuditError(result.stderr.strip() or f"failed to download {key}")


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditError(f"cannot read JSON from {path}: {exc}") from exc


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(state, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as handle:
        handle.write(rendered)
        temporary = Path(handle.name)
    temporary.replace(path)


def validate_config(config: dict[str, Any]) -> float:
    required = {
        "account_id", "region", "sample_hours", "retention_days", "budget_usd",
        "material_monthly_cost_usd",
        "max_log_bytes", "estimated_log_bytes_per_request", "observed_requests_per_week",
        "storage_usd_per_gb_month", "cost_explorer_query_usd",
        "effective_request_rates", "sources",
    }
    missing = sorted(required - config.keys())
    if missing:
        raise AuditError(f"config is missing: {', '.join(missing)}")
    if not re.fullmatch(r"\d{12}", str(config["account_id"])):
        raise AuditError("account_id must be 12 digits")
    if config["region"] != "us-east-2":
        raise AuditError("this reviewed audit is restricted to us-east-2")
    if not 0 < int(config["sample_hours"]) <= 48:
        raise AuditError("sample_hours must be between 1 and 48")
    if not 0 < int(config["retention_days"]) <= 7:
        raise AuditError("retention_days must be between 1 and 7")
    if float(config["material_monthly_cost_usd"]) <= 0:
        raise AuditError("material_monthly_cost_usd must be positive")
    sources = config["sources"]
    if not sources:
        raise AuditError("at least one source bucket is required")
    buckets = [source.get("bucket") for source in sources]
    prefixes = [source.get("prefix") for source in sources]
    if None in buckets or len(set(buckets)) != len(buckets):
        raise AuditError("source buckets must be present and unique")
    if set(buckets) != REVIEWED_BUCKETS:
        raise AuditError("sources must be exactly the four issue-reviewed buckets")
    if None in prefixes or len(set(prefixes)) != len(prefixes):
        raise AuditError("source prefixes must be present and unambiguous")
    for source in sources:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", source["bucket"]):
            raise AuditError(f"invalid source bucket: {source['bucket']}")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", source["prefix"]):
            raise AuditError(f"invalid source prefix: {source['prefix']}")
        tags = source.get("tags", {})
        if set(tags) != REQUIRED_TAGS or not all(tags.values()):
            raise AuditError(f"{source['bucket']} must define exactly {sorted(REQUIRED_TAGS)}")

    rates = config["effective_request_rates"]
    rate_keys = {
        "tier1_per_request_usd", "tier2_per_request_usd",
        "source_window_start", "source_window_end",
    }
    if set(rates) != rate_keys:
        raise AuditError(f"effective_request_rates must define exactly {sorted(rate_keys)}")
    if rates["tier1_per_request_usd"] <= 0 or rates["tier2_per_request_usd"] <= 0:
        raise AuditError("effective request rates must be positive")
    rate_start = dt.date.fromisoformat(rates["source_window_start"])
    rate_end = dt.date.fromisoformat(rates["source_window_end"])
    if rate_end <= rate_start:
        raise AuditError("effective rate source window must be non-empty")

    expected_requests = config["observed_requests_per_week"] * config["sample_hours"] / 168
    expected_bytes = expected_requests * config["estimated_log_bytes_per_request"]
    bounded_bytes = min(expected_bytes, config["max_log_bytes"])
    storage = bounded_bytes / 1_000_000_000 * config["storage_usd_per_gb_month"] * 7 / 30.4375
    # Conservatively budget one GET per 1,000 requests plus one Cost Explorer query.
    retrieval = expected_requests / 1000 * config["effective_request_rates"]["tier2_per_request_usd"]
    estimate = storage + retrieval + config["cost_explorer_query_usd"]
    if expected_bytes > config["max_log_bytes"]:
        raise AuditError("expected log volume exceeds max_log_bytes")
    if estimate >= config["budget_usd"]:
        raise AuditError(f"estimated incremental cost ${estimate:.4f} exceeds budget")
    return estimate


def bucket_region(raw: dict[str, Any]) -> str:
    return raw.get("LocationConstraint") or "us-east-1"


def durable_operator_arn(identity_arn: str) -> str:
    """Turn an STS role-session ARN into the stable IAM role principal."""
    assumed = re.fullmatch(r"arn:aws:sts::(\d{12}):assumed-role/([^/]+)/[^/]+", identity_arn)
    if assumed:
        return f"arn:aws:iam::{assumed.group(1)}:role/{assumed.group(2)}"
    if ":federated-user/" in identity_arn:
        raise AuditError("use a durable IAM role or user, not a federated-user session")
    return identity_arn


def prior_tags(aws: AwsCli, bucket: str) -> dict[str, str]:
    response = aws.call(
        "s3api", "get-bucket-tagging", "--bucket", bucket,
        optional_codes=("NoSuchTagSet", "NoSuchTagSetError"),
    )
    return {item["Key"]: item["Value"] for item in response.get("TagSet", [])}


def create_destination(
    aws: AwsCli,
    bucket: str,
    audit_id: str,
    operator_arn: str,
    account_id: str,
    retention_days: int,
    sources: list[dict[str, Any]],
) -> dict[str, Any]:
    aws.call(
        "s3api", "create-bucket", "--bucket", bucket,
        "--create-bucket-configuration", f"LocationConstraint={aws.region}",
    )
    aws.call(
        "s3api", "put-public-access-block", "--bucket", bucket,
        "--public-access-block-configuration",
        "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true",
    )
    aws.call(
        "s3api", "put-bucket-ownership-controls", "--bucket", bucket,
        "--ownership-controls", canonical({"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]}),
    )
    aws.call(
        "s3api", "put-bucket-encryption", "--bucket", bucket,
        "--server-side-encryption-configuration",
        canonical({"Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]}),
    )
    aws.call("s3api", "put-bucket-versioning", "--bucket", bucket, "--versioning-configuration", "Status=Enabled")
    lifecycle = {
        "Rules": [{
            "ID": "expire-temporary-audit-evidence",
            "Status": "Enabled",
            "Filter": {"Prefix": ""},
            "Expiration": {"Days": retention_days},
            "NoncurrentVersionExpiration": {"NoncurrentDays": retention_days},
            "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
        }]
    }
    aws.call(
        "s3api", "put-bucket-lifecycle-configuration", "--bucket", bucket,
        "--lifecycle-configuration", canonical(lifecycle),
    )
    statements: list[dict[str, Any]] = [{
        "Sid": "AuditOperator",
        "Effect": "Allow",
        "Principal": {"AWS": operator_arn},
        "Action": [
            "s3:ListBucket", "s3:GetBucketLocation", "s3:GetBucketLogging",
            "s3:GetBucketLifecycleConfiguration", "s3:GetBucketPublicAccessBlock",
            "s3:GetEncryptionConfiguration", "s3:GetBucketOwnershipControls",
            "s3:GetBucketTagging", "s3:GetBucketVersioning", "s3:GetBucketPolicy",
            "s3:DeleteBucket", "s3:GetObject", "s3:PutObject", "s3:DeleteObject",
        ],
        "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"],
    }, {
        "Sid": "DenyOtherPrincipals",
        "Effect": "Deny",
        "Principal": "*",
        "Action": "s3:*",
        "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"],
        "Condition": {
            "ArnNotEquals": {"aws:PrincipalArn": operator_arn},
            "StringNotEqualsIfExists": {
                "aws:PrincipalServiceName": ["logging.s3.amazonaws.com", "s3.amazonaws.com"]
            },
        },
    }, {
        "Sid": "DenyInsecureTransport",
        "Effect": "Deny",
        "Principal": "*",
        "Action": "s3:*",
        "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"],
        "Condition": {"Bool": {"aws:SecureTransport": "false"}},
    }]
    for source in sources:
        prefix = f"{audit_id}/{source['prefix']}/"
        statements.append({
            "Sid": "LogDelivery" + hashlib.sha256(source["bucket"].encode()).hexdigest()[:12],
            "Effect": "Allow",
            "Principal": {"Service": "logging.s3.amazonaws.com"},
            "Action": "s3:PutObject",
            "Resource": f"arn:aws:s3:::{bucket}/{prefix}*",
            "Condition": {
                "ArnLike": {"aws:SourceArn": f"arn:aws:s3:::{source['bucket']}"},
                "StringEquals": {"aws:SourceAccount": account_id},
            },
        })
    policy = {"Version": "2012-10-17", "Statement": statements}
    aws.call(
        "s3api", "put-bucket-policy", "--bucket", bucket,
        "--policy", canonical(policy),
    )
    return policy


def verify_destination(
    aws: AwsCli,
    bucket: str,
    retention_days: int,
    expected_policy: dict[str, Any] | None = None,
) -> None:
    if aws.call("s3api", "get-bucket-logging", "--bucket", bucket).get("LoggingEnabled"):
        raise AuditError("destination logging is enabled (recursive logging)")
    public = aws.call("s3api", "get-public-access-block", "--bucket", bucket)["PublicAccessBlockConfiguration"]
    if not all(public.get(key) for key in ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")):
        raise AuditError("destination public access is not fully blocked")
    encryption = aws.call("s3api", "get-bucket-encryption", "--bucket", bucket)
    algorithms = {
        rule["ApplyServerSideEncryptionByDefault"]["SSEAlgorithm"]
        for rule in encryption["ServerSideEncryptionConfiguration"]["Rules"]
    }
    if algorithms != {"AES256"}:
        raise AuditError("destination must use only S3-managed encryption")
    lifecycle = aws.call("s3api", "get-bucket-lifecycle-configuration", "--bucket", bucket)
    bounded = any(
        rule.get("Status") == "Enabled"
        and 0 < rule.get("Expiration", {}).get("Days", 999) <= retention_days
        and 0 < rule.get("NoncurrentVersionExpiration", {}).get("NoncurrentDays", 999) <= retention_days
        for rule in lifecycle.get("Rules", [])
    )
    if not bounded:
        raise AuditError("destination lifecycle does not bound current and noncurrent evidence")
    ownership = aws.call("s3api", "get-bucket-ownership-controls", "--bucket", bucket)
    if ownership["OwnershipControls"]["Rules"] != [{"ObjectOwnership": "BucketOwnerEnforced"}]:
        raise AuditError("destination object ownership is not BucketOwnerEnforced")
    if aws.call("s3api", "get-bucket-request-payment", "--bucket", bucket).get("Payer") != "BucketOwner":
        raise AuditError("destination has Requester Pays enabled")
    if aws.call("s3api", "get-bucket-versioning", "--bucket", bucket).get("Status") != "Enabled":
        raise AuditError("destination versioning is not enabled")
    if expected_policy is not None:
        actual_policy = aws.call("s3api", "get-bucket-policy", "--bucket", bucket).get("Policy", "{}")
        if canonical(json.loads(actual_policy)) != canonical(expected_policy):
            raise AuditError("destination bucket policy differs from the least-privilege policy")


def schedule_role(
    aws: AwsCli,
    role_name: str,
    account_id: str,
    group: str,
    buckets: list[str],
) -> str:
    trust = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "scheduler.amazonaws.com"},
            "Action": "sts:AssumeRole",
            "Condition": {
                "StringEquals": {"aws:SourceAccount": account_id},
                "ArnLike": {
                    "aws:SourceArn": f"arn:aws:scheduler:{aws.region}:{account_id}:schedule/{group}/*"
                },
            },
        }],
    }
    response = aws.call(
        "iam", "create-role", "--role-name", role_name,
        "--assume-role-policy-document", canonical(trust),
    )
    policy = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Action": "s3:PutBucketLogging",
            "Resource": [f"arn:aws:s3:::{bucket}" for bucket in buckets],
        }],
    }
    aws.call(
        "iam", "put-role-policy", "--role-name", role_name,
        "--policy-name", "RestoreReviewedBucketLogging", "--policy-document", canonical(policy),
    )
    arn = response.get("Role", {}).get("Arn")
    return arn or f"arn:aws:iam::{account_id}:role/{role_name}"


def schedule_logging(
    aws: AwsCli,
    group: str,
    name: str,
    when: dt.datetime,
    role_arn: str,
    bucket: str,
    logging_status: dict[str, Any],
) -> None:
    target = {
        "Arn": "arn:aws:scheduler:::aws-sdk:s3:putBucketLogging",
        "RoleArn": role_arn,
        "Input": canonical({"Bucket": bucket, "BucketLoggingStatus": logging_status}),
        "RetryPolicy": {"MaximumEventAgeInSeconds": 3600, "MaximumRetryAttempts": 10},
    }
    expression = f"at({when.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%S')})"
    aws.call(
        "scheduler", "create-schedule", "--group-name", group, "--name", name,
        "--schedule-expression", expression, "--schedule-expression-timezone", "UTC",
        "--flexible-time-window", "Mode=OFF", "--action-after-completion", "DELETE",
        "--target", canonical(target),
    )
    actual = aws.call("scheduler", "get-schedule", "--group-name", group, "--name", name)
    if actual.get("ScheduleExpression") != expression:
        raise AuditError(f"schedule {name} has the wrong execution time")
    actual_input = json.loads(actual.get("Target", {}).get("Input", "{}"))
    expected_input = {"Bucket": bucket, "BucketLoggingStatus": logging_status}
    if actual_input != expected_input or actual.get("State") != "ENABLED":
        raise AuditError(f"schedule {name} does not preserve the reviewed logging state")


def merge_tags(aws: AwsCli, bucket: str, existing: dict[str, str], requested: dict[str, str]) -> None:
    merged = {**existing, **requested}
    aws.call(
        "s3api", "put-bucket-tagging", "--bucket", bucket,
        "--tagging", canonical({"TagSet": [{"Key": key, "Value": merged[key]} for key in sorted(merged)]}),
    )
    if prior_tags(aws, bucket) != merged:
        raise AuditError(f"tag verification failed for {bucket}")


def activate_tags(aws: AwsCli) -> tuple[list[str], list[str]]:
    response = aws.call(
        "ce", "list-cost-allocation-tags", "--tag-keys", *sorted(REQUIRED_TAGS),
    )
    statuses = {item["TagKey"]: item["Status"] for item in response.get("CostAllocationTags", [])}
    available = sorted(REQUIRED_TAGS & statuses.keys())
    inactive = [key for key in available if statuses[key] != "Active"]
    if inactive:
        values = [{"TagKey": key, "Status": "Active"} for key in inactive]
        aws.call("ce", "update-cost-allocation-tags-status", "--cost-allocation-tags-status", canonical(values))
        response = aws.call(
            "ce", "list-cost-allocation-tags", "--tag-keys", *sorted(REQUIRED_TAGS),
        )
        statuses = {item["TagKey"]: item["Status"] for item in response.get("CostAllocationTags", [])}
    active = sorted(key for key in REQUIRED_TAGS if statuses.get(key) == "Active")
    return active, sorted(REQUIRED_TAGS - set(active))


def upload_state(aws: AwsCli, state_path: Path, state: dict[str, Any]) -> str:
    rendered = canonical(state).encode()
    digest = hashlib.sha256(rendered).hexdigest()
    key = f"{state['audit_id']}/control/state-{digest}.json"
    aws.call(
        "s3api", "put-object", "--bucket", state["destination_bucket"], "--key", key,
        "--body", str(state_path), "--server-side-encryption", "AES256",
    )
    return key


def start(config_path: Path, state_path: Path, sample_start: str | None, now: dt.datetime | None = None) -> dict[str, Any]:
    config = load_json(config_path)
    cost_estimate = validate_config(config)
    now = (now or dt.datetime.now(UTC)).astimezone(UTC)
    start_date = dt.date.fromisoformat(sample_start) if sample_start else now.date() + dt.timedelta(days=1)
    starts_at = dt.datetime.combine(start_date, dt.time(), UTC)
    if starts_at < now + dt.timedelta(minutes=15):
        raise AuditError("sample start must be a future UTC midnight at least 15 minutes away")
    ends_at = starts_at + dt.timedelta(hours=config["sample_hours"])
    audit_id = f"s3req-{start_date.strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}"
    destination = f"s3-request-audit-{config['account_id']}-{config['region']}-{uuid.uuid4().hex[:8]}"
    role_name = f"s3-request-audit-{audit_id[-17:]}"
    group = f"s3-request-audit-{audit_id[-17:]}"
    aws = AwsCli(config["region"])
    identity = aws.call("sts", "get-caller-identity")
    if identity.get("Account") != config["account_id"]:
        raise AuditError("configured AWS account does not match the active identity")
    operator_arn = durable_operator_arn(identity["Arn"])

    source_states: list[dict[str, Any]] = []
    for source in config["sources"]:
        bucket = source["bucket"]
        if bucket_region(aws.call("s3api", "get-bucket-location", "--bucket", bucket)) != config["region"]:
            raise AuditError(f"source is outside {config['region']}: {bucket}")
        source_states.append({
            **source,
            "prior_logging": aws.call("s3api", "get-bucket-logging", "--bucket", bucket),
            "prior_tags": prior_tags(aws, bucket),
        })

    destination_policy = create_destination(
        aws, destination, audit_id, operator_arn, config["account_id"],
        config["retention_days"], source_states,
    )
    verify_destination(aws, destination, config["retention_days"], destination_policy)
    role_arn = schedule_role(
        aws, role_name, config["account_id"], group,
        [item["bucket"] for item in source_states],
    )
    aws.call("scheduler", "create-schedule-group", "--name", group)

    schedules: list[dict[str, str]] = [dict() for _source in source_states]
    for index, source in enumerate(source_states):
        logging_status = {
            "LoggingEnabled": {
                "TargetBucket": destination,
                "TargetPrefix": f"{audit_id}/{source['prefix']}/",
                "TargetObjectKeyFormat": {"SimplePrefix": {}},
            }
        }
        restore_name = f"restore-{index}-{audit_id[-8:]}"
        schedule_logging(aws, group, restore_name, ends_at, role_arn, source["bucket"], source["prior_logging"])
        schedules[index]["restore"] = restore_name
        source["audit_logging"] = logging_status

    # Only after every source has a verified restore schedule may any enable
    # schedule be created. A partial setup can therefore never strand logging.
    for index, source in enumerate(source_states):
        enable_name = f"enable-{index}-{audit_id[-8:]}"
        schedule_logging(
            aws, group, enable_name, starts_at, role_arn,
            source["bucket"], source["audit_logging"],
        )
        schedules[index]["enable"] = enable_name

    state = {
        "schema_version": 1,
        "phase": "armed",
        "audit_id": audit_id,
        "account_id": config["account_id"],
        "region": config["region"],
        "operator_arn": operator_arn,
        "destination_bucket": destination,
        "started_at": starts_at.isoformat(),
        "ends_at": ends_at.isoformat(),
        "created_at": now.isoformat(),
        "sample_hours": config["sample_hours"],
        "retention_days": config["retention_days"],
        "budget_usd": config["budget_usd"],
        "material_monthly_cost_usd": config["material_monthly_cost_usd"],
        "estimated_incremental_cost_usd": round(cost_estimate, 6),
        "max_log_bytes": config["max_log_bytes"],
        "storage_usd_per_gb_month": config["storage_usd_per_gb_month"],
        "cost_explorer_query_usd": config["cost_explorer_query_usd"],
        "effective_request_rates": config["effective_request_rates"],
        "schedule_group": group,
        "schedule_role_name": role_name,
        "tag_keys_active": [],
        "tag_keys_pending_visibility": sorted(REQUIRED_TAGS),
        "sources": [{**source, "schedules": schedules[index]} for index, source in enumerate(source_states)],
        "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
    }
    # Persist recovery data before the first durable source mutation.
    write_state(state_path, state)
    state["state_evidence_key"] = upload_state(aws, state_path, state)
    write_state(state_path, state)

    for source in source_states:
        # All restore schedules are proven before the first source mutation (tagging).
        merge_tags(aws, source["bucket"], source["prior_tags"], source["tags"])
    active, pending = activate_tags(aws)
    state["tag_keys_active"] = active
    state["tag_keys_pending_visibility"] = pending
    write_state(state_path, state)
    return state


def restore_and_verify(state: dict[str, Any], aws: AwsCli) -> list[str]:
    restored: list[str] = []
    destination = state["destination_bucket"]
    for source in state["sources"]:
        current = aws.call("s3api", "get-bucket-logging", "--bucket", source["bucket"])
        if current != source["prior_logging"]:
            aws.call(
                "s3api", "put-bucket-logging", "--bucket", source["bucket"],
                "--bucket-logging-status", canonical(source["prior_logging"]),
            )
            current = aws.call("s3api", "get-bucket-logging", "--bucket", source["bucket"])
            restored.append(source["bucket"])
        if current != source["prior_logging"]:
            raise AuditError(f"teardown is incomplete for {source['bucket']}")
        enabled = current.get("LoggingEnabled", {})
        if enabled.get("TargetBucket") == destination:
            raise AuditError(f"recursive audit logging remains on {source['bucket']}")
    return restored


def cleanup_scheduler(state: dict[str, Any], aws: AwsCli) -> None:
    """Remove only this audit's completed control-plane resources."""
    group = state["schedule_group"]
    response = aws.call(
        "scheduler", "list-schedules", "--group-name", group,
        optional_codes=("ResourceNotFoundException",),
    )
    for schedule in response.get("Schedules", []):
        aws.call(
            "scheduler", "delete-schedule", "--group-name", group,
            "--name", schedule["Name"], optional_codes=("ResourceNotFoundException",),
        )
    aws.call(
        "scheduler", "delete-schedule-group", "--name", group,
        optional_codes=("ResourceNotFoundException",),
    )
    role = state["schedule_role_name"]
    aws.call(
        "iam", "delete-role-policy", "--role-name", role,
        "--policy-name", "RestoreReviewedBucketLogging",
        optional_codes=("NoSuchEntity",),
    )
    aws.call("iam", "delete-role", "--role-name", role, optional_codes=("NoSuchEntity",))


def tokenize_log(line: str) -> list[str]:
    return [token[1:-1] if token.startswith('"') else token for token in LOG_TOKEN.findall(line)]


def classify(operation: str, request_uri: str) -> str:
    upper = operation.upper()
    if any(marker in upper for marker in MULTIPART_MARKERS):
        return "multipart"
    method = request_uri.split(" ", 1)[0].upper()
    if method in TIER1_METHODS or any(marker in upper for marker in ("PUT", "POST", "COPY", "LIST")):
        return "tier1_like"
    if method in TIER2_METHODS or any(marker in upper for marker in ("GET", "HEAD")):
        return "tier2_like"
    return "other"


def iter_objects(aws: AwsCli, bucket: str, prefix: str) -> Iterable[dict[str, Any]]:
    token: str | None = None
    while True:
        args = ["s3api", "list-objects-v2", "--bucket", bucket, "--prefix", prefix]
        if token:
            args += ["--continuation-token", token]
        response = aws.call(*args)
        yield from response.get("Contents", [])
        token = response.get("NextContinuationToken")
        if not token:
            break


def cost_explorer(aws: AwsCli, start_date: str, end_date: str) -> dict[str, dict[str, float]]:
    response = aws.call(
        "ce", "get-cost-and-usage",
        "--time-period", canonical({"Start": start_date, "End": end_date}),
        "--granularity", "DAILY", "--metrics", "UnblendedCost", "UsageQuantity",
        "--filter", canonical({
            "And": [
                {"Dimensions": {"Key": "SERVICE", "Values": ["Amazon Simple Storage Service"]}},
                {"Dimensions": {"Key": "USAGE_TYPE", "Values": ["USE2-Requests-Tier1", "USE2-Requests-Tier2"]}},
            ]
        }),
        "--group-by", "Type=DIMENSION,Key=USAGE_TYPE",
    )
    totals: dict[str, dict[str, float]] = collections.defaultdict(lambda: {"requests": 0.0, "cost_usd": 0.0})
    for period in response.get("ResultsByTime", []):
        for group in period.get("Groups", []):
            name = group["Keys"][0]
            totals[name]["requests"] += float(group["Metrics"]["UsageQuantity"]["Amount"])
            totals[name]["cost_usd"] += float(group["Metrics"]["UnblendedCost"]["Amount"])
    return dict(totals)


def optimization_assignments(
    sources: dict[str, dict[str, Any]],
    material_monthly_cost_usd: float,
) -> dict[str, list[dict[str, Any]]]:
    assignments: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for bucket, values in sources.items():
        if values["estimated_monthly_request_cost_usd"] < material_monthly_cost_usd:
            continue
        assignments[values["repository"]].append({
            "bucket": bucket,
            "estimated_monthly_request_cost_usd": values["estimated_monthly_request_cost_usd"],
            "dominant_operations": list(values["operation_counts"].items())[:5],
        })
    return dict(sorted(assignments.items()))


def build_report(state: dict[str, Any], aws: AwsCli) -> dict[str, Any]:
    starts_at = dt.datetime.fromisoformat(state["started_at"])
    ends_at = dt.datetime.fromisoformat(state["ends_at"])
    if dt.datetime.now(UTC) < ends_at:
        raise AuditError("the 48-hour sample has not ended")
    fallback_restores = restore_and_verify(state, aws)
    cleanup_scheduler(state, aws)
    active_tags, pending_tags = activate_tags(aws)
    if pending_tags:
        raise AuditError(
            "cost-allocation tags are not active yet; retry activate-tags after Billing visibility: "
            + ", ".join(pending_tags)
        )
    destination = state["destination_bucket"]
    counters: dict[str, Any] = {}
    total_bytes = total_objects = malformed = outside_window = duplicates = 0
    seen: set[str] = set()
    with tempfile.TemporaryDirectory(prefix="s3-request-audit-") as scratch:
        scratch_path = Path(scratch)
        for source in state["sources"]:
            prefix = f"{state['audit_id']}/{source['prefix']}/"
            family = collections.Counter()
            statuses = collections.Counter()
            operations = collections.Counter()
            source_total = 0
            for obj in iter_objects(aws, destination, prefix):
                total_objects += 1
                total_bytes += int(obj.get("Size", 0))
                if total_bytes > state["max_log_bytes"]:
                    raise AuditError("delivered logs exceed max_log_bytes; refusing to download more")
                projected_cost = (
                    total_bytes / 1_000_000_000
                    * state["storage_usd_per_gb_month"]
                    * state["retention_days"] / 30.4375
                    + total_objects * state["effective_request_rates"]["tier2_per_request_usd"]
                    + state["cost_explorer_query_usd"]
                )
                if projected_cost >= state["budget_usd"]:
                    raise AuditError("retrieving delivered logs would exceed the audit budget")
                local = scratch_path / hashlib.sha256(obj["Key"].encode()).hexdigest()
                aws.download(destination, obj["Key"], local)
                for line in local.read_text(encoding="utf-8", errors="replace").splitlines():
                    digest = hashlib.sha256(line.encode()).hexdigest()
                    if digest in seen:
                        duplicates += 1
                        continue
                    seen.add(digest)
                    fields = tokenize_log(line)
                    if len(fields) < 11:
                        malformed += 1
                        continue
                    try:
                        timestamp = dt.datetime.strptime(fields[2], "[%d/%b/%Y:%H:%M:%S %z]")
                    except ValueError:
                        malformed += 1
                        continue
                    if not starts_at <= timestamp < ends_at:
                        outside_window += 1
                        continue
                    operation, request_uri, status = fields[6], fields[8], fields[9]
                    family[classify(operation, request_uri)] += 1
                    statuses[status] += 1
                    operations[operation] += 1
                    source_total += 1
            counters[source["bucket"]] = {
                "request_count": source_total,
                "families": dict(sorted(family.items())),
                "status_distribution": dict(sorted(statuses.items())),
                "operation_counts": dict(operations.most_common()),
                "repository": source["tags"]["Repository"],
            }
    total_requests = sum(item["request_count"] for item in counters.values())
    rates = state["effective_request_rates"]
    for bucket in counters.values():
        bucket["traffic_percentage"] = round(100 * bucket["request_count"] / total_requests, 3) if total_requests else 0
        tier1 = bucket["families"].get("tier1_like", 0) + bucket["families"].get("multipart", 0)
        tier2 = bucket["families"].get("tier2_like", 0)
        sampled = tier1 * rates["tier1_per_request_usd"] + tier2 * rates["tier2_per_request_usd"]
        bucket["estimated_sample_request_cost_usd"] = round(sampled, 6)
        bucket["estimated_weekly_request_cost_usd"] = round(sampled * 168 / state["sample_hours"], 4)
        bucket["estimated_monthly_request_cost_usd"] = round(sampled * 24 * 30.4375 / state["sample_hours"], 4)
    ce = cost_explorer(aws, starts_at.date().isoformat(), ends_at.date().isoformat())
    ce_requests = sum(item["requests"] for item in ce.values())
    delta = abs(total_requests - ce_requests) / ce_requests if ce_requests else None
    confidence = "medium" if total_requests and malformed / total_requests < 0.01 and delta is not None and delta <= 0.2 else "low"
    audit_cost = (
        total_bytes / 1_000_000_000 * state["storage_usd_per_gb_month"] * state["retention_days"] / 30.4375
        + total_objects * rates["tier2_per_request_usd"]
        + state["cost_explorer_query_usd"]
    )
    assignments = optimization_assignments(counters, state["material_monthly_cost_usd"])
    return {
        "audit_id": state["audit_id"],
        "sample_start_utc": state["started_at"],
        "sample_end_utc": state["ends_at"],
        "sample_dates_utc": [starts_at.date().isoformat(), (ends_at.date() - dt.timedelta(days=1)).isoformat()],
        "confidence": confidence,
        "sources": counters,
        "sampled_request_total": total_requests,
        "cost_explorer": ce,
        "cost_explorer_request_total": ce_requests,
        "sample_to_cost_explorer_delta_percentage": round(delta * 100, 3) if delta is not None else None,
        "delivery": {
            "objects": total_objects,
            "bytes": total_bytes,
            "malformed_records": malformed,
            "duplicate_records_omitted": duplicates,
            "outside_window_records_omitted": outside_window,
        },
        "teardown": {
            "logging_disabled_or_restored": True,
            "manual_fallback_restores": fallback_restores,
            "scheduler_resources_removed": True,
        },
        "active_cost_allocation_tags": active_tags,
        "material_monthly_cost_threshold_usd": state["material_monthly_cost_usd"],
        "optimization_assignments": assignments,
        "estimated_incremental_audit_cost_usd": round(audit_cost, 6),
        "budget_usd": state["budget_usd"],
        "known_limitations": [
            "S3 server access logging is best effort and can be delayed or omitted.",
            "Access-log operations are billing-like families, not Cost Explorer ledger entries.",
            "Duplicate delivery, non-billable requests, and requests outside the reviewed buckets can create reconciliation differences.",
            "Cost Explorer can lag and does not attribute request line items to buckets.",
        ],
    }


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        f"# S3 request attribution: {report['audit_id']}", "",
        f"UTC window: `{report['sample_start_utc']}` through `{report['sample_end_utc']}`",
        f"Confidence: **{report['confidence']}**", "",
        "| Bucket | Requests | Traffic | Weekly estimate | Monthly estimate |",
        "|---|---:|---:|---:|---:|",
    ]
    for bucket, values in report["sources"].items():
        lines.append(
            f"| `{bucket}` | {values['request_count']:,} | {values['traffic_percentage']:.3f}% "
            f"| ${values['estimated_weekly_request_cost_usd']:.4f} | ${values['estimated_monthly_request_cost_usd']:.4f} |"
        )
    lines += [
        "", "## Reconciliation", "",
        f"Access-log records in window: {report['sampled_request_total']:,}",
        f"Cost Explorer request usage: {report['cost_explorer_request_total']:,.0f}",
        f"Delta: {report['sample_to_cost_explorer_delta_percentage']}%",
        "", "## Teardown and audit cost", "",
        "Source logging was verified restored to its prior configuration.",
        f"Estimated incremental audit cost: ${report['estimated_incremental_audit_cost_usd']:.6f} "
        f"(budget: ${report['budget_usd']:.2f}).",
        "", "## Completeness limitations", "",
    ]
    lines += [f"- {item}" for item in report["known_limitations"]]
    lines += ["", "## Owning-repository assignments", ""]
    if report["optimization_assignments"]:
        for repository, buckets in report["optimization_assignments"].items():
            lines.append(f"- `{repository}`: {', '.join(item['bucket'] for item in buckets)}")
    else:
        lines.append("No bucket crossed the reviewed materiality threshold.")
    return "\n".join(lines) + "\n"


def gh(*args: str) -> str:
    command = ["gh", *args]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode:
        raise AuditError(result.stderr.strip() or f"GitHub command failed: {' '.join(command[:3])}")
    return result.stdout.strip()


def gh_json(*args: str) -> Any:
    return json.loads(gh(*args) or "null")


def file_followups(report: dict[str, Any]) -> dict[str, str]:
    """Create or update one aggregate-only optimization issue per owner."""
    filed: dict[str, str] = {}
    for repository, buckets in report.get("optimization_assignments", {}).items():
        title = "finops: reduce S3 request amplification"
        lines = [
            f"The bounded audit `{report['audit_id']}` assigned material S3 request spend to this repository.",
            "", "| Bucket | Estimated monthly request spend | Dominant operations |",
            "|---|---:|---|",
        ]
        for item in buckets:
            operations = ", ".join(f"{name} ({count:,})" for name, count in item["dominant_operations"])
            lines.append(
                f"| `{item['bucket']}` | ${item['estimated_monthly_request_cost_usd']:.4f} | {operations or 'none'} |"
            )
        lines += [
            "", f"Confidence: **{report['confidence']}**.",
            "", "Investigate avoidable request amplification and preserve the bucket's externally visible behavior.",
            "Raw access-log records and identifying request fields are intentionally omitted.",
            "", "Completeness limitations:",
        ]
        lines += [f"- {item}" for item in report["known_limitations"]]
        body = "\n".join(lines) + "\n"
        existing = gh_json(
            "issue", "list", "--repo", repository, "--state", "open",
            "--search", f'"{title}" in:title', "--json", "number,title,url", "--limit", "20",
        )
        match = next((item for item in existing if item["title"].lower() == title), None)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            if match:
                gh("issue", "comment", str(match["number"]), "--repo", repository, "--body-file", handle.name)
                filed[repository] = match["url"]
            else:
                filed[repository] = gh(
                    "issue", "create", "--repo", repository, "--title", title,
                    "--body-file", handle.name,
                )
    return filed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate")
    validate.add_argument("--config", type=Path, required=True)
    arm = sub.add_parser("start")
    arm.add_argument("--config", type=Path, required=True)
    arm.add_argument("--state", type=Path, required=True)
    arm.add_argument("--sample-start", help="future UTC date (YYYY-MM-DD); defaults to tomorrow")
    teardown = sub.add_parser("verify-teardown")
    teardown.add_argument("--state", type=Path, required=True)
    tags = sub.add_parser("activate-tags")
    tags.add_argument("--state", type=Path, required=True)
    report_parser = sub.add_parser("report")
    report_parser.add_argument("--state", type=Path, required=True)
    report_parser.add_argument("--json-out", type=Path, required=True)
    report_parser.add_argument("--markdown-out", type=Path, required=True)
    followups = sub.add_parser("file-followups")
    followups.add_argument("--report", type=Path, required=True)
    followups.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "validate":
            estimate = validate_config(load_json(args.config))
            print(f"validated; estimated incremental cost ${estimate:.6f}")
        elif args.command == "start":
            state = start(args.config, args.state, args.sample_start)
            print(json.dumps({key: state[key] for key in ("audit_id", "started_at", "ends_at", "destination_bucket")}, indent=2))
        elif args.command == "file-followups":
            result = file_followups(load_json(args.report))
            args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            print(f"recorded {len(result)} owning-repository follow-up(s) in {args.output}")
        else:
            state = load_json(args.state)
            aws = AwsCli(state["region"])
            if args.command == "verify-teardown":
                restored = restore_and_verify(state, aws)
                cleanup_scheduler(state, aws)
                print(json.dumps({"verified": True, "manual_fallback_restores": restored}, indent=2))
            elif args.command == "activate-tags":
                active, pending = activate_tags(aws)
                print(json.dumps({"active_or_activated": active, "pending_visibility": pending}, indent=2))
            elif args.command == "report":
                report = build_report(state, aws)
                args.json_out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                args.markdown_out.write_text(markdown_report(report), encoding="utf-8")
                print(f"wrote aggregate reports to {args.json_out} and {args.markdown_out}")
    except (AuditError, ValueError, KeyError) as exc:
        print(f"s3 request audit failed closed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
