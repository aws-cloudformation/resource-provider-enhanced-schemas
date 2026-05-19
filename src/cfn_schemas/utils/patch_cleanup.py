"""Utility to clean redundant patches from generated patch files.

Called by generators after writing patches to remove entries
that the base schema has since adopted.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _resolve_path(schema: dict, path: str) -> tuple[Any, bool]:
    """Walk a JSON pointer path. Returns (value, found)."""
    parts = [p for p in path.strip("/").split("/") if p]
    current = schema
    for part in parts:
        if isinstance(current, dict):
            if part in current:
                current = current[part]
            elif "$ref" in current:
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


def clean_redundant_patches(
    patch_file: Path,
    schema: dict[str, Any],
) -> int:
    """Remove patches from a file where the base schema already has the value.

    Returns the number of patches removed.
    """
    if not patch_file.exists():
        return 0

    patches = json.loads(patch_file.read_text())
    if not isinstance(patches, list):
        return 0

    original_count = len(patches)
    cleaned = []

    for patch in patches:
        if patch.get("op") != "add":
            cleaned.append(patch)
            continue

        path = patch.get("path", "")
        value = patch.get("value")

        # Check if parent exists and key already has this value
        parts = path.strip("/").rsplit("/", 1)
        if len(parts) == 2:
            parent_path, key = "/" + parts[0], parts[1]
            parent, found = _resolve_path(schema, parent_path)
            is_redundant = (
                found and isinstance(parent, dict)
                and key in parent and parent[key] == value
            )
            if is_redundant:
                logger.debug(
                    "Removing redundant patch: %s in %s",
                    path, patch_file.name,
                )
                continue

        cleaned.append(patch)

    removed = original_count - len(cleaned)
    if removed > 0:
        if cleaned:
            content = json.dumps(
                cleaned, indent=1, separators=(",", ": "), sort_keys=True,
            )
            patch_file.write_text(content + "\n")
        else:
            patch_file.unlink()
            logger.info("Deleted empty patch file: %s", patch_file)

    return removed
