"""Extract enum values and constraints from AWS Smithy API models.

Downloads Smithy models from GitHub and CloudFormation schemas from AWS,
then matches Smithy operation inputs to CF schema properties to extract
enums, length/range constraints, and regex patterns.
"""

from __future__ import annotations

import io
import json
import logging
import tempfile
import zipfile
from collections import namedtuple
from pathlib import Path
from typing import Any

import regex as re
import requests

from cfn_schemas.generators import register
from cfn_schemas.generators.base import BaseGenerator
from cfn_schemas.resolver import RefResolutionError, RefResolver

logger = logging.getLogger(__name__)

SMITHY_URL = "https://github.com/aws/api-models-aws/archive/refs/heads/main.zip"
SCHEMA_URL = "https://schema.cloudformation.us-east-1.amazonaws.com/CloudformationSchema.zip"

Patch = namedtuple("Patch", ["source", "shape"])

# Services to skip entirely
_SKIP_SERVICES = {
    "account", "chime", "chime-sdk-identity", "chime-sdk-messaging",
    "chime-sdk-meetings", "chime-sdk-voice", "payment-cryptography-data",
    "rds-data", "finspace-data", "appconfigdata", "iot-jobs-data-plane",
    "dataexchange", "bedrock-runtime", "swf", "cloudhsm", "cloudhsm-v2",
    "workdocs",
}

_SKIP_RESOURCE_TYPES = {"AWS::CloudFormation::Stack"}
_SKIP_PROPERTY_NAMES = {"State"}
_SKIP_RESOURCE_PATHS: dict[str, list[str]] = {
    "AWS::AmazonMQ::Broker": ["/properties/StorageType"],
    "AWS::CloudFormation::StackSet": ["/properties/ExecutionRoleName"],
    "AWS::CloudFront::Distribution": ["/definitions/Cookies/properties/Forward"],
    "AWS::Bedrock::Guardrail": ["/definitions/SensitiveInformationPolicyConfig/properties/RegexesConfig"],
    "AWS::Connect::RoutingProfile": ["/properties/QueueConfigs"],
    "AWS::Connect::User": ["/definitions/DeskPhoneNumber"],
    "AWS::DynamoDB::Table": [
        "/definitions/KeySchema",
        "/definitions/LocalSecondaryIndex/properties/KeySchema",
        "/definitions/Projection/properties/NonKeyAttributes",
        "/definitions/ProvisionedThroughput/properties/ReadCapacityUnits",
        "/definitions/ProvisionedThroughput/properties/WriteCapacityUnits",
        "/definitions/SSESpecification/properties/SSEType",
    ],
    "AWS::DynamoDB::GlobalTable": [
        "/definitions/KeySchema",
        "/definitions/LocalSecondaryIndex/properties/KeySchema",
        "/definitions/SSESpecification/properties/SSEType",
    ],
    "AWS::EC2::Instance": ["/properties/InstanceType"],
    "AWS::EC2::LaunchTemplate": [
        "/definitions/LaunchTemplateData/properties/InstanceType",
    ],
    "AWS::EC2::EC2Fleet": [
        "/definitions/FleetLaunchTemplateOverridesRequest/properties/InstanceType",
    ],
    "AWS::EC2::CapacityReservationFleet": [
        "/definitions/InstanceTypeSpecification/properties/InstanceType",
    ],
    "AWS::ElastiCache::ReplicationGroup": ["/properties/UserGroupIds"],
    "AWS::ElasticLoadBalancingV2::LoadBalancer": ["/properties/Tags"],
    "AWS::ElasticLoadBalancingV2::TargetGroup": ["/properties/Tags"],
    "AWS::GameLift::GameServerGroup": ["/definitions/InstanceType"],
    "AWS::MSK::Cluster": ["/properties/NumberOfBrokerNodes"],
    "AWS::SageMaker::Cluster": ["/definitions/InstanceType"],
    "AWS::SageMaker::DataQualityJobDefinition": [
        "/definitions/ClusterConfig/properties/InstanceType",
    ],
    "AWS::SageMaker::InferenceExperiment": [
        "/definitions/RealTimeInferenceConfig/properties/InstanceType",
    ],
    "AWS::SageMaker::ModelBiasJobDefinition": [
        "/definitions/ClusterConfig/properties/InstanceType",
    ],
    "AWS::SageMaker::ModelExplainabilityJobDefinition": [
        "/definitions/ClusterConfig/properties/InstanceType",
    ],
    "AWS::SageMaker::ModelPackage": [
        "/definitions/InferenceInstanceType",
        "/definitions/TransformInstanceType",
        "/definitions/TransformResources/properties/InstanceType",
    ],
    "AWS::SageMaker::ModelQualityJobDefinition": [
        "/definitions/ClusterConfig/properties/InstanceType",
    ],
    "AWS::SageMaker::MonitoringSchedule": [
        "/definitions/ClusterConfig/properties/InstanceType",
    ],
    "AWS::SNS::Topic": ["/properties/TopicName"],
    "AWS::Lambda::Function": ["/properties/Layers/items"],
    "AWS::Logs::LogAnomalyDetector": ["/properties/LogGroupArnList/items"],
    "AWS::EC2::NetworkInterface": ["/properties/InterfaceType"],
    "AWS::Backup::BackupSelection": ["/definitions/BackupSelectionResourceType/properties/SelectionName"],
}

_CASE_INSENSITIVE_PREFIXES = [
    "AWS::AmazonMQ::", "AWS::Batch::", "AWS::EC2::EIP", "AWS::EC2::IPAMPool",
    "AWS::EC2::NetworkAcl", "AWS::EC2::SecurityGroup",
    "AWS::EC2::TrafficMirrorFilterRule", "AWS::EC2::Volume",
    "AWS::ElastiCache::", "AWS::ElasticLoadBalancing::",
    "AWS::Route53Resolver::",
]

_PATH_EXCEPTIONS: dict[str, list[str]] = {
    "ses": ["/definitions/EventDestination/properties/MatchingEventTypes/items",
            "/definitions/DimensionConfiguration/properties/DimensionValueSource"],
    "ecs": ["/definitions/LogConfiguration/properties/LogDriver"],
    "lambda": ["/properties/FunctionName"],
    "cloudwatch": ["/properties/AlarmActions", "/properties/OKActions", "/properties/InsufficientDataActions"],
    "cloudwatch-events": ["/properties/Targets"],
    "connect": ["/properties/QueueConfigs"],
    "batch": ["/definitions/Ec2ConfigurationObject/properties/ImageIdOverride"],
    "securityhub": ["/definitions/MapFilter/properties/Value"],
    "rds": ["/properties/ReplicaMode"],
    "ec2": ["/properties/Domain", "/properties/Type"],
    "iam": ["/properties/InstanceProfileName"],
}

# Smithy service name → CF service name
_SERVICE_RENAMES: dict[str, str] = {
    "acm": "certificatemanager", "mq": "amazonmq", "kafka": "msk",
    "firehose": "kinesisfirehose", "elasticsearch-service": "elasticsearch",
    "elastic-load-balancing-v2": "elasticloadbalancingv2",
    "elastic-load-balancing": "elasticloadbalancing",
    "directory-service": "directoryservice", "api-gateway": "apigateway",
    "auto-scaling": "autoscaling", "auto-scaling-plans": "autoscalingplans",
    "config-service": "config", "cost-explorer": "costexplorer",
    "cognito-identity-provider": "cognitoidentityprovider",
    "cognito-identity": "cognitoidentity",
    "application-auto-scaling": "applicationautoscaling",
    "cloudwatch-events": "events", "cloudwatch-logs": "logs",
    "database-migration-service": "databasemigrationservice",
    "docdb-elastic": "docdbelastic", "resource-explorer-2": "resourceexplorer2",
    "route-53": "route53", "sfn": "stepfunctions",
    "ssm-quicksetup": "ssmquicksetup", "vpc-lattice": "vpclattice",
    "waf-regional": "wafregional",
}


def _rename_service(name: str) -> str:
    if name in _SERVICE_RENAMES:
        return _SERVICE_RENAMES[name]
    return name.replace("-", "").lower()


def _extract_enum(shape: dict) -> list[str] | None:
    if shape.get("type") != "enum":
        return None
    values = []
    for member_data in shape.get("members", {}).values():
        v = member_data.get("traits", {}).get("smithy.api#enumValue")
        if v:
            values.append(v)
    return sorted(values) if values else None


def _extract_constraints(shape: dict) -> dict[str, Any]:
    result: dict[str, Any] = {}
    traits = shape.get("traits", {})
    for trait_key in ("smithy.api#length", "smithy.api#range"):
        if trait_key in traits:
            data = traits[trait_key]
            if "min" in data:
                result["min"] = data["min"]
            if "max" in data:
                result["max"] = data["max"]
    if "smithy.api#pattern" in traits:
        result["pattern"] = traits["smithy.api#pattern"]
    return result


def _get_create_operations(schema: dict) -> list[str]:
    prefixes = ("Put", "Add", "Create", "Publish", "Register", "Allocate", "Start", "Run")
    ops = []
    for api in schema.get("handlers", {}).get("create", {}).get("permissions", []):
        if ":" not in api:
            continue
        action = api.split(":")[1]
        if any(action.startswith(p) for p in prefixes):
            ops.append(action)
    return ops


def _find_operation_input(smithy: dict, op_name: str) -> str | None:
    for name, data in smithy.get("shapes", {}).items():
        if data.get("type") == "operation" and name.endswith(f"#{op_name}"):
            target = data.get("input", {}).get("target")
            return str(target) if target else None
    return None


def _walk_objects(
    resolver: RefResolver, schema: dict, smithy: dict, shape: dict,
    path: str, source: list[str], visited: set[str],
) -> dict[str, Patch]:
    results: dict[str, Patch] = {}
    for member_name, member_data in shape.get("members", {}).items():
        for p_name, p_data in schema.get("properties", {}).items():
            if p_name in _SKIP_PROPERTY_NAMES:
                continue
            if p_name.lower() != member_name.lower():
                continue
            prop_path = f"{path}/properties/{p_name}"
            if prop_path in visited:
                continue
            visited.add(prop_path)

            while isinstance(p_data, dict) and "$ref" in p_data:
                prop_path = p_data["$ref"][1:]
                try:
                    p_data = resolver.resolve_from_url(p_data["$ref"])
                except RefResolutionError:
                    break

            target = member_data.get("target")
            member_shape = smithy.get("shapes", {}).get(target) if target else None
            if not member_shape:
                continue

            if member_shape.get("type") == "structure" and isinstance(p_data, dict) and p_data.get("type") == "object":
                results.update(_walk_objects(resolver, p_data, smithy, member_shape, prop_path, source, visited))
            elif member_shape.get("type") == "list" and isinstance(p_data, dict) and p_data.get("type") == "array":
                list_target = member_shape.get("member", {}).get("target")
                if list_target:
                    list_shape = smithy.get("shapes", {}).get(list_target)
                    if list_shape:
                        items_path = f"{prop_path}/items"
                        items_data = p_data.get("items", {})
                        while isinstance(items_data, dict) and "$ref" in items_data:
                            items_path = items_data["$ref"][1:]
                            items_data = resolver.resolve_from_url(items_data["$ref"])
                        if list_shape.get("type") == "structure":
                            results.update(_walk_objects(resolver, items_data, smithy, list_shape, items_path, source, visited))
                        else:
                            results[items_path] = Patch(source=source, shape=list_target)

            results[prop_path] = Patch(source=source, shape=target)
    return results


def _match_resource(resolver: RefResolver, smithy: dict, source: list[str]) -> dict[str, Patch]:
    _, schema = resolver.resolve("/")
    results: dict[str, Patch] = {}
    for op in _get_create_operations(schema):
        input_name = _find_operation_input(smithy, op)
        if not input_name:
            continue
        input_shape = smithy.get("shapes", {}).get(input_name)
        if not input_shape:
            continue
        results.update(_walk_objects(resolver, schema, smithy, input_shape, "", source, set()))
    return results


@register("smithy")
class SmithyGenerator(BaseGenerator):
    """Extract enums and constraints from AWS Smithy API models."""

    def run(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            smithy_dir = tmp / "smithy"
            schema_dir = tmp / "schemas"

            logger.info("Downloading Smithy API models...")
            r = requests.get(SMITHY_URL)
            zipfile.ZipFile(io.BytesIO(r.content)).extractall(smithy_dir)

            logger.info("Downloading CloudFormation schemas...")
            r = requests.get(SCHEMA_URL)
            zipfile.ZipFile(io.BytesIO(r.content)).extractall(schema_dir)

            models_dir = smithy_dir / "api-models-aws-main" / "models"
            all_patches = self._build_all_patches(models_dir, schema_dir)

            logger.info("Found patches for %d resource types", len(all_patches))
            self._write_all_patches(all_patches, models_dir, schema_dir)

    def _build_all_patches(self, models_dir: Path, schema_dir: Path) -> dict[str, dict[str, Patch]]:
        results: dict[str, dict[str, Patch]] = {}
        for service_dir in sorted(models_dir.iterdir()):
            if not service_dir.is_dir() or service_dir.name in _SKIP_SERVICES:
                continue
            svc_name = _rename_service(service_dir.name)
            service_path = service_dir / "service"
            if not service_path.exists():
                continue
            versions = sorted(d.name for d in service_path.iterdir() if d.is_dir())
            if not versions:
                continue
            latest = versions[-1]
            smithy_file = service_path / latest / f"{service_dir.name}-{latest}.json"
            if not smithy_file.exists():
                continue
            smithy = json.loads(smithy_file.read_text())
            for res_file in sorted(schema_dir.glob(f"aws-{svc_name}-*.json")):
                resolver = RefResolver.from_schema(json.loads(res_file.read_text()))
                _, schema = resolver.resolve("/")
                rt = schema.get("typeName", "")
                if not rt or rt in _SKIP_RESOURCE_TYPES:
                    continue
                source = [service_dir.name, latest]
                patches = {
                    p: v for p, v in _match_resource(resolver, smithy, source).items()
                    if p not in _SKIP_RESOURCE_PATHS.get(rt, [])
                }
                if patches:
                    results[rt] = patches
        return results

    def _write_all_patches(self, all_patches: dict, smithy_dir: Path, schema_dir: Path) -> None:
        patches_dir = self.patches_dir
        for rt, resource_patches in all_patches.items():
            rt_lower = rt.lower().replace("::", "-")
            resolver = RefResolver.from_schema(
                json.loads((schema_dir / f"{rt_lower}.json").read_text())
            )
            output: list[dict[str, Any]] = []
            for path, patch in resource_patches.items():
                _, schema_data = resolver.resolve(f"#{path}")
                smithy_file = smithy_dir / patch.source[0] / "service" / patch.source[1] / f"{patch.source[0]}-{patch.source[1]}.json"
                if not smithy_file.exists():
                    continue
                smithy = json.loads(smithy_file.read_text())
                shape = smithy.get("shapes", {}).get(patch.shape, {})
                if not shape:
                    continue

                is_ci = any(rt.startswith(p) for p in _CASE_INSENSITIVE_PREFIXES)
                service_name = patch.source[0]

                # Enums
                enum_values = _extract_enum(shape)
                if enum_values:
                    if any(f in schema_data for f in ("enum", "pattern", "properties", "items")):
                        if is_ci and "enum" in schema_data:
                            output.append({"op": "remove", "path": f"{path}/enum"})
                            output.append({"op": "add", "path": f"{path}/enumCaseInsensitive",
                                           "value": sorted(v.lower() for v in schema_data["enum"])})
                        continue
                    if service_name in _PATH_EXCEPTIONS and path in _PATH_EXCEPTIONS[service_name]:
                        continue
                    field = "enumCaseInsensitive" if is_ci else "enum"
                    values = sorted(v.lower() for v in enum_values) if is_ci else enum_values
                    output.append({"op": "add", "path": f"{path}/{field}", "value": values})
                    continue

                # Constraints
                constraints = _extract_constraints(shape)
                shape_type = shape.get("type")
                for field, value in constraints.items():
                    if service_name in _PATH_EXCEPTIONS and path in _PATH_EXCEPTIONS[service_name]:
                        continue
                    if field == "pattern":
                        if any(f in schema_data for f in ("enum", "pattern", "properties", "items")):
                            continue
                        if value in (".*", "^.*$"):
                            continue
                        try:
                            re.compile(value)
                        except Exception:
                            continue
                        output.append({"op": "add", "path": f"{path}/pattern", "value": value})
                    elif field in ("min", "max"):
                        if shape_type == "string":
                            jf = "maxLength" if field == "max" else "minLength"
                        elif shape_type == "list":
                            jf = "maxItems" if field == "max" else "minItems"
                        elif shape_type in ("integer", "long", "float", "double"):
                            jf = "maximum" if field == "max" else "minimum"
                        else:
                            continue
                        if jf in schema_data:
                            continue
                        if "pattern" in schema_data and re.match(r"^.*\{[0-9]+,[0-9]+\}\$?$", schema_data["pattern"]):
                            continue
                        output.append({"op": "add", "path": f"{path}/{jf}", "value": value})

            dir_name = self.resource_type_to_dir(rt)
            output_file = patches_dir / dir_name / "smithy.json"
            if output:
                self.write_json(output_file, output)
            elif output_file.exists():
                output_file.unlink()

        logger.info("Wrote smithy patches")
