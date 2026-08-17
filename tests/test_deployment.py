import re
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
    assert "Name: HINDSIGHT_API_KEY" in template
    assert "Name: HINDSIGHT_RATE_LIMIT_POOL_MAX_SIZE" in template
    assert "Value: !Ref RateLimitPoolMaxSize" in template
    assert "Name: HINDSIGHT_RATE_LIMIT_SCALE" in template
    assert "Value: !Ref RateLimitScale" in template
    assert "Name: HINDSIGHT_PROVIDER_CONCURRENCY" in template
    assert "Value: !Ref ProviderConcurrency" in template
    assert "Name: HINDSIGHT_SERVER_LIMIT_CONCURRENCY" in template
    assert "Value: !Ref ServerLimitConcurrency" in template
    assert "Name: HINDSIGHT_SERVER_BACKLOG" in template
    assert "Value: !Ref ServerBacklog" in template
    assert "Name: HINDSIGHT_STARTUP_READINESS_CHECK" in template
    assert 'Value: "true"' in template
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
    assert "MinTaskCount: !Ref MinTaskCount" in template
    assert "MaxTaskCount: !Ref MaxTaskCount" in template
    assert "AutoScalingMetric: !Ref AutoScalingMetric" in template
    assert "AutoScalingTargetValue: !Ref AutoScalingTargetValue" in template
    assert "Default: REQUEST_COUNT_PER_TARGET" in template
    assert not (ROOT / "deploy" / "apprunner-service.yaml").exists()


def test_ecs_scaling_profiles_default_to_bounded_showcase_and_guard_production() -> None:
    template = (ROOT / "deploy" / "ecs-express-service.yaml").read_text(encoding="utf-8")

    profile = template[
        template.index("  DeploymentProfile:") : template.index("  ImageIdentifier:")
    ]
    assert "Default: showcase" in profile
    assert "AllowedValues: [showcase, production]" in profile

    min_tasks = template[template.index("  MinTaskCount:") : template.index("  MaxTaskCount:")]
    max_tasks = template[template.index("  MaxTaskCount:") : template.index("  VpcCidr:")]
    assert "Default: 1" in min_tasks
    assert "Default: 1" in max_tasks
    assert "ProductionAvailability:" in template
    assert "RuleCondition: !Equals [!Ref DeploymentProfile, production]" in template
    assert 'Assert: !Not [!Equals [!Ref MinTaskCount, "1"]]' in template
    assert 'Assert: !Not [!Equals [!Ref MaxTaskCount, "1"]]' in template
    assert "ShowcaseCapacity:" in template
    assert "Fn::Contains:" in template
    assert 'Assert: !Equals [!Ref DatabasePoolMinSize, "0"]' in template
    assert 'Assert: !Equals [!Ref DatabasePoolMaxSize, "5"]' in template
    assert 'Assert: !Equals [!Ref RateLimitPoolMaxSize, "5"]' in template
    assert 'AssertDescription: Showcase accepts only a reviewed rate scale of 1, 2, or 5' in (
        template
    )
    assert 'Assert: !Equals [!Ref ProviderConcurrency, "4"]' in template
    assert "ProductionAuthentication:" in template
    assert 'Assert: !Not [!Equals [!Ref ApplicationApiKeySecretArn, ""]]' in template
    assert 'Assert: !Equals [!Ref EnhancedObservability, "true"]' in template
    assert "Key: deployment-profile" in template

    rate_pool = template[
        template.index("  RateLimitPoolMaxSize:") : template.index("  RateLimitScale:")
    ]
    rate_scale = template[
        template.index("  RateLimitScale:") : template.index("  ProviderConcurrency:")
    ]
    provider_concurrency = template[
        template.index("  ProviderConcurrency:") : template.index("  EnhancedObservability:")
    ]
    server_concurrency = template[
        template.index("  ServerLimitConcurrency:") : template.index("  ServerBacklog:")
    ]
    server_backlog = template[template.index("  ServerBacklog:") : template.index("  VpcCidr:")]
    assert "AllowedValues: [1, 2, 5, 10, 20]" in rate_pool
    assert 'AllowedValues: ["0.1", "0.25", "0.5", "1", "2", "5", "10"]' in rate_scale
    assert "AllowedValues: [1, 2, 4, 8, 16, 32, 64]" in provider_concurrency
    assert "AllowedValues: [16, 32, 64, 128, 256, 512, 1024, 2048, 4096]" in server_concurrency
    assert "AllowedValues: [64, 128, 256, 512, 1024, 2048, 4096, 8192]" in server_backlog


def test_production_api_key_is_injected_from_a_least_privilege_secret() -> None:
    template = (ROOT / "deploy" / "ecs-express-service.yaml").read_text(encoding="utf-8")

    declaration = template[
        template.index("  ApplicationApiKeySecretArn:") : template.index("  SecretsKmsKeyArn:")
    ]
    assert 'Default: ""' in declaration
    assert "secretsmanager:" in declaration
    assert "UseApplicationAuth: !Not" in template
    assert "- !Ref ApplicationApiKeySecretArn" in template
    assert "Name: HINDSIGHT_API_KEY" in template
    assert "ValueFrom: !Ref ApplicationApiKeySecretArn" in template
    pattern = re.search(r'AllowedPattern: "([^"]+)"', declaration)
    assert pattern is not None
    compiled = re.compile(pattern.group(1))
    valid = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:hindsight/prod-AbCd12"
    assert compiled.fullmatch(valid)
    assert compiled.fullmatch(valid + "*") is None
    assert compiled.fullmatch(valid + "?") is None
    assert compiled.fullmatch(valid + ":json-key::") is None
    assert compiled.fullmatch(valid + "\t") is None


def test_ecs_dashboard_covers_traffic_latency_errors_and_capacity() -> None:
    template = (ROOT / "deploy" / "ecs-express-service.yaml").read_text(encoding="utf-8")

    assert "Type: AWS::CloudWatch::Dashboard" in template
    assert '"RequestCount"' in template
    assert '"HTTPCode_Target_4XX_Count"' in template
    assert '"HTTPCode_ELB_4XX_Count"' in template
    assert '"HTTPCode_Target_5XX_Count"' in template
    assert '"HTTPCode_ELB_5XX_Count"' in template
    assert '"TargetResponseTime"' in template
    assert '"stat": "p95"' in template
    assert '"stat": "p99"' in template
    assert '"CPUUtilization"' in template
    assert '"MemoryUtilization"' in template
    assert '"DesiredTaskCount"' in template
    assert '"RunningTaskCount"' in template
    assert '"HealthyHostCount"' in template
    assert '"UnHealthyHostCount"' in template
    assert "ECSManagedResourceArns.IngressPath.TargetGroupArns" in template
    assert "OperationsDashboardName:" in template
    assert "Condition: UseEnhancedObservability" in template
    assert "Value: !If [UseEnhancedObservability, enabled, disabled]" in template
    assert template.count("Condition: UseEnhancedObservability") >= 4


def test_ci_is_local_reproducible_and_does_not_receive_cloud_secrets() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    for command in (
        "uv sync --locked",
        "uv run ruff check .",
        "cfn-lint==1.53.1",
        "uv run pytest",
        "uv build",
    ):
        assert command in workflow
    assert "docker build --tag hindsight-web-ci" in workflow
    assert "docker run --detach --name hindsight-web-smoke" in workflow
    assert "http://127.0.0.1:8000/health" in workflow
    assert "docker rm --force hindsight-web-smoke" in workflow
    assert "docker build --file Dockerfile.lambda --tag hindsight-lambda-ci" in workflow
    assert "runs-on: ubuntu-24.04" in workflow
    assert "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1" in workflow
    assert "astral-sh/setup-uv@ae62891fec2bb8e7d6c99fc78c9fec3a63790f8d" in workflow
    assert "enable-cache: false" in workflow
    assert "persist-credentials: false" in workflow
    assert "--max-time 2" in workflow
    assert "aws " not in workflow
    assert "ccloud " not in workflow
    assert "secrets." not in workflow
    assert ".env" not in workflow


def test_deployment_runbook_examples_match_profile_rules() -> None:
    runbook = (ROOT / "deploy" / "README.md").read_text(encoding="utf-8")
    showcase = runbook[
        runbook.index("    EnvironmentName=hackathon") : runbook.index(
            "For an isolated production stack"
        )
    ]
    production = runbook[
        runbook.index("DeploymentProfile=production") : runbook.index("Add `AlertEmail` only")
    ]

    assert "AlertEmail=" not in showcase
    assert "MaxTaskCount=<approved-one-of-2-4-8-16-20>" in production
    assert "ApplicationApiKeySecretArn=" in production
    assert "EnhancedObservability=true" in production


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


def test_bedrock_arn_parameter_rejects_wildcards() -> None:
    template = (ROOT / "deploy" / "ecs-express-service.yaml").read_text(encoding="utf-8")
    declaration = template[template.index("  BedrockResourceArns:") :]
    pattern = re.search(r'AllowedPattern: "([^"]+)"', declaration)
    assert pattern is not None
    compiled = re.compile(pattern.group(1).replace("\\\\", "\\"))

    assert compiled.fullmatch(
        "arn:aws:bedrock:eu-central-1::foundation-model/amazon.nova-2-lite-v1:0"
    )
    assert compiled.fullmatch("") is not None
    assert compiled.fullmatch("arn:aws:bedrock:*::foundation-model/*") is None
    assert compiled.fullmatch("arn:aws:bedrock:eu-central-1::foundation-model/amazon.nova?") is None


def test_waf_rate_limits_every_state_changing_demo_route() -> None:
    template = (ROOT / "deploy" / "ecs-express-service.yaml").read_text(encoding="utf-8")

    per_ip = template[
        template.index("        - Name: expensive-routes-per-ip") : template.index(
            "        - Name: expensive-routes-global"
        )
    ]
    global_limit = template[
        template.index("        - Name: expensive-routes-global") : template.index(
            "        - Name: all-traffic-per-ip"
        )
    ]
    route_methods = {
        "/demo/seed": "POST",
        "/demo/reset": "POST",
        "/demo/prepare": "POST",
        "/memories/search": "GET",
    }
    for block in (per_ip, global_limit):
        for route, method in route_methods.items():
            assert re.search(
                rf"SearchString: {method}(?:\n.*){{1,12}}\n.*SearchString: {re.escape(route)}",
                block,
            )


def test_waf_logging_keeps_count_matches_without_enabling_sampling() -> None:
    template = (ROOT / "deploy" / "ecs-express-service.yaml").read_text(encoding="utf-8")

    assert "Action: COUNT" in template
    assert "Action: EXCLUDED_AS_COUNT" in template
    # Sampled requests bypass RedactedFields, so they stay off.
    assert "SampledRequestsEnabled: true" not in template


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
    assert "limit_concurrency=_environment_int(" in cli
    assert "backlog=_environment_int(" in cli
    assert "timeout_graceful_shutdown=_environment_int(" in cli
