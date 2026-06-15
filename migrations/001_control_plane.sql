BEGIN;

CREATE TABLE IF NOT EXISTS forge_runs (
    id varchar(36) PRIMARY KEY,
    project_id varchar(128) NOT NULL,
    goal_id varchar(128) NOT NULL,
    provider_id varchar(128),
    status varchar(32) NOT NULL DEFAULT 'queued',
    current_node varchar(32) NOT NULL DEFAULT 'planner',
    turn integer NOT NULL DEFAULT 0 CHECK (turn >= 0),
    max_turns integer NOT NULL DEFAULT 90 CHECK (max_turns > 0),
    lease_owner varchar(128),
    lease_token varchar(64) UNIQUE,
    lease_expires_at timestamptz,
    heartbeat_at timestamptz,
    terminal_reason text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_forge_run_claim ON forge_runs(status, lease_expires_at, created_at);

CREATE TABLE IF NOT EXISTS forge_run_events (
    id varchar(36) PRIMARY KEY,
    run_id varchar(36) NOT NULL REFERENCES forge_runs(id) ON DELETE CASCADE,
    sequence integer NOT NULL,
    event_type varchar(64) NOT NULL,
    actor varchar(128) NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_forge_event_sequence UNIQUE(run_id, sequence)
);
CREATE INDEX IF NOT EXISTS idx_forge_run_events_run ON forge_run_events(run_id, sequence);

CREATE TABLE IF NOT EXISTS forge_approvals (
    id varchar(36) PRIMARY KEY,
    run_id varchar(36) NOT NULL REFERENCES forge_runs(id) ON DELETE CASCADE,
    action_type varchar(64) NOT NULL,
    action_digest varchar(64) NOT NULL,
    action_preview jsonb NOT NULL DEFAULT '{}'::jsonb,
    risk varchar(16) NOT NULL,
    status varchar(16) NOT NULL DEFAULT 'pending',
    requested_by varchar(128) NOT NULL,
    decided_by varchar(128),
    decision_reason text,
    expires_at timestamptz NOT NULL,
    decided_at timestamptz,
    consumed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_forge_approval_pending ON forge_approvals(status, expires_at);

CREATE TABLE IF NOT EXISTS forge_external_actions (
    id varchar(36) PRIMARY KEY,
    run_id varchar(36) NOT NULL REFERENCES forge_runs(id) ON DELETE CASCADE,
    approval_id varchar(36) NOT NULL UNIQUE REFERENCES forge_approvals(id),
    action_type varchar(64) NOT NULL,
    request_payload jsonb NOT NULL,
    status varchar(32) NOT NULL DEFAULT 'queued',
    result_payload jsonb,
    idempotency_key varchar(128) NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz
);

CREATE TABLE IF NOT EXISTS forge_browser_sessions (
    id varchar(36) PRIMARY KEY,
    run_id varchar(36) NOT NULL REFERENCES forge_runs(id) ON DELETE CASCADE,
    status varchar(32) NOT NULL DEFAULT 'created',
    current_url text,
    allowed_hosts jsonb NOT NULL DEFAULT '[]'::jsonb,
    storage_path text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS forge_browser_actions (
    id varchar(36) PRIMARY KEY,
    session_id varchar(36) NOT NULL REFERENCES forge_browser_sessions(id) ON DELETE CASCADE,
    sequence integer NOT NULL,
    action varchar(32) NOT NULL,
    arguments jsonb NOT NULL DEFAULT '{}'::jsonb,
    mutating boolean NOT NULL DEFAULT false,
    approval_id varchar(36) REFERENCES forge_approvals(id),
    status varchar(32) NOT NULL DEFAULT 'queued',
    result jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_forge_browser_action_sequence UNIQUE(session_id, sequence)
);

COMMIT;
