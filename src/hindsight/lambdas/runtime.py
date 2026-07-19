import base64
import json
import os
from functools import lru_cache

import boto3


def database_url() -> str:
    direct = os.getenv("DATABASE_URL")
    if direct:
        return direct
    secret_arn = os.getenv("DATABASE_SECRET_ARN")
    if not secret_arn:
        raise RuntimeError("DATABASE_SECRET_ARN or DATABASE_URL is required")
    return _database_url_from_secret(secret_arn)


@lru_cache(maxsize=4)
def _database_url_from_secret(secret_arn: str) -> str:
    response = boto3.client("secretsmanager").get_secret_value(SecretId=secret_arn)
    secret = response.get("SecretString")
    if secret is None:
        binary = response.get("SecretBinary")
        if binary is None:
            raise RuntimeError("database secret has no value")
        decoded = base64.b64decode(binary) if isinstance(binary, str) else binary
        secret = decoded.decode("utf-8")
    try:
        parsed = json.loads(secret)
    except json.JSONDecodeError:
        parsed = secret
    if isinstance(parsed, dict):
        database_url = parsed.get("DATABASE_URL") or parsed.get("database_url")
    else:
        database_url = parsed
    if not isinstance(database_url, str) or not database_url:
        raise RuntimeError("database secret must contain DATABASE_URL or a raw URL")
    return database_url


def positive_int(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value < 1:
        raise ValueError(f"{name} must be positive")
    return value
