"""Behavioural tests for individual manual patches."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import regex
from jsonschema import Draft7Validator, ValidationError, validators

from cfn_schemas.assembly import apply_patches, translate_custom_keywords

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


class TestBlockDeviceMappingKeys:
    """EC2 accepts several of Ebs/VirtualName/NoDevice, Auto Scaling exactly one (#83)."""

    # BlockDeviceMapping as published, with NoDevice left untyped: it is an object on
    # Instance, a string on LaunchTemplate/SpotFleet and a boolean on LaunchConfiguration,
    # and these cases exercise the presence keyword rather than NoDevice's type.
    BASE = {
        "definitions": {
            "BlockDeviceMapping": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "DeviceName": {"type": "string"},
                    "Ebs": {"type": "object"},
                    "NoDevice": {},
                    "VirtualName": {"type": "string"},
                },
            }
        }
    }

    def _validator(self, resource_dir: str):
        schema = apply_patches(self.BASE, _load_manual(resource_dir))
        standard = translate_custom_keywords(schema)
        return RegexValidator(standard["definitions"]["BlockDeviceMapping"])

    AT_LEAST_ONE = ["aws_ec2_instance", "aws_ec2_launchtemplate", "aws_ec2_spotfleet"]

    @pytest.mark.parametrize("resource_dir", AT_LEAST_ONE)
    @pytest.mark.parametrize(
        "mapping",
        [
            {"DeviceName": "/dev/sdh", "Ebs": {"VolumeSize": 8}},
            {"DeviceName": "/dev/sdh", "VirtualName": "ephemeral0"},
            {"DeviceName": "/dev/sdh", "NoDevice": ""},
            {"DeviceName": "/dev/sdh", "Ebs": {"VolumeSize": 8}, "VirtualName": "ephemeral0"},
            {"DeviceName": "/dev/sdh", "Ebs": {"VolumeSize": 8}, "NoDevice": ""},
            {"DeviceName": "/dev/sdh", "VirtualName": "ephemeral0", "NoDevice": ""},
            {
                "DeviceName": "/dev/sdh",
                "Ebs": {"VolumeSize": 8},
                "VirtualName": "ephemeral0",
                "NoDevice": "",
            },
        ],
    )
    def test_ec2_accepts_any_combination(self, resource_dir, mapping):
        assert list(self._validator(resource_dir).iter_errors(mapping)) == []

    @pytest.mark.parametrize("resource_dir", AT_LEAST_ONE)
    def test_ec2_rejects_none_present(self, resource_dir):
        assert list(self._validator(resource_dir).iter_errors({"DeviceName": "/dev/sdh"})) != []

    @pytest.mark.parametrize(
        "mapping",
        [
            {"DeviceName": "/dev/sdh", "Ebs": {"VolumeSize": 8}},
            {"DeviceName": "/dev/sdh", "VirtualName": "ephemeral0"},
            {"DeviceName": "/dev/sdh", "NoDevice": True},
        ],
    )
    def test_autoscaling_accepts_exactly_one(self, mapping):
        validator = self._validator("aws_autoscaling_launchconfiguration")
        assert list(validator.iter_errors(mapping)) == []

    @pytest.mark.parametrize(
        "mapping",
        [
            {"DeviceName": "/dev/sdh"},
            {"DeviceName": "/dev/sdh", "Ebs": {"VolumeSize": 8}, "VirtualName": "ephemeral0"},
            {"DeviceName": "/dev/sdh", "Ebs": {"VolumeSize": 8}, "NoDevice": True},
            # Auto Scaling rejects on the key being present, not on its value.
            {"DeviceName": "/dev/sdh", "Ebs": {"VolumeSize": 8}, "NoDevice": False},
            {"DeviceName": "/dev/sdh", "VirtualName": "ephemeral0", "NoDevice": True},
            {
                "DeviceName": "/dev/sdh",
                "Ebs": {"VolumeSize": 8},
                "VirtualName": "ephemeral0",
                "NoDevice": True,
            },
        ],
    )
    def test_autoscaling_rejects_combinations(self, mapping):
        validator = self._validator("aws_autoscaling_launchconfiguration")
        assert list(validator.iter_errors(mapping)) != []


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


class TestCodeBuildProjectEnums:
    """Source/Artifacts take the lowercase form of every value, Environment only aliases (#82)."""

    # The enum-bearing properties and Name as published in the AWS::CodeBuild::Project
    # provider schema, where each is an unconstrained string.
    BASE = {
        "properties": {"Name": {"type": "string"}},
        "definitions": {
            "Source": {"properties": {"Type": {"type": "string"}}},
            "Artifacts": {
                "properties": {
                    "Packaging": {"type": "string"},
                    "Type": {"type": "string"},
                }
            },
            "Environment": {
                "properties": {
                    "ComputeType": {"type": "string"},
                    "Type": {"type": "string"},
                }
            },
        },
    }

    ARN = "arn:aws:codebuild:us-east-1:123456789012:project/"

    @pytest.fixture(scope="class")
    def validator_for(self):
        schema = apply_patches(self.BASE, _load_manual("aws_codebuild_project"))

        def _validator(definition, prop):
            parent = schema["definitions"][definition] if definition else schema
            return RegexValidator(parent["properties"][prop])

        return _validator

    @pytest.mark.parametrize(
        "definition,prop,value",
        [
            ("Source", "Type", "BITBUCKET"),
            ("Source", "Type", "CODECOMMIT"),
            ("Source", "Type", "CODEPIPELINE"),
            ("Source", "Type", "GITHUB"),
            ("Source", "Type", "GITHUB_ENTERPRISE"),
            ("Source", "Type", "GITLAB"),
            ("Source", "Type", "GITLAB_SELF_MANAGED"),
            ("Source", "Type", "NO_SOURCE"),
            ("Source", "Type", "S3"),
            ("Source", "Type", "bitbucket"),
            ("Source", "Type", "codecommit"),
            ("Source", "Type", "codepipeline"),
            ("Source", "Type", "github"),
            ("Source", "Type", "github_enterprise"),
            ("Source", "Type", "gitlab"),
            ("Source", "Type", "gitlab_self_managed"),
            ("Source", "Type", "no_source"),
            ("Source", "Type", "s3"),
            ("Artifacts", "Type", "CODEPIPELINE"),
            ("Artifacts", "Type", "NO_ARTIFACTS"),
            ("Artifacts", "Type", "S3"),
            ("Artifacts", "Type", "codepipeline"),
            ("Artifacts", "Type", "no_artifacts"),
            ("Artifacts", "Type", "s3"),
            ("Artifacts", "Packaging", "NONE"),
            ("Artifacts", "Packaging", "ZIP"),
            ("Artifacts", "Packaging", "none"),
            ("Artifacts", "Packaging", "zip"),
            ("Environment", "Type", "ARM_CONTAINER"),
            ("Environment", "Type", "LINUX_CONTAINER"),
            ("Environment", "Type", "WINDOWS_SERVER_2022_CONTAINER"),
            ("Environment", "Type", "MAC_ARM"),
            ("Environment", "Type", "linuxContainer"),
            ("Environment", "Type", "container"),
            ("Environment", "ComputeType", "BUILD_GENERAL1_SMALL"),
            ("Environment", "ComputeType", "BUILD_LAMBDA_1GB"),
            ("Environment", "ComputeType", "CUSTOM_INSTANCE_TYPE"),
            ("Environment", "ComputeType", "ATTRIBUTE_BASED_COMPUTE"),
            ("Environment", "ComputeType", "small"),
            ("Environment", "ComputeType", "medium"),
            ("Environment", "ComputeType", "large"),
            ("Environment", "ComputeType", "xlarge"),
            ("Environment", "ComputeType", "twoxlarge"),
        ],
    )
    def test_accepts_enum_values(self, validator_for, definition, prop, value):
        assert list(validator_for(definition, prop).iter_errors(value)) == []

    @pytest.mark.parametrize(
        "definition,prop,value",
        [
            ("Source", "Type", "Codepipeline"),
            ("Source", "Type", "AZURE_DEV_OPS"),
            ("Source", "Type", "azure_dev_ops"),
            ("Artifacts", "Type", "noArtifacts"),
            ("Artifacts", "Packaging", "Zip"),
            # Environment rejects the lowercase form of its own canonical values.
            ("Environment", "Type", "armContainer"),
            ("Environment", "Type", "linux_container"),
            ("Environment", "Type", "linuxcontainer"),
            ("Environment", "Type", "LINUXCONTAINER"),
            ("Environment", "Type", "CONTAINER"),
            ("Environment", "Type", "Container"),
            ("Environment", "ComputeType", "2xlarge"),
            ("Environment", "ComputeType", "SMALL"),
            ("Environment", "ComputeType", "Small"),
            ("Environment", "ComputeType", "TWOXLARGE"),
            ("Environment", "ComputeType", "build_general1_small"),
            ("Environment", "ComputeType", "build_lambda_1gb"),
        ],
    )
    def test_rejects_enum_values(self, validator_for, definition, prop, value):
        assert list(validator_for(definition, prop).iter_errors(value)) != []

    @pytest.mark.parametrize(
        "name",
        [
            "MyProject",
            ARN + "MyProject",
            "arn:aws-us-gov:codebuild:us-gov-west-1:123456789012:project/P1",
            "arn:aws-cn:codebuild:cn-north-1:123456789012:project/P1",
            ARN + "P" * 150,
        ],
    )
    def test_accepts_name(self, validator_for, name):
        assert list(validator_for(None, "Name").iter_errors(name)) == []

    @pytest.mark.parametrize(
        "name",
        [
            "arn:aws:codebuild:us-east-1:12345:project/Short",
            "arn:aws:s3:::my-bucket",
            "arn:aws:codebuild:us-east-1:123456789012:build/P",
            "arn:azure:codebuild:us-east-1:123456789012:project/P",
            "arn:aws:codebuild:US-EAST-1:123456789012:project/P1",
            "notarn:aws:codebuild:us-east-1:123456789012:project/P",
            ARN + "x",
            ARN + "P" * 151,
            "x",
            "my_project!",
        ],
    )
    def test_rejects_name(self, validator_for, name):
        assert list(validator_for(None, "Name").iter_errors(name)) != []
