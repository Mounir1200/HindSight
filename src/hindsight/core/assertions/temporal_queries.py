EXISTING_RECORDING_SQL = """
SELECT *
FROM assertions
WHERE assertion_key = %s
  AND recorded_at = %s
LIMIT 1
"""

LATEST_ACTIVE_SQL = """
SELECT *
FROM assertions
WHERE assertion_key = %s
  AND superseded_at IS NULL
ORDER BY version_number DESC
LIMIT 1
FOR UPDATE
"""

INSERT_ASSERTION_SQL = """
INSERT INTO assertions (
  id, assertion_key, lineage_id, version_number, domain, subject_type,
  subject_id, predicate, value_json, value_number, value_text, unit,
  currency, valid_from, valid_until, recorded_at, written_by, source_id,
  confidence
)
VALUES (
  %s, %s, %s, %s, %s, %s, %s, %s, CAST(%s AS JSONB), %s, %s, %s,
  %s, %s, %s, %s, %s, %s, %s
)
"""

SUPERSEDE_ASSERTION_SQL = """
UPDATE assertions
SET superseded_at = %s,
    superseded_by = %s
WHERE id = %s
  AND superseded_at IS NULL
"""

CURRENT_TRUTH_SQL = """
SELECT *
FROM assertions
WHERE assertion_key = %s
  AND domain = %s
  AND subject_type = %s
  AND subject_id = %s
  AND predicate = %s
  AND valid_from <= %s
  AND (valid_until IS NULL OR valid_until > %s)
ORDER BY recorded_at DESC, version_number DESC
LIMIT 1
"""

KNOWN_AT_DECISION_SQL = """
SELECT *
FROM assertions
WHERE assertion_key = %s
  AND domain = %s
  AND subject_type = %s
  AND subject_id = %s
  AND predicate = %s
  AND valid_from <= %s
  AND (valid_until IS NULL OR valid_until > %s)
  AND recorded_at <= %s
ORDER BY recorded_at DESC, version_number DESC
LIMIT 1
"""

TEMPORAL_SNAPSHOT_SQL = """
WITH current_truth AS (
  SELECT *
  FROM assertions
  WHERE assertion_key = %s
    AND domain = %s
    AND subject_type = %s
    AND subject_id = %s
    AND predicate = %s
    AND valid_from <= %s
    AND (valid_until IS NULL OR valid_until > %s)
  ORDER BY recorded_at DESC, version_number DESC
  LIMIT 1
),
known_at_decision AS (
  SELECT *
  FROM assertions
  WHERE assertion_key = %s
    AND domain = %s
    AND subject_type = %s
    AND subject_id = %s
    AND predicate = %s
    AND valid_from <= %s
    AND (valid_until IS NULL OR valid_until > %s)
    AND recorded_at <= %s
  ORDER BY recorded_at DESC, version_number DESC
  LIMIT 1
)
SELECT 'current_truth' AS snapshot_kind, current_truth.*
FROM current_truth
UNION ALL
SELECT 'known_at_decision' AS snapshot_kind, known_at_decision.*
FROM known_at_decision
"""

ASSERTION_HISTORY_SQL = """
SELECT *
FROM assertions
WHERE assertion_key = %s
ORDER BY version_number
"""
