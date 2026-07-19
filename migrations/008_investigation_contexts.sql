CREATE TABLE IF NOT EXISTS investigation_contexts (
  case_id UUID PRIMARY KEY REFERENCES telecom_disputes(id),
  context_version STRING(64) NOT NULL,
  context JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT investigation_context_case_matches
    CHECK (context->>'case_id' = case_id::STRING)
);
