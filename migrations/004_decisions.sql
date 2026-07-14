CREATE TABLE IF NOT EXISTS decisions (
  id UUID PRIMARY KEY,
  domain STRING NOT NULL,
  agent_id STRING NOT NULL,
  action STRING NOT NULL,
  subject_type STRING NOT NULL,
  subject_id STRING NOT NULL,
  event_time TIMESTAMPTZ NOT NULL,
  decided_at TIMESTAMPTZ NOT NULL,
  selected_assertion_id UUID NOT NULL REFERENCES assertions(id),
  current_truth_assertion_id UUID NOT NULL REFERENCES assertions(id),
  known_assertion_id UUID NOT NULL REFERENCES assertions(id),
  input JSONB NOT NULL,
  output JSONB NOT NULL,
  rationale STRING,
  verdict STRING NOT NULL,
  agent_fault BOOL,
  knowledge_gap_seconds INT8 NOT NULL DEFAULT 0,
  root_cause STRING,
  investigated_at TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT decisions_knowledge_gap_non_negative
    CHECK (knowledge_gap_seconds >= 0)
);

CREATE INDEX IF NOT EXISTS decisions_subject_idx
ON decisions (domain, subject_type, subject_id, decided_at);

CREATE TABLE IF NOT EXISTS decision_evidence (
  id UUID PRIMARY KEY,
  decision_id UUID NOT NULL REFERENCES decisions(id) ON DELETE CASCADE,
  evidence_type STRING NOT NULL,
  assertion_id UUID NOT NULL REFERENCES assertions(id),
  available_to_agent BOOL NOT NULL,
  retrieval_started_at TIMESTAMPTZ,
  retrieved_at TIMESTAMPTZ,
  retrieval_method STRING,
  retrieval_query STRING,
  retrieval_rank INT,
  retrieval_score FLOAT8,
  was_presented_to_model BOOL NOT NULL DEFAULT false,
  presentation_position INT,
  was_cited_in_rationale BOOL NOT NULL DEFAULT false,
  was_used_for_decision BOOL NOT NULL DEFAULT false,
  exclusion_reason STRING,
  CONSTRAINT decision_evidence_identity_unique
    UNIQUE (decision_id, evidence_type, assertion_id),
  CONSTRAINT decision_evidence_retrieval_order
    CHECK (
      retrieval_started_at IS NULL
      OR retrieved_at IS NULL
      OR retrieval_started_at <= retrieved_at
    ),
  CONSTRAINT decision_evidence_rank_positive
    CHECK (retrieval_rank IS NULL OR retrieval_rank > 0),
  CONSTRAINT decision_evidence_presentation_position_positive
    CHECK (presentation_position IS NULL OR presentation_position > 0),
  CONSTRAINT decision_evidence_presented_was_retrieved
    CHECK (NOT was_presented_to_model OR retrieved_at IS NOT NULL),
  CONSTRAINT decision_evidence_used_was_retrieved
    CHECK (NOT was_used_for_decision OR retrieved_at IS NOT NULL),
  CONSTRAINT decision_evidence_position_requires_presentation
    CHECK (presentation_position IS NULL OR was_presented_to_model)
);

CREATE INDEX IF NOT EXISTS decision_evidence_decision_idx
ON decision_evidence (decision_id, retrieval_rank);
