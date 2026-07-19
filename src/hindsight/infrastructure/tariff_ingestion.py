import time
from typing import Any

from hindsight.core.assertions.repository import CockroachAssertionRepository
from hindsight.infrastructure.sources import CockroachSourceRepository
from hindsight.ingestion.tariffs import TariffIngestionResult, TariffIngestionService


class CockroachTariffIngestion:
    def __init__(
        self,
        connection: Any,
        *,
        max_rows: int = 10_000,
        max_retries: int = 3,
        trust_level: str = "untrusted",
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        self._connection = connection
        self._max_rows = max_rows
        self._max_retries = max_retries
        self._trust_level = trust_level

    def ingest(
        self,
        payload: bytes,
        *,
        source_uri: str,
        metadata: dict[str, object] | None = None,
    ) -> TariffIngestionResult:
        for attempt in range(self._max_retries + 1):
            try:
                with self._connection.transaction():
                    service = TariffIngestionService(
                        CockroachSourceRepository(self._connection),
                        CockroachAssertionRepository(self._connection, max_retries=0),
                        max_rows=self._max_rows,
                        trust_level=self._trust_level,
                    )
                    return service.ingest(
                        payload,
                        source_uri=source_uri,
                        metadata=metadata,
                    )
            except Exception as error:
                if getattr(error, "sqlstate", None) != "40001" or attempt == self._max_retries:
                    raise
                time.sleep(0.05 * 2**attempt)
        raise RuntimeError("unreachable retry state")
