-- job-audit 003: the job-list table (owner ask 2026-09-17: "a list of jobs (csv) -> a table with
-- the functions by job and the automatable percentage of the work").
--
-- Loop: acquire (a CSV of the org's job titles with function + headcount, uploaded on myapps
-- /jobs/ or fed to load.py map-jobs) -> decide (each title maps to an O*NET occupation:
-- exact title match, then trigram similarity over work.title, then ONE gateway pick among the
-- top candidates for the leftovers, validated against the candidate list) -> store
-- (work.job_batch + work.job with the match method + confidence and the importance-weighted
-- automate / augment / human shares from work.task) -> act (table on the page, CSV export).
--
-- work.title: every known title for an occupation (O*NET Occupation Data titles, Alternate
-- Titles, Sample of Reported Titles; ~60k rows). pg_trgm gives similarity() and a GIN index
-- so the match is one SQL query, no model.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS work.title (
    id        bigserial PRIMARY KEY,
    soc_code  text NOT NULL REFERENCES work.occupation (soc_code),
    title     text NOT NULL,
    title_norm text NOT NULL,                       -- lowercased, punctuation stripped, single spaces
    source    text NOT NULL CHECK (source IN ('onet', 'alternate', 'reported')),
    UNIQUE (soc_code, title_norm, source)
);
CREATE INDEX IF NOT EXISTS work_title_norm_trgm_idx ON work.title USING gin (title_norm gin_trgm_ops);
CREATE INDEX IF NOT EXISTS work_title_norm_idx ON work.title (title_norm);

CREATE TABLE IF NOT EXISTS work.job_batch (
    id          bigserial PRIMARY KEY,
    label       text NOT NULL,                      -- file name or a name the owner gave
    rows_in     integer NOT NULL,
    rows_mapped integer NOT NULL DEFAULT 0,
    source      text NOT NULL,                      -- 'myapps' | 'job-audit'
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS work.job (
    id             bigserial PRIMARY KEY,
    batch_id       bigint NOT NULL REFERENCES work.job_batch (id) ON DELETE CASCADE,
    job_title      text NOT NULL,
    function       text,
    headcount      numeric,
    soc_code       text REFERENCES work.occupation (soc_code),   -- NULL = unmapped (kept, never dropped silently)
    matched_title  text,                            -- the work.title row that won
    match_method   text NOT NULL CHECK (match_method IN ('exact', 'similar', 'model', 'none')),
    confidence     numeric NOT NULL DEFAULT 0,      -- 1.0 exact; trigram similarity; model = candidate similarity
    automate_share numeric,                         -- importance-weighted % of the occupation's task weight
    augment_share  numeric,
    human_share    numeric,
    exposure       numeric,                         -- AEI observed exposure of the occupation
    top_automate   jsonb,                           -- up to 3 task statements
    created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS work_job_batch_idx ON work.job (batch_id);

GRANT SELECT ON work.title, work.job_batch, work.job TO myapps_work;
GRANT INSERT ON work.job_batch, work.job TO myapps_work;
GRANT UPDATE (rows_mapped) ON work.job_batch TO myapps_work;
GRANT DELETE ON work.job_batch, work.job TO myapps_work;   -- the owner can remove a batch he uploaded
GRANT USAGE, SELECT ON SEQUENCE work.job_batch_id_seq, work.job_id_seq TO myapps_work;

INSERT INTO ops.table_registry (table_ref, purpose, migration_file, registered_by, owner, data_domain, reader_policy)
VALUES
  ('work.title', 'Every known title per O*NET occupation (occupation, alternate, reported titles) with a trigram index; the deterministic job-title -> SOC matcher. Loader: products/job-audit load.py.', 'job-audit/work_003_job_list.sql', 'job-audit', 'gui', 'work', 'standard'),
  ('work.job_batch', 'One uploaded job list (CSV) = one batch.', 'job-audit/work_003_job_list.sql', 'job-audit', 'gui', 'work', 'standard'),
  ('work.job', 'One row per job title in a batch: the matched occupation (method + confidence) and its automate/augment/human shares. Writers: myapps /jobs/ (myapps_work), load.py map-jobs (livingos).', 'job-audit/work_003_job_list.sql', 'job-audit', 'gui', 'work', 'standard')
ON CONFLICT (table_ref) DO NOTHING;

-- owner livingos, like the other work.* tables (owned sequences and indexes follow; idempotent on re-apply)
ALTER TABLE work.title     OWNER TO livingos;
ALTER TABLE work.job_batch OWNER TO livingos;
ALTER TABLE work.job       OWNER TO livingos;

-- grafana_ro read parity (explicit: the livingos default privileges do not cover objects created by postgres_po)
GRANT SELECT ON work.title, work.job_batch, work.job TO grafana_ro;
