# Verifying a constraint change

Every change to a constraint is a claim about what the AWS service does. The standard for this
repo is that the claim is **validated against the live service**, not inferred from a document
or a model.

## Reading is not verifying

A constraint's presence in the AWS docs, the registry schema, the Smithy model, or an SDK model
tells you what *some artifact* says — not what the service enforces. These artifacts disagree
with the service often enough that any one of them, read alone, will mislead you:

- A doc's read-replica note claimed `BackupRetentionPeriod` and `PreferredBackupWindow` are
  inherited (ignored) on an RDS replica. A deploy showed they are **honored** — the doc was
  stale. Warning on them would have been a new false positive.
- The Smithy model capped Synthetics `MemoryInMB` at 3008; the registry schema and SDK model
  left it uncapped; the **service** enforces a different number again (10240). Only a deploy
  settled it.
- Glue `Table.TableType`'s two-value enum looked authoritative; the service accepts and persists
  *any* string.

So: use the artifacts to form a hypothesis and to find the boundary to test — then test it.

## What "validated" means

Deploy it, or call the create/modify API, in a real account, and read the result back:

1. **Test acceptance AND behavior.** "The stack reached `CREATE_COMPLETE`" is not enough when a
   property can be silently ignored. Read the created resource back and confirm the value was
   actually used. (A ScalingPolicy with two agreeing ID forms deploys fine; the real question
   was what happens when they *disagree* — which only a read-back or a conflicting-input test
   reveals.)
2. **Test the boundary, not a happy value.** For a max, show the boundary value is accepted and
   boundary+1 is rejected, and quote the service's own rejection message. For an enum removal,
   show a real-but-unlisted value is accepted and a bogus one is rejected (or, if the service
   accepts anything, that is itself the finding — see the open-set case in the product steering).
3. **Test the engine/shape that actually bites.** A constraint can hold for one engine and not
   another. An RDS replica password is accepted on a logical-replication engine and rejected on
   a physical one; testing only the permissive engine gives the wrong answer.
4. **Prefer a CloudFormation deploy** over a raw API call when the resource's CFN handler does
   work the raw API doesn't (property mapping, follow-up calls), because the handler is what the
   schema is validating for.

## Record the evidence, and admit the gaps

In the patch comment and the PR, state what you deployed and what the service returned (the
verbatim message is the strongest evidence). If you could not reach the live service for an
item, say so plainly and do not let it ride as "validated" — a change rebuilt from the AWS
model contracts instead of a deploy is weaker evidence and should be labeled as such, so a
reviewer can decide. An honest "not verified, blocked by X" is worth more than a confident
claim the next deploy disproves.

## Clean up what you create

Live verification creates real resources (and sometimes real cost). Delete everything you make,
and confirm the deletion — don't leave an orphan because a dependent teardown was slow. If a
resource can't be torn down in-session, record its identifier so it isn't lost.

## Sandbox / credentials note

This repo's *build* never needs credentials. Verification does, but it happens in a throwaway
test account, entirely outside the build. Never add that account, a role, or any credential to
the generator code or CI — keep verification a manual step a contributor runs, and keep the
build credential-less.
