"""Base class for all schema generators."""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from pathlib import Path

logger = logging.getLogger(__name__)


class BaseGenerator(ABC):
    """Base class for schema generators.

    Each generator reads from some data source (APIs, files, URLs)
    and writes its output to the schemas/ directory. Generators are
    independent and idempotent — running one doesn't affect others,
    and running the same one twice produces the same output.
    """

    def __init__(self, schemas_dir: Path) -> None:
        self.schemas_dir = schemas_dir
        self.resources_dir = schemas_dir / "resources"
        self.providers_dir = schemas_dir / "providers"
        self.patches_dir = schemas_dir / "patches"

    @abstractmethod
    def run(self) -> None:
        """Run the generator. Writes output files to schemas_dir."""

    # Key ordering for resource schemas — grouped logically
    _SCHEMA_KEY_ORDER = [
        # Identity
        "typeName",
        "description",
        "sourceUrl",
        # Properties
        "properties",
        "required",
        "definitions",
        # Identifiers
        "primaryIdentifier",
        "additionalIdentifiers",
        # Property classifications
        "createOnlyProperties",
        "readOnlyProperties",
        "writeOnlyProperties",
        "conditionalCreateOnlyProperties",
        "deprecatedProperties",
        # Metadata
        "tagging",
        "lifecycle",
        "replacementStrategy",
        # Boilerplate
        "additionalProperties",
        "nonPublicProperties",
        "nonPublicDefinitions",
        "propertyTransform",
    ]

    def write_json(self, path: Path, data: object) -> None:
        """Write JSON with consistent formatting."""
        path.parent.mkdir(parents=True, exist_ok=True)
        data = self._sort_data(data, is_root=True)
        path.write_text(
            json.dumps(data, indent=1, separators=(",", ": ")) + "\n"
        )

    @classmethod
    def _sort_data(cls, obj: object, is_root: bool = False) -> object:
        """Recursively sort dicts. At root level, use logical key ordering."""
        if isinstance(obj, dict):
            if is_root and "typeName" in obj:
                items = []
                # Add keys in defined order first
                for key in cls._SCHEMA_KEY_ORDER:
                    if key in obj:
                        items.append((key, cls._sort_data(obj[key])))
                # Add any remaining keys alphabetically
                for key in sorted(obj.keys()):
                    if key not in cls._SCHEMA_KEY_ORDER:
                        items.append((key, cls._sort_data(obj[key])))
            else:
                items = [(k, cls._sort_data(v)) for k, v in sorted(obj.items())]
            return dict(items)
        if isinstance(obj, list):
            return [cls._sort_data(v) for v in obj]
        return obj

    def read_json(self, path: Path) -> object:
        """Read a JSON file."""
        return json.loads(path.read_text())

    def get_resource_types(self) -> list[str]:
        """Get all known resource types from the provider mappings."""
        resource_types: set[str] = set()
        for provider_file in self.providers_dir.glob("*.json"):
            mappings = json.loads(provider_file.read_text())
            resource_types.update(mappings.keys())
        return sorted(resource_types)

    @staticmethod
    def resource_type_to_dir(resource_type: str) -> str:
        """Convert AWS::EC2::Instance to aws_ec2_instance."""
        return resource_type.replace("::", "_").lower()
