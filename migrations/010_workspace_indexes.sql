CREATE INDEX IF NOT EXISTS decisions_audit_history_idx
ON decisions (domain, subject_type, investigated_at DESC, id DESC);
