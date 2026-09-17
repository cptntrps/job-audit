-- job-audit 004: lookup instead of pre-populated roles (owner ask 2026-09-17: "I don't want
-- pre-populated, I want to write one and see"). The page creates a work.role row on demand
-- when the owner pilots a looked-up occupation; nothing is seeded any more (load.py seed-roles
-- removed). Additive grant only.

GRANT INSERT (function, lead, title, soc_code, headcount, note) ON work.role TO myapps_work;
GRANT UPDATE (function, lead, headcount, note) ON work.role TO myapps_work;
GRANT USAGE, SELECT ON SEQUENCE work.role_id_seq TO myapps_work;
