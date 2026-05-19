"""Generator base class and registry."""

from __future__ import annotations

from cfn_schemas.generators.base import BaseGenerator

# Registry populated as generators are imported
GENERATORS: dict[str, type[BaseGenerator]] = {}


def register(name: str):
    """Decorator to register a generator class."""

    def wrapper(cls: type[BaseGenerator]) -> type[BaseGenerator]:
        GENERATORS[name] = cls
        return cls

    return wrapper


def _load_generators() -> None:
    """Import all generator modules to trigger registration."""
    from cfn_schemas.generators import (
        format,  # noqa: F401
        lifecycle,  # noqa: F401
        manual,  # noqa: F401
        sam,  # noqa: F401
        schemas,  # noqa: F401
        smithy,  # noqa: F401
    )


_load_generators()

__all__ = ["GENERATORS", "BaseGenerator", "register"]
