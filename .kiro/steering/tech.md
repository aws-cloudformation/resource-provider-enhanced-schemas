# Patch model and custom keywords

## Where to make a change

Human edits land in one of two places:

- **`schemas/patches/extensions/<resource>/manual.json`** — a hand-authored RFC-6902 patch
  overlay for one resource type. This is the source of truth and may use custom keywords.
- **`src/cfn_schemas/generators/smithy.py` `_SKIP_RESOURCE_PATHS`** — a dict that tells the
  smithy generator *not to emit* a constraint for a given JSON path, when the Smithy model is
  wrong for CloudFormation (e.g. a shared shape whose limit is wrong, or a pattern that omits a
  valid form).

### manual.json overrides smithy automatically

On assembly the patch order is `format` < `lifecycle` < `manual` < `smithy`, but the smithy
generator **defers to manual**: `_write_all_patches` calls `_collect_manual_paths` /
`_enum_governed` and skips emitting any field that `manual.json` already governs. So to
override a single smithy-supplied constraint, add just that one field to `manual.json` — the
smithy generator will stop emitting it and your value wins. You do **not** need a skip-list
entry for a plain override.

Use `_SKIP_RESOURCE_PATHS` only to *remove* a smithy constraint with no replacement, or to
remove a whole property node so a `manual.json` can re-add a subset of its keywords. Note the
skip granularity is the **property**, not a single keyword: to drop only a `maximum` while
keeping the `minimum`, skip the whole property in the list and re-add the `minimum` in
`manual.json` (see the Synthetics `MemoryInMB` entry for the pattern).

## Custom keywords and how they translate

We ship two schema formats. The cfn-lint format keeps our custom keywords; the **standard**
format translates them to Draft-7 (`assembly.py`, `_TRANSLATORS`) for consumers that only do
Draft-7. The keywords:

| Keyword | Meaning | Standard translation |
|---------|---------|----------------------|
| `requiredXor` | exactly one of the listed properties is present | `oneOf` of presence schemas |
| `requiredOr` | at least one is present | `anyOf` of presence schemas |
| `dependentExcluded` | if prop present, listed others must be absent | `allOf[ anyOf[ not(prop), not(any excl) ] ]` |
| `enumCaseInsensitive` | enum, case-insensitive | anchored `(?i)` pattern |

A keyword with no faithful Draft-7 equivalent is dropped from the standard artifact rather than
left in looking enforced — everit (the EV-side validator) ignores unknown keywords silently, so
leaving one in would imply a constraint that enforces nothing.

### Empty arrays count as absent

"Presence" for the keywords above means **present and non-empty**. `_present(name)` is
`{"required":[name], "properties":{name:{"minItems":1}}}` — the `minItems` guard is a no-op for
scalars and makes an empty list `[]` read as absent. This matters because CloudFormation drops
empty lists before calling the service, so a schema that errored on `NetworkInterfaces: []`
beside `SubnetId` was a false positive. `dependentExcluded` is expressed as
`allOf[anyOf[not,not]]` rather than `dependencies`/`if`-`then`, because `dependencies` triggers
on key presence regardless of value (so it cannot express the empty-array guard) and `if`/`then`
is not permitted in these schemas.

## Validation and tests

- `cfn-schemas validate` — schema integrity.
- `cfn-schemas audit-patches` — flags patches whose JSON path no longer resolves (run this
  after a base-schema change may have moved a path out from under a patch).
- `pytest tests/` — unit tests (CI runs `ruff check src/ tests/` + `pytest tests/` on Python
  3.10 / 3.12 / 3.13). mypy is not in CI.
- `pytest tests/data/ --build-dir build/` — data tests over the *assembled* output; they need a
  fresh `generate` + `assemble` first. One of them (`test_no_manual_smithy_value_conflicts`)
  checks that a `manual.json` value does not conflict with a stale generated `smithy.json`, so
  regenerate smithy before running it.
