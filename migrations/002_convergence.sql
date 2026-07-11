-- Additive-only migration for the convergence ledger, verifier evidence, and
-- compounding lesson memory (specs/convergent-autonomous-harness.html).
-- No existing table is altered; rollback is a plain DROP TABLE of the three
-- tables below.
BEGIN;

CREATE TABLE IF NOT EXISTS forge_progress_ledger (
    id varchar(36) PRIMARY KEY,
    run_id varchar(36) NOT NULL REFERENCES forge_runs(id) ON DELETE CASCADE,
    cycle integer NOT NULL CHECK (cycle >= 0),
    test_id varchar(256) NOT NULL,
    passed boolean NOT NULL,
    duration_ms integer,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_forge_progress_ledger_run_cycle
    ON forge_progress_ledger(run_id, cycle);
CREATE INDEX IF NOT EXISTS idx_forge_progress_ledger_run_test
    ON forge_progress_ledger(run_id, test_id, cycle);

CREATE TABLE IF NOT EXISTS forge_verdict_evidence (
    id varchar(36) PRIMARY KEY,
    run_id varchar(36) NOT NULL REFERENCES forge_runs(id) ON DELETE CASCADE,
    goal_id varchar(128),
    tier varchar(16) NOT NULL, -- 'T1' | 'T2' | 'skipped'
    test_id varchar(256) NOT NULL,
    exit_code integer NOT NULL,
    output_digest varchar(32) NOT NULL,
    env_fingerprint varchar(64),
    duration_ms integer,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_forge_verdict_evidence_run
    ON forge_verdict_evidence(run_id, tier);

CREATE TABLE IF NOT EXISTS forge_lessons (
    id varchar(36) PRIMARY KEY,
    project_id varchar(128) NOT NULL REFERENCES hermes_projects(id) ON DELETE CASCADE,
    fingerprint varchar(32) NOT NULL,
    task_id varchar(128),
    failure_type varchar(64) NOT NULL,
    observation text NOT NULL,
    prevention text NOT NULL,
    evidence_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    confidence numeric NOT NULL DEFAULT 0.9,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_forge_lesson_project_fingerprint UNIQUE(project_id, fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_forge_lessons_failure_type
    ON forge_lessons(project_id, failure_type, confidence DESC);

COMMIT;
