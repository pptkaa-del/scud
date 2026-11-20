-- scud_reports — report job queue + file metadata.
CREATE TYPE report_status AS ENUM ('pending', 'running', 'ready', 'failed', 'expired');

CREATE TABLE report_jobs
(
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    report_type   VARCHAR(50)   NOT NULL,
    name          VARCHAR(255),
    params        JSONB         NOT NULL DEFAULT '{}',
    status        report_status NOT NULL DEFAULT 'pending',
    requested_by  BIGINT        NOT NULL,
    format        VARCHAR(16),
    file_path     TEXT,
    file_name     TEXT,
    file_size     BIGINT,
    row_count     INTEGER,
    error_message TEXT,
    attempts      INTEGER     NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at    TIMESTAMPTZ,
    completed_at  TIMESTAMPTZ,
    expires_at    TIMESTAMPTZ
);

CREATE INDEX idx_report_jobs_pending ON report_jobs (created_at) WHERE status = 'pending';
-- Реклейм зависших задач: claim_pending матчит status='running' AND started_at < ...
-- Без частичного индекса OR-ветка running делает seqscan по всей таблице на
-- каждый опрос воркера. Партнёр к idx_report_jobs_pending для bitmap-OR.
CREATE INDEX idx_report_jobs_running_stale ON report_jobs (started_at) WHERE status = 'running';
CREATE INDEX idx_report_jobs_ready_expiry ON report_jobs (expires_at) WHERE status = 'ready';
CREATE INDEX idx_report_jobs_requested_by ON report_jobs (requested_by);
