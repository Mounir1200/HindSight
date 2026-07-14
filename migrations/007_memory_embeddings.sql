CREATE TABLE IF NOT EXISTS memory_embeddings (
  memory_id UUID NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  model_id STRING NOT NULL,
  domain STRING NOT NULL,
  namespace STRING NOT NULL,
  kind STRING NOT NULL,
  route STRING NOT NULL,
  service_type STRING NOT NULL,
  content_sha256 STRING(64) NOT NULL,
  embedding VECTOR(1024) NOT NULL,
  input_tokens INT8 NOT NULL,
  embedded_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (memory_id, model_id),
  CONSTRAINT memory_embeddings_input_tokens_non_negative CHECK (input_tokens >= 0)
);

CREATE VECTOR INDEX IF NOT EXISTS memory_embeddings_cosine_idx
ON memory_embeddings (
  domain,
  namespace,
  kind,
  model_id,
  route,
  service_type,
  embedding vector_cosine_ops
);
