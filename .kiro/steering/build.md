# How the build works (automated and dynamic)

Nothing under `schemas/` except `schemas/patches/` and `schemas/manual/` is committed. The
schemas are **rebuilt from upstream every 2 hours** (`.github/workflows/update-schemas.yaml`,
cron `0 */2 * * *`, plus on push to `main`) and published to the `latest` release. Treat the
generated schemas as a build output, never as source.

## The pipeline

`cfn-schemas generate` runs these generators (CLI in `src/cfn_schemas/cli.py`, generators in
`src/cfn_schemas/generators/`):

| Generator | Pulls from | Produces |
|-----------|-----------|----------|
| `schemas` | CloudFormation registry schema zip (public URL) | base per-resource schemas |
| `smithy` | AWS API models (`api-models-aws` main.zip) + registry zip | enum / length / range / pattern patches |
| `format` | — | semantic formats from property names |
| `lifecycle` | — | shutdown/sunset annotations |
| `sam` | public SAM schema URL | `AWS::Serverless::*` schemas |

Then `cfn-schemas assemble` overlays the patches onto the base schemas and writes the two
published formats (cfn-lint content-addressed, and flat standard), plus `standard-patches`
(custom keywords translated to Draft-7).

## It is credential-less by design

Every generator fetches **public HTTPS URLs** (`SMITHY_URL`, `SCHEMA_URL` in `smithy.py`, the
SAM URL in `sam.py`) with `requests`/`urlopen`. There is **no boto3 and no AWS credential** in
the build. Do not add one lightly: it would make the hourly CI build depend on a role and
break the "anyone can build this from a clean checkout" property.

Consequence worth knowing: a constraint that can only be sourced by *calling a live AWS API at
build time* (for example, pulling a current enum from a `Describe*` call) cannot be auto-kept
fresh today. Such a value is captured in a manual patch and goes stale until someone refreshes
it. If you add one, say so in the patch comment.

## Generated files are git-ignored — never commit them

`.gitignore` excludes `schemas/resources/`, `schemas/providers/`, `schemas/metadata/`,
`schemas/raw/`, and the per-resource generated overlays
`schemas/patches/extensions/*/{smithy,format,lifecycle}.json`. Only `manual.json` under
`schemas/patches/extensions/<resource>/` is committed. If `git status` shows a generated file,
something is wrong — do not `git add` it.

## Reproduce a build locally

```bash
pip install -e ".[dev]"          # needs Python 3.10+
cfn-schemas generate --only smithy --keep-going   # regenerate after editing a patch/skip-list
cfn-schemas assemble --output build/cfnlint       # cfn-lint format
cfn-schemas assemble --standard --output build/standard
cfn-schemas audit-patches                          # find patches with broken paths
```

Regenerate (`--only smithy`) before running the data tests: the smithy generator's defer logic
(see tech steering) only takes effect in freshly generated output, so a stale
`schemas/patches/extensions/*/smithy.json` can make a data test disagree with your `manual.json`.
