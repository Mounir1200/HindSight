import json
from typing import Any

from hindsight.core.memory import MEMORY_EMBEDDING_DIMENSIONS, TextEmbedding

DEFAULT_EMBEDDING_MODEL_ID = "amazon.titan-embed-text-v2:0"
MAX_EMBEDDING_TEXT_CHARS = 50_000


class BedrockTitanTextEmbedder:
    def __init__(
        self,
        model_id: str = DEFAULT_EMBEDDING_MODEL_ID,
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

                client = boto3.client("bedrock-runtime", region_name=region_name)
            except Exception as error:
                raise EmbeddingProviderError(
                    "Bedrock embedding client initialization failed"
                ) from error

        self._model_id = model_id
        self._client = client

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dimensions(self) -> int:
        return MEMORY_EMBEDDING_DIMENSIONS

    def embed(self, text: str) -> TextEmbedding:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("embedding text cannot be empty")
        if len(text) > MAX_EMBEDDING_TEXT_CHARS:
            raise ValueError("embedding text exceeds the Bedrock input limit")

        try:
            response = self._client.invoke_model(
                modelId=self._model_id,
                contentType="application/json",
                accept="application/json",
                body=json.dumps(
                    {
                        "inputText": text,
                        "dimensions": MEMORY_EMBEDDING_DIMENSIONS,
                        "normalize": True,
                    }
                ),
            )
        except Exception as error:
            raise EmbeddingProviderError("Bedrock embedding request failed") from error
        return _parse_embedding(response, self._model_id)


def _parse_embedding(response: object, model_id: str) -> TextEmbedding:
    try:
        if not isinstance(response, dict):
            raise TypeError("response must be an object")
        body = response.get("body")
        if body is None or not callable(getattr(body, "read", None)):
            raise TypeError("response.body must be readable")
        raw = body.read()
        if isinstance(raw, bytes):
            raw = raw.decode()
        if not isinstance(raw, str):
            raise TypeError("response.body must contain JSON text")
        payload = json.loads(raw)
        values = payload["embedding"]
        input_tokens = payload["inputTextTokenCount"]
        if not isinstance(values, list) or any(
            isinstance(value, bool) or not isinstance(value, int | float) for value in values
        ):
            raise TypeError("embedding must be an array")
        if isinstance(input_tokens, bool) or not isinstance(input_tokens, int):
            raise TypeError("inputTextTokenCount must be an integer")
        return TextEmbedding(tuple(float(value) for value in values), model_id, input_tokens)
    except Exception as error:
        raise EmbeddingProviderError("Bedrock returned an invalid embedding") from error


class EmbeddingProviderError(RuntimeError):
    pass
