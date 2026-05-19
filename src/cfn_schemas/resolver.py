"""JSON Schema $ref resolver.

Adapted from cfn-lint, which adapted it from the jsonschema library.
Original copyright (c) 2013 Julian Berman, MIT license.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any
from urllib.parse import unquote, urldefrag, urljoin


def id_of(schema: Any) -> str:
    if schema is True or schema is False:
        return ""
    return schema.get("$id", "")


@dataclass(frozen=True)
class RefResolutionError(Exception):
    cause: str = field(init=True)

    def __str__(self) -> str:
        return str(self.cause)


_SUBSCHEMAS_KEYWORDS = ("$id", "id", "$anchor", "$dynamicAnchor")


def _match_subschema_keywords(value: dict) -> Any:
    for keyword in _SUBSCHEMAS_KEYWORDS:
        if keyword in value:
            yield keyword, value


def _search_schema(schema: Any, matcher: Any) -> Any:
    values: deque = deque([schema])
    while values:
        value = values.pop()
        if not isinstance(value, dict):
            continue
        yield from matcher(value)
        values.extendleft(value.values())


def _match_keyword(keyword: str) -> Any:
    def matcher(value: dict) -> Any:
        if keyword in value:
            yield value

    return matcher


@dataclass
class RefResolver:
    referrer: Any = field(init=True)
    base_uri: str = field(init=True, default="")
    _scopes_stack: Any = field(init=False)
    _urljoin_cache: Any = field(init=True, default=None)
    _cache: Any = field(init=True, default=None)
    store: Any = field(init=True, default=None)

    def __post_init__(self) -> None:
        self._scopes_stack = [self.base_uri]
        if self._cache is None:
            self._cache = lru_cache(1024)(self.resolve_from_url)
        if self._urljoin_cache is None:
            self._urljoin_cache = lru_cache(1024)(urljoin)
        if self.store is None:
            self.store = {}

    @classmethod
    def from_schema(cls, schema: Any, **kwargs: Any) -> RefResolver:
        return cls(base_uri=id_of(schema), referrer=schema, **kwargs)

    def push_scope(self, scope: str) -> None:
        self._scopes_stack.append(
            self._urljoin_cache(self.resolution_scope, scope),
        )

    def pop_scope(self) -> None:
        try:
            self._scopes_stack.pop()
        except IndexError as e:
            raise RefResolutionError(
                "Failed to pop the scope from an empty stack."
            ) from e

    @property
    def resolution_scope(self) -> str:
        return self._scopes_stack[-1]

    def resolve(self, ref: str) -> tuple[str, Any]:
        url = self._urljoin_cache(self.resolution_scope, ref).rstrip("/")
        match = self._find_in_subschemas(url)
        if match is not None:
            return match
        return url, self._cache(url)

    def resolve_from_url(self, url: str) -> Any:
        url, fragment = urldefrag(url)
        if url:
            try:
                document = self.store[url]
            except KeyError as e:
                raise RefResolutionError(
                    f"Unresolvable URL: {url!r}"
                ) from e
        else:
            document = self.referrer
        return self.resolve_fragment(document, fragment)

    def resolve_fragment(self, document: Any, fragment: str) -> Any:
        fragment = fragment.lstrip("/")
        if not fragment:
            return document

        for keyword in ["id", "$id"]:
            for subschema in _search_schema(document, _match_keyword(keyword)):
                if "#" + fragment == subschema[keyword]:
                    return subschema

        parts = unquote(fragment).split("/") if fragment else []
        for part in parts:
            part = part.replace("~1", "/").replace("~0", "~")
            if isinstance(document, Sequence) and not isinstance(document, str):
                try:
                    part = int(part)  # type: ignore[assignment]
                except ValueError:
                    pass
            try:
                document = document[part]
            except (TypeError, LookupError) as e:
                raise RefResolutionError(
                    f"Unresolvable JSON pointer: {fragment!r}"
                ) from e
        return document

    def _get_subschemas_cache(self) -> dict[str, list]:
        cache: dict[str, list] = {key: [] for key in _SUBSCHEMAS_KEYWORDS}
        for keyword, subschema in _search_schema(
            self.referrer, _match_subschema_keywords
        ):
            cache[keyword].append(subschema)
        return cache

    def _find_in_subschemas(self, url: str) -> tuple[str, Any] | None:
        uri, fragment = urldefrag(url)
        subschemas = self._get_subschemas_cache()["$id"]
        if not subschemas:
            return None
        for subschema in subschemas:
            target_uri = self._urljoin_cache(
                self.resolution_scope, subschema["$id"]
            )
            if target_uri.rstrip("/") == uri.rstrip("/"):
                if fragment:
                    subschema = self.resolve_fragment(subschema, fragment)
                return url, subschema
        return None
