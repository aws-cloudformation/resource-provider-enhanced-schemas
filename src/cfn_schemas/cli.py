"""CLI entry point for cfn-schemas."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from cfn_schemas.assembly import assemble_all
from cfn_schemas.audit import audit_patches, format_report
from cfn_schemas.generators import GENERATORS
from cfn_schemas.generators.schemas import SchemasGenerator
from cfn_schemas.utils.patch_cleanup import clean_redundant_patches

logger = logging.getLogger(__name__)


def _get_schemas_dir() -> Path:
    """Return the schemas/ directory at the repo root."""
    return Path(__file__).parent.parent.parent / "schemas"


def cmd_generate(args: argparse.Namespace) -> int:
    schemas_dir = _get_schemas_dir()
    only = set(args.only.split(",")) if args.only else None

    for name, generator_cls in GENERATORS.items():
        if only and name not in only:
            continue
        logger.info(f"Running generator: {name}")
        generator = generator_cls(schemas_dir=schemas_dir)
        try:
            generator.run()
            logger.info(f"Generator {name} completed")
        except Exception:
            logger.exception(f"Generator {name} failed")
            if not args.keep_going:
                return 1

    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    schemas_dir = _get_schemas_dir()
    if not schemas_dir.exists():
        logger.error(f"Schemas directory not found: {schemas_dir}")
        return 1

    providers_dir = schemas_dir / "providers"
    resources_dir = schemas_dir / "resources"

    errors = 0
    for provider_file in sorted(providers_dir.glob("*.json")):
        mappings = json.loads(provider_file.read_text())
        for resource_type, schema_hash in mappings.items():
            schema_file = resources_dir / f"{schema_hash}.json"
            if not schema_file.exists():
                logger.error(
                    f"{provider_file.name}: {resource_type} references "
                    f"missing schema {schema_hash}"
                )
                errors += 1

    if errors:
        logger.error(f"Validation failed with {errors} error(s)")
        return 1

    logger.info("Validation passed")
    return 0


def cmd_assemble(args: argparse.Namespace) -> int:
    return assemble_all(_get_schemas_dir(), Path(args.output), standard=args.standard)


def cmd_audit_patches(args: argparse.Namespace) -> int:
    result = audit_patches(_get_schemas_dir())
    print(format_report(result))
    if args.fail_on_broken and result.broken:
        return 1
    if args.fail_on_redundant and result.redundant:
        return 1
    return 0


def cmd_clean_patches(args: argparse.Namespace) -> int:
    schemas_dir = _get_schemas_dir()
    patches_dir = schemas_dir / "patches"
    providers_dir = schemas_dir / "providers"
    resources_dir = schemas_dir / "resources"

    type_to_hash: dict[str, str] = {}
    us_east = providers_dir / "us-east-1.json"
    if us_east.exists():
        type_to_hash = json.loads(us_east.read_text())

    total_removed = 0
    for patch_dir in sorted(patches_dir.iterdir()):
        if not patch_dir.is_dir():
            continue
        rt = None
        for t in type_to_hash:
            if t.replace("::", "_").lower() == patch_dir.name:
                rt = t
                break
        if not rt:
            continue
        schema_file = resources_dir / f"{type_to_hash[rt]}.json"
        if not schema_file.exists():
            continue
        schema = json.loads(schema_file.read_text())
        for patch_file in sorted(patch_dir.glob("*.json")):
            removed = clean_redundant_patches(patch_file, schema)
            if removed:
                logger.info(
                    "Cleaned %d redundant patches from %s/%s",
                    removed, patch_dir.name, patch_file.name,
                )
                total_removed += removed

    print(f"Removed {total_removed} redundant patches")
    return 0


def cmd_finalize(args: argparse.Namespace) -> int:
    logger.warning(
        "'finalize' is deprecated. Use 'assemble --output build/' instead."
    )
    g = SchemasGenerator(schemas_dir=_get_schemas_dir())
    g._apply_extension_patches()
    return 0


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(
        prog="cfn-schemas",
        description="Generate and manage enhanced CloudFormation schemas",
    )
    sub = parser.add_subparsers(dest="command")

    gen = sub.add_parser("generate", help="Run schema generators")
    gen.add_argument(
        "--only",
        help="Comma-separated list of generators to run",
    )
    gen.add_argument(
        "--keep-going",
        action="store_true",
        help="Continue running other generators if one fails",
    )

    sub.add_parser("validate", help="Validate schema integrity")

    assemble = sub.add_parser(
        "assemble", help="Build merged schemas from base + patches",
    )
    assemble.add_argument(
        "--output",
        default="build",
        help="Output directory for assembled schemas (default: build)",
    )
    assemble.add_argument(
        "--standard",
        action="store_true",
        help="Translate custom keywords into standard JSON Schema",
    )

    audit = sub.add_parser(
        "audit-patches",
        help="Find broken, redundant, or stale patches",
    )
    audit.add_argument(
        "--fail-on-broken",
        action="store_true",
        help="Exit with error code if broken patches are found",
    )
    audit.add_argument(
        "--fail-on-redundant",
        action="store_true",
        help="Exit with error code if redundant patches are found",
    )

    sub.add_parser(
        "clean-patches",
        help="Remove redundant patches from all patch files",
    )
    sub.add_parser(
        "finalize",
        help="Apply extension patches into resource schemas",
    )

    commands = {
        "generate": cmd_generate,
        "validate": cmd_validate,
        "assemble": cmd_assemble,
        "audit-patches": cmd_audit_patches,
        "clean-patches": cmd_clean_patches,
        "finalize": cmd_finalize,
    }

    args = parser.parse_args()
    if args.command in commands:
        sys.exit(commands[args.command](args))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
