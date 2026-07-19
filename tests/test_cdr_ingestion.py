import io
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hindsight.infrastructure.cdr_ingestion import CockroachCdrIngestion
from hindsight.infrastructure.sources import InMemorySourceRepository
from hindsight.ingestion.cdrs import (
    CdrConflictError,
    CdrIngestionResult,
    CdrIngestionService,
    InMemoryCdrRepository,
    parse_cdr_csv,
)
from hindsight.ingestion.s3 import ingest_s3_event

ROOT = Path(__file__).resolve().parents[1]
CDR_CSV = b"""external_id,msisdn_hash,route,service_type,started_at,duration_sec
CALL-001,da3d12d669f4657a318fbe5d77d3aba526b8f9e67756a6d4f734734689080a31,FR->SN,voice,2026-07-02T12:00:00Z,600
CALL-002,2e9040f3c7d6e10fe05670b4a1af1a3821046319b43599398d9f1d5fc9c9ac11,FR->SN,voice,2026-07-02T16:00:00Z,300
"""


def _service() -> tuple[CdrIngestionService, InMemoryCdrRepository]:
    cdrs = InMemoryCdrRepository()
    return CdrIngestionService(InMemorySourceRepository(), cdrs), cdrs


def test_cdr_ingestion_is_content_idempotent_and_records_provenance() -> None:
    service, cdrs = _service()

    first = service.ingest(CDR_CSV, source_uri="s3://demo/cdrs/calls.csv")
    replay = service.ingest(CDR_CSV, source_uri="s3://demo/cdrs/copy.csv")

    assert first.replayed is False
    assert replay.replayed is True
    assert replay.source_id == first.source_id
    assert first.checksum.startswith("sha256:")
    assert first.cdrs_processed == replay.cdrs_processed == 2
    assert len(cdrs.records()) == 2
    assert {str(item.source_id) for item in cdrs.records()} == {first.source_id}


def test_cdr_csv_rejects_unsupported_or_ambiguous_input() -> None:
    with pytest.raises(ValueError, match="only voice CDRs"):
        parse_cdr_csv(CDR_CSV.replace(b",voice,", b",data,"))
    with pytest.raises(ValueError, match="64 lowercase hex"):
        parse_cdr_csv(CDR_CSV.replace(b"da3d12d6", b"MSISDN42"))
    with pytest.raises(ValueError, match="must include a timezone"):
        parse_cdr_csv(CDR_CSV.replace(b"2026-07-02T12:00:00Z", b"2026-07-02T12:00:00"))
    duplicate = CDR_CSV + CDR_CSV.splitlines(keepends=True)[1]
    with pytest.raises(ValueError, match="duplicate external_id"):
        parse_cdr_csv(duplicate)
    with pytest.raises(ValueError, match="1-row limit"):
        parse_cdr_csv(CDR_CSV, max_rows=1)


def test_external_id_cannot_be_reused_for_different_content() -> None:
    service, _ = _service()
    service.ingest(CDR_CSV, source_uri="s3://demo/cdrs/calls.csv")
    changed = CDR_CSV.replace(b",600\n", b",601\n")

    with pytest.raises(CdrConflictError, match="already identifies another CDR"):
        service.ingest(changed, source_uri="s3://demo/cdrs/changed.csv")


def test_deployable_cdr_fixture_matches_the_supported_voice_model() -> None:
    rows = parse_cdr_csv((ROOT / "examples" / "cdrs" / "demo-cdrs.csv").read_bytes())

    assert len(rows) == 2
    assert {row.service_type for row in rows} == {"voice"}
    assert {row.route for row in rows} == {"DE->KE"}


def test_s3_event_reads_exact_cdr_object_version_and_enforces_prefix() -> None:
    service, cdrs = _service()
    s3 = _FakeS3(CDR_CSV)
    event = _s3_event("cdrs%2Fsummer+calls.csv")

    results = ingest_s3_event(
        event,
        s3_client=s3,
        ingestion=service,
        object_prefix="cdrs/",
        object_label="CDR",
    )

    assert s3.calls == [
        {"Bucket": "hindsight-demo", "Key": "cdrs/summer calls.csv", "VersionId": "v1"}
    ]
    assert s3.body.closed is True
    assert results[0].source_uri == "s3://hindsight-demo/cdrs/summer calls.csv"
    assert cdrs.records()[0].row.started_at == datetime(2026, 7, 2, 12, tzinfo=UTC)

    with pytest.raises(ValueError, match=r"accepts cdrs/\*\.csv"):
        ingest_s3_event(
            _s3_event("tariffs%2Frates.csv"),
            s3_client=_FakeS3(CDR_CSV),
            ingestion=service,
            object_prefix="cdrs/",
            object_label="CDR",
        )


def test_cockroach_cdr_ingestion_retries_the_complete_object_transaction(monkeypatch) -> None:
    import hindsight.infrastructure.cdr_ingestion as module

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
            return CdrIngestionResult("source", source_uri, "sha256:test", 2, False)

    monkeypatch.setattr(module, "CdrIngestionService", _Service)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)

    result = CockroachCdrIngestion(connection).ingest(
        CDR_CSV,
        source_uri="s3://demo/cdrs/calls.csv",
    )

    assert result.cdrs_processed == 2
    assert attempts == connection.transactions == 2


def _s3_event(key: str) -> dict[str, object]:
    return {
        "Records": [
            {
                "eventSource": "aws:s3",
                "eventName": "ObjectCreated:Put",
                "eventTime": "2026-07-19T10:00:00Z",
                "s3": {
                    "bucket": {"name": "hindsight-demo"},
                    "object": {"key": key, "versionId": "v1", "eTag": "etag-1"},
                },
            }
        ]
    }


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
