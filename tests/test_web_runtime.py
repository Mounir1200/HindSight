import pytest

import hindsight.web.runtime as runtime_module
from hindsight.web.runtime import DemoRuntimeConfig


def test_runtime_integrations_are_off_by_default() -> None:
    config = DemoRuntimeConfig.from_environment(None, {})

    assert config.database_url is None
    assert config.bedrock_model_id is None
    assert config.vector_enabled is False
    assert config.mcp_cluster_id is None
    assert config.mcp_api_key is None


def test_runtime_maps_explicit_live_integrations() -> None:
    config = DemoRuntimeConfig.from_environment(
        "postgresql://runtime",
        {
            "HINDSIGHT_DEMO_BEDROCK": "true",
            "HINDSIGHT_DEMO_VECTOR": "true",
            "HINDSIGHT_DEMO_MCP": "true",
            "BEDROCK_MODEL_ID": "test-model",
            "BEDROCK_EMBEDDING_MODEL_ID": "test-embedding-model",
            "AWS_REGION": "eu-central-1",
            "COCKROACH_MCP_CLUSTER_ID": "cluster-id",
            "COCKROACH_MCP_API_KEY": "secret",
        },
    )

    assert config.bedrock_model_id == "test-model"
    assert config.vector_enabled is True
    assert config.embedding_model_id == "test-embedding-model"
    assert config.aws_region == "eu-central-1"
    assert config.mcp_cluster_id == "cluster-id"
    assert config.mcp_api_key == "secret"


def test_runtime_passes_the_shared_database_checkout_to_the_demo(monkeypatch) -> None:
    checkout = object()
    received: dict[str, object] = {}

    def execute_demo(database_url: str | None, **options: object) -> dict[str, object]:
        received["database_url"] = database_url
        received.update(options)
        return {"status": "ok"}

    monkeypatch.setattr(runtime_module, "execute_demo", execute_demo)
    config = DemoRuntimeConfig.from_environment("postgresql://runtime", {})

    result = config.runner(checkout)()

    assert result == {"status": "ok"}
    assert received["database_url"] == "postgresql://runtime"
    assert received["connection_context_factory"] is checkout


@pytest.mark.parametrize(
    ("environment", "message"),
    [
        ({"HINDSIGHT_DEMO_VECTOR": "true"}, "require DATABASE_URL"),
        ({"HINDSIGHT_DEMO_MCP": "true"}, "requires HINDSIGHT_DEMO_BEDROCK"),
        (
            {"HINDSIGHT_DEMO_BEDROCK": "true"},
            "requires BEDROCK_MODEL_ID",
        ),
        ({"HINDSIGHT_DEMO_VECTOR": "sometimes"}, "must be true or false"),
    ],
)
def test_runtime_rejects_incomplete_or_ambiguous_configuration(
    environment: dict[str, str],
    message: str,
) -> None:
    database_url = environment.get("DATABASE_URL")
    if "HINDSIGHT_DEMO_BEDROCK" in environment:
        database_url = "postgresql://runtime"

    with pytest.raises(ValueError, match=message):
        DemoRuntimeConfig.from_environment(database_url, environment)
