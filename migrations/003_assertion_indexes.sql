CREATE INDEX IF NOT EXISTS assertions_temporal_lookup_idx
ON assertions (
  assertion_key,
  domain,
  subject_type,
  subject_id,
  predicate,
  valid_from,
  recorded_at
);
