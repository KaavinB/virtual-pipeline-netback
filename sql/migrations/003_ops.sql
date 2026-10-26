-- Pipeline observability: every ingestion run and every data-quality check leaves a row.

CREATE TABLE ops.ingest_run (
    run_id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source          text NOT NULL,               -- e.g. 'EIA:RNGWHHD'
    started_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz,
    status          text NOT NULL DEFAULT 'running'
                    CHECK (status IN ('running', 'success', 'failed')),
    window_start    date,                        -- earliest period requested
    rows_fetched    int,                         -- rows returned by the API
    rows_skipped    int,                         -- null / non-numeric values
    rows_duplicate  int,                         -- repeated periods inside one payload
    rows_inserted   int,
    rows_updated    int,                         -- existing period whose value changed (revision)
    error           text
);

CREATE TABLE ops.dq_results (
    result_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    checked_at  timestamptz NOT NULL DEFAULT now(),
    check_name  text    NOT NULL,
    target      text    NOT NULL,                -- table or feed the check is about
    severity    text    NOT NULL CHECK (severity IN ('error', 'warn')),
    passed      boolean NOT NULL,
    observed    numeric,                         -- the measured value (count, days, ...)
    threshold   numeric,                         -- the limit it was compared with
    details     jsonb   NOT NULL DEFAULT '{}'
);

CREATE INDEX ON ops.dq_results (check_name, checked_at DESC);

-- Current health: the most recent result of every check. Dashboards read this.
CREATE VIEW ops.v_dq_latest AS
SELECT DISTINCT ON (check_name) *
FROM ops.dq_results
ORDER BY check_name, checked_at DESC;
