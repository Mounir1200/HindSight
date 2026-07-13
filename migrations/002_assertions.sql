CREATE TABLE IF NOT EXISTS assertions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  assertion_key STRING NOT NULL,
  lineage_id UUID NOT NULL,
  version_number INT NOT NULL,
  domain STRING NOT NULL,
  subject_type STRING NOT NULL,
  subject_id STRING NOT NULL,
  predicate STRING NOT NULL,
  value_json JSONB NOT NULL,
  value_number DECIMAL(20, 8),
  value_text STRING,
  unit STRING,
  currency STRING,
  valid_from TIMESTAMPTZ NOT NULL,
  valid_until TIMESTAMPTZ,
  recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  superseded_at TIMESTAMPTZ,
  superseded_by UUID,
  written_by STRING NOT NULL,
  source_id UUID REFERENCES sources(id),
  confidence FLOAT8 NOT NULL DEFAULT 1.0,
  conflict_decision STRING,
  conflict_reason STRING,
  CONSTRAINT assertions_version_unique UNIQUE (assertion_key, version_number),
  CONSTRAINT assertions_recording_unique UNIQUE (assertion_key, recorded_at),
  CONSTRAINT assertions_valid_interval
    CHECK (valid_until IS NULL OR valid_until > valid_from),
  CONSTRAINT assertions_recorded_interval
    CHECK (superseded_at IS NULL OR superseded_at > recorded_at)
);

