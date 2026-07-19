import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

MAX_ADVISORY_CHARS = 600
MAX_ADVISORY_TOKENS = 192
_PURPOSES = {"billing_explanation", "remediation_summary"}

SYSTEM_PROMPT = """You are a bounded HindSight advisory writer.
Use only the supplied deterministic facts. Never calculate or alter amounts, rates, verdicts,
or remediation outcomes. You have no tools and no authority to write data. Return plain English
in at most 80 words. Describe model text as advisory, not as a source of truth."""


@dataclass(frozen=True, slots=True)
class AdvisoryResponse:
    text: str
    usage: dict[str, int]
    request_id: str | None = None


@dataclass(frozen=True, slots=True)
class AdvisoryResult:
    text: str
    status: str
    usage: dict[str, int]
    request_id: str | None = None
    error_code: str | None = None


class AdvisoryClient(Protocol):
    provider: str

    @property
    def model_id(self) -> str: ...

    def generate(
        self,
        *,
        purpose: str,
        facts: Mapping[str, object],
        request_metadata: Mapping[str, str],
    ) -> AdvisoryResponse: ...


class _ConverseClient(Protocol):
    @property
    def model_id(self) -> str: ...

    def converse(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tool_config: dict[str, Any] | None,
        request_metadata: dict[str, str],
        max_output_tokens: int,
    ) -> Any: ...


class BedrockAdvisoryClient:
    provider = "amazon_bedrock"

    def __init__(self, client: _ConverseClient) -> None:
        self._client = client

    @property
    def model_id(self) -> str:
        return self._client.model_id

    def generate(
        self,
        *,
        purpose: str,
        facts: Mapping[str, object],
        request_metadata: Mapping[str, str],
    ) -> AdvisoryResponse:
        if purpose not in _PURPOSES:
            raise ValueError("unsupported advisory purpose")
        response = self._client.converse(
            system_prompt=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "text": json.dumps(
                                {"purpose": purpose, "facts": dict(facts)},
                                sort_keys=True,
                                separators=(",", ":"),
                            )
                        }
                    ],
                }
            ],
            tool_config=None,
            request_metadata=dict(request_metadata),
            max_output_tokens=MAX_ADVISORY_TOKENS,
        )
        if response.stop_reason != "end_turn":
            raise ValueError("Bedrock advisory did not complete")
        return AdvisoryResponse(
            text=_advisory_text(response.message),
            usage=dict(response.usage),
            request_id=response.request_id,
        )


def resolve_advisory(
    client: AdvisoryClient | None,
    *,
    purpose: str,
    facts: Mapping[str, object],
    request_metadata: Mapping[str, str],
    fallback: str,
) -> AdvisoryResult:
    if client is None:
        return AdvisoryResult(fallback, "not_requested", {})
    try:
        response = client.generate(
            purpose=purpose,
            facts=facts,
            request_metadata=request_metadata,
        )
        text = response.text.strip()
        if not text or len(text) > MAX_ADVISORY_CHARS:
            raise ValueError("advisory text is empty or exceeds its limit")
        return AdvisoryResult(text, "completed", response.usage, response.request_id)
    except Exception as error:
        code = (
            "invalid_advisory_response"
            if isinstance(error, TypeError | ValueError)
            else "advisory_provider_unavailable"
        )
        return AdvisoryResult(
            fallback,
            "unavailable",
            {},
            error_code=code,
        )


def _advisory_text(message: object) -> str:
    if not isinstance(message, Mapping) or message.get("role") != "assistant":
        raise ValueError("Bedrock advisory returned an invalid message")
    content = message.get("content")
    if not isinstance(content, list) or not content:
        raise ValueError("Bedrock advisory returned no content")
    if any(not isinstance(block, Mapping) or set(block) != {"text"} for block in content):
        raise ValueError("Bedrock advisory attempted unsupported output")
    text = "\n".join(str(block["text"]).strip() for block in content).strip()
    if not text or len(text) > MAX_ADVISORY_CHARS:
        raise ValueError("Bedrock advisory text is empty or exceeds its limit")
    return text
