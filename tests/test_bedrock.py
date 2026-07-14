from typing import Any

from hindsight.infrastructure.bedrock import BedrockConverseClient


class RecordingBedrockRuntime:
    def __init__(self) -> None:
        self.request: dict[str, Any] | None = None

    def converse(self, **request: Any) -> dict[str, Any]:
        self.request = request
        return {
            "output": {
                "message": {
                    "role": "assistant",
                    "content": [{"text": "Advisory explanation."}],
                }
            },
            "stopReason": "end_turn",
            "usage": {"inputTokens": 8, "outputTokens": 3, "totalTokens": 11},
            "ResponseMetadata": {"RequestId": "aws-request-1", "HTTPStatusCode": 200},
        }


def test_bedrock_adapter_sends_bounded_converse_request() -> None:
    runtime = RecordingBedrockRuntime()
    client = BedrockConverseClient("test-model", "eu-central-1", runtime)

    response = client.converse(
        system_prompt="Use the evidence tool.",
        messages=[{"role": "user", "content": [{"text": "Investigate."}]}],
        tool_config={"tools": []},
        request_metadata={"run_id": "run-1"},
    )

    assert runtime.request == {
        "modelId": "test-model",
        "system": [{"text": "Use the evidence tool."}],
        "messages": [{"role": "user", "content": [{"text": "Investigate."}]}],
        "toolConfig": {"tools": []},
        "inferenceConfig": {"temperature": 0, "maxTokens": 1_200},
        "requestMetadata": {"run_id": "run-1"},
    }
    assert response.request_id == "aws-request-1"
    assert response.stop_reason == "end_turn"
