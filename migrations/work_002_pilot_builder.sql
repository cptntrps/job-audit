-- job-audit 002: the pilot builder (owner ask 2026-09-17: "piloting should actually come up
-- with a pilot"). Method from the Cooper Simson reel DdVargvpEQ8 ("the AI delegation loop"):
-- interview -> playbook -> toolbox -> proof; corrections fix the playbook, not the chat.
--
-- Additive only. task.aei_profile carries EVERY per-task Economic Index metric (artifact
-- types AI produces for the task, collaboration patterns, autonomy, times) so the builder
-- reasons from evidence, not from the task title. pilot gains the builder's record:
--   interview  the owner's answers about how THIS task happens in THIS function (jsonb)
--   playbook   the drafted playbook (markdown) — a local model PROPOSES it, the owner edits
--   proof      the definition-of-done checks (jsonb list) wired into the playbook
--   baseline   volume/time numbers from the interview (jsonb), the score stage's denominator
-- Deterministic gate (page + job): a pilot can only move to 'started' when playbook is
-- present and proof has at least one check. Models propose; the gate enforces.

ALTER TABLE work.task  ADD COLUMN IF NOT EXISTS aei_profile jsonb;
ALTER TABLE work.pilot ADD COLUMN IF NOT EXISTS interview  jsonb;
ALTER TABLE work.pilot ADD COLUMN IF NOT EXISTS playbook   text;
ALTER TABLE work.pilot ADD COLUMN IF NOT EXISTS proof      jsonb;
ALTER TABLE work.pilot ADD COLUMN IF NOT EXISTS baseline   jsonb;
ALTER TABLE work.pilot ADD COLUMN IF NOT EXISTS drafted_at timestamptz;
ALTER TABLE work.pilot ADD COLUMN IF NOT EXISTS model_used text;

GRANT UPDATE (interview, playbook, proof, baseline, drafted_at, model_used) ON work.pilot TO myapps_work;

ALTER TABLE work.pilot_event DROP CONSTRAINT IF EXISTS pilot_event_action_check;
ALTER TABLE work.pilot_event ADD CONSTRAINT pilot_event_action_check
    CHECK (action IN ('proposed', 'started', 'done', 'dropped', 'reopened', 'hours',
                      'interviewed', 'drafted', 'edited'));
