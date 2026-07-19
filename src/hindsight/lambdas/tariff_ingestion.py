import json
import os
from dataclasses import asdict
from typing import Any

import boto3

from hindsight.infrastructure.database import connect_database
from hindsight.infrastructure.tariff_ingestion import CockroachTariffIngestion
from hindsight.ingestion.s3 import ingest_s3_event
from hindsight.lambdas.runtime import database_url, positive_int


def handler(event: dict[str, object], context: Any) -> dict[str, object]:
    max_bytes = positive_int("MAX_INGESTION_BYTES", 2_000_000)
    max_rows = positive_int("MAX_TARIFF_ROWS", 10_000)
    with connect_database(database_url()) as connection:
        ingestion = CockroachTariffIngestion(
            connection,
            max_rows=max_rows,
            trust_level=os.getenv("TARIFF_TRUST_LEVEL", "untrusted"),
        )
        results = ingest_s3_event(
            event,
            s3_client=boto3.client("s3"),
            ingestion=ingestion,
            max_bytes=max_bytes,
        )
    response = {
        "status": "completed",
        "objects_processed": len(results),
        "objects": [asdict(result) for result in results],
    }
    print(
        json.dumps(
            {
                "event": "tariff_ingestion_complete",
                "request_id": getattr(context, "aws_request_id", None),
                "objects_processed": len(results),
                "replayed_objects": sum(result.replayed for result in results),
                "assertions_processed": sum(result.assertions_processed for result in results),
            }
        )
    )
    return response
