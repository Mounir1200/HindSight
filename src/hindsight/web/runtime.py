import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from hindsight.application import execute_demo
from hindsight.infrastructure.embeddings import DEFAULT_EMBEDDING_MODEL_ID

DemoRunner = Callable[[], dict[str, object]]
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off", ""})


@dataclass(frozen=True, slots=True)
class DemoRuntimeConfig:
    database_url: str | None
    bedrock_model_id: str | None
    vector_enabled: bool
    embedding_model_id: str
    aws_region: str | None
    mcp_cluster_id: str | None
    mcp_api_key: str | None

    @classmethod
    def from_environment(
        cls,
        database_url: str | None,
        environment: Mapping[str, str] | None = None,
    ) -> "DemoRuntimeConfig":
        values = environment if environment is not None else os.environ
        bedrock_enabled = _flag(values, "HINDSIGHT_DEMO_BEDROCK")
        vector_enabled = _flag(values, "HINDSIGHT_DEMO_VECTOR")
        mcp_enabled = _flag(values, "HINDSIGHT_DEMO_MCP")

        if mcp_enabled and not bedrock_enabled:
            raise ValueError("HINDSIGHT_DEMO_MCP requires HINDSIGHT_DEMO_BEDROCK")
        if (bedrock_enabled or vector_enabled or mcp_enabled) and not database_url:
            raise ValueError("live demo integrations require DATABASE_URL")

        model_id = _optional(values, "BEDROCK_MODEL_ID") if bedrock_enabled else None
        if bedrock_enabled and model_id is None:
            raise ValueError("HINDSIGHT_DEMO_BEDROCK requires BEDROCK_MODEL_ID")

        cluster_id = _optional(values, "COCKROACH_MCP_CLUSTER_ID") if mcp_enabled else None
        api_key = _optional(values, "COCKROACH_MCP_API_KEY") if mcp_enabled else None
        if mcp_enabled and (cluster_id is None or api_key is None):
            raise ValueError(
                "HINDSIGHT_DEMO_MCP requires COCKROACH_MCP_CLUSTER_ID and COCKROACH_MCP_API_KEY"
            )

        return cls(
            database_url=database_url,
            bedrock_model_id=model_id,
            vector_enabled=vector_enabled,
            embedding_model_id=(
                _optional(values, "BEDROCK_EMBEDDING_MODEL_ID") or DEFAULT_EMBEDDING_MODEL_ID
            ),
            aws_region=_optional(values, "AWS_REGION"),
            mcp_cluster_id=cluster_id,
            mcp_api_key=api_key,
        )

    def runner(self) -> DemoRunner:
        return lambda: execute_demo(
            self.database_url,
            bedrock_model_id=self.bedrock_model_id,
            vector_enabled=self.vector_enabled,
            embedding_model_id=self.embedding_model_id,
            aws_region=self.aws_region,
            mcp_cluster_id=self.mcp_cluster_id,
            mcp_api_key=self.mcp_api_key,
        )


def _flag(environment: Mapping[str, str], name: str) -> bool:
    value = environment.get(name, "").strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ValueError(f"{name} must be true or false")


def _optional(environment: Mapping[str, str], name: str) -> str | None:
    value = environment.get(name, "").strip()
    return value or None
