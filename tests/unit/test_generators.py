"""Tests for the schema generators and infrastructure."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cfn_schemas.assembly import apply_patches, assemble_schema, translate_custom_keywords
from cfn_schemas.generators import GENERATORS
from cfn_schemas.generators.base import BaseGenerator
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
        assert result["allOf"] == [{"oneOf": [{"required": ["A"]}, {"required": ["B"]}]}]

    def test_required_or(self):
        schema = {"properties": {"A": {}, "B": {}}, "requiredOr": ["A", "B"]}
        result = translate_custom_keywords(schema)
        assert "requiredOr" not in result
        assert result["allOf"] == [{"anyOf": [{"required": ["A"]}, {"required": ["B"]}]}]

    def test_dependent_excluded(self):
        schema = {"properties": {"A": {}, "B": {}}, "dependentExcluded": {"A": ["B"]}}
        result = translate_custom_keywords(schema)
        assert "dependentExcluded" not in result
        assert result["allOf"] == [
            {"dependencies": {"A": {"not": {"anyOf": [{"required": ["B"]}]}}}}
        ]

    def test_nested_translation(self):
        schema = {
            "properties": {"X": {"type": "string"}},
            "definitions": {"Inner": {"requiredXor": ["A", "B"]}},
        }
        result = translate_custom_keywords(schema)
        assert "requiredXor" not in result["definitions"]["Inner"]
        assert result["definitions"]["Inner"]["allOf"] == [
            {"oneOf": [{"required": ["A"]}, {"required": ["B"]}]}
        ]

    def test_preserves_existing_allof(self):
        schema = {"allOf": [{"required": ["X"]}], "requiredXor": ["A", "B"]}
        result = translate_custom_keywords(schema)
        assert len(result["allOf"]) == 2
        assert result["allOf"][0] == {"required": ["X"]}

    def test_leaves_annotations_alone(self):
        schema = {"lifecycle": {"status": "shutdown"}, "enumCaseInsensitive": ["a", "b"]}
        result = translate_custom_keywords(schema)
        assert result["lifecycle"] == {"status": "shutdown"}
        assert result["enumCaseInsensitive"] == ["a", "b"]


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


def _make_generator(tmp_path: Path) -> BaseGenerator:
    """Create a concrete generator for testing base class methods."""
    schemas_dir = tmp_path / "schemas"
    schemas_dir.mkdir(exist_ok=True)

    class _TestGen(BaseGenerator):
        def run(self):
            pass

    return _TestGen(schemas_dir=schemas_dir)
