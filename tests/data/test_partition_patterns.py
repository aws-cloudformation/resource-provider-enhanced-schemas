"""Keep ARN partition handling invariant across all assembled authoring paths."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from cfn_schemas.utils.partitions import _SKIP_RE, _arn_partition_spans, widen_pattern

# Patterns the widener deliberately cannot inspect safely. Some already accept
# every partition; this list tracks the blind spot, not a claim they are narrow.
_EXEMPTIONS = set(
    json.loads(
        (Path(__file__).parents[1] / "fixtures" / "arn_partition_exemptions.json").read_text()
    )
)


def _patterns(node, path=""):
    if isinstance(node, dict):
        for key, value in node.items():
            child_path = f"{path}/{key}"
            if key == "pattern" and isinstance(value, str):
                yield child_path, value
            else:
                yield from _patterns(value, child_path)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _patterns(value, f"{path}/{index}")


def _has_uninspected_arn(pattern):
    arns = list(re.finditer("arn:", pattern))
    if not arns:
        return False
    if _SKIP_RE.search(pattern):
        return True
    spans = _arn_partition_spans(pattern)
    for start, end in spans:
        try:
            re.compile(f"^(?:{pattern[start:end]})$")
        except re.error:
            # The widener also leaves ECMA-262-only partition segments alone.
            return True
    return any(
        not any(start <= arn.end() < end for start, end in spans)
        for arn in arns
    )


def test_ecma_only_partition_is_uninspected():
    # This valid ECMA-262 named capture is narrow, but Python re cannot compile it.
    pattern = r"^arn:(?<partition>aws):iam::[0-9]{12}:role/.+$"
    assert widen_pattern(pattern) == pattern
    assert _has_uninspected_arn(pattern)


@pytest.mark.parametrize("output_format", ["cfnlint", "standard"])
def test_assembled_arn_partition_patterns(request, output_format):
    output_dir = request.getfixturevalue(f"{output_format}_dir")
    if output_format == "cfnlint":
        output_dir = output_dir / "resources"
    files = sorted(output_dir.glob("*.json"))
    assert files, f"No assembled schemas in {output_dir}"

    narrow = []
    uninspected = []
    for schema_file in files:
        schema = json.loads(schema_file.read_text())
        resource = schema.get("typeName", schema_file.stem)
        for path, pattern in _patterns(schema):
            location = f"{resource}{path}: {pattern!r}"
            if widen_pattern(pattern) != pattern:
                narrow.append(location)
            elif _has_uninspected_arn(pattern) and pattern not in _EXEMPTIONS:
                uninspected.append(location)

    assert not narrow, "ARN patterns still need partition widening:\n" + "\n".join(narrow)
    assert not uninspected, (
        "New ARN patterns cannot be inspected safely; review them before updating "
        "arn_partition_exemptions.json:\n" + "\n".join(uninspected)
    )
