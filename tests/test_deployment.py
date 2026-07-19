from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_container_uses_locked_dependencies_and_explicit_inputs() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "FROM python:3.12-slim" in dockerfile
    assert "uv sync --locked --no-dev --no-editable" in dockerfile
    assert "COPY . " not in dockerfile
    assert "COPY .\n" not in dockerfile
    assert "COPY .env" not in dockerfile
    assert "DATABASE_URL=" not in dockerfile
    assert "COCKROACH_MCP_API_KEY=" not in dockerfile
    assert "USER hindsight" in dockerfile


def test_container_exposes_the_runtime_contract_and_health_probe() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "HOST=0.0.0.0" in dockerfile
    assert "PORT=8000" in dockerfile
    assert "EXPOSE 8000" in dockerfile
    assert 'os.environ.get(\\"PORT\\", \\"8000\\")' in dockerfile
    assert "/health" in dockerfile
    assert 'CMD ["hindsight", "serve"]' in dockerfile


def test_build_context_excludes_local_credentials_and_workspace_state() -> None:
    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

    assert ".env" in ignored
    assert ".env.*" in ignored
    assert ".git" in ignored
    assert ".venv" in ignored
    assert "cahier_des_charges_hindsight.md" in ignored


def test_app_runner_uses_the_image_health_check_and_runtime_secret() -> None:
    template = (ROOT / "deploy" / "apprunner-service.yaml").read_text(encoding="utf-8")

    assert "ImageRepositoryType: ECR" in template
    assert "Path: /health" in template
    assert "RuntimeEnvironmentSecrets:" in template
    assert "Name: DATABASE_URL" in template
    assert "Name: COCKROACH_MCP_API_KEY" in template
    assert "Name: HINDSIGHT_DEMO_RESET_TOKEN" in template
    assert "HINDSIGHT_DEMO_BEDROCK" in template
    assert "HINDSIGHT_DEMO_VECTOR" in template
    assert "HINDSIGHT_DEMO_MCP" in template
    assert "MIGRATION_DATABASE_URL" not in template
    assert "MaxSize:\n    Type: Number\n    Default: 1" in template


def test_server_replaces_duplicate_access_logs_with_structured_requests() -> None:
    cli = (ROOT / "src" / "hindsight" / "cli.py").read_text(encoding="utf-8")

    assert 'log_config["loggers"]["hindsight.web"]' in cli
    assert '"format": "%(message)s"' in cli
    assert "access_log=False" in cli
