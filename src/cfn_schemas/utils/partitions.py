"""Make ARN patterns in schemas valid in every AWS partition.

A region without a fetchable registry gets the us-east-1 schemas, and the
cfn-lint output uses one schema per type for every region, so an ARN pattern
has to accept every partition.
"""

from __future__ import annotations

import re
from typing import Any

# Every AWS partition name, as the AWS SDKs list them.
_PARTITIONS = (
    "aws", "aws-cn", "aws-us-gov", "aws-iso", "aws-iso-b", "aws-iso-e",
    "aws-iso-f", "aws-eusc",
)

# Matches every AWS partition name.
_ANY_PARTITION = r"aws[a-z\-]*"

# Patterns with a lookaround or a backreference are returned unchanged: adding
# an alternative inside a lookaround inverts what it tests.
_SKIP_RE = re.compile(r"\(\?<?[=!]|\\[1-9]")


def widen_pattern(pattern: str) -> str:
    """Return ``pattern`` with the partition after each ``arn:`` valid in all partitions.

    A partition that does not match every name in ``_PARTITIONS`` gets
    ``_ANY_PARTITION`` as an alternative, so the result matches at least what
    ``pattern`` matches.
    """
    if _SKIP_RE.search(pattern):
        return pattern
    for start, end in reversed(_arn_partition_spans(pattern)):
        segment = pattern[start:end]
        if not _rejects_a_partition(segment):
            continue
        if segment == "aws":
            replacement = _ANY_PARTITION
        elif segment.startswith("(") and _closing_paren(segment) == len(segment) - 1:
            replacement = f"{segment[:-1]}|{_ANY_PARTITION})"
        else:
            replacement = f"(?:{segment}|{_ANY_PARTITION})"
        pattern = pattern[:start] + replacement + pattern[end:]
    return pattern


def widen_partition_patterns(spec: Any) -> Any:
    """Apply ``widen_pattern`` to every ``pattern`` keyword in a schema."""
    if isinstance(spec, dict):
        for key, value in spec.items():
            if key == "pattern" and isinstance(value, str):
                spec[key] = widen_pattern(value)
            else:
                widen_partition_patterns(value)
    elif isinstance(spec, list):
        for item in spec:
            widen_partition_patterns(item)
    return spec


def _arn_partition_spans(pattern: str) -> list[tuple[int, int]]:
    """Return the (start, end) of the partition segment after each ``arn:``.

    The segment runs to the next ``:`` outside any group or character class, or
    to the end of the pattern. A segment that meets a top-level ``|`` or ``$``,
    or closes a group it did not open, is not returned, because rewriting it
    would change what the surrounding pattern means. An ``arn:`` inside a
    segment already returned is part of that segment.
    """
    spans: list[tuple[int, int]] = []
    scan_index = 0
    for match in re.finditer("arn:", pattern):
        while scan_index < match.start():
            if pattern[scan_index] == "\\":
                scan_index += 2
            elif pattern[scan_index] == "[":
                scan_index = _skip_class(pattern, scan_index)
            else:
                scan_index += 1
        if scan_index > match.start():
            continue
        start = end = match.end()
        if spans and start < spans[-1][1]:
            continue
        depth = 0
        while end < len(pattern):
            char = pattern[end]
            if char == "\\":
                end += 2
                continue
            if char == "[":
                end = _skip_class(pattern, end)
                continue
            if char == "(":
                depth += 1
            elif char == ")":
                if depth == 0:
                    break
                depth -= 1
            elif depth == 0 and char in "|$":
                break
            elif depth == 0 and char == ":":
                spans.append((start, end))
                break
            end += 1
        else:
            if end > start and depth == 0:
                spans.append((start, end))
    return spans


def _rejects_a_partition(segment: str) -> bool:
    """True when the segment accepts a known AWS partition, but not every partition.

    The check uses Python ``re``. A segment that only compiles as ECMA-262 is
    treated as accepting every partition and left alone.
    """
    try:
        regex = re.compile(f"^(?:{segment})$")
    except re.error:
        return False
    matches = [bool(regex.match(partition)) for partition in _PARTITIONS]
    return any(matches) and not all(matches)


def _skip_class(pattern: str, index: int) -> int:
    """Index just past the character class that opens at ``pattern[index]``."""
    index += 1
    if index < len(pattern) and pattern[index] == "^":
        index += 1
    if index < len(pattern) and pattern[index] == "]":
        index += 1
    while index < len(pattern) and pattern[index] != "]":
        index += 2 if pattern[index] == "\\" else 1
    return index + 1


def _closing_paren(segment: str) -> int:
    """Index of the parenthesis that closes the one at ``segment[0]``."""
    depth = 0
    index = 0
    while index < len(segment):
        char = segment[index]
        if char == "\\":
            index += 2
            continue
        if char == "[":
            index = _skip_class(segment, index)
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return -1
