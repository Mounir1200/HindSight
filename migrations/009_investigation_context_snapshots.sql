CREATE TABLE IF NOT EXISTS investigation_context_snapshots (
  id UUID PRIMARY KEY,
  case_id UUID NOT NULL REFERENCES telecom_disputes(id),
  context_version STRING(64) NOT NULL,
  content_hash STRING(64) NOT NULL,
  context JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT investigation_context_snapshots_case_hash_unique
    UNIQUE (case_id, content_hash),
  CONSTRAINT investigation_context_snapshots_hash_length
    CHECK (length(content_hash) = 64),
  CONSTRAINT investigation_context_snapshots_case_matches
    CHECK (context->>'case_id' = case_id::STRING)
);

CREATE INDEX IF NOT EXISTS investigation_context_snapshots_case_idx
ON investigation_context_snapshots (case_id, created_at DESC, id DESC);
