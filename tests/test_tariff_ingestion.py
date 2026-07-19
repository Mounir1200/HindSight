import io
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from hindsight.core.assertions.repository import InMemoryAssertionRepository
from hindsight.infrastructure.sources import InMemorySourceRepository
from hindsight.infrastructure.tariff_ingestion import CockroachTariffIngestion
from hindsight.ingestion.s3 import ingest_s3_event
from hindsight.ingestion.tariffs import (
    TariffIngestionResult,
    TariffIngestionService,
    parse_tariff_csv,
)

TARIFF_CSV = b"""assertion_key,route,service_type,value,currency,unit,valid_from,recorded_at,source
FR-SN-VOICE,FR->SN,voice,0.25,EUR,minute,2026-01-01T00:00:00Z,2026-01-01T00:00:00Z,pricing-v1
FR-SN-VOICE,FR->SN,voice,0.15,EUR,minute,2026-07-01T00:00:00Z,2026-07-03T00:00:00Z,pricing-correction
"""
ROOT = Path(__file__).resolve().parents[1]


def _service() -> tuple[TariffIngestionService, InMemoryAssertionRepository]:
    assertions = InMemoryAssertionRepository()
    return (
        TariffIngestionService(
            InMemorySourceRepository(),
            assertions,
        ),
        assertions,
    )


def test_tariff_csv_ingestion_is_append_only_and_idempotent() -> None:
    service, assertions = _service()

    first = service.ingest(TARIFF_CSV, source_uri="s3://demo/tariffs/rates.csv")
    replay = service.ingest(TARIFF_CSV, source_uri="s3://demo/tariffs/rates.csv")

    history = assertions.history("FR-SN-VOICE")
    assert first.replayed is False
    assert replay.replayed is True
    assert replay.source_id == first.source_id
    assert first.assertions_processed == replay.assertions_processed == 2
    assert [item.version_number for item in history] == [1, 2]
    assert [item.value_number for item in history] == [Decimal("0.25"), Decimal("0.15")]
    assert all(str(item.source_id) == first.source_id for item in history)


def test_tariff_csv_rejects_invalid_input_before_ingestion() -> None:
    naive_timestamp = TARIFF_CSV.replace(b"2026-01-01T00:00:00Z", b"2026-01-01T00:00:00")
    with pytest.raises(ValueError, match="must include a timezone"):
        parse_tariff_csv(naive_timestamp)

    duplicate = TARIFF_CSV + TARIFF_CSV.splitlines(keepends=True)[1]
    with pytest.raises(ValueError, match="duplicate assertion_key and recorded_at"):
        parse_tariff_csv(duplicate)

    header, first, correction = TARIFF_CSV.splitlines(keepends=True)
    with pytest.raises(ValueError, match="versions must be ordered"):
        parse_tariff_csv(header + correction + first)


def test_deployable_tariff_fixture_uses_a_separate_ordered_lineage() -> None:
    rows = parse_tariff_csv((ROOT / "examples" / "tariffs" / "demo-rates.csv").read_bytes())

    assert len(rows) == 2
    assert {row.assertion_key for row in rows} == {"DE-KE-VOICE-S3"}
    assert rows[0].recorded_at < rows[1].recorded_at


def test_s3_event_reads_the_exact_object_version_and_closes_the_body() -> None:
    service, assertions = _service()
    s3 = _FakeS3(TARIFF_CSV)
    event = {
        "Records": [
            {
                "eventSource": "aws:s3",
                "eventName": "ObjectCreated:Put",
                "eventTime": "2026-07-19T10:00:00Z",
                "s3": {
                    "bucket": {"name": "hindsight-demo"},
                    "object": {
                        "key": "tariffs%2Fsummer+rates.csv",
                        "versionId": "version-1",
                        "eTag": "etag-1",
                    },
                },
            }
        ]
    }

    results = ingest_s3_event(event, s3_client=s3, ingestion=service)

    assert s3.calls == [
        {
            "Bucket": "hindsight-demo",
            "Key": "tariffs/summer rates.csv",
            "VersionId": "version-1",
        }
    ]
    assert s3.body.closed is True
    assert results[0].source_uri == "s3://hindsight-demo/tariffs/summer rates.csv"
    assert assertions.history("FR-SN-VOICE")[1].recorded_at == datetime(2026, 7, 3, tzinfo=UTC)


def test_cockroach_ingestion_retries_the_complete_transaction(monkeypatch) -> None:
    import hindsight.infrastructure.tariff_ingestion as module

    connection = _RetryConnection()
    attempts = 0

    class _Service:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def ingest(self, payload, *, source_uri, metadata=None):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise _SerializationFailure
            return TariffIngestionResult("source", source_uri, "sha256:test", 2, False)

    monkeypatch.setattr(module, "TariffIngestionService", _Service)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)

    result = CockroachTariffIngestion(connection).ingest(
        TARIFF_CSV,
        source_uri="s3://demo/tariffs/rates.csv",
    )

    assert result.assertions_processed == 2
    assert attempts == connection.transactions == 2


class _Body(io.BytesIO):
    pass


class _FakeS3:
    def __init__(self, payload: bytes) -> None:
        self.body = _Body(payload)
        self.calls: list[dict[str, str]] = []

    def get_object(self, **request: str) -> dict[str, object]:
        self.calls.append(request)
        return {"Body": self.body, "ContentLength": len(self.body.getvalue())}


class _Transaction:
    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None


class _RetryConnection:
    def __init__(self) -> None:
        self.transactions = 0

    def transaction(self) -> _Transaction:
        self.transactions += 1
        return _Transaction()


class _SerializationFailure(Exception):
    sqlstate = "40001"
