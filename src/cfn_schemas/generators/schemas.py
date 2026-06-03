"""Download base CloudFormation resource provider schemas from AWS.

Downloads per-region schema ZIPs, content-hashes them, and writes:
- schemas/resources/{hash}.json — deduplicated schema files
- schemas/providers/{region}.json — region → resource type → hash mappings

Uses etag-based caching to skip downloads when nothing has changed upstream.
"""

from __future__ import annotations

import hashlib
import json
import logging
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from cfn_schemas.generators import register
from cfn_schemas.generators.base import BaseGenerator

logger = logging.getLogger(__name__)

REGIONS = [
    "af-south-1", "ap-east-1", "ap-east-2", "ap-northeast-1", "ap-northeast-2",
    "ap-northeast-3", "ap-south-1", "ap-south-2", "ap-southeast-1",
    "ap-southeast-2", "ap-southeast-3", "ap-southeast-4", "ap-southeast-5",
    "ap-southeast-6", "ap-southeast-7", "ca-central-1", "ca-west-1",
    "cn-north-1", "cn-northwest-1", "eu-central-1", "eu-central-2",
    "eu-north-1", "eu-south-1", "eu-south-2", "eu-west-1", "eu-west-2",
    "eu-west-3", "il-central-1", "me-central-1", "me-south-1", "mx-central-1",
    "sa-east-1", "us-east-1", "us-east-2", "us-gov-east-1", "us-gov-west-1",
    "us-west-1", "us-west-2",
]

# ISO regions copy us-east-1 schemas
_ISO_REGIONS = [
    "us-iso-east-1", "us-iso-west-1", "us-isob-east-1", "us-isob-west-1",
    "us-isof-east-1", "us-isof-south-1", "eu-isoe-west-1", "eusc-de-east-1",
]

_SKIP_REGIONS = {"me-south-1"}


def _schema_url(region: str) -> str:
    suffix = ".cn" if region in ("cn-north-1", "cn-northwest-1") else ""
    return f"https://schema.cloudformation.{region}.amazonaws.com{suffix}/CloudformationSchema.zip"


def _remove_descriptions(obj: Any) -> Any:
    """Strip description fields to reduce schema size."""
    if isinstance(obj, dict):
        return {
            k: _remove_descriptions(v)
            for k, v in obj.items()
            if k != "description"
        }
    if isinstance(obj, list):
        return [_remove_descriptions(v) for v in obj]
    return obj


_EMPTY_DEF = {"additionalProperties": False, "properties": {}, "type": "object"}


def _remove_empty_definitions(spec: dict) -> dict:
    """Remove empty definitions that break meta-schema validation."""
    if "definitions" in spec:
        spec["definitions"] = {
            k: v for k, v in spec["definitions"].items()
            if v and v != _EMPTY_DEF
        }
    return spec


def _patch_add(spec: dict, parts: list[str], value: Any) -> None:
    target = spec
    for part in parts[:-1]:
        if isinstance(target, dict):
            target = target.setdefault(part, {})
    if isinstance(target, dict):
        target[parts[-1]] = value


def _patch_replace(spec: dict, parts: list[str], value: Any) -> None:
    target = spec
    for part in parts[:-1]:
        if isinstance(target, dict) and part in target:
            target = target[part]
        else:
            return
    if isinstance(target, dict) and parts[-1] in target:
        target[parts[-1]] = value


def _patch_remove(spec: dict, parts: list[str]) -> None:
    target = spec
    for part in parts[:-1]:
        if isinstance(target, dict) and part in target:
            target = target[part]
        else:
            return
    if isinstance(target, dict) and parts[-1] in target:
        del target[parts[-1]]


@register("schemas")
class SchemasGenerator(BaseGenerator):
    """Download base CloudFormation schemas from AWS."""

    def run(self) -> None:
        self.resources_dir.mkdir(parents=True, exist_ok=True)
        self.providers_dir.mkdir(parents=True, exist_ok=True)
        metadata_dir = self.schemas_dir / "metadata"
        metadata_dir.mkdir(parents=True, exist_ok=True)

        downloaded, failed = self._download_all_regions(metadata_dir)

        if not downloaded:
            if failed:
                logger.error("All regions failed to download")
            else:
                logger.info("All schemas are up to date")
            return

        hash_to_schema, region_mappings = self._build_content_store(
            downloaded,
        )
        self._include_manual_schemas(hash_to_schema, region_mappings)
        self._write_region_mappings(region_mappings)
        self._cleanup_orphans()

        logger.info(
            "Updated %d regions, %d unique schemas",
            len(downloaded), len(hash_to_schema),
        )
        if failed:
            logger.warning("Failed regions: %s", ", ".join(failed))

    def _download_all_regions(
        self, metadata_dir: Path
    ) -> tuple[dict[str, dict], list[str]]:
        downloaded: dict[str, dict[str, str]] = {}
        failed: list[str] = []
        for region in REGIONS:
            if region in _SKIP_REGIONS:
                continue
            result = self._download_region(region, metadata_dir)
            if result is None:
                failed.append(region)
            elif result is not False:
                downloaded[region] = result
        return downloaded, failed

    def _build_content_store(
        self, downloaded: dict[str, dict]
    ) -> tuple[dict[str, dict], dict[str, dict[str, str]]]:
        hash_to_schema: dict[str, dict] = {}
        region_mappings: dict[str, dict[str, str]] = {}
        for region, schemas in downloaded.items():
            region_mappings[region] = {}
            for resource_type, schema in schemas.items():
                h = hashlib.sha256(
                    json.dumps(schema, sort_keys=True).encode()
                ).hexdigest()[:16]
                hash_to_schema[h] = schema
                region_mappings[region][resource_type] = h
        for h, schema in hash_to_schema.items():
            self.write_json(self.resources_dir / f"{h}.json", schema)
        return hash_to_schema, region_mappings

    def _include_manual_schemas(
        self,
        hash_to_schema: dict[str, dict],
        region_mappings: dict[str, dict[str, str]],
    ) -> None:
        manual_dir = self.schemas_dir / "manual"
        if not manual_dir.exists():
            return
        for manual_file in sorted(manual_dir.glob("*.json")):
            schema = json.loads(manual_file.read_text())
            type_name = schema.get("typeName", "")
            if not type_name:
                continue
            h = hashlib.sha256(
                json.dumps(schema, sort_keys=True).encode()
            ).hexdigest()[:16]
            hash_to_schema[h] = schema
            self.write_json(self.resources_dir / f"{h}.json", schema)
            for type_map in region_mappings.values():
                type_map[type_name] = h

    def _write_region_mappings(
        self, region_mappings: dict[str, dict[str, str]]
    ) -> None:
        for region, type_map in region_mappings.items():
            self.write_json(
                self.providers_dir / f"{region}.json",
                dict(sorted(type_map.items())),
            )
        if "us-east-1" in region_mappings:
            for region in _ISO_REGIONS:
                self.write_json(
                    self.providers_dir / f"{region}.json",
                    dict(sorted(region_mappings["us-east-1"].items())),
                )

    def _cleanup_orphans(self) -> None:
        referenced = set()
        for provider_file in self.providers_dir.glob("*.json"):
            mappings = json.loads(provider_file.read_text())
            referenced.update(mappings.values())
        for schema_file in self.resources_dir.glob("*.json"):
            if schema_file.stem not in referenced:
                logger.info(
                    "Removing orphaned schema: %s", schema_file.stem,
                )
                schema_file.unlink()

    def _download_region(
        self, region: str, metadata_dir: Path
    ) -> dict[str, dict] | None | bool:
        """Download schemas for a region.

        Returns dict of schemas, False (up-to-date), or None (failed).
        """
        url = _schema_url(region)

        if not self._has_newer_version(url, metadata_dir):
            return False

        logger.info("Downloading %s", region)
        try:
            req = Request(url)
            with urlopen(req) as resp:
                data = resp.read()
                etag = resp.headers.get("ETag")
                if etag:
                    self._save_etag(url, etag, metadata_dir)

            return self._parse_schema_zip(data)
        except Exception as e:
            logger.warning("Failed downloading %s: %s", region, e)
            return None

    def _save_etag(
        self, url: str, etag: str, metadata_dir: Path
    ) -> None:
        url_hash = hashlib.sha256(url.encode()).hexdigest()
        meta_file = metadata_dir / f"{url_hash}.json"
        meta_file.write_text(json.dumps({"etag": etag, "url": url}))

    def _parse_schema_zip(self, data: bytes) -> dict[str, dict]:
        schemas: dict[str, dict] = {}
        with zipfile.ZipFile(BytesIO(data)) as zf:
            for name in zf.namelist():
                if not name.endswith(".json"):
                    continue
                spec = json.loads(zf.read(name))
                spec.pop("handlers", None)
                tagging = spec.get("tagging", {})
                if "permissions" in tagging:
                    del spec["tagging"]["permissions"]
                spec = _remove_descriptions(spec)
                type_name = spec.get("typeName", "")
                if type_name:
                    spec = self._apply_provider_patches(spec, type_name)
                    spec = _remove_empty_definitions(spec)
                    schemas[type_name] = spec
        return schemas

    def _apply_extension_patches(self) -> None:
        """Apply all extension patches into the resource schema files."""
        patches_dir = self.patches_dir
        if not patches_dir.exists():
            return

        # Build type_name → schema hash from us-east-1 (most complete)
        us_east = self.providers_dir / "us-east-1.json"
        if not us_east.exists():
            return
        mappings = json.loads(us_east.read_text())

        count = 0
        for resource_type, schema_hash in mappings.items():
            dir_name = self.resource_type_to_dir(resource_type)
            patch_dir = patches_dir / dir_name
            if not patch_dir.exists():
                continue

            schema_file = self.resources_dir / f"{schema_hash}.json"
            if not schema_file.exists():
                continue

            schema = json.loads(schema_file.read_text())
            modified = self._apply_add_patches(schema, patch_dir)

            if modified:
                self.write_json(schema_file, schema)
                count += 1

        logger.info("Applied extension patches to %d schemas", count)

    def _apply_add_patches(
        self, schema: dict, patch_dir: Path
    ) -> bool:
        """Apply all 'add' patches from a directory into a schema."""
        modified = False
        for patch_file in sorted(patch_dir.glob("*.json")):
            patches = json.loads(patch_file.read_text())
            for patch in patches:
                if patch.get("op") != "add":
                    continue
                parts = [
                    p for p in patch.get("path", "").split("/") if p
                ]
                if not parts:
                    continue
                _patch_add(schema, parts, patch.get("value"))
                modified = True
        return modified

    def _apply_provider_patches(
        self, spec: dict, type_name: str
    ) -> dict:
        """Apply provider patches (schema fixes) during download."""
        dir_name = self.resource_type_to_dir(type_name)
        patch_dir = self.schemas_dir / "patches" / "providers" / dir_name
        if not patch_dir.exists():
            return spec

        for patch_file in sorted(patch_dir.glob("*.json")):
            patches = json.loads(patch_file.read_text())
            spec = self._apply_single_provider_patch_file(
                spec, patches, type_name, patch_file,
            )

        return spec

    def _apply_single_provider_patch_file(
        self,
        spec: dict,
        patches: list[dict],
        type_name: str,
        patch_file: Path,
    ) -> dict:
        for patch in patches:
            op = patch.get("op")
            path = patch.get("path", "")
            value = patch.get("value")
            parts = [p for p in path.split("/") if p]

            if op == "test":
                if not self._test_patch(
                    spec, parts, value, type_name, path, patch_file,
                ):
                    return spec
            elif op == "add" and parts:
                _patch_add(spec, parts, value)
            elif op == "replace" and parts:
                _patch_replace(spec, parts, value)
            elif op == "remove" and parts:
                _patch_remove(spec, parts)

        return spec

    def _test_patch(
        self,
        spec: dict,
        parts: list[str],
        value: Any,
        type_name: str,
        path: str,
        patch_file: Path,
    ) -> bool:
        """Run a test op. Returns False if test fails."""
        target = spec
        for part in parts:
            if isinstance(target, dict) and part in target:
                target = target[part]
            else:
                logger.warning(
                    "Provider patch test failed for %s:"
                    " path %s not found (%s)",
                    type_name, path, patch_file.name,
                )
                return False
        if target != value:
            logger.warning(
                "Provider patch test failed for %s:"
                " %s expected %r got %r (%s)",
                type_name, path, value, target, patch_file.name,
            )
            return False
        return True

    def _has_newer_version(
        self, url: str, metadata_dir: Path
    ) -> bool:
        """Check etag to see if we need to re-download."""
        url_hash = hashlib.sha256(url.encode()).hexdigest()
        meta_file = metadata_dir / f"{url_hash}.json"
        if not meta_file.exists():
            return True
        try:
            cached_etag = json.loads(meta_file.read_text()).get("etag")
            if not cached_etag:
                return True
            req = Request(url, method="HEAD")
            with urlopen(req) as resp:
                remote_etag = resp.headers.get("ETag")
                if cached_etag == remote_etag:
                    return False
        except Exception:
            pass
        return True
