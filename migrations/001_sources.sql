CREATE TABLE IF NOT EXISTS sources (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  domain STRING NOT NULL,
  kind STRING NOT NULL,
  uri STRING,
  checksum STRING,
  trust_level STRING NOT NULL DEFAULT 'untrusted',
  ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  metadata JSONB,
  CONSTRAINT sources_domain_checksum_unique UNIQUE (domain, checksum)
);

