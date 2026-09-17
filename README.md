# job-audit — which parts of a job are AI's, which need a person, and what to pilot first

Owner ask 2026-09-17, from the Instagram reel `DdUNm6sx_x1` (Angus the Nontechnical): feed the
Anthropic Economic Index and the O*NET task list to an agent, split each job's tasks into
automate versus augment, and turn that into a 90-day plan. Built as the platform's five-stage
loop, deterministic end to end (no model anywhere):

| Stage | Where | What |
|---|---|---|
| acquire | `load.py fetch` | O*NET 30.0 (occupations, task statements, task importance) + Anthropic Economic Index (occupation exposure, task penetration, per-task usage / automation / augmentation / autonomy / time metrics from BOTH the 1P API and the Claude.ai releases, blended per task: the API release is automation-heavy by construction, Claude.ai sits near 50/50). Public downloads into `~/data/job-audit/`. |
| store | `load.py load` | `work.occupation`, `work.task` (one row per O*NET task, ~18k; ~3.2k carry Economic Index metrics, plus `aei_profile` with every per-task metric: artifact types AI produces, collaboration patterns, use cases). Idempotent upserts. |
| decide | `load.classify` | bucket `human` (no AI evidence), `automate` (automation share ≥ 55%), `augment` (the rest); `priority` = importance/5 × evidence × time-saved factor (capped ×4). |
| act | myapps `/jobs/` + the **pilot builder** (`portal/pilot_builder.py` in myapps; migration `work_002`) | look up any job title (`work.role` rows are created on demand when a looked-up occupation gets a pilot; nothing is seeded), the split by importance-weighted share and the pilot candidates; the owner marks pilots started / done / dropped. |
| score | `work.pilot` + `work.pilot_event` | status per pilot, hours saved per month reported by the lead. |

**Falsifiable goal:** at least 3 pilots marked `started` across the three functions within 90 days
of the first role report (by 2026-12-16). Measured from `work.pilot`; if it fails, the split rule
or the role seeds are wrong and get revised, not the goal.

Schema: `migrations/work_001_job_audit.sql` (applied by postgres-po 2026-09-17). Writers:
`load.py` as livingos; the page as role `myapps_work` (pilot verdict columns only).

Refresh: rerun `load.py all` when a new Economic Index release lands (`AEI_RELEASE=YYYY-MM-DD`)
or a new O*NET database version; the loader is idempotent and recomputes buckets.

Tests: `python3 -m pytest -q tests/` (pure rules and parsers; no DB, no network).

## The pilot builder (work_002, 2026-09-17)

"Pilot this" no longer just files a row. Method from the Cooper Simson reel `DdVargvpEQ8`,
the AI delegation loop: **interview** the function until the task could run without the
person -> draft the **playbook** the task runs on -> keep a **toolbox** of templates and
scripts the playbook links to -> wire a **proof** list so the agent checks its own work
before hand-back -> when it goes wrong, fix the playbook, not the chat.

| Step | Who | Where |
|---|---|---|
| Evidence | deterministic | `task.aei_profile` rendered as facts (penetration, automation share, autonomy, minutes alone vs with AI, artifact types, collaboration patterns) |
| Interview | the owner, 8 fixed questions | `pilot.interview` (jsonb) |
| Draft | ONE model call through the livingos-api gateway (`bare-sonnet` by default; key `myapps-jobs`, scope generic) | returns JSON; `validate_draft` is the deterministic gate: all 9 playbook sections, ≥ 3 proof checks, ≥ 2 steps, numeric baseline |
| Playbook | markdown in `pilot.playbook`, checks in `pilot.proof`, numbers in `pilot.baseline` | owner edits on the page ("corrections fix the playbook") |
| Start gate | deterministic | a pilot can only move to `started` with a playbook and ≥ 3 proof checks |

Every step appends to `work.pilot_event` (`interviewed`, `drafted`, `edited`). The model
proposes; nothing it writes reaches the row without passing the validator, and nothing it
writes can start a pilot.
