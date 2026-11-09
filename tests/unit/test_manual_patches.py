"""Behavioural tests for individual manual patches."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import regex
from jsonschema import Draft7Validator, ValidationError, validators

from cfn_schemas.assembly import apply_patches

EXTENSIONS_DIR = Path(__file__).parent.parent.parent / "schemas" / "patches" / "extensions"


def _regex_pattern(validator, pattern, instance, schema):
    # The stdlib re module rejects \p{...} classes; cfn-lint uses the regex module.
    if validator.is_type(instance, "string") and not regex.search(pattern, instance):
        yield ValidationError(f"{instance!r} does not match {pattern!r}")


RegexValidator = validators.extend(Draft7Validator, {"pattern": _regex_pattern})


def _load_manual(resource_dir: str) -> list[dict]:
    return json.loads((EXTENSIONS_DIR / resource_dir / "manual.json").read_text())


class TestSqsQueueTags:
    # Tag definition as published in the AWS::SQS::Queue provider schema.
    BASE = {
        "definitions": {
            "Tag": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "Key": {"type": "string"},
                    "Value": {"type": "string"},
                },
                "required": ["Value", "Key"],
            }
        }
    }

    @pytest.fixture(scope="class")
    def tag_validator(self):
        schema = apply_patches(self.BASE, _load_manual("aws_sqs_queue"))
        return RegexValidator(schema["definitions"]["Tag"])

    @pytest.mark.parametrize(
        "tag",
        [
            {"Key": "Description", "Value": "Placeholder queue - unused"},
            {"Key": "team:owner", "Value": "a.b_c/d=e+f-g@h"},
            {"Key": "Umgebung", "Value": "Größe 42 née"},
            {"Key": "Empty", "Value": ""},
            {"Key": "k" * 128, "Value": "v" * 256},
        ],
    )
    def test_accepts_valid_tags(self, tag_validator, tag):
        assert list(tag_validator.iter_errors(tag)) == []

    @pytest.mark.parametrize(
        "tag",
        [
            {"Key": "Description", "Value": "Placeholder queue (unused)"},
            {"Key": "Cost#Center", "Value": "x"},
            {"Key": "Description", "Value": "semi;colon"},
            {"Key": "Description", "Value": "Placeholder queue — unused"},
            {"Key": "Range", "Value": "1–2"},
            {"Key": "Cost—Center", "Value": "x"},
            {"Key": "", "Value": "x"},
            {"Key": "k" * 129, "Value": "x"},
            {"Key": "Description", "Value": "v" * 257},
        ],
    )
    def test_rejects_invalid_tags(self, tag_validator, tag):
        assert list(tag_validator.iter_errors(tag)) != []



class TestServerlessFunctionAlarms:
    """DeploymentPreference.Alarms accepts a list of alarm names (#54)."""

    # DeploymentPreference.Alarms as published in the upstream SAM schema.
    BASE = {
        "properties": {"Arn": {"type": "string"}},
        "definitions": {
            "DeploymentPreference": {
                "type": "object",
                "properties": {
                    "Alarms": {
                        "anyOf": [
                            {"type": "object"},
                            {"type": "array", "items": {"type": "object"}},
                        ]
                    }
                },
            }
        },
    }

    def _alarms_schema(self):
        patched = apply_patches(self.BASE, _load_manual("aws_serverless_function"))
        return patched["definitions"]["DeploymentPreference"]["properties"]["Alarms"]

    def test_accepts_list_of_names(self):
        RegexValidator(self._alarms_schema()).validate(["MyService-High-5xx-Alarm"])

    def test_rejects_non_list_scalar(self):
        with pytest.raises(ValidationError):
            RegexValidator(self._alarms_schema()).validate("not-a-list")


class TestIamManagedPolicyArns:
    """IAM normalizes the ARN account segment, so aws is case-insensitive (#85)."""

    # ManagedPolicyArns as published in the AWS::IAM::{Role,User,Group} schemas.
    BASE = {
        "properties": {
            "ManagedPolicyArns": {"type": "array", "items": {"type": "string"}}
        }
    }

    @pytest.fixture(scope="class", params=["aws_iam_role", "aws_iam_user", "aws_iam_group"])
    def arns_validator(self, request):
        schema = apply_patches(self.BASE, _load_manual(request.param))
        return RegexValidator(schema["properties"]["ManagedPolicyArns"])

    @pytest.mark.parametrize(
        "arn",
        [
            "arn:aws:iam::aws:policy/ReadOnlyAccess",
            "arn:aws:iam::Aws:policy/ReadOnlyAccess",
            "arn:aws:iam::AWS:policy/ReadOnlyAccess",
            "arn:aws:iam::aWs:policy/ReadOnlyAccess",
            "arn:aws-us-gov:iam::AWS:policy/ReadOnlyAccess",
            "arn:aws:iam::123456789012:policy/MyCustomPolicy",
            "arn:aws:iam::aws:policy/service-role/AmazonEC2RoleforSSM",
        ],
    )
    def test_accepts_valid_arns(self, arns_validator, arn):
        assert list(arns_validator.iter_errors([arn])) == []

    @pytest.mark.parametrize(
        "arn",
        [
            "arn:aws:iam::AWSX:policy/ReadOnlyAccess",
            "arn:aws:iam::12345:policy/ReadOnlyAccess",
            "arn:aws:iam::aws:policy/",
            "arn:aws:iam::aws:role/ReadOnlyAccess",
        ],
    )
    def test_rejects_invalid_arns(self, arns_validator, arn):
        assert list(arns_validator.iter_errors([arn])) != []


class TestDlmCreateRuleInterval:
    """CreateRule.Interval is an enumerated set, and only without a CronExpression (#84)."""

    # CreateRule as published in the AWS::DLM::LifecyclePolicy provider schema.
    BASE = {
        "definitions": {
            "CreateRule": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "CronExpression": {"type": "string"},
                    "Interval": {"type": "integer"},
                    "IntervalUnit": {"type": "string"},
                    "Location": {"type": "string"},
                },
            }
        }
    }

    @pytest.fixture(scope="class")
    def create_rule_validator(self):
        schema = apply_patches(self.BASE, _load_manual("aws_dlm_lifecyclepolicy"))
        return RegexValidator(schema["definitions"]["CreateRule"])

    @pytest.mark.parametrize(
        "create_rule",
        [
            {"Interval": 1, "IntervalUnit": "HOURS"},
            {"Interval": 24, "IntervalUnit": "HOURS"},
            {"IntervalUnit": "HOURS"},
            # Not validated by DLM when a cron expression drives the schedule.
            {"Interval": 0, "CronExpression": "cron(0 12 * * ? *)"},
            {"Interval": 5, "CronExpression": "cron(0 12 * * ? *)"},
        ],
    )
    def test_accepts(self, create_rule_validator, create_rule):
        assert list(create_rule_validator.iter_errors(create_rule)) == []

    @pytest.mark.parametrize(
        "create_rule",
        [
            {"Interval": 0, "IntervalUnit": "HOURS"},
            {"Interval": 5, "IntervalUnit": "HOURS"},
            {"Interval": 25, "IntervalUnit": "HOURS"},
        ],
    )
    def test_rejects(self, create_rule_validator, create_rule):
        assert list(create_rule_validator.iter_errors(create_rule)) != []
