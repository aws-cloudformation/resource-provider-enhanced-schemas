"""Assemble final schemas by applying patches to base schemas.

Produces two output formats:
- cfn-lint: content-addressed schemas with region mappings (providers/ + resources/)
- standard: flat per-resource-type files with custom keywords translated
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _navigate(target: Any, part: str) -> tuple[Any, bool]:
    """Navigate one level into a schema node. Returns (value, found)."""
    if isinstance(target, dict) and part in target:
        return target[part], True
    if isinstance(target, list):
        try:
            return target[int(part)], True
        except (ValueError, IndexError):
            return None, False
    return None, False


def apply_patches(
    schema: dict[str, Any], patches: list[dict[str, Any]]
) -> dict[str, Any]:
    """Apply RFC 6902-style JSON patches to a schema."""
    for patch in patches:
        op = patch.get("op")
        path = patch.get("path", "")
        value = patch.get("value")

        parts = [p for p in path.split("/") if p]
        if not parts:
            if op == "add" and isinstance(value, dict):
                schema.update(value)
            continue

        if op == "test":
            target = schema
            for part in parts:
                target, found = _navigate(target, part)
                if not found:
                    return schema
            if target != value:
                return schema
            continue

        target = schema
        for part in parts[:-1]:
            next_target, found = _navigate(target, part)
            if found:
                target = next_target
            elif isinstance(target, dict):
                target = target.setdefault(part, {})
            else:
                break

        key = parts[-1]
        if op == "add":
            if isinstance(target, list) and key == "-":
                target.append(value)
            elif isinstance(target, list):
                try:
                    target[int(key)] = value
                except (ValueError, IndexError):
                    pass
            elif isinstance(target, dict):
                target[key] = value
        elif op == "replace":
            if isinstance(target, dict) and key in target:
                target[key] = value
            elif isinstance(target, list):
                try:
                    target[int(key)] = value
                except (ValueError, IndexError):
                    pass
        elif op == "remove":
            if isinstance(target, dict) and key in target:
                del target[key]

    return schema


def assemble_schema(
    base_schema: dict[str, Any],
    patch_files: list[Path],
) -> dict[str, Any]:
    """Apply all patch files to a base schema."""
    schema = json.loads(json.dumps(base_schema))  # deep copy
    for patch_file in patch_files:
        patches = json.loads(patch_file.read_text())
        schema = apply_patches(schema, patches)
    return schema


def _translate_required_xor(properties: list[str]) -> dict[str, Any]:
    return {"oneOf": [{"required": [p]} for p in properties]}


def _translate_required_or(properties: list[str]) -> dict[str, Any]:
    return {"anyOf": [{"required": [p]} for p in properties]}


def _translate_dependent_excluded(
    properties: dict[str, list[str]],
) -> dict[str, Any]:
    dependencies: dict[str, Any] = {}
    for prop, exclusions in properties.items():
        dependencies[prop] = {
            "not": {"anyOf": [{"required": [e]} for e in exclusions]}
        }
    return {"dependencies": dependencies}


_TRANSLATORS: dict[str, Any] = {
    "requiredXor": _translate_required_xor,
    "requiredOr": _translate_required_or,
    "dependentExcluded": _translate_dependent_excluded,
}


def translate_custom_keywords(schema: Any) -> Any:
    """Translate custom validation keywords into standard JSON Schema.

    Walks the schema tree and replaces:
    - requiredXor -> oneOf with required
    - requiredOr -> anyOf with required
    - dependentExcluded -> dependencies with not/anyOf
    """
    if isinstance(schema, list):
        return [translate_custom_keywords(item) for item in schema]
    if not isinstance(schema, dict):
        return schema

    result: dict[str, Any] = {}
    all_of: list[dict[str, Any]] = []

    for key, value in schema.items():
        if key in _TRANSLATORS:
            all_of.append(_TRANSLATORS[key](value))
        else:
            result[key] = translate_custom_keywords(value)

    if all_of:
        existing = result.get("allOf", [])
        result["allOf"] = existing + all_of

    return result


def _format_json(data: Any) -> str:
    return json.dumps(data, indent=1, separators=(",", ": "), sort_keys=True) + "\n"


def _get_patch_files(patches_dir: Path, resource_type: str) -> list[Path]:
    dir_name = resource_type.replace("::", "_").lower()
    patch_dir = patches_dir / dir_name
    if patch_dir.exists():
        return sorted(patch_dir.glob("*.json"))
    return []


def _get_all_patch_files(schemas_dir: Path, resource_type: str) -> list[Path]:
    """Get patch files from both providers and extensions in order."""
    dir_name = resource_type.replace("::", "_").lower()
    files: list[Path] = []
    providers_dir = schemas_dir / "patches" / "providers" / dir_name
    if providers_dir.exists():
        files.extend(sorted(providers_dir.glob("*.json")))
    extensions_dir = schemas_dir / "patches" / "extensions" / dir_name
    if extensions_dir.exists():
        files.extend(sorted(extensions_dir.glob("*.json")))
    return files


def _assemble_resource(
    resource_type: str,
    schema_hash: str,
    resources_dir: Path,
    schemas_dir: Path,
) -> dict[str, Any] | None:
    schema_file = resources_dir / f"{schema_hash}.json"
    if not schema_file.exists():
        return None
    base = json.loads(schema_file.read_text())
    patch_files = _get_all_patch_files(schemas_dir, resource_type)
    return assemble_schema(base, patch_files)


def assemble_all(
    schemas_dir: Path, output_dir: Path, standard: bool = False
) -> int:
    """Assemble all schemas and write to output_dir.

    Default: produces providers/{region}.json + resources/{hash}.json
    (content-addressed with region mappings, custom keywords preserved).

    With standard=True: produces flat {resource_type}.json files with
    custom keywords translated to standard JSON Schema.
    """
    providers_dir = schemas_dir / "providers"
    resources_dir = schemas_dir / "resources"

    if standard:
        return _assemble_standard(
            providers_dir, resources_dir, schemas_dir, output_dir,
        )
    return _assemble_cfnlint(
        providers_dir, resources_dir, schemas_dir, output_dir,
    )


def _assemble_cfnlint(
    providers_dir: Path,
    resources_dir: Path,
    schemas_dir: Path,
    output_dir: Path,
) -> int:
    """Assemble with region mappings and content-addressed schemas."""
    out_resources = output_dir / "resources"
    out_providers = output_dir / "providers"
    out_resources.mkdir(parents=True, exist_ok=True)
    out_providers.mkdir(parents=True, exist_ok=True)

    type_to_assembled: dict[str, dict] = {}
    provider_files = sorted(providers_dir.glob("*.json"))
    primary = providers_dir / "us-east-1.json"
    if primary.exists():
        provider_files = [primary] + [f for f in provider_files if f != primary]
    for provider_file in provider_files:
        mappings = json.loads(provider_file.read_text())
        for resource_type, schema_hash in mappings.items():
            if resource_type in type_to_assembled:
                continue
            assembled = _assemble_resource(
                resource_type, schema_hash, resources_dir, schemas_dir,
            )
            if assembled:
                type_to_assembled[resource_type] = assembled

    schema_to_hash: dict[str, str] = {}
    for resource_type, schema in type_to_assembled.items():
        content = json.dumps(schema, sort_keys=True)
        h = hashlib.sha256(content.encode()).hexdigest()[:16]
        schema_to_hash[resource_type] = h
        out_file = out_resources / f"{h}.json"
        if not out_file.exists():
            out_file.write_text(_format_json(schema))

    for provider_file in sorted(providers_dir.glob("*.json")):
        mappings = json.loads(provider_file.read_text())
        out_mappings = {}
        for resource_type in sorted(mappings.keys()):
            if resource_type in schema_to_hash:
                out_mappings[resource_type] = schema_to_hash[resource_type]
        out_file = out_providers / provider_file.name
        out_file.write_text(_format_json(out_mappings))

    logger.info(
        "Assembled %d schemas (%d unique) to %s",
        len(schema_to_hash), len(set(schema_to_hash.values())), output_dir,
    )
    return 0


def _assemble_standard(
    providers_dir: Path,
    resources_dir: Path,
    schemas_dir: Path,
    output_dir: Path,
) -> int:
    """Assemble flat per-resource-type files with standard JSON Schema."""
    output_dir.mkdir(parents=True, exist_ok=True)

    # Collect all resource types across all regions, prefer us-east-1 hash
    all_types: dict[str, str] = {}
    us_east_1 = providers_dir / "us-east-1.json"
    if us_east_1.exists():
        all_types.update(json.loads(us_east_1.read_text()))

    for provider_file in sorted(providers_dir.glob("*.json")):
        mappings = json.loads(provider_file.read_text())
        for resource_type, schema_hash in mappings.items():
            if resource_type not in all_types:
                all_types[resource_type] = schema_hash

    count = 0
    for resource_type, schema_hash in sorted(all_types.items()):
        assembled = _assemble_resource(
            resource_type, schema_hash, resources_dir, schemas_dir,
        )
        if not assembled:
            continue

        assembled = translate_custom_keywords(assembled)
        dir_name = resource_type.replace("::", "_").lower()
        out_file = output_dir / f"{dir_name}.json"
        out_file.write_text(_format_json(assembled))
        count += 1

    logger.info("Assembled %d standard schemas to %s", count, output_dir)
    return 0
