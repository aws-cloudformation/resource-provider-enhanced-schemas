"""Manual patches generator.

Unlike other generators that fetch from external sources, manual patches
are hand-curated JSON files committed directly to the repo. This generator
validates them and provides tooling for managing them.

The source of truth is the manual.json files in
schemas/patches/<resource>/manual.json — not a Python data structure.
"""

from __future__ import annotations

import json
import logging

from cfn_schemas.generators import register
from cfn_schemas.generators.base import BaseGenerator

logger = logging.getLogger(__name__)

VALID_OPS = {"add", "remove", "replace", "test"}


@register("manual")
class ManualPatchesGenerator(BaseGenerator):
    """Validate manual patch files.

    Manual patches are edited directly as JSON. This generator validates
    their structure rather than generating them from code.
    """

    def run(self) -> None:
        patches_dir = self.patches_dir
        count = 0
        errors = 0

        for manual_file in sorted(patches_dir.glob("*/manual.json")):
            try:
                patches = json.loads(manual_file.read_text())
                if not isinstance(patches, list):
                    logger.error("%s: root must be an array", manual_file)
                    errors += 1
                    continue
                for patch in patches:
                    if patch.get("op") not in VALID_OPS:
                        logger.error(
                            "%s: invalid op %r", manual_file, patch.get("op")
                        )
                        errors += 1
                    if "path" not in patch:
                        logger.error("%s: missing path", manual_file)
                        errors += 1
                count += 1
            except json.JSONDecodeError as e:
                logger.error("%s: invalid JSON: %s", manual_file, e)
                errors += 1

        if errors:
            raise ValueError(f"Manual patch validation failed with {errors} error(s)")

        logger.info("Validated %d manual patch files", count)
