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


def test_ecr_bootstrap_retains_scanned_immutable_images() -> None:
    template = (ROOT / "deploy" / "ecr-bootstrap.yaml").read_text(encoding="utf-8")

    assert "Type: AWS::ECR::Repository" in template
    assert "ImageTagMutability: IMMUTABLE" in template
    assert "ScanOnPush: true" in template
    assert "DeletionPolicy: Retain" in template
    assert "Delete untagged images after seven days" in template


def test_ecs_express_uses_a_digest_health_check_and_runtime_secrets() -> None:
    template = (ROOT / "deploy" / "ecs-express-service.yaml").read_text(encoding="utf-8")

    assert "Type: AWS::ECS::ExpressGatewayService" in template
    assert 'AllowedPattern: "^\\\\S+@sha256:[0-9a-f]{64}$"' in template
    assert "HealthCheckPath: /health" in template
    assert "ContainerPort: 8000" in template
    assert "Secrets:" in template
    assert "Name: DATABASE_URL" in template
    assert "Name: COCKROACH_MCP_API_KEY" in template
    assert "Name: HINDSIGHT_DEMO_RESET_TOKEN" in template
    assert "Name: HINDSIGHT_RATE_LIMIT_HMAC_KEY" in template
    assert "Type: AWS::SecretsManager::Secret" in template
    assert "PasswordLength: 64" in template
    assert "HINDSIGHT_DEMO_BEDROCK" in template
    assert "HINDSIGHT_DEMO_VECTOR" in template
    assert "HINDSIGHT_DEMO_MCP" in template
    assert "HINDSIGHT_RATE_LIMIT_BACKEND" in template
    assert "Value: cockroach" in template
    assert "HINDSIGHT_RATE_LIMIT_TRUSTED_PROXY_CIDRS" in template
    assert "HINDSIGHT_RATE_LIMIT_TRUST_APP_RUNNER_XFF" in template
    assert "MIGRATION_DATABASE_URL" not in template
    assert "MinTaskCount: 1" in template
    assert "MaxTaskCount: 1" in template
    assert not (ROOT / "deploy" / "apprunner-service.yaml").exists()


def test_ecs_express_creates_separate_least_privilege_roles() -> None:
    template = (ROOT / "deploy" / "ecs-express-service.yaml").read_text(encoding="utf-8")

    assert template.count("Type: AWS::IAM::Role") == 3
    assert "AmazonECSTaskExecutionRolePolicy" in template
    assert "AmazonECSInfrastructureRoleforExpressGatewayServices" in template
    assert "Service: ecs.amazonaws.com" in template
    assert template.count("Service: ecs-tasks.amazonaws.com") == 2
    assert "secretsmanager:GetSecretValue" in template
    assert "bedrock:InvokeModel" in template
    assert "bedrock:GetInferenceProfile" in template
    assert 'Resource: !Split [",", !Ref BedrockResourceArns]' in template
    assert "AmazonBedrockFullAccess" not in template


def test_ecs_express_has_dedicated_network_and_layered_waf_rules() -> None:
    template = (ROOT / "deploy" / "ecs-express-service.yaml").read_text(encoding="utf-8")

    assert "Type: AWS::EC2::VPC" in template
    assert template.count("Type: AWS::EC2::Subnet\n") == 2
    assert "Type: AWS::WAFv2::WebACL" in template
    assert "Type: AWS::WAFv2::WebACLAssociation" in template
    assert "AWSManagedRulesAmazonIpReputationList" in template
    assert "AWSManagedRulesCommonRuleSet" in template
    assert "AWSManagedRulesKnownBadInputsRuleSet" in template
    assert "AggregateKeyType: IP" in template
    assert "AggregateKeyType: CONSTANT" in template
    assert "EvaluationWindowSec: 60" in template
    assert "SearchString: /demo/seed" in template
    assert "SearchString: /demo/reset" in template
    assert "SearchString: /memories/search" in template
    assert "SampledRequestsEnabled: false" in template
    assert (
        "ResourceArn: !GetAtt Service.ECSManagedResourceArns.IngressPath.LoadBalancerArn"
        in template
    )
    assert template.count("ResponseCode: 429") >= 3
    assert "Type: URL_DECODE" in template
    assert "Type: NORMALIZE_PATH" in template
    assert "Type: AWS::WAFv2::LoggingConfiguration" in template
    assert "DefaultBehavior: DROP" in template
    assert "Name: x-demo-reset-token" in template
    assert "Type: AWS::CloudWatch::Alarm" in template


def test_server_replaces_duplicate_access_logs_with_structured_requests() -> None:
    cli = (ROOT / "src" / "hindsight" / "cli.py").read_text(encoding="utf-8")

    assert 'log_config["loggers"]["hindsight.web"]' in cli
    assert '"format": "%(message)s"' in cli
    assert "access_log=False" in cli
    assert "proxy_headers=False" in cli
