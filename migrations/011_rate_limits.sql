CREATE TABLE IF NOT EXISTS api_rate_limit_buckets (
  bucket_key STRING(130) PRIMARY KEY,
  tokens DECIMAL(20, 10) NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL,
  CONSTRAINT api_rate_limit_tokens_non_negative
    CHECK (tokens >= 0)
) WITH (
  ttl_expiration_expression = 'expires_at',
  ttl_job_cron = '@hourly'
);

CREATE INDEX IF NOT EXISTS api_rate_limit_buckets_expiry_idx
ON api_rate_limit_buckets (expires_at);

CREATE TABLE IF NOT EXISTS api_rate_limit_leases (
  lease_key STRING(64) NOT NULL,
  slot INT4 NOT NULL,
  holder_hash STRING(64) NOT NULL,
  acquired_at TIMESTAMPTZ NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (lease_key, slot),
  CONSTRAINT api_rate_limit_lease_slot_non_negative
    CHECK (slot >= 0)
) WITH (
  ttl_expiration_expression = 'expires_at',
  ttl_job_cron = '@hourly'
);

CREATE INDEX IF NOT EXISTS api_rate_limit_leases_expiry_idx
ON api_rate_limit_leases (expires_at);
