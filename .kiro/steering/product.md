# What this repository is

This repo produces **enhanced CloudFormation resource provider schemas**: the base AWS
registry schemas plus extra validation constraints drawn from AWS API (Smithy) models,
property-name analysis, lifecycle status, the SAM schema, and human-authored patches. The
build runs automatically and publishes the result as release artifacts; consumers (cfn-lint
and others) download those artifacts.

The repo holds only two things a human edits: **generator code** (`src/cfn_schemas/`) and
**human-authored patches** (`schemas/patches/`, `schemas/manual/`). Everything else under
`schemas/` is generated and git-ignored.

## The scope boundary: this repo expresses schema constraints only

A constraint here becomes, in the consumer, a **hard validation error** on a property value
or on the presence/combination of properties — something a JSON Schema keyword (or one of our
custom keywords) can express about a single resource in isolation. That is the whole surface
area of this repo.

**If a check cannot be expressed as such a constraint, it does not belong here.** In
particular, these belong in cfn-lint (as a rule), not in this repo:

- **Warnings / advisory signals.** Our keywords only produce errors. A value the service
  *accepts but ignores or that is merely discouraged* should warn, not error — so it is a
  cfn-lint rule. Example: an RDS read replica silently inherits `StorageEncrypted` /
  `MasterUsername` / `DBName` from its source; setting them is not a failure, so the schema
  must not error — a cfn-lint warning tells the user the value is ignored.
- **Cross-field consistency that needs to resolve a reference.** A JSON Schema keyword cannot
  compare one property against the resolved value of another (or against a `Ref` target).
  Example: ApplicationAutoScaling `ScalingPolicy` accepts both `ScalingTargetId` and the
  `ResourceId`/`ScalableDimension`/`ServiceNamespace` trio **when they agree**, and fails only
  when they disagree — "must agree" is a cfn-lint rule, not a `oneOf`.
- **Typo-catching on an open-ended field.** When the service accepts any string (no closed
  set it enforces) but there is a known meaningful set, an enum here would hard-error valid
  values the service accepts. Drop the enum and let cfn-lint warn on an unrecognized value.
  Example: Glue `Table.TableType` — the service persists any string, so the two-value enum
  was a false positive; the known-set check moved to a cfn-lint warning.

When you remove something from this repo for one of these reasons, file the corresponding
cfn-lint issue so the signal is preserved at the right severity, and link it from the PR.

## Why "false positive" is the thing to minimize

A false positive — the schema erroring on something CloudFormation actually deploys — trains
users to ignore the linter. Removing a false positive is the primary kind of change here. But
the opposite failure matters too: **removing a constraint that is real** means the consumer
stops catching a genuine deploy error (a false negative / linter regression). Relax a
constraint only where the service genuinely imposes no such limit — not because enforcing it
is inconvenient, and not because one engine or one input shape happened not to hit it.
