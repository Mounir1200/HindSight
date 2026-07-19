from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

MAX_OUTPUT_TOKENS = 1_200


@dataclass(frozen=True, slots=True)
class ConverseResponse:
    message: dict[str, Any]
    stop_reason: str
    usage: dict[str, int]
    request_id: str | None


class BedrockConverseClient:
    def __init__(
        self,
        model_id: str,
        region_name: str | None = None,
        client: Any | None = None,
    ) -> None:
        if not isinstance(model_id, str) or not model_id.strip():
            raise ValueError("model_id cannot be empty")
        if region_name is not None and (
            not isinstance(region_name, str) or not region_name.strip()
        ):
            raise ValueError("region_name cannot be empty")

        if client is None:
            try:
                import boto3
            except ImportError as error:
                raise RuntimeError(
                    "boto3 is required when no Bedrock Runtime client is injected"
                ) from error
            client = boto3.client("bedrock-runtime", region_name=region_name)

        self._model_id = model_id
        self._client = client

    @property
    def model_id(self) -> str:
        return self._model_id

    def converse(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tool_config: dict[str, Any] | None,
        request_metadata: dict[str, str],
        max_output_tokens: int = MAX_OUTPUT_TOKENS,
    ) -> ConverseResponse:
        _validate_request(
            system_prompt,
            messages,
            tool_config,
            request_metadata,
            max_output_tokens,
        )
        request = {
            "modelId": self._model_id,
            "system": [{"text": system_prompt}],
            "messages": messages,
            "inferenceConfig": {"temperature": 0, "maxTokens": max_output_tokens},
            "requestMetadata": request_metadata,
        }
        if tool_config is not None:
            request["toolConfig"] = tool_config
        response = self._client.converse(**request)
        return _parse_response(response)


def _validate_request(
    system_prompt: str,
    messages: list[dict[str, Any]],
    tool_config: dict[str, Any] | None,
    request_metadata: dict[str, str],
    max_output_tokens: int,
) -> None:
    if not isinstance(system_prompt, str) or not system_prompt.strip():
        raise ValueError("system_prompt cannot be empty")
    if not isinstance(messages, list) or not all(isinstance(item, dict) for item in messages):
        raise TypeError("messages must be a list of dictionaries")
    if tool_config is not None and not isinstance(tool_config, dict):
        raise TypeError("tool_config must be a dictionary or None")
    if not isinstance(request_metadata, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in request_metadata.items()
    ):
        raise TypeError("request_metadata must map strings to strings")
    if (
        isinstance(max_output_tokens, bool)
        or not isinstance(max_output_tokens, int)
        or not 1 <= max_output_tokens <= MAX_OUTPUT_TOKENS
    ):
        raise ValueError("max_output_tokens is outside the allowed range")


def _parse_response(response: object) -> ConverseResponse:
    root = _mapping(response, "response")
    output = _mapping(root.get("output"), "response.output")
    message = _message(output.get("message"))

    stop_reason = root.get("stopReason")
    if not isinstance(stop_reason, str) or not stop_reason:
        raise ValueError("response.stopReason must be a non-empty string")

    usage = _usage(root.get("usage"))
    metadata_value = root.get("ResponseMetadata")
    request_id: str | None = None
    if metadata_value is not None:
        metadata = _mapping(metadata_value, "response.ResponseMetadata")
        request_id_value = metadata.get("RequestId")
        if request_id_value is not None:
            if not isinstance(request_id_value, str) or not request_id_value:
                raise ValueError("response.ResponseMetadata.RequestId must be a string")
            request_id = request_id_value

    return ConverseResponse(
        message=message,
        stop_reason=stop_reason,
        usage=usage,
        request_id=request_id,
    )


def _mapping(value: object, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{field_name} must be an object with string keys")
    return value


def _message(value: object) -> dict[str, Any]:
    message = _mapping(value, "response.output.message")
    role = message.get("role")
    content = message.get("content")
    if not isinstance(role, str) or not role:
        raise ValueError("response.output.message.role must be a non-empty string")
    if not isinstance(content, list) or not all(isinstance(item, Mapping) for item in content):
        raise ValueError("response.output.message.content must be a list of objects")
    return {
        "role": role,
        "content": [dict(item) for item in content],
    }


def _usage(value: object) -> dict[str, int]:
    usage = _mapping(value, "response.usage")
    required = {"inputTokens", "outputTokens", "totalTokens"}
    if not required.issubset(usage):
        raise ValueError("response.usage is missing required token counts")

    sanitized: dict[str, int] = {}
    for key, count in usage.items():
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError(f"response.usage.{key} must be a non-negative integer")
        sanitized[key] = count
    return sanitized
