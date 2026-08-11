"""Regression tests for Cognito schema enrichment."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

from cfn_schemas.assembly import assemble_schema
from cfn_schemas.generators.smithy import SmithyGenerator, _rename_service

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_USER_POOL_DOMAIN_PATCH = (
    _REPOSITORY_ROOT
    / "schemas"
    / "patches"
    / "extensions"
    / "aws_cognito_userpooldomain"
    / "manual.json"
)


@pytest.mark.parametrize(
    "smithy_service",
    ["cognito-identity", "cognito-identity-provider"],
)
def test_cognito_models_use_cognito_cloudformation_prefix(smithy_service):
    assert _rename_service(smithy_service) == "cognito"


def test_discovers_cognito_schema_without_unconditional_domain_constraints(tmp_path):
    models_dir = tmp_path / "models"
    _write_cognito_identity_provider_model(models_dir)

    provider_schemas_dir = tmp_path / "provider-schemas"
    _write_user_pool_domain_schema(provider_schemas_dir)

    generator = SmithyGenerator(tmp_path / "schemas")
    resource_patches = generator._build_all_patches(models_dir, provider_schemas_dir)

    user_pool_domain_patches = resource_patches["AWS::Cognito::UserPoolDomain"]
    assert "/properties/Domain" not in user_pool_domain_patches
    assert set(user_pool_domain_patches) == {"/properties/UserPoolId"}
    assert (
        user_pool_domain_patches["/properties/UserPoolId"].shape
        == "com.amazonaws.cognitoidentityprovider#UserPoolIdType"
    )


@pytest.mark.parametrize(
    "domain",
    ["", "-myprefix", "myprefix-", "---", "a" * 64],
)
def test_user_pool_domain_patch_rejects_invalid_prefix(domain):
    validator = _user_pool_domain_validator()

    assert not validator.is_valid({"Domain": domain})


@pytest.mark.parametrize(
    "domain",
    ["a", "valid-prefix-123", "a" * 63],
)
def test_user_pool_domain_patch_accepts_valid_prefix(domain):
    validator = _user_pool_domain_validator()

    assert validator.is_valid({"Domain": domain})


def test_user_pool_domain_patch_accepts_custom_fully_qualified_domain():
    validator = _user_pool_domain_validator()

    assert validator.is_valid(
        {
            "Domain": "auth.example.com",
            "CustomDomainConfig": {"CertificateArn": "test-certificate"},
        }
    )


def _write_cognito_identity_provider_model(models_dir: Path) -> None:
    smithy_service_dir = (
        models_dir / "cognito-identity-provider" / "service" / "2016-04-18"
    )
    smithy_service_dir.mkdir(parents=True)
    smithy_model = {
        "shapes": {
            "com.amazonaws.cognitoidentityprovider#CreateUserPoolDomain": {
                "type": "operation",
                "input": {
                    "target": (
                        "com.amazonaws.cognitoidentityprovider#"
                        "CreateUserPoolDomainRequest"
                    )
                },
            },
            "com.amazonaws.cognitoidentityprovider#CreateUserPoolDomainRequest": {
                "type": "structure",
                "members": {
                    "Domain": {
                        "target": "com.amazonaws.cognitoidentityprovider#DomainType"
                    },
                    "UserPoolId": {
                        "target": "com.amazonaws.cognitoidentityprovider#UserPoolIdType"
                    },
                },
            },
            "com.amazonaws.cognitoidentityprovider#DomainType": {
                "type": "string",
                "traits": {
                    "smithy.api#length": {"min": 1, "max": 63},
                    "smithy.api#pattern": (
                        r"^[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?$"
                    ),
                },
            },
            "com.amazonaws.cognitoidentityprovider#UserPoolIdType": {
                "type": "string",
                "traits": {"smithy.api#length": {"min": 1, "max": 55}},
            },
        }
    }
    smithy_model_file = (
        smithy_service_dir / "cognito-identity-provider-2016-04-18.json"
    )
    smithy_model_file.write_text(json.dumps(smithy_model))


def _write_user_pool_domain_schema(provider_schemas_dir: Path) -> None:
    provider_schemas_dir.mkdir()
    provider_schema = {
        "typeName": "AWS::Cognito::UserPoolDomain",
        "handlers": {
            "create": {"permissions": ["cognito-idp:CreateUserPoolDomain"]}
        },
        "properties": {
            "Domain": {"type": "string"},
            "UserPoolId": {"type": "string"},
        },
    }
    (provider_schemas_dir / "aws-cognito-userpooldomain.json").write_text(
        json.dumps(provider_schema)
    )


def _user_pool_domain_validator() -> Draft7Validator:
    base_schema = {
        "type": "object",
        "properties": {
            "Domain": {"type": "string"},
            "CustomDomainConfig": {"type": "object"},
        },
        "required": ["Domain"],
    }
    assembled_schema = assemble_schema(base_schema, [_USER_POOL_DOMAIN_PATCH])
    return Draft7Validator(assembled_schema)
