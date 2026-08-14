import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_lambda_image_uses_locked_python_312_runtime() -> None:
    dockerfile = (ROOT / "Dockerfile.lambda").read_text(encoding="utf-8")

    assert "public.ecr.aws/lambda/python:3.12" in dockerfile
    assert "uv sync --locked --no-dev --no-editable" in dockerfile
    assert 'CMD ["hindsight.lambdas.tariff_ingestion.handler"]' in dockerfile


def test_tariff_stack_is_private_bounded_and_secret_backed() -> None:
    template = (ROOT / "deploy" / "tariff-ingestion.yaml").read_text(encoding="utf-8")

    assert "Type: AWS::S3::Bucket" in template
    assert "Type: AWS::Lambda::Function" in template
    assert "Event: s3:ObjectCreated:*" in template
    assert 'AllowedPattern: "^\\\\S+@sha256:[0-9a-f]{64}$"' in template
    assert "AllowedValues: [100000, 500000, 1000000, 2000000]" in template
    assert "AllowedValues: [100, 500, 1000, 5000, 10000]" in template
    assert "AllowedValues: [1, 2, 4, 8, 16]" in template
    assert "ReservedConcurrentExecutions: !Ref ReservedConcurrency" in template
    assert "FunctionName: hindsight-tariff-ingestion" not in template
    assert "DATABASE_SECRET_ARN: !Ref DatabaseSecretArn" in template
    assert "TARIFF_TRUST_LEVEL: untrusted" in template
    assert "BlockPublicPolicy: true" in template
    assert "VersioningConfiguration:" in template
    assert "aws:SecureTransport: false" in template
    assert "Type: AWS::SQS::Queue" in template
    assert "MaximumRetryAttempts: 2" in template
    assert "MaximumEventAgeInSeconds: 3600" in template
    assert "Value: tariffs/" in template
    assert "Value: .csv" in template

    declaration = template[
        template.index("  LambdaImageUri:") : template.index("  DatabaseSecretArn:")
    ]
    match = re.search(r'AllowedPattern: "([^"]+)"', declaration)
    assert match is not None
    image_pattern = re.compile(match.group(1).replace("\\\\", "\\"))
    immutable = "123456789012.dkr.ecr.eu-central-1.amazonaws.com/hindsight@sha256:" + "a" * 64
    assert image_pattern.fullmatch(immutable)
    mutable = "123456789012.dkr.ecr.eu-central-1.amazonaws.com/hindsight:latest"
    assert image_pattern.fullmatch(mutable) is None
