"""Audit patches against base schemas to find stale, redundant, or broken patches.

Reports:
- BROKEN: patch path doesn't resolve against any version of the schema
- REDUNDANT: base schema already has the value the patch would add
- PARTIAL: path resolves in some regions but not others (informational)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class PatchIssue:
    resource_type: str
    patch_file: str
    patch_index: int
    path: str
    issue: str  # BROKEN, REDUNDANT, PARTIAL
    detail: str


@dataclass
class AuditResult:
    issues: list[PatchIssue] = field(default_factory=list)
    total_patches: int = 0
    total_files: int = 0

    @property
    def broken(self) -> list[PatchIssue]:
        return [i for i in self.issues if i.issue == "BROKEN"]

    @property
    def redundant(self) -> list[PatchIssue]:
        return [i for i in self.issues if i.issue == "REDUNDANT"]

    @property
    def partial(self) -> list[PatchIssue]:
        return [i for i in self.issues if i.issue == "PARTIAL"]


def _resolve_ref(schema: dict, current: dict) -> tuple[Any, bool]:
    """Follow a $ref in the schema. Returns (resolved, success)."""
    ref_path = current["$ref"].lstrip("#/")
    node = schema
    for rp in ref_path.split("/"):
        if isinstance(node, dict) and rp in node:
            node = node[rp]
        else:
            return None, False
    return node, True


def _resolve_path(schema: dict, path: str) -> tuple[Any, bool]:
    """Walk a JSON pointer path into a schema. Returns (value, found)."""
    parts = [p for p in path.strip("/").split("/") if p]
    current = schema
    for part in parts:
        if isinstance(current, dict):
            if part in current:
                current = current[part]
            elif "$ref" in current:
                current, ok = _resolve_ref(schema, current)
                if not ok:
                    return None, False
                if isinstance(current, dict) and part in current:
                    current = current[part]
                else:
                    return None, False
            else:
                return None, False
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None, False
        else:
            return None, False
    return current, True


def _get_parent_and_key(path: str) -> tuple[str, str]:
    """Split a patch path into parent path and final key."""
    parts = path.strip("/").rsplit("/", 1)
    if len(parts) == 1:
        return "/", parts[0]
    return "/" + parts[0], parts[1]


def _check_patch_against_schema(
    schema: dict,
    patch: dict,
    resource_type: str,
    patch_file: str,
    patch_index: int,
) -> PatchIssue | None:
    """Check a single patch operation against a schema."""
    op = patch.get("op", "")
    path = patch.get("path", "")
    value = patch.get("value")

    if op == "remove":
        _, found = _resolve_path(schema, path)
        if not found:
            return PatchIssue(
                resource_type=resource_type,
                patch_file=patch_file,
                patch_index=patch_index,
                path=path,
                issue="BROKEN",
                detail="remove target does not exist",
            )
        return None

    if op != "add":
        return None

    parent_path, key = _get_parent_and_key(path)
    parent, parent_found = _resolve_path(schema, parent_path)
    if not parent_found:
        detail = (
            f"parent path '{parent_path}' does not exist in schema"
        )
        return PatchIssue(
            resource_type=resource_type,
            patch_file=patch_file,
            patch_index=patch_index,
            path=path,
            issue="BROKEN",
            detail=detail,
        )

    if isinstance(parent, dict) and key in parent:
        if parent[key] == value:
            return PatchIssue(
                resource_type=resource_type,
                patch_file=patch_file,
                patch_index=patch_index,
                path=path,
                issue="REDUNDANT",
                detail="schema already has this exact value",
            )

    return None


def audit_patches(schemas_dir: Path) -> AuditResult:
    """Audit all extension and provider patches against base schemas."""
    result = AuditResult()
    resources_dir = schemas_dir / "resources"
    providers_dir = schemas_dir / "providers"

    type_to_hashes: dict[str, set[str]] = {}
    for provider_file in sorted(providers_dir.glob("*.json")):
        mappings = json.loads(provider_file.read_text())
        for rt, h in mappings.items():
            type_to_hashes.setdefault(rt, set()).add(h)

    schema_cache: dict[str, dict] = {}

    _audit_patch_dir(
        schemas_dir / "patches",
        resources_dir, type_to_hashes, schema_cache, result,
    )

    return result


def _load_schemas_for_type(
    hashes: set[str],
    resources_dir: Path,
    schema_cache: dict[str, dict],
) -> dict[str, dict]:
    """Load all schema versions for a resource type."""
    schemas: dict[str, dict] = {}
    for h in hashes:
        if h not in schema_cache:
            schema_file = resources_dir / f"{h}.json"
            if schema_file.exists():
                schema_cache[h] = json.loads(schema_file.read_text())
        if h in schema_cache:
            schemas[h] = schema_cache[h]
    return schemas


def _classify_patch_issues(
    issues_per_version: dict[str, PatchIssue | None],
    resource_type: str,
    patch_file_name: str,
    patch_index: int,
    patch_path: str,
    schema_count: int,
    result: AuditResult,
) -> None:
    """Classify patch issues across schema versions and append to result."""
    all_broken = all(
        v is not None and v.issue == "BROKEN"
        for v in issues_per_version.values()
    )
    all_redundant = all(
        v is not None and v.issue == "REDUNDANT"
        for v in issues_per_version.values()
    )
    some_broken = any(
        v is not None and v.issue == "BROKEN"
        for v in issues_per_version.values()
    ) and not all_broken

    if all_broken:
        sample = next(v for v in issues_per_version.values() if v)
        result.issues.append(PatchIssue(
            resource_type=resource_type,
            patch_file=patch_file_name,
            patch_index=patch_index,
            path=patch_path,
            issue="BROKEN",
            detail=sample.detail,
        ))
    elif all_redundant:
        result.issues.append(PatchIssue(
            resource_type=resource_type,
            patch_file=patch_file_name,
            patch_index=patch_index,
            path=patch_path,
            issue="REDUNDANT",
            detail="value already present in all schema versions",
        ))
    elif some_broken:
        broken_count = sum(
            1 for v in issues_per_version.values()
            if v and v.issue == "BROKEN"
        )
        result.issues.append(PatchIssue(
            resource_type=resource_type,
            patch_file=patch_file_name,
            patch_index=patch_index,
            path=patch_path,
            issue="PARTIAL",
            detail=(
                f"path broken in {broken_count}/{schema_count}"
                " schema versions"
            ),
        ))


def _audit_resource_patches(
    patch_dir: Path,
    resource_type: str,
    schemas_for_type: dict[str, dict],
    result: AuditResult,
) -> None:
    """Audit all patch files for a single resource type."""
    for patch_file in sorted(patch_dir.glob("*.json")):
        patches = json.loads(patch_file.read_text())
        result.total_files += 1
        result.total_patches += len(patches)

        for i, patch in enumerate(patches):
            issues_per_version: dict[str, PatchIssue | None] = {}
            for h, schema in schemas_for_type.items():
                issue = _check_patch_against_schema(
                    schema, patch, resource_type, patch_file.name, i,
                )
                issues_per_version[h] = issue

            _classify_patch_issues(
                issues_per_version,
                resource_type,
                patch_file.name,
                i,
                patch.get("path", ""),
                len(schemas_for_type),
                result,
            )


def _audit_patch_dir(
    patches_dir: Path,
    resources_dir: Path,
    type_to_hashes: dict[str, set[str]],
    schema_cache: dict[str, dict],
    result: AuditResult,
) -> None:
    """Audit all patch files in a directory."""
    if not patches_dir.exists():
        return

    for patch_dir in sorted(patches_dir.iterdir()):
        if not patch_dir.is_dir():
            continue

        dir_name = patch_dir.name
        resource_type = None
        for rt in type_to_hashes:
            if rt.replace("::", "_").lower() == dir_name:
                resource_type = rt
                break

        if not resource_type:
            _audit_unknown_type(patch_dir, dir_name, result)
            continue

        schemas_for_type = _load_schemas_for_type(
            type_to_hashes[resource_type], resources_dir, schema_cache,
        )
        if not schemas_for_type:
            continue

        _audit_resource_patches(
            patch_dir, resource_type, schemas_for_type, result,
        )


def _audit_unknown_type(
    patch_dir: Path, dir_name: str, result: AuditResult
) -> None:
    """Record issues for patches targeting an unknown resource type."""
    for patch_file in sorted(patch_dir.glob("*.json")):
        patches = json.loads(patch_file.read_text())
        result.total_files += 1
        result.total_patches += len(patches)
        for i, patch in enumerate(patches):
            result.issues.append(PatchIssue(
                resource_type=dir_name,
                patch_file=patch_file.name,
                patch_index=i,
                path=patch.get("path", ""),
                issue="BROKEN",
                detail="resource type not found in any region",
            ))


def format_report(result: AuditResult) -> str:
    """Format an audit result as a human-readable report."""
    lines = [
        "Patch Audit Report",
        "==================",
        f"Files scanned:  {result.total_files}",
        f"Patches scanned: {result.total_patches}",
        "",
        f"Issues found: {len(result.issues)}",
        f"  BROKEN:    {len(result.broken)}"
        " (path doesn't exist in any schema version)",
        f"  REDUNDANT: {len(result.redundant)}"
        " (base schema already has this value)",
        f"  PARTIAL:   {len(result.partial)}"
        " (path missing in some schema versions)",
    ]

    for category, issues in [
        ("BROKEN", result.broken),
        ("REDUNDANT", result.redundant),
        ("PARTIAL", result.partial),
    ]:
        if issues:
            lines.append(f"\n--- {category} ---")
            for issue in sorted(
                issues,
                key=lambda i: (
                    i.resource_type, i.patch_file, i.patch_index,
                ),
            ):
                lines.append(
                    f"  {issue.resource_type}"
                    f" [{issue.patch_file}#{issue.patch_index}]"
                    f" {issue.path}"
                )
                lines.append(f"    {issue.detail}")

    return "\n".join(lines)
