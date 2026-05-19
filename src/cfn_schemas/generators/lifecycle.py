"""Generate lifecycle patches for deprecated/sunset/shutdown resource types.

Sources:
- https://docs.aws.amazon.com/general/latest/gr/full_shutdown_services.html
- https://docs.aws.amazon.com/general/latest/gr/sunset_services.html
- https://docs.aws.amazon.com/general/latest/gr/maintenance_services.html
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from cfn_schemas.generators import register
from cfn_schemas.generators.base import BaseGenerator

logger = logging.getLogger(__name__)

# (prefix, date)
SHUTDOWN = [
    ("AWS::CodeStar::GitHubRepository", "2024-07-25"),
    ("AWS::Evidently", "2025-10-17"),
    ("AWS::IoTAnalytics", "2025-12-15"),
    ("AWS::IoTEvents", "2026-05-20"),
    ("AWS::IoTThingsGraph", "2022-11-09"),
    ("AWS::NimbleStudio", "2024-06-30"),
    ("AWS::OpsWorks", "2024-05-01"),
    ("AWS::QLDB", "2025-07-31"),
]

SUNSET = [
    ("AWS::AppMesh", "2026-09-30"),
    ("AWS::FinSpace", "2026-10-07"),
    ("AWS::FraudDetector", "2025-11-07"),
    ("AWS::Greengrass::", "2026-10-01"),
    ("AWS::Inspector::", "2026-05-20"),
    ("AWS::MediaStore", "2025-11-13"),
    ("AWS::Pinpoint", "2026-10-30"),
    ("AWS::Proton", "2026-10-07"),
    ("AWS::SimSpaceWeaver", "2026-05-20"),
    ("AWS::WAF::", ""),
    ("AWS::WAFRegional", ""),
]

MAINTENANCE = [
    ("AWS::Cloud9", "2024-07-25"),
    ("AWS::CodeGuruReviewer", "2025-10-07"),
    ("AWS::Forecast", "2024-07-29"),
    ("AWS::Timestream::Database", "2025-06-20"),
    ("AWS::Timestream::ScheduledQuery", "2025-06-20"),
    ("AWS::Timestream::Table", "2025-06-20"),
    ("AWS::AutoScaling::LaunchConfiguration", "2024-10-01"),
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
