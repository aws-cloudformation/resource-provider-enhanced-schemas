"""Generate lifecycle patches for deprecated/sunset/shutdown resource types.

Sources (the canonical authority for lifecycle status; reconciled by hand):
- https://docs.aws.amazon.com/general/latest/gr/full_shutdown_services.html  -> shutdown
- https://docs.aws.amazon.com/general/latest/gr/sunset_services.html         -> sunset
- https://docs.aws.amazon.com/general/latest/gr/maintenance_services.html    -> maintenance

These three lists are maintained by hand and must be refreshed periodically
against the pages above; the generator does not scrape them. Each entry maps an
AWS service to its CloudFormation type prefix(es); the generator attaches the
lifecycle key to every registered type that matches (see _match_types). Dates
follow the pages: the shutdown and sunset lists use the page's End of Support
Date, the maintenance list uses the page's Announcement Date.

Last reconciled against the live pages on 2026-10-23 (see the lifecycle refresh
PR). A prefix that matches no currently registered type (e.g. a service whose
types CloudFormation has already deregistered) emits no patch and is harmless.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from cfn_schemas.generators import register
from cfn_schemas.generators.base import BaseGenerator

logger = logging.getLogger(__name__)

# (prefix, date). Date = End of Support Date from the full shutdown page.
# Full shutdown: the service is removed from the AWS portfolio; a create fails.
SHUTDOWN = [
    ("AWS::AppTest::", "2025-10-07"),  # Mainframe Modernization Application Testing
    ("AWS::CodeStar::GitHubRepository", "2024-07-25"),  # AWS CodeStar
    ("AWS::Evidently", "2025-10-17"),  # Amazon CloudWatch Evidently
    ("AWS::FinSpace", "2026-10-07"),  # Amazon FinSpace (was mislabeled sunset)
    ("AWS::Inspector::", "2026-05-20"),  # Amazon Inspector Classic, not InspectorV2
    ("AWS::IoTAnalytics", "2025-12-15"),  # AWS IoT Analytics
    ("AWS::IoTEvents", "2026-05-20"),  # AWS IoT Events (no registered type currently)
    ("AWS::IoTThingsGraph", "2022-11-09"),  # AWS IoT Things Graph
    ("AWS::KinesisAnalytics::", "2026-01-27"),  # Kinesis Data Analytics for SQL, not V2
    ("AWS::LookoutVision::", "2025-10-31"),  # Lookout for Vision (no registered type currently)
    ("AWS::MediaStore", "2025-11-12"),  # AWS Elemental MediaStore (was mislabeled sunset)
    ("AWS::NimbleStudio", "2024-06-30"),  # Amazon Nimble Studio
    ("AWS::OpsWorks", "2024-05-01"),  # AWS OpsWorks
    ("AWS::Panorama::", "2026-05-20"),  # AWS Panorama
    ("AWS::QLDB", "2025-07-31"),  # Amazon QLDB
    ("AWS::RoboMaker::", "2025-09-10"),  # AWS RoboMaker
    ("AWS::SimSpaceWeaver", "2026-03-31"),  # AWS SimSpace Weaver (was mislabeled sunset)
    ("AWS::VoiceID::", "2026-05-20"),  # Amazon Connect Customer Voice ID
]

# Date = End of Support Date from the sunset page.
# Sunset: the service still deploys but support ends on the date; plan to migrate.
SUNSET = [
    ("AWS::AppMesh", "2026-09-30"),  # AWS App Mesh
    ("AWS::DevOpsGuru::", "2027-09-29"),  # Amazon DevOps Guru
    ("AWS::Greengrass::", "2026-10-07"),  # AWS IoT Greengrass V1, not GreengrassV2
    ("AWS::LookoutEquipment::", "2026-10-07"),  # Amazon Lookout for Equipment
    ("AWS::ManagedBlockchain::", "2027-09-29"),  # Amazon Managed Blockchain
    ("AWS::Pinpoint", "2026-10-30"),  # Amazon Pinpoint
    ("AWS::Proton", "2026-10-07"),  # AWS Proton
    ("AWS::WAF::", "2026-10-07"),  # AWS WAF Classic, not WAFv2
    ("AWS::WAFRegional", "2026-10-07"),  # AWS WAF Classic (regional)
    ("AWS::WorkSpacesThinClient::", "2027-03-31"),  # Amazon WorkSpaces Thin Client
]

# Date = Announcement Date from the maintenance page.
# Maintenance: existing use continues but customers can't on-board; a create of a
# new resource is blocked. Many entries are a single type because only that
# feature of an otherwise-live service is in maintenance.
MAINTENANCE = [
    ("AWS::AppRunner::", "2026-03-31"),  # AWS App Runner
    ("AWS::AuditManager::", "2026-03-31"),  # AWS Audit Manager
    ("AWS::AutoScaling::LaunchConfiguration", "2024-10-01"),  # EC2 Auto Scaling
    ("AWS::Bedrock::Agent", "2026-06-30"),  # Amazon Bedrock Agents Classic
    ("AWS::Bedrock::AgentAlias", "2026-06-30"),  # Amazon Bedrock Agents Classic
    ("AWS::Cloud9", "2024-07-25"),  # AWS Cloud9
    ("AWS::CloudTrail::EventDataStore", "2026-03-31"),  # AWS CloudTrail Lake only
    ("AWS::CodeGuruReviewer", "2025-10-07"),  # Amazon CodeGuru Reviewer
    ("AWS::DirectoryService::SimpleAD", "2026-06-30"),  # Simple AD only
    ("AWS::Forecast", "2024-07-29"),  # Amazon Forecast
    ("AWS::FraudDetector", "2025-10-07"),  # Amazon Fraud Detector (was mislabeled sunset)
    ("AWS::IoT::SecurityProfile", "2026-06-30"),  # IoT Device Defender Detect
    ("AWS::IoTFleetWise::", "2026-03-31"),  # AWS IoT FleetWise
    ("AWS::IoTSiteWise::AccessPolicy", "2025-10-07"),  # IoT SiteWise Monitor
    ("AWS::IoTSiteWise::Dashboard", "2025-10-07"),  # IoT SiteWise Monitor
    ("AWS::IoTSiteWise::Portal", "2025-10-07"),  # IoT SiteWise Monitor
    ("AWS::IoTSiteWise::Project", "2025-10-07"),  # IoT SiteWise Monitor
    ("AWS::Kendra::", "2026-06-30"),  # Amazon Kendra
    ("AWS::M2::", "2025-11-07"),  # Mainframe Modernization Managed Runtime Environment
    ("AWS::Omics::AnnotationStore", "2025-11-07"),  # HealthOmics annotation store
    ("AWS::Omics::VariantStore", "2025-11-07"),  # HealthOmics variant store
    ("AWS::QBusiness::", "2026-06-30"),  # Amazon Q Business
    ("AWS::Route53RecoveryReadiness::", "2026-03-31"),  # ARC Readiness Check
    ("AWS::S3ObjectLambda::", "2025-11-07"),  # Amazon S3 Object Lambda
    ("AWS::SageMaker::DataQualityJobDefinition", "2026-06-30"),  # Model Monitor
    ("AWS::SageMaker::HumanTaskUi", "2026-06-30"),  # SageMaker AI A2I
    ("AWS::SageMaker::ModelBiasJobDefinition", "2026-06-30"),  # Clarify
    ("AWS::SageMaker::ModelExplainabilityJobDefinition", "2026-06-30"),  # Clarify
    ("AWS::SageMaker::ModelQualityJobDefinition", "2026-06-30"),  # Model Monitor
    ("AWS::SageMaker::MonitoringSchedule", "2026-06-30"),  # Model Monitor
    ("AWS::ServiceCatalogAppRegistry::", "2026-06-30"),  # Service Catalog App Registry
    ("AWS::SSMIncidents::", "2025-11-07"),  # Systems Manager Incident Manager
    ("AWS::Timestream::Database", "2025-06-20"),  # Timestream for LiveAnalytics
    ("AWS::Timestream::ScheduledQuery", "2025-06-20"),  # Timestream for LiveAnalytics
    ("AWS::Timestream::Table", "2025-06-20"),  # Timestream for LiveAnalytics
    ("AWS::WorkSpacesWeb::", "2026-09-29"),  # WorkSpaces Secure Browser
]


def _match_types(all_types: list[str], prefix: str) -> list[str]:
    return [
        tn
        for tn in all_types
        if tn.startswith(prefix)
        and (prefix.endswith("::") or tn == prefix or tn[len(prefix)] == ":")
    ]


@register("lifecycle")
class LifecycleGenerator(BaseGenerator):
    """Generate lifecycle status patches for deprecated resource types."""

    def run(self) -> None:
        all_types = self._get_resource_types_from_schemas()
        patches_dir = self.patches_dir
        count = 0

        for status, services in [
            ("shutdown", SHUTDOWN),
            ("sunset", SUNSET),
            ("maintenance", MAINTENANCE),
        ]:
            for prefix, date in services:
                for type_name in _match_types(all_types, prefix):
                    self._write_patch(patches_dir, type_name, status, date)
                    count += 1

        logger.info("Wrote %d lifecycle patches", count)

    def _get_resource_types_from_schemas(self) -> list[str]:
        """Get resource types by reading schema files directly."""
        types: list[str] = []
        for f in sorted(self.resources_dir.glob("*.json")):
            schema = json.loads(f.read_text())
            tn = schema.get("typeName", "")
            if tn:
                types.append(tn)
        return types

    def _write_patch(
        self, patches_dir: Path, type_name: str, status: str, date: str
    ) -> None:
        dir_name = self.resource_type_to_dir(type_name)
        output_dir = patches_dir / dir_name
        output_file = output_dir / "lifecycle.json"

        value: dict[str, str] = {"status": status}
        if date:
            value["date"] = date

        patch = [{"op": "add", "path": "/lifecycle", "value": value}]
        self.write_json(output_file, patch)
        logger.info("  %s -> %s (%s)", type_name, status, date or "no date")
