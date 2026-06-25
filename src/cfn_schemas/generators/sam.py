"""Download SAM JSON Schema and decompose into per-resource-type schemas.

Downloads the monolithic SAM schema from the serverless-application-model repo,
extracts per-resource schemas in CF provider schema format, resolves
PassThroughProp references to real CF types, and writes to schemas/.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from copy import deepcopy
from typing import Any
from urllib.request import urlopen

from cfn_schemas.assembly import apply_patches
from cfn_schemas.generators import register
from cfn_schemas.generators.base import BaseGenerator

logger = logging.getLogger(__name__)

SAM_SCHEMA_URL = (
    "https://raw.githubusercontent.com/aws/serverless-application-model"
    "/main/samtranslator/schema/schema.json"
)

SAM_DEF_PREFIX = "samtranslator__internal__schema_source__"

SAM_TYPE_MAP = {
    "aws_serverless_api": "AWS::Serverless::Api",
    "aws_serverless_application": "AWS::Serverless::Application",
    "aws_serverless_connector": "AWS::Serverless::Connector",
    "aws_serverless_function": "AWS::Serverless::Function",
    "aws_serverless_graphqlapi": "AWS::Serverless::GraphQLApi",
    "aws_serverless_httpapi": "AWS::Serverless::HttpApi",
    "aws_serverless_layerversion": "AWS::Serverless::LayerVersion",
    "aws_serverless_simpletable": "AWS::Serverless::SimpleTable",
    "aws_serverless_statemachine": "AWS::Serverless::StateMachine",
}

SAM_TO_CFN_TYPE: dict[str, str] = {
    "aws_serverless_api": "AWS::ApiGateway::RestApi",
    "aws_serverless_application": "AWS::CloudFormation::Stack",
    "aws_serverless_function": "AWS::Lambda::Function",
    "aws_serverless_httpapi": "AWS::ApiGatewayV2::Api",
    "aws_serverless_layerversion": "AWS::Lambda::LayerVersion",
    "aws_serverless_simpletable": "AWS::DynamoDB::Table",
    "aws_serverless_statemachine": "AWS::StepFunctions::StateMachine",
}

_PASSTHROUGH_RE = re.compile(
    r"is passed directly to the \[`(\w+)`\].*?`(AWS::[A-Za-z0-9:]+)`"
)


def _collect_refs(obj: Any) -> set[str]:
    refs: set[str] = set()
    if isinstance(obj, dict):
        if "$ref" in obj:
            ref = obj["$ref"]
            if ref.startswith("#/definitions/"):
                refs.add(ref[len("#/definitions/"):])
        for v in obj.values():
            refs.update(_collect_refs(v))
    elif isinstance(obj, list):
        for item in obj:
            refs.update(_collect_refs(item))
    return refs


def _collect_all_refs(all_defs: dict, root_refs: set[str]) -> set[str]:
    collected: set[str] = set()
    queue = list(root_refs)
    while queue:
        ref = queue.pop()
        if ref in collected:
            continue
        collected.add(ref)
        if ref in all_defs:
            queue.extend(_collect_refs(all_defs[ref]) - collected)
    return collected


def _clean_schema(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _clean_schema(v) for k, v in obj.items()
                if k not in ("markdownDescription", "title", "description")}
    if isinstance(obj, list):
        return [_clean_schema(item) for item in obj]
    return obj


def _short_def_name(full_key: str) -> str:
    if SAM_DEF_PREFIX in full_key:
        parts = full_key[len(SAM_DEF_PREFIX):].split("__")
        if len(parts) >= 2:
            return parts[-1]
    return full_key


def _shorten_ref(ref_str: str) -> str:
    if not ref_str.startswith("#/definitions/"):
        return ref_str
    full_name = ref_str[len("#/definitions/"):]
    return f"#/definitions/{_short_def_name(full_name)}"


def _rewrite_refs(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: (
                _shorten_ref(v)
                if k == "$ref" and isinstance(v, str)
                else _rewrite_refs(v)
            )
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_rewrite_refs(item) for item in obj]
    return obj


def _inline_refs(obj: Any, defs: dict, depth: int = 3) -> Any:
    if depth <= 0 or not isinstance(obj, dict):
        return obj
    if "$ref" in obj:
        ref_name = obj["$ref"].replace("#/definitions/", "")
        if ref_name in defs:
            resolved = _inline_refs(deepcopy(defs[ref_name]), defs, depth - 1)
            if isinstance(resolved, dict):
                # Merge sibling keys (e.g. description) into resolved def
                for k, v in obj.items():
                    if k != "$ref" and k not in resolved:
                        resolved[k] = v
            return resolved
        return obj
    return {k: _inline_refs(v, defs, depth) for k, v in obj.items()}


@register("sam")
class SamGenerator(BaseGenerator):
    """Download SAM schema and produce per-resource-type schemas."""

    def run(self) -> None:
        logger.info("Downloading SAM schema...")
        with urlopen(SAM_SCHEMA_URL, timeout=30) as resp:
            sam_schema = json.loads(resp.read().decode("utf-8"))

        all_defs = sam_schema.get("definitions", {})
        logger.info("Loaded %d definitions from SAM schema", len(all_defs))

        # Build per-resource schemas
        type_hashes: dict[str, str] = {}

        for module_name, type_name in sorted(SAM_TYPE_MAP.items()):
            schema = self._build_resource_schema(type_name, module_name, all_defs)
            if not schema:
                continue

            # Resolve PassThroughProps
            self._resolve_passthroughs(schema, all_defs, type_name, module_name)

            h = hashlib.sha256(
                json.dumps(schema, sort_keys=True).encode()
            ).hexdigest()[:16]
            type_hashes[type_name] = h
            self.write_json(self.resources_dir / f"{h}.json", schema)
            logger.info("  %s -> %s", type_name, h)

        # Write SAM provider mapping (separate from region files)
        self.write_json(
            self.providers_dir / "sam.json",
            dict(sorted(type_hashes.items())),
        )

        logger.info("Generated %d SAM resource schemas", len(type_hashes))

    def _build_resource_schema(
        self, type_name: str, module_name: str, all_defs: dict
    ) -> dict[str, Any] | None:
        module_defs = {
            k[len(f"{SAM_DEF_PREFIX}{module_name}__"):]: k
            for k in all_defs if k.startswith(f"{SAM_DEF_PREFIX}{module_name}__")
        }
        if "Properties" not in module_defs:
            return None

        props_schema = deepcopy(all_defs[module_defs["Properties"]])
        root_refs = _collect_refs(props_schema)
        all_needed = _collect_all_refs(all_defs, root_refs)

        definitions = {}
        for ref_key in sorted(all_needed):
            if ref_key in all_defs:
                short = _short_def_name(ref_key)
                definitions[short] = _clean_schema(
                    deepcopy(all_defs[ref_key])
                )

        definitions = _rewrite_refs(definitions)
        props_schema = _rewrite_refs(_clean_schema(props_schema))

        schema: dict[str, Any] = {
            "typeName": type_name,
            "additionalProperties": False,
            "properties": props_schema.get("properties", {}),
        }
        if props_schema.get("required"):
            schema["required"] = props_schema["required"]
        if definitions:
            schema["definitions"] = definitions

        # Copy readOnlyProperties from underlying CFN type
        cfn_type = SAM_TO_CFN_TYPE.get(module_name)
        if cfn_type:
            self._copy_readonly_props(schema, cfn_type)

        return schema

    def _copy_readonly_props(self, schema: dict, cfn_type: str) -> None:
        """Copy readOnlyProperties and their schemas from the underlying CFN type."""
        us_east = self.providers_dir / "us-east-1.json"
        if not us_east.exists():
            return
        mappings = json.loads(us_east.read_text())
        cfn_hash = mappings.get(cfn_type)
        if not cfn_hash:
            return
        cfn_file = self.resources_dir / f"{cfn_hash}.json"
        if not cfn_file.exists():
            return
        cfn_schema = json.loads(cfn_file.read_text())
        pi = cfn_schema.get("primaryIdentifier", [])
        if pi:
            schema["primaryIdentifier"] = pi
        ro_props = cfn_schema.get("readOnlyProperties", [])
        if ro_props:
            schema["readOnlyProperties"] = ro_props
            cfn_props = cfn_schema.get("properties", {})
            cfn_defs = cfn_schema.get("definitions", {})
            for ro_prop in ro_props:
                parts = ro_prop.strip("/").split("/")
                if len(parts) >= 2 and parts[0] == "properties":
                    prop_name = parts[1]
                    if prop_name not in schema["properties"]:
                        if prop_name in cfn_props:
                            prop_val = _inline_refs(
                                deepcopy(cfn_props[prop_name]), cfn_defs,
                            )
                            schema["properties"][prop_name] = prop_val

    def _resolve_passthroughs(
        self, schema: dict, all_defs: dict, type_name: str, module_name: str
    ) -> None:
        """Replace PassThroughProp references with real CF property schemas."""
        for def_name, def_schema in schema.get("definitions", {}).items():
            if not isinstance(def_schema, dict):
                continue
            for prop_name, prop_def in list(def_schema.get("properties", {}).items()):
                if not self._is_passthrough(prop_def):
                    continue
                resolved = self._resolve_passthrough_prop(all_defs, def_name, prop_name)
                if resolved:
                    def_schema["properties"][prop_name] = resolved

        for prop_name, prop_def in list(schema.get("properties", {}).items()):
            if not self._is_passthrough(prop_def):
                continue
            orig = self._find_original_prop(all_defs, module_name, prop_name)
            if not orig:
                continue
            md = orig.get("markdownDescription", "")
            m = _PASSTHROUGH_RE.search(md)
            if m:
                resolved = self._lookup_cfn_prop(m.group(2).rstrip("."), m.group(1))
                if resolved:
                    schema["properties"][prop_name] = resolved

        self._copy_missing_refs(schema)

    @staticmethod
    def _is_passthrough(prop_def: dict) -> bool:
        if not isinstance(prop_def, dict):
            return False
        if prop_def.get("$ref") == "#/definitions/PassThroughProp":
            return True
        return any(
            isinstance(i, dict) and i.get("$ref") == "#/definitions/PassThroughProp"
            for i in prop_def.get("allOf", [])
        )

    def _resolve_passthrough_prop(
        self, all_defs: dict, short_name: str, prop_name: str
    ) -> dict | None:
        for key, value in all_defs.items():
            if key == short_name or key.endswith(f"__{short_name}"):
                if isinstance(value, dict):
                    prop = value.get("properties", {}).get(prop_name)
                    if prop:
                        md = prop.get("markdownDescription", "")
                        m = _PASSTHROUGH_RE.search(md)
                        if m:
                            return self._lookup_cfn_prop(
                                m.group(2).rstrip("."), m.group(1),
                            )
        return None

    def _find_original_prop(
        self, all_defs: dict, module_name: str, prop_name: str
    ) -> dict | None:
        key = f"{SAM_DEF_PREFIX}{module_name}__Properties"
        if key in all_defs:
            return all_defs[key].get("properties", {}).get(prop_name)
        return None

    def _copy_missing_refs(self, schema: dict) -> None:
        """Copy definitions referenced by $ref but missing from schema."""
        definitions = schema.setdefault("definitions", {})
        us_east = self.providers_dir / "us-east-1.json"
        if not us_east.exists():
            return
        mappings = json.loads(us_east.read_text())

        while True:
            needed = _collect_refs(schema) - set(definitions.keys())
            if not needed:
                break
            found_any = False
            for h in mappings.values():
                cfn_file = self.resources_dir / f"{h}.json"
                if not cfn_file.exists():
                    continue
                cfn_schema = json.loads(cfn_file.read_text())
                cfn_defs = cfn_schema.get("definitions", {})
                for ref_name in list(needed):
                    if ref_name in cfn_defs:
                        definitions[ref_name] = _clean_schema(deepcopy(cfn_defs[ref_name]))
                        needed.discard(ref_name)
                        found_any = True
                if not needed:
                    break
            if not found_any:
                break

    def _lookup_cfn_prop(self, cfn_type: str, cfn_prop: str) -> dict | None:
        """Look up a property from a fully-patched CF resource schema."""
        us_east = self.providers_dir / "us-east-1.json"
        if not us_east.exists():
            return None
        mappings = json.loads(us_east.read_text())
        cfn_hash = mappings.get(cfn_type)
        if not cfn_hash:
            return None
        cfn_file = self.resources_dir / f"{cfn_hash}.json"
        if not cfn_file.exists():
            return None
        cfn_schema = json.loads(cfn_file.read_text())

        # Apply all patches (providers + extensions) so we get format,
        # smithy, and other enhancements that other generators produced.
        dir_name = cfn_type.replace("::", "_").lower()
        for patch_subdir in ("providers", "extensions"):
            patch_dir = self.schemas_dir / "patches" / patch_subdir / dir_name
            if patch_dir.exists():
                for patch_file in sorted(patch_dir.glob("*.json")):
                    patches = json.loads(patch_file.read_text())
                    cfn_schema = apply_patches(cfn_schema, patches)

        prop = cfn_schema.get("properties", {}).get(cfn_prop)
        if not prop:
            return None
        # Inline $refs
        return _inline_refs(deepcopy(prop), cfn_schema.get("definitions", {}))
