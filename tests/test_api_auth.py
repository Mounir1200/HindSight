from __future__ import annotations

import hashlib

import pytest

import hindsight.web.api_auth as api_auth_module
from hindsight.web.api_auth import ApiKeyAuthenticator

API_KEY = "production-key-" + ("a" * 48)


def test_missing_configuration_disables_authentication_for_showcase() -> None:
    authenticator = ApiKeyAuthenticator.from_environment({})

    assert authenticator.enabled is False
    assert authenticator.authorizes(None) is True
    assert authenticator.authorizes("anything is ignored") is True


@pytest.mark.parametrize(
    "api_key",
    [
        "",
        "too-short",
        "a" * 1_025,
        ("a" * 31) + " ",
        ("a" * 31) + "\n",
    ],
)
def test_present_but_invalid_configuration_fails_closed(api_key: str) -> None:
    with pytest.raises(ValueError, match="HINDSIGHT_API_KEY") as error:
        ApiKeyAuthenticator.from_environment({"HINDSIGHT_API_KEY": api_key})

    if api_key:
        assert api_key not in str(error.value)


def test_exact_bearer_credential_is_accepted() -> None:
    authenticator = ApiKeyAuthenticator.from_environment({"HINDSIGHT_API_KEY": API_KEY})

    assert authenticator.enabled is True
    assert authenticator.authorizes(f"Bearer {API_KEY}") is True


@pytest.mark.parametrize(
    "authorization",
    [
        None,
        "",
        API_KEY,
        f"bearer {API_KEY}",
        f"BEARER {API_KEY}",
        f"Basic {API_KEY}",
        f"Bearer  {API_KEY}",
        f"Bearer {API_KEY} ",
        f" Bearer {API_KEY}",
        "Bearer ",
        "Bearer wrong-production-key-" + ("b" * 48),
        "Bearer clé-non-ascii-mais-suffisamment-longue-xxxxxxxxxxxxxxxx",
    ],
)
def test_malformed_or_wrong_authorization_is_rejected(authorization: str | None) -> None:
    authenticator = ApiKeyAuthenticator.from_environment({"HINDSIGHT_API_KEY": API_KEY})

    assert authenticator.authorizes(authorization) is False


def test_validly_formed_credentials_use_constant_time_digest_comparison(monkeypatch) -> None:
    comparisons: list[tuple[bytes, bytes]] = []

    def compare_digest(candidate: bytes, expected: bytes) -> bool:
        comparisons.append((candidate, expected))
        return False

    monkeypatch.setattr(api_auth_module.hmac, "compare_digest", compare_digest)
    authenticator = ApiKeyAuthenticator.from_environment({"HINDSIGHT_API_KEY": API_KEY})

    assert authenticator.authorizes("Bearer " + ("z" * 64)) is False
    assert comparisons == [
        (
            hashlib.sha256(("z" * 64).encode()).digest(),
            hashlib.sha256(API_KEY.encode()).digest(),
        )
    ]


def test_authenticator_representation_never_contains_the_secret() -> None:
    authenticator = ApiKeyAuthenticator.from_environment({"HINDSIGHT_API_KEY": API_KEY})

    assert API_KEY not in repr(authenticator)
    assert hashlib.sha256(API_KEY.encode()).hexdigest() not in repr(authenticator)
