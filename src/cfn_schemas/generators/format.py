"""Generate format annotations by scanning schema property names.

Assigns semantic format types (e.g. AWS::EC2::VPC.Id, AWS::IAM::Role.Arn)
to properties based on their names. This enables cross-resource type validation.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from cfn_schemas.generators import register
from cfn_schemas.generators.base import BaseGenerator
from cfn_schemas.resolver import RefResolver

logger = logging.getLogger(__name__)


def _descend(instance: Any, keywords: Sequence[str]) -> Iterator[deque[str]]:
    """Walk a schema tree, yielding paths to properties matching keywords."""
    if isinstance(instance, dict):
        for k, v in instance.items():
            if k in keywords:
                yield deque([k])
            for e in _descend(v, keywords):
                if e:
                    yield deque([k, *e])
    if isinstance(instance, list):
        for i, v in enumerate(instance):
            for e in _descend(v, keywords):
                if e:
                    yield deque([str(i), *e])


def _resolve_through_refs(
    ref: str, resolver: RefResolver
) -> tuple[str, dict[str, Any]]:
    """Follow $ref chains to the final resolved schema."""
    _, resolved = resolver.resolve(ref)
    while isinstance(resolved, dict) and "$ref" in resolved:
        ref = resolved["$ref"]
        _, resolved = resolver.resolve(ref)
    return ref, resolved


def _make_patch(
    value: dict[str, Any], ref: str, resolver: RefResolver
) -> dict[str, Any] | None:
    """Create a patch dict, following $refs and targeting items for arrays."""
    if ref == "#/definitions/Arn":
        return None

    ref, resolved = _resolve_through_refs(ref, resolver)
    path = ref[1:]  # strip leading #

    if isinstance(resolved, dict) and "items" in resolved:
        path = f"{path}/items"

    key = next(iter(value))
    return {"op": "add", "path": f"{path}/{key}", "value": value[key]}


def _make_format_patch(
    fmt: str, ref: str, resolver: RefResolver
) -> dict[str, Any] | None:
    return _make_patch({"format": fmt}, ref, resolver)


# Property name → format mappings for simple cases
_SIMPLE_MAPPINGS: list[tuple[list[str], str, set[str]]] = [
    (["VpcId", "VPCId"], "AWS::EC2::VPC.Id", set()),
    (["SubnetId"], "AWS::EC2::Subnet.Id", set()),
    (["LogGroupName"], "AWS::Logs::LogGroup.Name", set()),
    (["ImageId", "AmiId"], "AWS::EC2::Image.Id", {"AWS::Cloud9::EnvironmentEC2"}),
]

# Property name → format for ARN-style properties with exclusions
_ARN_MAPPINGS: list[tuple[list[str], str, set[str]]] = [
    (
        ["RoleArn", "RoleARN", "IAMRoleARN", "IamRoleArn"],
        "AWS::IAM::Role.Arn",
        {"AWS::Kendra::Index"},
    ),
    (
        ["KmsKeyArn", "KMSKeyArn", "KmsArn", "KMSArn"],
        "AWS::KMS::Key.Arn",
        set(),
    ),
    (
        ["TopicArn", "TopicARN", "SnsTopicArn", "SNSTopicArn", "SnsTopicARN"],
        "AWS::SNS::Topic.Arn",
        set(),
    ),
    (
        ["FunctionArn", "FunctionARN", "LambdaArn", "LambdaFunctionArn"],
        "AWS::Lambda::Function.Arn",
        set(),
    ),
    (
        ["BucketName", "S3BucketName", "S3Bucket"],
        "AWS::S3::Bucket.Name",
        set(),
    ),
]

_ROLE_ARN_SUFFIXES = ("RoleArn", "RoleARN")

# Prefix exclusions for ARN mappings
_ARN_PREFIX_EXCLUSIONS: dict[str, list[str]] = {
    "AWS::SNS::Topic.Arn": ["AWS::MSK::", "AWS::FMS::"],
    "AWS::Lambda::Function.Arn": ["AWS::CloudFront::", "AWS::AppSync::"],
    "AWS::S3::Bucket.Name": ["AWS::Lightsail::", "AWS::S3Outposts::"],
}

_CERT_EXCLUSIONS = {
    "AWS::EC2::CustomerGateway",
    "AWS::EMRContainers::Endpoint",
    "AWS::MediaPackage::OriginEndpoint",
    "AWS::MediaPackageV2::OriginEndpoint",
}
_CERT_PREFIX_EXCLUSIONS = ["AWS::DMS::", "AWS::Greengrass::", "AWS::IoT"]

# Security group property names
_SG_IDS_PROPS = [
    "CustomSecurityGroupIds", "Ec2SecurityGroupIds", "GroupSet",
    "InputSecurityGroups", "SecurityGroupIdList", "SecurityGroupIds",
    "SecurityGroups", "VpcSecurityGroupIds",
]
_SG_ID_PROPS = [
    "ClusterSecurityGroupId", "DefaultSecurityGroup",
    "DestinationSecurityGroupId", "EC2SecurityGroupId", "SecurityGroup",
    "SecurityGroupId", "SourceSecurityGroupId", "VpcSecurityGroupId",
]
_SG_NAME_PROPS = [
    "CacheSecurityGroupName", "ClusterSecurityGroupName",
    "EC2SecurityGroupName", "SourceSecurityGroupName",
]
_SG_SKIP_TYPES = {
    "AWS::Pipes::Pipe", "AWS::EC2::NetworkInsightsAnalysis",
    "AWS::AutoScaling::LaunchConfiguration", "AWS::EC2::Instance",
}

# CIDR property names
_CIDR_V4_PROPS = [
    "CidrIp", "CIDRIP", "Cidr", "Cidrs", "CidrBlock",
    "DestinationCidr", "DestinationCidrBlock", "SourceCidrBlock",
    "CidrList", "CidrAllowList",
]
_CIDR_V6_PROPS = ["Ipv6CidrBlock", "Ipv6Cidrs", "CidrIpv6"]
_CIDR_SKIP_TYPES = {
    "AWS::SecurityHub::Insight", "AWS::EC2::IPAMPool", "AWS::EC2::PrefixList",
}

def _fmt(path: str, value: Any) -> dict[str, Any]:
    return {"op": "add", "path": path, "value": value}


_SG_ID = "AWS::EC2::SecurityGroup.Id"
_SG_NAME = "AWS::EC2::SecurityGroup.Name"
_SG_IDS_FMT = "AWS::EC2::SecurityGroup.Ids"
_SG_NAMES_FMT = "AWS::EC2::SecurityGroup.Names"

# Manual overrides for resources that need special handling
_MANUAL_PATCHES: dict[str, list[dict[str, Any]]] = {
    "AWS::EC2::SecurityGroup": [
        _fmt("/properties/GroupId/format", _SG_ID),
        _fmt("/properties/GroupName/format", _SG_NAME),
        _fmt("/properties/Id/anyOf", [
            {"format": _SG_ID}, {"format": _SG_NAME},
        ]),
    ],
    "AWS::EC2::SecurityGroupIngress": [
        _fmt("/properties/GroupId/format", _SG_ID),
    ],
    "AWS::EC2::SecurityGroupEgress": [
        _fmt("/properties/GroupId/format", _SG_ID),
    ],
    "AWS::AutoScaling::LaunchConfiguration": [
        _fmt("/properties/SecurityGroups/anyOf", [
            {"format": _SG_IDS_FMT}, {"format": _SG_NAMES_FMT},
        ]),
        _fmt("/properties/SecurityGroups/items/anyOf", [
            {"format": _SG_ID}, {"format": _SG_NAME},
        ]),
    ],
    "AWS::EC2::Instance": [
        _fmt("/properties/SecurityGroups/format", _SG_NAMES_FMT),
        _fmt("/properties/SecurityGroups/items/format", _SG_NAME),
        _fmt("/properties/SecurityGroupIds/anyOf", [
            {"format": _SG_IDS_FMT}, {"format": _SG_NAMES_FMT},
        ]),
        _fmt("/properties/SecurityGroupIds/items/anyOf", [
            {"format": _SG_ID}, {"format": _SG_NAME},
        ]),
        _fmt(
            "/definitions/NetworkInterface/properties"
            "/GroupSet/format",
            _SG_IDS_FMT,
        ),
        _fmt(
            "/definitions/NetworkInterface/properties"
            "/GroupSet/items/format",
            _SG_ID,
        ),
    ],
    "AWS::IAM::Role": [
        _fmt("/properties/Arn/format", "AWS::IAM::Role.Arn"),
    ],
    "AWS::EC2::IPAMPool": [
        _fmt("/definitions/Cidr/anyOf", [
            {"format": "ipv4-network"}, {"format": "ipv6-network"},
        ]),
    ],
    "AWS::EC2::PrefixList": [
        _fmt("/definitions/Entry/properties/Cidr/anyOf", [
            {"format": "ipv4-network"}, {"format": "ipv6-network"},
        ]),
    ],
    "AWS::SQS::Queue": [
        _fmt("/properties/Arn/format", "AWS::SQS::Queue.Arn"),
    ],
    "AWS::Lambda::Function": [
        _fmt("/properties/Arn/format", "AWS::Lambda::Function.Arn"),
        _fmt(
            "/properties/FunctionName/format",
            "AWS::Lambda::Function.Name",
        ),
    ],
    "AWS::KMS::Key": [
        _fmt("/properties/Arn/format", "AWS::KMS::Key.Arn"),
        _fmt("/properties/KeyId/format", "AWS::KMS::Key.Id"),
    ],
    "AWS::KMS::Alias": [
        _fmt(
            "/properties/AliasName/format",
            "AWS::KMS::Alias.AliasName",
        ),
    ],
    "AWS::Lambda::Alias": [
        _fmt(
            "/properties/AliasArn/format",
            "AWS::Lambda::Function.Arn",
        ),
    ],
    "AWS::CertificateManager::Certificate": [
        _fmt(
            "/properties/Id/format", "AWS::ACM::Certificate.Arn",
        ),
    ],
}


def _is_excluded(resource_type: str, fmt: str, exclusions: set[str]) -> bool:
    if resource_type in exclusions:
        return True
    for prefix in _ARN_PREFIX_EXCLUSIONS.get(fmt, []):
        if resource_type.startswith(prefix):
            return True
    return False


def _make_cidr_patches(
    resource_type: str, ref: str, resolver: RefResolver, fmt: str
) -> list[dict[str, Any]]:
    if resource_type in _CIDR_SKIP_TYPES:
        return []
    ref, resolved = _resolve_through_refs(ref, resolver)
    path = ref[1:]
    if isinstance(resolved, dict) and "items" in resolved:
        return [{"op": "add", "path": f"{path}/items/format", "value": fmt}]
    return [{"op": "add", "path": f"{path}/format", "value": fmt}]


def _make_sg_ids_patches(
    resource_type: str, ref: str, resolver: RefResolver
) -> list[dict[str, Any]]:
    if resource_type in _SG_SKIP_TYPES:
        return []
    ref, resolved = _resolve_through_refs(ref, resolver)
    if not isinstance(resolved, dict) or resolved.get("type") != "array":
        return []
    path = ref[1:]
    items = resolved.get("items", {})
    if isinstance(items, dict) and "$ref" in items:
        items_path = items["$ref"][1:]
    else:
        items_path = f"{path}/items"
    return [
        _fmt(f"{path}/format", _SG_IDS_FMT),
        _fmt(f"{items_path}/format", _SG_ID),
    ]


def _make_sg_id_patches(
    resource_type: str, ref: str, resolver: RefResolver
) -> list[dict[str, Any]]:
    if resource_type in _SG_SKIP_TYPES:
        return []
    ref, resolved = _resolve_through_refs(ref, resolver)
    return [_fmt(f"{ref[1:]}/format", _SG_ID)]


def _make_sg_name_patches(
    resource_type: str, ref: str, resolver: RefResolver
) -> list[dict[str, Any]]:
    ref, resolved = _resolve_through_refs(ref, resolver)
    return [_fmt(f"{ref[1:]}/format", _SG_NAME)]


def _make_subnet_ids_patches(
    resource_type: str, ref: str, resolver: RefResolver
) -> list[dict[str, Any]]:
    ref, resolved = _resolve_through_refs(ref, resolver)
    path = ref[1:]
    items = resolved.get("items", {}) if isinstance(resolved, dict) else {}
    if isinstance(items, dict) and "$ref" in items:
        items_path = items["$ref"][1:]
    else:
        items_path = f"{path}/items"
    return [
        _fmt(f"{path}/format", "AWS::EC2::Subnet.Ids"),
        _fmt(f"{items_path}/format", "AWS::EC2::Subnet.Id"),
    ]


@register("format")
class FormatGenerator(BaseGenerator):
    """Generate format annotation patches by scanning property names."""

    def run(self) -> None:
        patches_dir = self.patches_dir
        count = 0
        written: set[Path] = set()

        for schema_file in sorted(self.resources_dir.glob("*.json")):
            schema = json.loads(schema_file.read_text())
            resource_type = schema.get("typeName", "")
            if not resource_type:
                continue

            resolver = RefResolver.from_schema(schema)
            resource_patches: list[dict[str, Any]] = []

            # Manual overrides first
            if resource_type in _MANUAL_PATCHES:
                resource_patches.extend(_MANUAL_PATCHES[resource_type])

            # Simple property name → format mappings
            for keywords, fmt, exclusions in _SIMPLE_MAPPINGS:
                if resource_type in exclusions:
                    continue
                for path in _descend(schema, keywords):
                    if path[-2] == "properties":
                        p = _make_format_patch(fmt, "#/" + "/".join(path), resolver)
                        if p:
                            resource_patches.append(p)

            # ARN-style mappings
            for keywords, fmt, exclusions in _ARN_MAPPINGS:
                if _is_excluded(resource_type, fmt, exclusions):
                    continue
                for path in _descend(schema, keywords):
                    if path[-2] == "properties":
                        p = _make_format_patch(fmt, "#/" + "/".join(path), resolver)
                        if p:
                            resource_patches.append(p)

            # Suffix match for *RoleArn/*RoleARN
            if not _is_excluded(resource_type, "AWS::IAM::Role.Arn", set()):
                for section in ["definitions", "properties"]:
                    container = schema.get(section, {})
                    if section == "definitions":
                        for def_name, def_value in container.items():
                            for prop_name in def_value.get("properties", {}):
                                if prop_name.endswith(_ROLE_ARN_SUFFIXES) and prop_name not in _ROLE_ARN_SUFFIXES:  # noqa: E501
                                    p = _make_format_patch(
                                        "AWS::IAM::Role.Arn",
                                        f"#/definitions/{def_name}/properties/{prop_name}",
                                        resolver,
                                    )
                                    if p:
                                        resource_patches.append(p)
                    else:
                        for prop_name in container:
                            if prop_name.endswith(_ROLE_ARN_SUFFIXES) and prop_name not in _ROLE_ARN_SUFFIXES:  # noqa: E501
                                p = _make_format_patch(
                                    "AWS::IAM::Role.Arn",
                                    f"#/properties/{prop_name}",
                                    resolver,
                                )
                                if p:
                                    resource_patches.append(p)

            # KMS key ID (multi-format)
            _kms_props = [
                "KmsKeyId", "KMSKeyId", "KmsMasterKeyId",
                "KMSMasterKeyID",
            ]
            for path in _descend(schema, _kms_props):
                if path[-2] == "properties":
                    ref = "#/" + "/".join(path)
                    ref, _ = _resolve_through_refs(ref, resolver)
                    resource_patches.append({
                        "op": "add",
                        "path": f"{ref[1:]}/anyOf",
                        "value": [
                            {"format": "AWS::KMS::Key.Arn"},
                            {"format": "AWS::KMS::Key.Id"},
                            {"format": "AWS::KMS::Alias.AliasName"},
                        ],
                    })

            # Certificate ARNs
            _cert_props = ["CertificateArn", "CertificateARN", "AcmCertificateArn"]
            for path in _descend(schema, _cert_props):
                if path[-2] == "properties":
                    if resource_type in _CERT_EXCLUSIONS:
                        continue
                    if any(resource_type.startswith(p) for p in _CERT_PREFIX_EXCLUSIONS):
                        continue
                    ref = "#/" + "/".join(path)
                    p = _make_format_patch("AWS::ACM::Certificate.Arn", ref, resolver)
                    if p:
                        resource_patches.append(p)

            # CIDR blocks
            _cidr_mappings = [
                (_CIDR_V4_PROPS, "ipv4-network"),
                (_CIDR_V6_PROPS, "ipv6-network"),
            ]
            for keywords, fmt in _cidr_mappings:
                for path in _descend(schema, keywords):
                    if path[-2] == "properties":
                        resource_patches.extend(
                            _make_cidr_patches(resource_type, "#/" + "/".join(path), resolver, fmt)
                        )

            # Subnets (array)
            for path in _descend(schema, ["Subnets"]):
                if path[-2] == "properties":
                    resource_patches.extend(
                        _make_subnet_ids_patches(resource_type, "#/" + "/".join(path), resolver)
                    )

            # Security groups
            for path in _descend(schema, _SG_IDS_PROPS):
                if path[-2] == "properties":
                    resource_patches.extend(
                        _make_sg_ids_patches(resource_type, "#/" + "/".join(path), resolver)
                    )
            for path in _descend(schema, _SG_ID_PROPS):
                if path[-2] == "properties":
                    resource_patches.extend(
                        _make_sg_id_patches(resource_type, "#/" + "/".join(path), resolver)
                    )
            for path in _descend(schema, _SG_NAME_PROPS):
                if path[-2] == "properties":
                    resource_patches.extend(
                        _make_sg_name_patches(resource_type, "#/" + "/".join(path), resolver)
                    )

            # Write patches
            if resource_patches:
                # Deduplicate by path
                seen: set[str] = set()
                deduped: list[dict[str, Any]] = []
                for p in resource_patches:
                    key = p["path"]
                    if key not in seen:
                        seen.add(key)
                        deduped.append(p)

                dir_name = self.resource_type_to_dir(resource_type)
                output_file = patches_dir / dir_name / "format.json"
                self.write_json(output_file, sorted(deduped, key=lambda x: x["path"]))
                written.add(output_file)
                count += 1

        # Clean up stale format.json files
        for existing in patches_dir.glob("*/format.json"):
            if existing not in written:
                logger.info("Removing stale %s", existing)
                existing.unlink()

        logger.info("Wrote %d format patch files", count)
