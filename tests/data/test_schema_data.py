"""Data integrity tests for assembled schema output.

Run against a build directory:
    pytest tests/data/ -v --build-dir build/
"""

from __future__ import annotations

import json
from pathlib import Path

import referencing
from jsonschema import Draft7Validator
from referencing import jsonschema as ref_jsonschema

_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"


def _build_meta_validator() -> Draft7Validator:
    """Build a validator for the CFN Resource Provider meta-schema."""
    draft7 = json.loads((_FIXTURES_DIR / "draft7.json").read_text())
    base = json.loads((_FIXTURES_DIR / "base.definition.schema.v1.json").read_text())
    provider = json.loads((_FIXTURES_DIR / "provider.definition.schema.v1.json").read_text())

    base.pop("$id", None)
    provider.pop("$id", None)

    registry = referencing.Registry().with_resources([
        ("http://json-schema.org/draft-07/schema#", ref_jsonschema.DRAFT7.create_resource(draft7)),
        ("http://json-schema.org/draft-07/schema", ref_jsonschema.DRAFT7.create_resource(draft7)),
        ("base.definition.schema.v1.json", ref_jsonschema.DRAFT7.create_resource(base)),
        (
            "provider.configuration.definition.schema.v1",
            ref_jsonschema.DRAFT7.create_resource({"type": "object"}),
        ),
    ])

    return Draft7Validator(provider, registry=registry)


# --- cfn-lint output validation ---


class TestCfnLintOutput:
    def test_providers_exist(self, cfnlint_dir):
        providers = list((cfnlint_dir / "providers").glob("*.json"))
        assert len(providers) > 30, f"Only {len(providers)} provider files"

    def test_resources_exist(self, cfnlint_dir):
        resources = list((cfnlint_dir / "resources").glob("*.json"))
        assert len(resources) > 1000, f"Only {len(resources)} resource files"

    def test_provider_references_resolve(self, cfnlint_dir):
        resources_dir = cfnlint_dir / "resources"
        missing = []
        for provider_file in sorted((cfnlint_dir / "providers").glob("*.json")):
            mappings = json.loads(provider_file.read_text())
            for rt, h in mappings.items():
                if not (resources_dir / f"{h}.json").exists():
                    missing.append(f"{provider_file.stem}/{rt}")
        assert not missing, f"Missing schemas: {missing[:10]}"

    def test_no_orphaned_resources(self, cfnlint_dir):
        referenced = set()
        for f in (cfnlint_dir / "providers").glob("*.json"):
            referenced.update(json.loads(f.read_text()).values())
        resources = [f.stem for f in (cfnlint_dir / "resources").glob("*.json")]
        orphaned = [r for r in resources if r not in referenced]
        assert not orphaned, f"Orphaned: {orphaned[:10]}"

    def test_schemas_have_typename(self, cfnlint_dir):
        failures = []
        for f in sorted((cfnlint_dir / "resources").glob("*.json")):
            data = json.loads(f.read_text())
            if "typeName" not in data:
                failures.append(f.stem)
        assert not failures, f"Missing typeName: {failures[:10]}"

    def test_schemas_are_valid_json(self, cfnlint_dir):
        failures = []
        for f in sorted((cfnlint_dir / "resources").glob("*.json")):
            try:
                data = json.loads(f.read_text())
                assert isinstance(data, dict)
            except (json.JSONDecodeError, AssertionError):
                failures.append(f.stem)
        assert not failures, f"Invalid JSON: {failures[:10]}"

    def test_no_broken_refs(self, cfnlint_dir):
        failures = []
        for f in sorted((cfnlint_dir / "resources").glob("*.json")):
            data = json.loads(f.read_text())
            broken = _find_broken_refs(data)
            if broken:
                tn = data.get("typeName", f.stem)
                failures.append(f"{tn}: {broken[0]}")
        assert not failures, (
            f"{len(failures)} schemas with broken $ref:\n"
            + "\n".join(failures[:20])
        )


# --- Standard output validation ---


class TestStandardOutput:
    def test_no_custom_validation_keywords(self, standard_dir):
        custom_keywords = {"requiredXor", "requiredOr", "dependentExcluded"}
        failures = []
        for f in sorted(standard_dir.glob("*.json")):
            content = f.read_text()
            for kw in custom_keywords:
                if f'"{kw}"' in content:
                    failures.append(f"{f.stem}: has {kw}")
        assert not failures, "Custom keywords found:\n" + "\n".join(failures[:20])


class TestMetaSchemaValidation:
    def test_cfnlint_schemas_pass_meta_schema(self, cfnlint_dir):
        """Every assembled schema must conform to the CFN provider meta-schema."""
        validator = _build_meta_validator()
        failures = []
        for f in sorted((cfnlint_dir / "resources").glob("*.json")):
            data = json.loads(f.read_text())
            errors = list(validator.iter_errors(data))
            if errors:
                tn = data.get("typeName", f.stem)
                for e in errors:
                    failures.append(f"{tn}: {e.message}")
        assert not failures, (
            f"{len(failures)} meta-schema violations:\n"
            + "\n".join(failures[:30])
        )

    def test_json_pointer_properties_resolve(self, cfnlint_dir):
        """All JSON pointers in readOnly/writeOnly/etc must resolve within the schema."""
        pointer_sections = [
            "readOnlyProperties",
            "writeOnlyProperties",
            "conditionalCreateOnlyProperties",
            "nonPublicProperties",
            "nonPublicDefinitions",
            "createOnlyProperties",
            "deprecatedProperties",
            "primaryIdentifier",
        ]
        failures = []
        for f in sorted((cfnlint_dir / "resources").glob("*.json")):
            data = json.loads(f.read_text())
            tn = data.get("typeName", f.stem)
            for section in pointer_sections:
                for pointer in data.get(section, []):
                    if _resolve_cfn_pointer(data, pointer) is None:
                        failures.append(f"{tn}: {section} -> {pointer}")
        assert not failures, (
            f"{len(failures)} unresolvable JSON pointers:\n"
            + "\n".join(failures[:30])
        )


class TestPatchConflicts:
    def test_no_manual_smithy_value_conflicts(self, extensions_dir):
        """Manual patches should not conflict with smithy on the same path."""
        conflicts = []
        for resource_dir in sorted(extensions_dir.iterdir()):
            if not resource_dir.is_dir():
                continue
            manual_file = resource_dir / "manual.json"
            smithy_file = resource_dir / "smithy.json"
            if not manual_file.exists() or not smithy_file.exists():
                continue
            manual = json.loads(manual_file.read_text())
            smithy = json.loads(smithy_file.read_text())
            manual_by_path = {
                p["path"]: p.get("value")
                for p in manual if p.get("op") == "add"
            }
            smithy_by_path = {
                p["path"]: p.get("value")
                for p in smithy if p.get("op") == "add"
            }
            for path in set(manual_by_path) & set(smithy_by_path):
                if manual_by_path[path] != smithy_by_path[path]:
                    conflicts.append(
                        f"{resource_dir.name}: {path} "
                        f"(manual={manual_by_path[path]!r}, "
                        f"smithy={smithy_by_path[path]!r})"
                    )
        assert not conflicts, (
            f"{len(conflicts)} manual/smithy conflicts:\n"
            + "\n".join(conflicts[:20])
        )


def _collect_refs(obj, path=""):
    """Collect all $ref values in a schema."""
    refs = []
    if isinstance(obj, dict):
        if "$ref" in obj:
            refs.append(obj["$ref"])
        for k, v in obj.items():
            refs.extend(_collect_refs(v, f"{path}/{k}"))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            refs.extend(_collect_refs(v, f"{path}/{i}"))
    return refs


def _find_broken_refs(schema):
    """Find $ref pointers that don't resolve within the schema."""
    definitions = schema.get("definitions", {})
    broken = []
    for ref in _collect_refs(schema):
        if not ref.startswith("#/definitions/"):
            continue
        def_name = ref[len("#/definitions/"):]
        if def_name not in definitions:
            broken.append(ref)
    return broken


def _resolve_cfn_pointer(schema, pointer):
    """Resolve a CFN-style JSON pointer within a schema.

    CFN pointers use '/properties/X/Y' navigation and '*' for array items.
    Follows $ref links and traverses anyOf/allOf/oneOf.
    """
    parts = [p for p in pointer.split("/") if p]
    if not parts:
        return schema
    return _walk_cfn_pointer(schema, schema, parts[1:])


def _walk_cfn_pointer(root, document, pointer):
    if isinstance(document, dict) and "$ref" in document:
        ref = document["$ref"]
        resolved = _resolve_local_ref(root, ref) if ref.startswith("#/") else None
        return _walk_cfn_pointer(root, resolved, pointer) if resolved else None

    if not pointer:
        return document

    point = pointer[0]
    result = None

    if point == "*":
        if isinstance(document, dict) and "items" in document:
            result = _walk_cfn_pointer(root, document["items"], pointer[1:])
    elif isinstance(document, dict):
        props = document.get("properties", {})
        if point in props:
            result = _walk_cfn_pointer(root, props[point], pointer[1:])
        if result is None:
            for combiner in ("anyOf", "allOf", "oneOf"):
                for item in document.get(combiner, []):
                    result = _walk_cfn_pointer(root, item, [point] + pointer[1:])
                    if result is not None:
                        break
                if result is not None:
                    break

    return result


def _resolve_local_ref(root, ref):
    parts = ref[2:].split("/")
    target = root
    for part in parts:
        if isinstance(target, dict) and part in target:
            target = target[part]
        else:
            return None
    return target


