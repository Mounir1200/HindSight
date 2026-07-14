CREATE TABLE IF NOT EXISTS telecom_cdrs (
  id UUID PRIMARY KEY,
  external_id STRING NOT NULL UNIQUE,
  msisdn_hash STRING NOT NULL,
  route STRING NOT NULL,
  service_type STRING NOT NULL,
  started_at TIMESTAMPTZ NOT NULL,
  duration_sec INT8,
  data_mb DECIMAL(18, 4),
  source_id UUID REFERENCES sources(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT telecom_cdrs_duration_positive
    CHECK (duration_sec IS NULL OR duration_sec > 0),
  CONSTRAINT telecom_cdrs_data_non_negative
    CHECK (data_mb IS NULL OR data_mb >= 0),
  CONSTRAINT telecom_cdrs_usage_present
    CHECK (duration_sec IS NOT NULL OR data_mb IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS telecom_invoices (
  id UUID PRIMARY KEY,
  cdr_id UUID NOT NULL UNIQUE REFERENCES telecom_cdrs(id),
  amount DECIMAL(18, 4) NOT NULL,
  currency STRING NOT NULL,
  status STRING NOT NULL DEFAULT 'issued',
  decision_id UUID NOT NULL UNIQUE REFERENCES decisions(id),
  selected_assertion_id UUID NOT NULL REFERENCES assertions(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT telecom_invoices_amount_non_negative CHECK (amount >= 0),
  CONSTRAINT telecom_invoices_status
    CHECK (status IN ('issued', 'corrected'))
);

CREATE TABLE IF NOT EXISTS telecom_disputes (
  id UUID PRIMARY KEY,
  invoice_id UUID NOT NULL UNIQUE REFERENCES telecom_invoices(id),
  claim STRING NOT NULL,
  status STRING NOT NULL DEFAULT 'open',
  opened_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  closed_at TIMESTAMPTZ,
  resolution JSONB,
  CONSTRAINT telecom_disputes_state CHECK (
    (status = 'open' AND closed_at IS NULL AND resolution IS NULL)
    OR (status = 'closed' AND closed_at IS NOT NULL AND resolution IS NOT NULL)
  )
);

CREATE TABLE IF NOT EXISTS remediation_runs (
  id UUID PRIMARY KEY,
  domain STRING NOT NULL,
  case_type STRING NOT NULL,
  case_id STRING NOT NULL,
  status STRING NOT NULL DEFAULT 'started',
  started_at TIMESTAMPTZ NOT NULL,
  completed_at TIMESTAMPTZ,
  executed_by STRING NOT NULL,
  request JSONB NOT NULL,
  result JSONB,
  CONSTRAINT remediation_runs_case_unique
    UNIQUE (domain, case_type, case_id),
  CONSTRAINT remediation_runs_state CHECK (
    (status = 'started' AND completed_at IS NULL AND result IS NULL)
    OR (status = 'applied' AND completed_at IS NOT NULL AND result IS NOT NULL)
  )
);

CREATE TABLE IF NOT EXISTS telecom_refunds (
  id UUID PRIMARY KEY,
  dispute_id UUID NOT NULL UNIQUE REFERENCES telecom_disputes(id),
  remediation_run_id UUID NOT NULL UNIQUE REFERENCES remediation_runs(id),
  amount DECIMAL(18, 4) NOT NULL,
  currency STRING NOT NULL,
  status STRING NOT NULL DEFAULT 'created',
  created_at TIMESTAMPTZ NOT NULL,
  CONSTRAINT telecom_refunds_amount_positive CHECK (amount > 0),
  CONSTRAINT telecom_refunds_status CHECK (status IN ('created', 'paid', 'failed'))
);

CREATE TABLE IF NOT EXISTS telecom_incidents (
  id UUID PRIMARY KEY,
  dispute_id UUID NOT NULL REFERENCES telecom_disputes(id),
  remediation_run_id UUID NOT NULL UNIQUE REFERENCES remediation_runs(id),
  category STRING NOT NULL,
  status STRING NOT NULL DEFAULT 'open',
  description STRING NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  CONSTRAINT telecom_incidents_identity_unique UNIQUE (dispute_id, category),
  CONSTRAINT telecom_incidents_status CHECK (status IN ('open', 'resolved'))
);

CREATE TABLE IF NOT EXISTS memories (
  id UUID PRIMARY KEY,
  memory_key STRING NOT NULL,
  lineage_id UUID NOT NULL,
  version_number INT8 NOT NULL DEFAULT 1,
  domain STRING NOT NULL,
  namespace STRING NOT NULL,
  kind STRING NOT NULL,
  content STRING NOT NULL,
  content_struct JSONB NOT NULL,
  valid_from TIMESTAMPTZ NOT NULL,
  valid_until TIMESTAMPTZ,
  recorded_at TIMESTAMPTZ NOT NULL,
  superseded_at TIMESTAMPTZ,
  superseded_by UUID,
  written_by STRING NOT NULL,
  source_id UUID REFERENCES sources(id),
  confidence FLOAT8 NOT NULL DEFAULT 1.0,
  conflict_decision STRING,
  conflict_reason STRING,
  remediation_run_id UUID NOT NULL UNIQUE REFERENCES remediation_runs(id),
  CONSTRAINT memories_version_unique UNIQUE (memory_key, version_number),
  CONSTRAINT memories_version_positive CHECK (version_number > 0),
  CONSTRAINT memories_confidence CHECK (confidence >= 0 AND confidence <= 1),
  CONSTRAINT memories_valid_interval
    CHECK (valid_until IS NULL OR valid_until > valid_from),
  CONSTRAINT memories_recorded_interval
    CHECK (superseded_at IS NULL OR superseded_at > recorded_at)
);

CREATE INDEX IF NOT EXISTS memories_lookup_idx
ON memories (domain, namespace, kind, recorded_at DESC);
