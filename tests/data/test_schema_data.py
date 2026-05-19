"""Data integrity tests for assembled schema output.

Run against a build directory:
    pytest tests/data/ -v --build-dir build/
"""

from __future__ import annotations

import json

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


