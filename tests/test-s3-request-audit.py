#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github/scripts/s3-request-audit.py"
CONFIG = ROOT / "audits/s3-request-attribution.json"
SPEC = importlib.util.spec_from_file_location("s3_request_audit", SCRIPT)
audit = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(audit)


class ConfigTests(unittest.TestCase):
    def config(self):
        return json.loads(CONFIG.read_text(encoding="utf-8"))

    def test_reviewed_config_is_bounded(self):
        estimate = audit.validate_config(self.config())
        self.assertLess(estimate, 1)

    def test_time_and_cost_budget_fail_closed(self):
        for key, value in (("sample_hours", 49), ("retention_days", 8), ("budget_usd", 0.001)):
            with self.subTest(key=key):
                config = self.config()
                config[key] = value
                with self.assertRaises(audit.AuditError):
                    audit.validate_config(config)

    def test_ambiguous_prefix_and_missing_tags_fail_closed(self):
        config = self.config()
        config["sources"][1]["prefix"] = config["sources"][0]["prefix"]
        with self.assertRaisesRegex(audit.AuditError, "unambiguous"):
            audit.validate_config(config)
        config = self.config()
        del config["sources"][0]["tags"]["Owner"]
        with self.assertRaisesRegex(audit.AuditError, "exactly"):
            audit.validate_config(config)

    def test_unreviewed_source_bucket_fails_closed(self):
        config = self.config()
        config["sources"][0]["bucket"] = "unreviewed-source-bucket"
        with self.assertRaisesRegex(audit.AuditError, "issue-reviewed"):
            audit.validate_config(config)


class SafetyTests(unittest.TestCase):
    def test_schedule_is_exact_retrying_and_self_deleting(self):
        aws = mock.Mock()
        expected_input = {"Bucket": "source", "BucketLoggingStatus": {}}
        aws.call.side_effect = [
            {},
            {
                "ScheduleExpression": "at(2026-08-05T00:00:00)",
                "State": "ENABLED",
                "Target": {"Input": audit.canonical(expected_input)},
            },
        ]
        when = audit.dt.datetime(2026, 8, 5, tzinfo=audit.UTC)
        audit.schedule_logging(aws, "group", "restore", when, "role", "source", {})
        create_args = aws.call.call_args_list[0].args
        self.assertIn("at(2026-08-05T00:00:00)", create_args)
        self.assertIn("DELETE", create_args)
        target = json.loads(create_args[create_args.index("--target") + 1])
        self.assertEqual(target["RetryPolicy"]["MaximumRetryAttempts"], 10)
        self.assertEqual(json.loads(target["Input"]), expected_input)

    def test_recursive_destination_fails(self):
        aws = mock.Mock()
        aws.call.return_value = {"LoggingEnabled": {"TargetBucket": "itself"}}
        with self.assertRaisesRegex(audit.AuditError, "recursive"):
            audit.verify_destination(aws, "audit-bucket", 7)

    def test_missing_lifecycle_fails(self):
        aws = mock.Mock()
        aws.call.side_effect = [
            {},
            {"PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True, "IgnorePublicAcls": True,
                "BlockPublicPolicy": True, "RestrictPublicBuckets": True,
            }},
            {"ServerSideEncryptionConfiguration": {"Rules": [
                {"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}
            ]}},
            {"Rules": []},
        ]
        with self.assertRaisesRegex(audit.AuditError, "lifecycle"):
            audit.verify_destination(aws, "audit-bucket", 7)

    def test_destination_policy_and_storage_controls_are_verified(self):
        policy = {"Version": "2012-10-17", "Statement": []}
        aws = mock.Mock()
        aws.call.side_effect = [
            {},
            {"PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True, "IgnorePublicAcls": True,
                "BlockPublicPolicy": True, "RestrictPublicBuckets": True,
            }},
            {"ServerSideEncryptionConfiguration": {"Rules": [
                {"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}
            ]}},
            {"Rules": [{
                "Status": "Enabled", "Expiration": {"Days": 7},
                "NoncurrentVersionExpiration": {"NoncurrentDays": 7},
            }]},
            {"OwnershipControls": {"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]}},
            {"Payer": "BucketOwner"},
            {"Status": "Enabled"},
            {"Policy": json.dumps(policy)},
        ]
        audit.verify_destination(aws, "audit-bucket", 7, policy)

    def test_tag_merge_preserves_unrelated_tags(self):
        aws = mock.Mock()
        aws.call.side_effect = [
            {},
            {"TagSet": [
                {"Key": "DoNotTouch", "Value": "preserved"},
                {"Key": "Project", "Value": "new"},
            ]},
        ]
        audit.merge_tags(
            aws, "source", {"DoNotTouch": "preserved"}, {"Project": "new"},
        )
        put_args = aws.call.call_args_list[0].args
        payload = json.loads(put_args[put_args.index("--tagging") + 1])
        self.assertEqual(
            {item["Key"]: item["Value"] for item in payload["TagSet"]},
            {"DoNotTouch": "preserved", "Project": "new"},
        )

    def test_teardown_restores_and_rechecks_prior_state(self):
        aws = mock.Mock()
        aws.call.side_effect = [
            {"LoggingEnabled": {"TargetBucket": "audit"}},
            {},
            {},
        ]
        state = {
            "destination_bucket": "audit",
            "sources": [{"bucket": "source", "prior_logging": {}}],
        }
        self.assertEqual(audit.restore_and_verify(state, aws), ["source"])
        put = aws.call.call_args_list[1].args
        self.assertIn("put-bucket-logging", put)
        self.assertIn("{}", put)

    def test_incomplete_teardown_fails(self):
        active = {"LoggingEnabled": {"TargetBucket": "audit"}}
        aws = mock.Mock()
        aws.call.side_effect = [active, {}, active]
        state = {
            "destination_bucket": "audit",
            "sources": [{"bucket": "source", "prior_logging": {}}],
        }
        with self.assertRaisesRegex(audit.AuditError, "incomplete"):
            audit.restore_and_verify(state, aws)


class ReportTests(unittest.TestCase):
    def test_log_classification(self):
        self.assertEqual(audit.classify("REST.GET.OBJECT", "GET /x HTTP/1.1"), "tier2_like")
        self.assertEqual(audit.classify("REST.PUT.OBJECT", "PUT /x HTTP/1.1"), "tier1_like")
        self.assertEqual(audit.classify("REST.POST.UPLOADS", "POST /x?uploads HTTP/1.1"), "multipart")
        self.assertEqual(audit.classify("REST.DELETE.OBJECT", "DELETE /x HTTP/1.1"), "other")

    def test_tokenizer_preserves_timestamp_and_request(self):
        line = 'owner bucket [02/Aug/2026:00:00:01 +0000] 1.2.3.4 requester req REST.GET.OBJECT key "GET /key HTTP/1.1" 200 - 1 1 2 1 "-" "agent" - host'
        fields = audit.tokenize_log(line)
        self.assertEqual(fields[2], "[02/Aug/2026:00:00:01 +0000]")
        self.assertEqual(fields[8], "GET /key HTTP/1.1")
        self.assertEqual(fields[9], "200")

    def test_state_write_is_valid_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            audit.write_state(path, {"phase": "armed"})
            self.assertEqual(json.loads(path.read_text()), {"phase": "armed"})

    def test_material_findings_are_assigned_to_owners(self):
        sources = {
            "material": {
                "estimated_monthly_request_cost_usd": 12.5,
                "repository": "nateyoder/project",
                "operation_counts": {"REST.GET.OBJECT": 10},
            },
            "immaterial": {
                "estimated_monthly_request_cost_usd": 1.0,
                "repository": "nateyoder/project",
                "operation_counts": {},
            },
        }
        self.assertEqual(
            audit.optimization_assignments(sources, 5),
            {"nateyoder/project": [{
                "bucket": "material",
                "estimated_monthly_request_cost_usd": 12.5,
                "dominant_operations": [("REST.GET.OBJECT", 10)],
            }]},
        )

    @mock.patch.object(audit, "gh")
    def test_followup_creation_uses_aggregate_report_only(self, gh):
        gh.side_effect = ["[]", "https://github.com/nateyoder/project/issues/1"]
        report = {
            "audit_id": "sample",
            "confidence": "medium",
            "known_limitations": ["best effort"],
            "optimization_assignments": {
                "nateyoder/project": [{
                    "bucket": "bucket",
                    "estimated_monthly_request_cost_usd": 9.5,
                    "dominant_operations": [["REST.GET.OBJECT", 12]],
                }]
            },
        }
        self.assertEqual(
            audit.file_followups(report),
            {"nateyoder/project": "https://github.com/nateyoder/project/issues/1"},
        )
        created = gh.call_args_list[1].args
        self.assertIn("--body-file", created)
        self.assertNotIn("requester", " ".join(created))


if __name__ == "__main__":
    unittest.main()
