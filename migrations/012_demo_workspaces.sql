CREATE TABLE IF NOT EXISTS demo_workspaces (
  workspace_id STRING(128) PRIMARY KEY,
  state STRING(16) NOT NULL,
  payload JSONB NOT NULL,
  version INT8 NOT NULL DEFAULT 1,
  lease_token UUID,
  lease_expires_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT demo_workspaces_id_length CHECK (
    length(workspace_id) BETWEEN 1 AND 128
  ),
  CONSTRAINT demo_workspaces_state CHECK (
    state IN ('empty', 'prepared', 'running', 'completed')
  ),
  CONSTRAINT demo_workspaces_payload_object CHECK (
    jsonb_typeof(payload) = 'object'
  ),
  CONSTRAINT demo_workspaces_payload_size CHECK (
    octet_length(payload::STRING) <= 64000
  ),
  CONSTRAINT demo_workspaces_empty_payload CHECK (
    state <> 'empty' OR payload = '{}'::JSONB
  ),
  CONSTRAINT demo_workspaces_version_positive CHECK (version > 0),
  CONSTRAINT demo_workspaces_time_order CHECK (updated_at >= created_at),
  CONSTRAINT demo_workspaces_lease_state CHECK (
    (
      state IN ('empty', 'prepared')
      AND lease_token IS NULL
      AND lease_expires_at IS NULL
    )
    OR (
      state = 'running'
      AND lease_token IS NOT NULL
      AND lease_expires_at IS NOT NULL
      AND lease_expires_at > updated_at
    )
    OR (
      state = 'completed'
      AND lease_token IS NOT NULL
      AND lease_expires_at IS NULL
    )
  )
);

CREATE INDEX IF NOT EXISTS demo_workspaces_running_lease_idx
ON demo_workspaces (lease_expires_at)
STORING (version)
WHERE state = 'running';

CREATE INDEX IF NOT EXISTS demo_workspaces_state_updated_idx
ON demo_workspaces (state, updated_at DESC);
