from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_cdr_stack_is_private_bounded_and_uses_the_cdr_handler() -> None:
    template = (ROOT / "deploy" / "cdr-ingestion.yaml").read_text(encoding="utf-8")

    assert "Type: AWS::S3::Bucket" in template
    assert "Type: AWS::Lambda::Function" in template
    assert "hindsight.lambdas.cdr_ingestion.handler" in template
    assert "ReservedConcurrentExecutions: 1" in template
    assert "DATABASE_SECRET_ARN: !Ref DatabaseSecretArn" in template
    assert "MAX_CDR_ROWS: !Ref MaxCdrRows" in template
    assert "CDR_TRUST_LEVEL: untrusted" in template
    assert "BlockPublicPolicy: true" in template
    assert "VersioningConfiguration:" in template
    assert "aws:SecureTransport: false" in template
    assert "Type: AWS::SQS::Queue" in template
    assert "MaximumRetryAttempts: 2" in template
    assert "MaximumEventAgeInSeconds: 3600" in template
    assert "Value: cdrs/" in template
    assert "Value: .csv" in template
