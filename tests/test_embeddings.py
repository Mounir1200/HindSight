import json
from io import BytesIO
from typing import Any

from hindsight.infrastructure.embeddings import BedrockTitanTextEmbedder


class RecordingEmbeddingRuntime:
    def __init__(self) -> None:
        self.request: dict[str, Any] | None = None

    def invoke_model(self, **request: Any) -> dict[str, Any]:
        self.request = request
        return {
            "body": BytesIO(
                json.dumps(
                    {
                        "embedding": [0.0] * 1_024,
                        "inputTextTokenCount": 7,
                    }
                ).encode()
            )
        }


def test_titan_embedder_sends_normalized_fixed_dimension_request() -> None:
    runtime = RecordingEmbeddingRuntime()
    embedder = BedrockTitanTextEmbedder(client=runtime)

    embedding = embedder.embed("A delayed retroactive telecom tariff.")

    assert runtime.request is not None
    assert runtime.request["modelId"] == "amazon.titan-embed-text-v2:0"
    assert runtime.request["contentType"] == "application/json"
    assert runtime.request["accept"] == "application/json"
    assert json.loads(runtime.request["body"]) == {
        "inputText": "A delayed retroactive telecom tariff.",
        "dimensions": 1_024,
        "normalize": True,
    }
    assert embedding.model_id == embedder.model_id
    assert embedding.dimensions == 1_024
    assert embedding.input_tokens == 7
