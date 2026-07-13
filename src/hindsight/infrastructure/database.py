from typing import Any

import psycopg
from psycopg.rows import dict_row


def connect_database(database_url: str) -> Any:
    return psycopg.connect(
        database_url,
        autocommit=True,
        row_factory=dict_row,
        connect_timeout=5,
        application_name="hindsight",
    )
