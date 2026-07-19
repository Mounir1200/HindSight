from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import unquote_plus


class ObjectIngestion[ResultT](Protocol):
    def ingest(
        self,
        payload: bytes,
        *,
        source_uri: str,
        metadata: dict[str, object] | None = None,
    ) -> ResultT: ...


@dataclass(frozen=True, slots=True)
class S3ObjectReference:
    bucket: str
    key: str
    version_id: str | None
    event_name: str
    event_time: str | None
    etag: str | None

    @property
    def uri(self) -> str:
        return f"s3://{self.bucket}/{self.key}"


def ingest_s3_event[ResultT](
    event: dict[str, object],
    *,
    s3_client: Any,
    ingestion: ObjectIngestion[ResultT],
    max_bytes: int = 2_000_000,
    object_prefix: str = "tariffs/",
    object_label: str = "tariff",
) -> list[ResultT]:
    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    results: list[ResultT] = []
    for reference in _event_records(
        event,
        object_prefix=object_prefix,
        object_label=object_label,
    ):
        request: dict[str, str] = {"Bucket": reference.bucket, "Key": reference.key}
        if reference.version_id:
            request["VersionId"] = reference.version_id
        response = s3_client.get_object(**request)
        body = response["Body"]
        try:
            content_length = response.get("ContentLength")
            if content_length is not None and int(content_length) > max_bytes:
                raise ValueError(f"{reference.uri} exceeds the {max_bytes}-byte limit")
            payload = body.read(max_bytes + 1)
        finally:
            body.close()
        if len(payload) > max_bytes:
            raise ValueError(f"{reference.uri} exceeds the {max_bytes}-byte limit")
        results.append(
            ingestion.ingest(
                payload,
                source_uri=reference.uri,
                metadata={
                    "bucket": reference.bucket,
                    "key": reference.key,
                    "version_id": reference.version_id,
                    "event_name": reference.event_name,
                    "event_time": reference.event_time,
                    "etag": reference.etag,
                },
            )
        )
    return results


def _event_records(
    event: dict[str, object],
    *,
    object_prefix: str,
    object_label: str,
) -> list[S3ObjectReference]:
    if not object_prefix or not object_label:
        raise ValueError("object prefix and label cannot be empty")
    records = event.get("Records")
    if not isinstance(records, list) or not records:
        raise ValueError("event must contain at least one S3 record")
    parsed: list[S3ObjectReference] = []
    for record in records:
        if not isinstance(record, dict) or record.get("eventSource") != "aws:s3":
            raise ValueError("event contains a non-S3 record")
        event_name = str(record.get("eventName", ""))
        if not event_name.startswith("ObjectCreated:"):
            raise ValueError("only S3 ObjectCreated events are supported")
        s3 = record.get("s3")
        if not isinstance(s3, dict):
            raise ValueError("S3 event record is missing object data")
        bucket_data = s3.get("bucket")
        object_data = s3.get("object")
        if not isinstance(bucket_data, dict) or not isinstance(object_data, dict):
            raise ValueError("S3 event record is missing bucket or object data")
        bucket = str(bucket_data.get("name", "")).strip()
        key = unquote_plus(str(object_data.get("key", ""))).strip()
        if not bucket or not key:
            raise ValueError("S3 event record has an empty bucket or key")
        if not key.startswith(object_prefix) or not key.casefold().endswith(".csv"):
            raise ValueError(f"{object_label} ingestion accepts {object_prefix}*.csv objects only")
        parsed.append(
            S3ObjectReference(
                bucket=bucket,
                key=key,
                version_id=_optional_string(object_data.get("versionId")),
                event_name=event_name,
                event_time=_optional_string(record.get("eventTime")),
                etag=_optional_string(object_data.get("eTag")),
            )
        )
    return parsed


def _optional_string(value: object) -> str | None:
    return str(value) if value is not None else None
