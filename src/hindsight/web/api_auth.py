from __future__ import annotations

import hashlib
import hmac
import os
from collections.abc import Mapping
from dataclasses import dataclass, field

_MIN_API_KEY_BYTES = 32
_MAX_API_KEY_BYTES = 1_024
_BEARER_PREFIX = "Bearer "


@dataclass(frozen=True, slots=True)
class ApiKeyAuthenticator:
    """Validate an optional deployment-wide API key without retaining it in clear text."""

    enabled: bool
    _key_digest: bytes = field(default=b"", repr=False)

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> ApiKeyAuthenticator:
        values = environment if environment is not None else os.environ
        raw_key = values.get("HINDSIGHT_API_KEY")
        if raw_key is None:
            return cls(enabled=False)

        key = raw_key.encode("utf-8")
        if not _MIN_API_KEY_BYTES <= len(key) <= _MAX_API_KEY_BYTES:
            raise ValueError("HINDSIGHT_API_KEY must contain between 32 and 1024 UTF-8 bytes")
        if _contains_whitespace_or_control(raw_key):
            raise ValueError("HINDSIGHT_API_KEY cannot contain whitespace or control characters")

        return cls(enabled=True, _key_digest=hashlib.sha256(key).digest())

    def authorizes(self, authorization_header: str | None) -> bool:
        """Return whether a request satisfies this deployment's authentication policy."""

        if not self.enabled:
            return True
        if authorization_header is None or not authorization_header.startswith(_BEARER_PREFIX):
            return False

        candidate = authorization_header[len(_BEARER_PREFIX) :]
        candidate_bytes = candidate.encode("utf-8")
        if (
            not candidate
            or len(candidate_bytes) > _MAX_API_KEY_BYTES
            or _contains_whitespace_or_control(candidate)
        ):
            return False

        candidate_digest = hashlib.sha256(candidate_bytes).digest()
        return hmac.compare_digest(candidate_digest, self._key_digest)


def _contains_whitespace_or_control(value: str) -> bool:
    return any(
        character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F
        for character in value
    )
