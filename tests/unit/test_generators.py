"""Tests for the schema generators and infrastructure."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from cfn_schemas.assembly import (
    apply_patches,
    assemble_schema,
    translate_custom_keywords,
    translate_patch_op,
)
from cfn_schemas.generators import GENERATORS
from cfn_schemas.generators.base import BaseGenerator
from cfn_schemas.generators.format import _ROLE_NAME_PATTERN
from cfn_schemas.resolver import RefResolutionError, RefResolver


class TestGeneratorRegistry:
    def test_all_generators_registered(self):
        expected = {"format", "lifecycle", "manual", "sam", "schemas", "smithy"}
        assert set(GENERATORS.keys()) == expected

    def test_generators_are_base_subclasses(self):
        for name, cls in GENERATORS.items():
            assert issubclass(cls, BaseGenerator), f"{name} is not a BaseGenerator"


class TestBaseGenerator:
    def test_resource_type_to_dir(self):
        assert BaseGenerator.resource_type_to_dir("AWS::EC2::Instance") == "aws_ec2_instance"
        assert BaseGenerator.resource_type_to_dir("AWS::S3::Bucket") == "aws_s3_bucket"

    def test_write_and_read_json(self, tmp_path):
        gen = _make_generator(tmp_path)
        data = {"key": "value", "nested": {"a": 1}}
        path = tmp_path / "test.json"
        gen.write_json(path, data)
        assert gen.read_json(path) == data

    def test_write_json_creates_parents(self, tmp_path):
        gen = _make_generator(tmp_path)
        path = tmp_path / "a" / "b" / "c.json"
        gen.write_json(path, {"x": 1})
        assert path.exists()


class TestRefResolver:
    def test_resolve_ref(self):
        schema = {
            "definitions": {"Tag": {"type": "object", "properties": {"Key": {"type": "string"}}}},
            "properties": {"Tags": {"$ref": "#/definitions/Tag"}},
        }
        r = RefResolver.from_schema(schema)
        _, resolved = r.resolve("#/definitions/Tag")
        assert resolved["type"] == "object"

    def test_resolve_nested(self):
        schema = {
            "definitions": {"Inner": {"type": "string"}, "Outer": {"$ref": "#/definitions/Inner"}},
        }
        r = RefResolver.from_schema(schema)
        _, resolved = r.resolve("#/definitions/Outer")
        assert resolved == {"$ref": "#/definitions/Inner"}

    def test_resolve_missing_raises(self):
        r = RefResolver.from_schema({"properties": {}})
        with pytest.raises(RefResolutionError):
            r.resolve("#/definitions/Missing")


class TestApplyPatches:
    def test_add_property(self):
        schema = {"properties": {"Name": {"type": "string"}}}
        patches = [{"op": "add", "path": "/properties/Name/maxLength", "value": 128}]
        result = apply_patches(schema, patches)
        assert result["properties"]["Name"]["maxLength"] == 128

    def test_add_nested(self):
        schema = {"properties": {}}
        patches = [{"op": "add", "path": "/properties/Type", "value": {"type": "string"}}]
        result = apply_patches(schema, patches)
        assert result["properties"]["Type"] == {"type": "string"}

    def test_remove(self):
        schema = {"properties": {"Name": {"type": "string", "enum": ["a", "b"]}}}
        patches = [{"op": "remove", "path": "/properties/Name/enum"}]
        result = apply_patches(schema, patches)
        assert "enum" not in result["properties"]["Name"]

    def test_add_root_level(self):
        schema = {"properties": {}}
        patches = [{"op": "add", "path": "/lifecycle", "value": {"status": "shutdown"}}]
        result = apply_patches(schema, patches)
        assert result["lifecycle"]["status"] == "shutdown"


class TestAssembleSchema:
    def test_assemble_with_patches(self, tmp_path):
        base = {"typeName": "AWS::Test::Resource", "properties": {"Name": {"type": "string"}}}
        patch1 = [{"op": "add", "path": "/properties/Name/maxLength", "value": 64}]
        patch2 = [{"op": "add", "path": "/lifecycle", "value": {"status": "sunset"}}]

        p1 = tmp_path / "format.json"
        p1.write_text(json.dumps(patch1))
        p2 = tmp_path / "lifecycle.json"
        p2.write_text(json.dumps(patch2))

        result = assemble_schema(base, [p1, p2])
        assert result["properties"]["Name"]["maxLength"] == 64
        assert result["lifecycle"]["status"] == "sunset"
        # Original not mutated
        assert "maxLength" not in base["properties"]["Name"]

    def test_assemble_no_patches(self):
        base = {"typeName": "AWS::Test::Resource"}
        result = assemble_schema(base, [])
        assert result == base


class TestTranslateCustomKeywords:
    def test_required_xor(self):
        schema = {"properties": {"A": {}, "B": {}}, "requiredXor": ["A", "B"]}
        result = translate_custom_keywords(schema)
        assert "requiredXor" not in result
        assert result["allOf"] == [{"oneOf": [{"required": ["A"], "properties": {"A": {"minItems": 1}}}, {"required": ["B"], "properties": {"B": {"minItems": 1}}}]}]

    def test_required_or(self):
        schema = {"properties": {"A": {}, "B": {}}, "requiredOr": ["A", "B"]}
        result = translate_custom_keywords(schema)
        assert "requiredOr" not in result
        assert result["allOf"] == [{"anyOf": [{"required": ["A"], "properties": {"A": {"minItems": 1}}}, {"required": ["B"], "properties": {"B": {"minItems": 1}}}]}]

    def test_dependent_excluded(self):
        schema = {"properties": {"A": {}, "B": {}}, "dependentExcluded": {"A": ["B"]}}
        result = translate_custom_keywords(schema)
        assert "dependentExcluded" not in result
        assert result["allOf"] == [
            {"allOf": [{"anyOf": [{"not": {"required": ["A"], "properties": {"A": {"minItems": 1}}}}, {"not": {"anyOf": [{"required": ["B"], "properties": {"B": {"minItems": 1}}}]}}]}]}
        ]

    def test_nested_translation(self):
        schema = {
            "properties": {"X": {"type": "string"}},
            "definitions": {"Inner": {"requiredXor": ["A", "B"]}},
        }
        result = translate_custom_keywords(schema)
        assert "requiredXor" not in result["definitions"]["Inner"]
        assert result["definitions"]["Inner"]["allOf"] == [
            {"oneOf": [{"required": ["A"], "properties": {"A": {"minItems": 1}}}, {"required": ["B"], "properties": {"B": {"minItems": 1}}}]}
        ]

    def test_preserves_existing_allof(self):
        schema = {"allOf": [{"required": ["X"]}], "requiredXor": ["A", "B"]}
        result = translate_custom_keywords(schema)
        assert len(result["allOf"]) == 2
        assert result["allOf"][0] == {"required": ["X"]}

    def test_enum_case_insensitive(self):
        schema = {"type": "string", "enumCaseInsensitive": ["KMS", "nodejs20.x"]}
        result = translate_custom_keywords(schema)
        assert "enumCaseInsensitive" not in result
        assert result["allOf"] == [{"pattern": "(?i)^(?:KMS|nodejs20\\.x)$"}]
        # the emitted pattern actually matches case-insensitively and anchors
        pattern = result["allOf"][0]["pattern"]
        assert re.match(pattern, "kms") and re.match(pattern, "NODEJS20.X")
        assert not re.match(pattern, "kms2")

    def test_leaves_annotations_alone(self):
        schema = {"lifecycle": {"status": "shutdown"}, "description": "hello"}
        result = translate_custom_keywords(schema)
        assert result["lifecycle"] == {"status": "shutdown"}
        assert result["description"] == "hello"
        assert "allOf" not in result

    def test_drops_untranslatable_keywords(self):
        schema = {"properties": {"Actions": {"type": "array", "maxUniqueItems": 5}}}
        result = translate_custom_keywords(schema)
        assert "maxUniqueItems" not in json.dumps(result)
        assert result["properties"]["Actions"]["type"] == "array"

    def test_unique_keys_becomes_unique_items(self):
        schema = {"type": "array", "uniqueKeys": ["Name"]}
        result = translate_custom_keywords(schema)
        assert "uniqueKeys" not in result
        assert result["uniqueItems"] is True

    def test_unique_keys_idempotent_with_existing_unique_items(self):
        schema = {"type": "array", "uniqueItems": True, "uniqueKeys": ["Name"]}
        result = translate_custom_keywords(schema)
        assert "uniqueKeys" not in result
        assert result["uniqueItems"] is True
        assert "allOf" not in result  # no redundant wrapper


class TestTranslatePatchOp:
    def test_required_xor_path(self):
        op = {"op": "add", "path": "/requiredXor", "value": ["A", "B"]}
        assert translate_patch_op(op) == {
            "op": "add",
            "path": "/oneOf",
            "value": [{"required": ["A"], "properties": {"A": {"minItems": 1}}}, {"required": ["B"], "properties": {"B": {"minItems": 1}}}],
        }

    def test_required_or_path(self):
        op = {"op": "add", "path": "/requiredOr", "value": ["A", "B"]}
        assert translate_patch_op(op) == {
            "op": "add",
            "path": "/anyOf",
            "value": [{"required": ["A"], "properties": {"A": {"minItems": 1}}}, {"required": ["B"], "properties": {"B": {"minItems": 1}}}],
        }

    def test_dependent_excluded_path(self):
        op = {"op": "add", "path": "/dependentExcluded", "value": {"A": ["B"]}}
        assert translate_patch_op(op) == {
            "op": "add",
            "path": "/allOf",
            "value": [{"anyOf": [{"not": {"required": ["A"], "properties": {"A": {"minItems": 1}}}}, {"not": {"anyOf": [{"required": ["B"], "properties": {"B": {"minItems": 1}}}]}}]}],
        }

    def test_enum_case_insensitive_nested_path(self):
        op = {
            "op": "add",
            "path": "/definitions/X/properties/Y/enumCaseInsensitive",
            "value": ["KMS", "nodejs20.x"],
        }
        result = translate_patch_op(op)
        assert result["path"] == "/definitions/X/properties/Y/pattern"
        assert result["value"] == "(?i)^(?:KMS|nodejs20\\.x)$"

    def test_passthrough_standard_op(self):
        op = {"op": "add", "path": "/properties/Foo/maxItems", "value": 5}
        assert translate_patch_op(op) == op

    def test_drops_max_unique_items_op(self):
        op = {"op": "add", "path": "/properties/AlarmActions/maxUniqueItems", "value": 5}
        assert translate_patch_op(op) is None

    def test_translates_unique_keys_op(self):
        op = {"op": "add", "path": "/properties/Attrs/uniqueKeys", "value": ["Name"]}
        assert translate_patch_op(op) == {
            "op": "add",
            "path": "/properties/Attrs/uniqueItems",
            "value": True,
        }

    def test_applied_standard_patch_has_no_custom_keywords(self):
        base = {"properties": {"A": {}, "B": {}}}
        raw = [{"op": "add", "path": "/requiredXor", "value": ["A", "B"]}]
        std = [translate_patch_op(o) for o in raw]
        result = apply_patches(base, std)
        assert "requiredXor" not in result
        assert result["oneOf"] == [{"required": ["A"], "properties": {"A": {"minItems": 1}}}, {"required": ["B"], "properties": {"B": {"minItems": 1}}}]


class TestLifecycleGenerator:
    def test_match_exact(self):
        from cfn_schemas.generators.lifecycle import _match_types
        types = ["AWS::QLDB::Ledger", "AWS::QLDB::Stream", "AWS::S3::Bucket"]
        assert _match_types(types, "AWS::QLDB") == ["AWS::QLDB::Ledger", "AWS::QLDB::Stream"]

    def test_match_prefix(self):
        from cfn_schemas.generators.lifecycle import _match_types
        types = ["AWS::WAF::Rule", "AWS::WAF::WebACL", "AWS::WAFRegional::Rule"]
        assert _match_types(types, "AWS::WAF::") == ["AWS::WAF::Rule", "AWS::WAF::WebACL"]

    def test_no_match(self):
        from cfn_schemas.generators.lifecycle import _match_types
        assert _match_types(["AWS::S3::Bucket"], "AWS::EC2") == []


class TestFormatGenerator:
    def test_variants_merge_not_clobber(self, tmp_path):
        """Multiple content-hashed schemas of one type must not clobber each
        other's format patches (issue #4642)."""
        from cfn_schemas.generators.format import FormatGenerator

        resources = tmp_path / "resources"
        resources.mkdir()
        # Richer variant (most regions): CertificateArn is a real property.
        (resources / "0_modern.json").write_text(
            json.dumps(
                {
                    "typeName": "AWS::CertificateManager::Certificate",
                    "primaryIdentifier": ["/properties/CertificateArn"],
                    "properties": {"CertificateArn": {"type": "string"}},
                }
            )
        )
        # Leaner variant (gov/cn/opt-in regions): no CertificateArn property.
        # Sorted-glob order puts this one last, so pre-fix it would win.
        (resources / "1_legacy.json").write_text(
            json.dumps(
                {
                    "typeName": "AWS::CertificateManager::Certificate",
                    "primaryIdentifier": ["/properties/Id"],
                    "properties": {"Id": {"type": "string"}},
                }
            )
        )

        FormatGenerator(schemas_dir=tmp_path).run()

        out = tmp_path / "patches" / "extensions" / (
            "aws_certificatemanager_certificate"
        ) / "format.json"
        paths = {p["path"] for p in json.loads(out.read_text())}
        assert "/properties/CertificateArn/format" in paths

    def _run_one(self, tmp_path, schema):
        from cfn_schemas.generators.format import FormatGenerator

        resources = tmp_path / "resources"
        resources.mkdir(exist_ok=True)
        (resources / "0.json").write_text(json.dumps(schema))
        FormatGenerator(schemas_dir=tmp_path).run()
        out = (
            tmp_path
            / "patches"
            / "extensions"
            / BaseGenerator.resource_type_to_dir(schema["typeName"])
            / "format.json"
        )
        if not out.exists():
            return {}
        return {p["path"]: p.get("value") for p in json.loads(out.read_text())}

    def test_launchtemplate_securitygroups_are_names(self, tmp_path):
        """SecurityGroups on a LaunchTemplate are names, not ids (issue #52)."""
        patches = self._run_one(
            tmp_path,
            {
                "typeName": "AWS::EC2::LaunchTemplate",
                "properties": {},
                "definitions": {
                    "LaunchTemplateData": {
                        "type": "object",
                        "properties": {
                            "SecurityGroups": {"type": "array", "items": {"type": "string"}},
                            "SecurityGroupIds": {"type": "array", "items": {"type": "string"}},
                        },
                    }
                },
            },
        )
        sg = "/definitions/LaunchTemplateData/properties/SecurityGroups/items/format"
        ids = "/definitions/LaunchTemplateData/properties/SecurityGroupIds/items/format"
        assert patches[sg] == "AWS::EC2::SecurityGroup.Name"
        assert patches[ids] == "AWS::EC2::SecurityGroup.Id"

    def test_pipes_subnets_get_no_id_format(self, tmp_path):
        """Pipes target-parameter Subnets accept dynamic $. paths (issue #51)."""
        patches = self._run_one(
            tmp_path,
            {
                "typeName": "AWS::Pipes::Pipe",
                "properties": {},
                "definitions": {
                    "AwsVpcConfiguration": {
                        "type": "object",
                        "properties": {
                            "Subnets": {"type": "array", "items": {"type": "string"}}
                        },
                    }
                },
            },
        )
        assert not any("Subnets" in path for path in patches)

    def test_attribute_formats_added(self, tmp_path):
        """Read-only attributes that return an ARN/id get a format (issue #53)."""
        kms = self._run_one(
            tmp_path,
            {
                "typeName": "AWS::KMS::ReplicaKey",
                "properties": {"Arn": {"type": "string"}},
            },
        )
        assert kms["/properties/Arn/format"] == "AWS::KMS::Key.Arn"
        sm = self._run_one(
            tmp_path,
            {
                "typeName": "AWS::SageMaker::Domain",
                "properties": {"SecurityGroupIdForDomainBoundary": {"type": "string"}},
            },
        )
        assert sm["/properties/SecurityGroupIdForDomainBoundary/format"] == "AWS::EC2::SecurityGroup.Id"

    def test_empty_shared_definition_not_stamped(self, tmp_path):
        """Empty {} shared defs are skipped; non-empty shared defs still stamp (#31)."""
        patches = self._run_one(
            tmp_path,
            {
                "typeName": "AWS::Example::Thing",
                "properties": {},
                "definitions": {
                    "PassThroughProp": {},
                    "SharedRole": {"type": "string"},
                    "A": {
                        "type": "object",
                        "properties": {"FooRoleArn": {"$ref": "#/definitions/PassThroughProp"}},
                    },
                    "B": {
                        "type": "object",
                        "properties": {"BarRoleArn": {"$ref": "#/definitions/SharedRole"}},
                    },
                    "C": {
                        "type": "object",
                        "properties": {"BazRoleArn": {"$ref": "#/definitions/SharedRole"}},
                    },
                },
            },
        )
        assert "/definitions/PassThroughProp/format" not in patches
        assert patches["/definitions/SharedRole/format"] == "AWS::IAM::Role.Arn"

    def test_arn_only_role_properties_get_plain_format(self, tmp_path):
        """*Role properties that accept an ARN only get a plain format."""
        spot = self._run_one(
            tmp_path,
            {
                "typeName": "AWS::EC2::SpotFleet",
                "properties": {},
                "definitions": {
                    "SpotFleetRequestConfigData": {
                        "type": "object",
                        "properties": {"IamFleetRole": {"type": "string"}},
                    }
                },
            },
        )
        assert (
            spot[
                "/definitions/SpotFleetRequestConfigData"
                "/properties/IamFleetRole/format"
            ]
            == "AWS::IAM::Role.Arn"
        )

        bucket = self._run_one(
            tmp_path,
            {
                "typeName": "AWS::S3::Bucket",
                "properties": {},
                "definitions": {
                    "ReplicationConfiguration": {
                        "type": "object",
                        "properties": {"Role": {"type": "string"}},
                    }
                },
            },
        )
        assert (
            bucket["/definitions/ReplicationConfiguration/properties/Role/format"]
            == "AWS::IAM::Role.Arn"
        )

    def test_batch_role_properties_accept_arn_or_name(self, tmp_path):
        """Batch accepts an ARN or a role name, so these get an anyOf."""
        batch = self._run_one(
            tmp_path,
            {
                "typeName": "AWS::Batch::ComputeEnvironment",
                "properties": {"ServiceRole": {"type": "string"}},
                "definitions": {
                    "ComputeResources": {
                        "type": "object",
                        "properties": {"SpotIamFleetRole": {"type": "string"}},
                    }
                },
            },
        )
        expected = [
            {"format": "AWS::IAM::Role.Arn"},
            {"pattern": _ROLE_NAME_PATTERN},
        ]
        assert batch["/properties/ServiceRole/anyOf"] == expected
        assert (
            batch[
                "/definitions/ComputeResources/properties/SpotIamFleetRole/anyOf"
            ]
            == expected
        )
        # an ARN-only format must NOT be assigned alongside the anyOf
        assert "/properties/ServiceRole/format" not in batch

    @pytest.mark.parametrize(
        "value,accepted",
        [
            # a role ARN, with and without a path -- matched by the format branch
            ("arn:aws:iam::123456789012:role/AWSBatchServiceRole", True),
            (
                "arn:aws:iam::123456789012:role/service-role/AWSBatchServiceRole",
                True,
            ),
            # a bare role name, and the path-prefixed form Batch documents
            ("AWSBatchServiceRole", True),
            ("/foo/bar", True),
            # a malformed ARN must satisfy neither branch
            ("arn:aws:iam::123456789012:notarole/x", False),
            # no documented empty sentinel for these properties
            ("", False),
        ],
    )
    def test_role_name_pattern_table(self, value, accepted):
        """The anyOf accepts exactly the documented ARN and name forms."""
        # cfn-lint's AWS::IAM::Role.Arn format (rule E1156)
        fmt = r"^arn:aws[a-zA-Z-]*:iam::\d{12}:role/.+$"
        matched = bool(re.match(fmt, value)) or bool(
            re.match(_ROLE_NAME_PATTERN, value)
        )
        assert matched is accepted

    def test_role_name_pattern_matches_aws_documented_form(self):
        """The literal pattern equals AWS's \\u-escaped IAM path/name spec."""
        assert _ROLE_NAME_PATTERN == (
            "^(\u002F[\u0021-\u007E]+\u002F)?[a-zA-Z0-9_+=,.@-]{1,64}$"
        )


class TestManualGenerator:
    def test_validates_good_patches(self, tmp_path):
        _make_generator(tmp_path)
        patches_dir = tmp_path / "schemas" / "patches" / "extensions" / "aws_s3_bucket"
        patches_dir.mkdir(parents=True)
        (patches_dir / "manual.json").write_text(
            json.dumps([{"op": "add", "path": "/properties/X/enum", "value": ["a"]}])
        )
        from cfn_schemas.generators.manual import ManualPatchesGenerator
        m = ManualPatchesGenerator(schemas_dir=tmp_path / "schemas")
        m.run()  # should not raise

    def test_rejects_bad_op(self, tmp_path):
        patches_dir = tmp_path / "schemas" / "patches" / "extensions" / "aws_s3_bucket"
        patches_dir.mkdir(parents=True)
        (patches_dir / "manual.json").write_text(
            json.dumps([{"op": "invalid", "path": "/x"}])
        )
        from cfn_schemas.generators.manual import ManualPatchesGenerator
        m = ManualPatchesGenerator(schemas_dir=tmp_path / "schemas")
        with pytest.raises(ValueError):
            m.run()


class TestSamOverrides:
    def test_endpoint_configuration_string_branch_added(self, tmp_path):
        from cfn_schemas.generators.sam import SamGenerator

        gen = SamGenerator(schemas_dir=tmp_path / "schemas")
        schema = {
            "typeName": "AWS::Serverless::Api",
            "properties": {
                "EndpointConfiguration": {
                    "anyOf": [
                        {"type": "object"},
                        {"$ref": "#/definitions/EndpointConfiguration"},
                    ]
                }
            },
        }
        gen._apply_overrides(schema, "AWS::Serverless::Api")
        branches = schema["properties"]["EndpointConfiguration"]["anyOf"]
        # String shorthand (cfn-lint#4736) added; existing forms untouched.
        assert {"type": "string", "enum": ["EDGE", "REGIONAL", "PRIVATE"]} in branches
        assert {"type": "object"} in branches
        assert {"$ref": "#/definitions/EndpointConfiguration"} in branches

    def test_apply_overrides_is_idempotent(self, tmp_path):
        from cfn_schemas.generators.sam import SamGenerator

        gen = SamGenerator(schemas_dir=tmp_path / "schemas")
        schema = {"properties": {"EndpointConfiguration": {"anyOf": [{"type": "object"}]}}}
        gen._apply_overrides(schema, "AWS::Serverless::Api")
        gen._apply_overrides(schema, "AWS::Serverless::Api")
        branches = schema["properties"]["EndpointConfiguration"]["anyOf"]
        assert branches.count(
            {"type": "string", "enum": ["EDGE", "REGIONAL", "PRIVATE"]}
        ) == 1

    def test_no_override_for_unlisted_type(self, tmp_path):
        from cfn_schemas.generators.sam import SamGenerator

        gen = SamGenerator(schemas_dir=tmp_path / "schemas")
        schema = {"properties": {"EndpointConfiguration": {"anyOf": [{"type": "object"}]}}}
        gen._apply_overrides(schema, "AWS::Serverless::Function")
        assert schema["properties"]["EndpointConfiguration"]["anyOf"] == [{"type": "object"}]


def _make_generator(tmp_path: Path) -> BaseGenerator:
    """Create a concrete generator for testing base class methods."""
    schemas_dir = tmp_path / "schemas"
    schemas_dir.mkdir(exist_ok=True)

    class _TestGen(BaseGenerator):
        def run(self):
            pass

    return _TestGen(schemas_dir=schemas_dir)
