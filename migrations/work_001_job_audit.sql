-- job-audit 001: the job task-audit loop (owner ask 2026-09-17, from the Instagram reel
-- DdUNm6sx_x1: Anthropic Economic Index + O*NET tasks -> automate vs augment split ->
-- 90-day pilot plan). Deterministic end to end; no model in the loop.
--
-- Loop: acquire (O*NET 30.0 task statements + ratings, Anthropic Economic Index task
-- metrics + occupation exposure) -> store (work.occupation, work.task) -> decide
-- (work.role names the occupations per GBS function; task.bucket + task.priority are
-- computed at load) -> act (myapps /jobs/ renders the split and the 90-day pilot list per
-- role; the owner starts/drops pilots) -> score (work.pilot status + hours_saved, appended
-- to work.pilot_event). Falsifiable goal: >=3 pilots marked started within 90 days of the
-- first role report (2026-12-16).
--
-- Substrate: new schema `work`. postgres-po applies; tables owned like the other consumer
-- schemas. Writers: products/job-audit load.py (livingos) for occupation/task; the owner via
-- myapps (role myapps_work) for pilot verdicts only.
--
-- Apply path (postgres-po, 2026-09-17): this file runs on PG_ADMIN_DSN as postgres_po in one
-- transaction (CREATE ROLE is substrate-only). Schema and tables are owned by livingos like
-- the other consumer schemas, so load.py writes as livingos and later work_00N migrations
-- run on the app path. grafana_ro gets SELECT parity (explicit + default privileges, the
-- ha_intel shape). ops.table_registry.reader_policy vocabulary is
-- standard|restricted|tripwired|sealed; 'standard' is the open-reader policy.

CREATE SCHEMA IF NOT EXISTS work AUTHORIZATION livingos;

CREATE TABLE IF NOT EXISTS work.occupation (
    soc_code          text PRIMARY KEY,          -- 'NN-NNNN' (O*NET '.00' stripped)
    title             text NOT NULL,
    observed_exposure numeric,                   -- AEI labor_market_impacts/job_exposure.csv
    loaded_at         timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS work.task (
    task_id                integer PRIMARY KEY,  -- O*NET Task ID (= AEI node_external_id)
    soc_code               text NOT NULL REFERENCES work.occupation (soc_code),
    statement              text NOT NULL,
    task_type              text,                  -- Core | Supplemental
    importance             numeric,               -- O*NET Task Ratings IM, 1..5
    penetration            numeric,               -- AEI task_penetration 0..1 (by statement)
    usage_pct              numeric,               -- AEI onet level-0 pct (share of API usage)
    automation_pct         numeric,               -- AEI collaboration_bucket_automation_pct
    augmentation_pct       numeric,
    ai_autonomy_mean       numeric,               -- 1..5
    human_only_time_min    numeric,               -- AEI human_only_time_mean (hours) * 60
    human_with_ai_time_min numeric,               -- AEI human_with_ai_time_mean (minutes)
    bucket                 text NOT NULL CHECK (bucket IN ('automate', 'augment', 'human')),
    priority               numeric NOT NULL DEFAULT 0,   -- importance x evidence x time saved; see load.py
    loaded_at              timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS work_task_soc_idx ON work.task (soc_code, bucket, priority DESC);

-- the occupations each GBS function is made of (seeded by load.py --seed-roles; owner-editable)
CREATE TABLE IF NOT EXISTS work.role (
    id          bigserial PRIMARY KEY,
    function    text NOT NULL,                    -- 'HR / Workforce', 'License ops', 'GTS Infrastructure'
    lead        text,                             -- function lead first name
    title       text NOT NULL,
    soc_code    text NOT NULL REFERENCES work.occupation (soc_code),
    headcount   integer,
    note        text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (function, soc_code)
);

-- pilots: one row per (role, task) the owner decided to try; the SCORE stage
CREATE TABLE IF NOT EXISTS work.pilot (
    id           bigserial PRIMARY KEY,
    role_id      bigint  NOT NULL REFERENCES work.role (id),
    task_id      integer NOT NULL REFERENCES work.task (task_id),
    kind         text    NOT NULL CHECK (kind IN ('automate', 'augment')),
    status       text    NOT NULL DEFAULT 'proposed'
                 CHECK (status IN ('proposed', 'started', 'done', 'dropped')),
    started_at   timestamptz,
    done_at      timestamptz,
    hours_saved  numeric,                         -- reported by the lead, per month
    note         text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (role_id, task_id)
);

CREATE TABLE IF NOT EXISTS work.pilot_event (
    id         bigserial PRIMARY KEY,
    pilot_id   bigint NOT NULL REFERENCES work.pilot (id),
    action     text   NOT NULL CHECK (action IN ('proposed', 'started', 'done', 'dropped', 'reopened', 'hours')),
    reason     text,
    source     text   NOT NULL,                   -- 'myapps' | 'job-audit'
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS work_pilot_event_pilot_idx ON work.pilot_event (pilot_id, created_at);

-- owner livingos, like the other consumer schemas (owned sequences follow; idempotent on re-apply)
ALTER TABLE work.occupation  OWNER TO livingos;
ALTER TABLE work.task        OWNER TO livingos;
ALTER TABLE work.role        OWNER TO livingos;
ALTER TABLE work.pilot       OWNER TO livingos;
ALTER TABLE work.pilot_event OWNER TO livingos;

-- grafana_ro read parity with the other consumer schemas
GRANT USAGE ON SCHEMA work TO grafana_ro;
GRANT SELECT ON work.occupation, work.task, work.role, work.pilot, work.pilot_event TO grafana_ro;
ALTER DEFAULT PRIVILEGES FOR ROLE livingos IN SCHEMA work GRANT SELECT ON TABLES TO grafana_ro;

-- myapps: pilot verdicts only. Password is set by postgres-po at apply time (never in this file).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'myapps_work') THEN
        CREATE ROLE myapps_work LOGIN CONNECTION LIMIT 10;
    END IF;
END $$;
GRANT USAGE ON SCHEMA work TO myapps_work;
GRANT SELECT ON work.occupation, work.task, work.role, work.pilot, work.pilot_event TO myapps_work;
GRANT INSERT (role_id, task_id, kind, note) ON work.pilot TO myapps_work;
GRANT UPDATE (status, started_at, done_at, hours_saved, note, updated_at) ON work.pilot TO myapps_work;
GRANT INSERT ON work.pilot_event TO myapps_work;
GRANT USAGE, SELECT ON SEQUENCE work.pilot_id_seq, work.pilot_event_id_seq TO myapps_work;

INSERT INTO ops.table_registry (table_ref, purpose, migration_file, registered_by, owner, data_domain, reader_policy)
VALUES
  ('work.occupation', 'SOC occupations with Anthropic Economic Index observed exposure. Loader: products/job-audit load.py.', 'job-audit/work_001_job_audit.sql', 'job-audit', 'gui', 'work', 'standard'),
  ('work.task', 'O*NET task statements per occupation with importance, AEI penetration/usage/automation/augmentation metrics, and the computed bucket + priority. Loader: products/job-audit load.py.', 'job-audit/work_001_job_audit.sql', 'job-audit', 'gui', 'work', 'standard'),
  ('work.role', 'Occupations that make up each GBS function (seeded, owner-editable).', 'job-audit/work_001_job_audit.sql', 'job-audit', 'gui', 'work', 'standard'),
  ('work.pilot', 'Pilots the owner decided to try per role and task; status + hours_saved is the score stage. Writers: myapps /jobs/ (myapps_work).', 'job-audit/work_001_job_audit.sql', 'job-audit', 'gui', 'work', 'standard'),
  ('work.pilot_event', 'Append-only pilot lifecycle log.', 'job-audit/work_001_job_audit.sql', 'job-audit', 'gui', 'work', 'standard')
ON CONFLICT (table_ref) DO NOTHING;
