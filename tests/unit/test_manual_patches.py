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
