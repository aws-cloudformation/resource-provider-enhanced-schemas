# CloudFormation Resource Provider Enhanced Schemas

Enhanced [AWS CloudFormation resource provider schemas](https://docs.aws.amazon.com/cloudformation-cli/latest/userguide/resource-type-schema.html) with additional validation constraints extracted from AWS APIs, Smithy models, and expert knowledge.

## Usage

Download the latest schemas from the [latest release](https://github.com/aws-cloudformation/resource-provider-enhanced-schemas/releases/tag/latest):

- **`schemas-cfn-lint.zip`** — Content-addressed schemas with per-region mappings and custom validation keywords
- **`schemas-standard.zip`** — Flat per-resource-type schemas with standard JSON Schema keywords

### cfn-lint format

```
providers/us-east-1.json    # { "AWS::EC2::Instance": "4be2f6b628bc540f", ... }
resources/4be2f6b628bc540f.json   # The actual schema
```

Load `providers/{region}.json`, look up the resource type hash, read `resources/{hash}.json`.

### Standard format

```
aws_ec2_instance.json
aws_s3_bucket.json
...
```

One file per resource type, all custom keywords translated to standard JSON Schema.

## What's enhanced

| Source | What it adds |
|--------|-------------|
| Smithy API models | Enum values, length/range constraints, regex patterns |
| Property name analysis | Semantic formats (VPC IDs, IAM Role ARNs, CIDR blocks, etc.) |
| Lifecycle status | Shutdown/sunset/maintenance annotations |
| Manual patches | Expert-curated corrections and constraints |
| SAM schema | AWS::Serverless::* resource type schemas |

## Repository structure

This repo contains only the generator code and human-authored patches. All schemas are built hourly from upstream sources and published as release artifacts.

```
src/cfn_schemas/         # Generator code
schemas/patches/         # Human-authored patches (flat: patches/{resource}/*.json)
schemas/manual/          # Synthetic schemas (e.g. AWS::CDK::Metadata)
tests/
```

## Development

```bash
pip install -e ".[dev]"

# Download schemas and run all generators (writes to schemas/ locally)
cfn-schemas generate

# Assemble cfn-lint format (providers/ + resources/)
cfn-schemas assemble --output build/cfnlint

# Assemble standard format (flat, translated keywords)
cfn-schemas assemble --standard --output build/standard

# Validate schema integrity
cfn-schemas validate

# Audit patches for broken paths
cfn-schemas audit-patches
```

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the LICENSE file.
