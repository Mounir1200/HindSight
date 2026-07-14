CREATE TABLE IF NOT EXISTS agent_runs (
  id UUID PRIMARY KEY,
  correlation_id UUID NOT NULL,
  domain STRING(64) NOT NULL,
  agent_id STRING(64) NOT NULL,
  run_type STRING(128) NOT NULL,
  subject_type STRING(64) NOT NULL,
  subject_id STRING(256) NOT NULL,
  provider STRING(64) NOT NULL,
  model_id STRING(512) NOT NULL,
  prompt_version STRING(64) NOT NULL,
  toolset_version STRING(64) NOT NULL,
  status STRING(16) NOT NULL,
  started_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  completed_at TIMESTAMPTZ,
  input_summary JSONB NOT NULL,
  output JSONB,
  error JSONB,
  usage JSONB NOT NULL,
  stop_reason STRING,
  CONSTRAINT agent_runs_time_order CHECK (
    updated_at >= started_at
    AND (completed_at IS NULL OR completed_at >= started_at)
  ),
  CONSTRAINT agent_runs_state CHECK (
    (
      status = 'running'
      AND completed_at IS NULL
      AND output IS NULL
      AND error IS NULL
      AND stop_reason IS NULL
    )
    OR (
      status = 'completed'
      AND completed_at IS NOT NULL
      AND output IS NOT NULL
      AND error IS NULL
      AND stop_reason IS NOT NULL
    )
    OR (
      status = 'failed'
      AND completed_at IS NOT NULL
      AND output IS NULL
      AND error IS NOT NULL
    )
  )
);

CREATE INDEX IF NOT EXISTS agent_runs_subject_idx
ON agent_runs (domain, subject_type, subject_id, started_at DESC);

CREATE TABLE IF NOT EXISTS tool_calls (
  id UUID PRIMARY KEY,
  run_id UUID NOT NULL REFERENCES agent_runs(id),
  tool_use_id STRING(64) NOT NULL,
  sequence_number INT8 NOT NULL,
  tool_name STRING(64) NOT NULL,
  status STRING NOT NULL,
  requested_at TIMESTAMPTZ NOT NULL,
  completed_at TIMESTAMPTZ NOT NULL,
  arguments JSONB NOT NULL,
  result JSONB,
  error JSONB,
  CONSTRAINT tool_calls_tool_use_unique UNIQUE (run_id, tool_use_id),
  CONSTRAINT tool_calls_sequence_unique UNIQUE (run_id, sequence_number),
  CONSTRAINT tool_calls_sequence_positive CHECK (sequence_number > 0),
  CONSTRAINT tool_calls_time_order CHECK (completed_at >= requested_at),
  CONSTRAINT tool_calls_state CHECK (
    (status = 'succeeded' AND result IS NOT NULL AND error IS NULL)
    OR (status = 'failed' AND result IS NULL AND error IS NOT NULL)
  )
);
