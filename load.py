#!/usr/bin/env python3
"""job-audit load — ACQUIRE, STORE and DECIDE stages of the job task-audit loop.

Sources (public, deterministic):
  O*NET 30.0 database (text)        occupations, task statements, task importance (IM)
  Anthropic Economic Index          labor_market_impacts/job_exposure.csv (per SOC),
                                    (every other per-task metric — artifact types, collaboration
                                    patterns, use cases — lands in task.aei_profile for the pilot builder)
                                    labor_market_impacts/task_penetration.csv (per statement),
                                    release_<date>/data/aei_1p_api_<date>.csv and
                                    aei_claude_ai_<date>.csv (per O*NET task: usage share,
                                    automation/augmentation split, autonomy, times; blended)

Decide rule (per task, pure function `classify`):
  human     no AEI usage row AND penetration < 0.05  (AI is not doing this task yet)
  automate  automation_pct >= 55
  augment   otherwise (augmentation-led or mixed)
  priority  = importance/5  x  max(penetration, usage share scaled)  x  time-saved factor
             time-saved factor = 1 + minutes saved per occurrence / 60, capped at 4

Usage:
  load.py fetch              download the three sources into ~/data/job-audit/
  load.py load               parse + upsert work.occupation / work.task (idempotent)
  load.py seed-roles         insert the GBS function roles (idempotent on function+soc)
  load.py load-titles        parse every known title per occupation into work.title (the job-list matcher)
  load.py all                fetch + load + seed-roles + load-titles

Env: PG_DSN (livingos writer). Stdlib + psycopg only.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import os
import sys
import urllib.request
import zipfile
from pathlib import Path

import psycopg

DATA = Path(os.environ.get("JOB_AUDIT_DATA", os.path.expanduser("~/data/job-audit")))
ONET_ZIP = "https://www.onetcenter.org/dl_files/database/db_30_0_text.zip"
AEI_BASE = "https://huggingface.co/datasets/Anthropic/EconomicIndex/resolve/main"
AEI_RELEASE = os.environ.get("AEI_RELEASE", "2026-06-26")
AEI_FILES = {
    "job_exposure.csv": f"{AEI_BASE}/labor_market_impacts/job_exposure.csv",
    "task_penetration.csv": f"{AEI_BASE}/labor_market_impacts/task_penetration.csv",
    "aei_1p_api.csv": f"{AEI_BASE}/release_{AEI_RELEASE.replace('-', '_')}/data/aei_1p_api_{AEI_RELEASE}.csv",
    "aei_claude_ai.csv": f"{AEI_BASE}/release_{AEI_RELEASE.replace('-', '_')}/data/aei_claude_ai_{AEI_RELEASE}.csv",
}
# Both usage sources are blended per task (mean of the metrics each source publishes): the 1P API
# release is automation-heavy by construction (~77% automation), the Claude.ai release sits near
# 50/50; a job audit for knowledge workers needs both views, not one. usage_pct keeps the max.
AEI_SOURCES = ("aei_1p_api.csv", "aei_claude_ai.csv")
ONET_MEMBERS = ("Occupation Data.txt", "Task Statements.txt", "Task Ratings.txt",
                "Alternate Titles.txt", "Sample of Reported Titles.txt")
AEI_METRICS = {
    "pct": "usage_pct",
    "collaboration_bucket_automation_pct": "automation_pct",
    "collaboration_bucket_augmentation_pct": "augmentation_pct",
    "ai_autonomy_mean": "ai_autonomy_mean",
    "human_only_time_mean": "human_only_time_hours",
    "human_with_ai_time_mean": "human_with_ai_time_min",
}
AUTOMATE_AT = 55.0
HUMAN_BELOW = 0.05

# The functions and the O*NET occupations they are made of. Owner-editable in work.role.
# Lead names are NOT kept in code: set work.role.lead in the database (private), never here (public repo).
ROLE_SEED = [
    ("HR / Workforce", None, "Human Resources Specialists", "13-1071"),
    ("HR / Workforce", None, "Human Resources Managers", "11-3121"),
    ("HR / Workforce", None, "Compensation, Benefits, and Job Analysis Specialists", "13-1141"),
    ("HR / Workforce", None, "Training and Development Specialists", "13-1151"),
    ("HR / Workforce", None, "Human Resources Assistants, Except Payroll and Timekeeping", "43-4161"),
    ("HR / Workforce", None, "Labor Relations Specialists", "13-1075"),
    ("License ops / IT procurement", None, "Purchasing Agents, Except Wholesale, Retail, and Farm Products", "13-1023"),
    ("License ops / IT procurement", None, "Purchasing Managers", "11-3061"),
    ("License ops / IT procurement", None, "Computer Systems Analysts", "15-1211"),
    ("License ops / IT procurement", None, "Computer User Support Specialists", "15-1232"),
    ("GTS Infrastructure", None, "Network and Computer Systems Administrators", "15-1244"),
    ("GTS Infrastructure", None, "Computer Network Architects", "15-1241"),
    ("GTS Infrastructure", None, "Computer Network Support Specialists", "15-1231"),
    ("GTS Infrastructure", None, "Database Administrators", "15-1242"),
    ("GTS Infrastructure", None, "Information Security Analysts", "15-1212"),
    ("GTS Infrastructure", None, "Computer and Information Systems Managers", "11-3021"),
    ("GTS Infrastructure", None, "Software Developers", "15-1252"),
]


# ---- pure helpers (unit-tested) ------------------------------------------------

def soc(code: str) -> str:
    """'15-1244.00' -> '15-1244'. Specialty codes ('15-1299.08') keep the parent."""
    return code.split(".", 1)[0].strip()


def classify(importance: float | None, penetration: float | None, usage_pct: float | None,
             automation_pct: float | None, human_only_hours: float | None,
             human_with_ai_min: float | None) -> tuple[str, float]:
    """Return (bucket, priority). Deterministic; documented in the module docstring."""
    pen = float(penetration or 0.0)
    usage = float(usage_pct or 0.0)
    has_aei = usage_pct is not None
    if not has_aei and pen < HUMAN_BELOW:
        bucket = "human"
    elif (automation_pct or 0.0) >= AUTOMATE_AT:
        bucket = "automate"
    else:
        bucket = "augment"
    imp = (float(importance) if importance is not None else 3.0) / 5.0
    # usage share is a percent of ALL API usage; 0.10% is already a heavily used task.
    evidence = max(pen, min(usage / 0.10, 1.0))
    saved_min = 0.0
    if human_only_hours is not None and human_with_ai_min is not None:
        saved_min = max(float(human_only_hours) * 60.0 - float(human_with_ai_min), 0.0)
    time_factor = min(1.0 + saved_min / 60.0, 4.0)
    priority = round(imp * evidence * time_factor, 4) if bucket != "human" else round(imp * 0.05, 4)
    return bucket, priority


def parse_onet(folder: Path) -> tuple[dict, dict, dict]:
    """occupations {soc: title}, tasks {task_id: row}, importance {task_id: IM}."""
    occ, tasks, imp = {}, {}, {}
    with open(folder / "Occupation Data.txt", encoding="utf-8") as f:
        for row in csv.DictReader(f, delimiter="\t"):
            occ.setdefault(soc(row["O*NET-SOC Code"]), row["Title"])
    with open(folder / "Task Statements.txt", encoding="utf-8") as f:
        for row in csv.DictReader(f, delimiter="\t"):
            tasks[int(row["Task ID"])] = {"soc": soc(row["O*NET-SOC Code"]), "statement": row["Task"].strip(),
                                         "type": row.get("Task Type") or None}
    with open(folder / "Task Ratings.txt", encoding="utf-8") as f:
        for row in csv.DictReader(f, delimiter="\t"):
            if row["Scale ID"] == "IM":
                imp[int(row["Task ID"])] = float(row["Data Value"])
    return occ, tasks, imp


def parse_release(path: Path) -> dict:
    """{task_id: {col: value}} from the GLOBAL, level-0 O*NET rows of the latest period in one release file."""
    metrics, latest = {}, None
    with open(path) as f:
        for row in csv.DictReader(f):
            if row["category_name"] != "onet" or row["hierarchy_level"] != "0" or row["geo_id"] != "GLOBAL":
                continue
            col = AEI_METRICS.get(row["metric_id"], "profile:" + row["metric_id"])
            if latest is None or row["date_start"] > latest:
                if latest is not None:
                    metrics = {}
                latest = row["date_start"]
            if row["date_start"] != latest:
                continue
            try:
                tid = int(row["node_external_id"])
            except ValueError:
                continue
            metrics.setdefault(tid, {})[col] = float(row["value"])
    return metrics


def blend(sources: list[dict]) -> dict:
    """Pure: per task, mean of each metric across the sources that publish it; usage_pct = max."""
    out: dict = {}
    for src in sources:
        for tid, m in src.items():
            acc = out.setdefault(tid, {})
            for col, v in m.items():
                acc.setdefault(col, []).append(v)
    return {tid: {col: (max(vs) if col == "usage_pct" else sum(vs) / len(vs)) for col, vs in m.items()}
            for tid, m in out.items()}


def parse_aei(folder: Path) -> tuple[dict, dict, dict]:
    """exposure {soc: value}, penetration {statement: value}, blended task metrics {task_id: {col: value}}."""
    exposure, pen = {}, {}
    with open(folder / "job_exposure.csv") as f:
        for row in csv.DictReader(f):
            exposure[row["occ_code"].strip()] = float(row["observed_exposure"])
    with open(folder / "task_penetration.csv") as f:
        for row in csv.DictReader(f):
            pen[row["task"].strip()] = float(row["penetration"])
    present = [folder / name for name in AEI_SOURCES if (folder / name).exists()]
    if not present:
        raise RuntimeError(f"no AEI release file under {folder} (expected {AEI_SOURCES})")
    metrics = blend([parse_release(p) for p in present])
    return exposure, pen, metrics


# ---- stages --------------------------------------------------------------------

def fetch(log) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    onet_dir = DATA / "onet"
    if not all((onet_dir / m).exists() for m in ONET_MEMBERS):
        log(f"  fetch O*NET {ONET_ZIP}")
        with urllib.request.urlopen(ONET_ZIP, timeout=300) as r:
            blob = r.read()
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            onet_dir.mkdir(exist_ok=True)
            for m in ONET_MEMBERS:
                name = next(n for n in z.namelist() if n.endswith("/" + m))
                (onet_dir / m).write_bytes(z.read(name))
    for name, url in AEI_FILES.items():
        dest = DATA / name
        if dest.exists() and dest.stat().st_size > 0:
            continue
        log(f"  fetch {url}")
        with urllib.request.urlopen(url, timeout=600) as r, open(dest, "wb") as out:
            while chunk := r.read(1 << 20):
                out.write(chunk)
    log(f"  sources present under {DATA}")


def load(conn, log) -> dict:
    occ, tasks, imp = parse_onet(DATA / "onet")
    exposure, pen, metrics = parse_aei(DATA)
    with conn.cursor() as cur:
        cur.executemany("""INSERT INTO work.occupation (soc_code, title, observed_exposure, loaded_at)
                           VALUES (%s, %s, %s, now())
                           ON CONFLICT (soc_code) DO UPDATE SET title = EXCLUDED.title,
                             observed_exposure = EXCLUDED.observed_exposure, loaded_at = now()""",
                        [(s, t, exposure.get(s)) for s, t in occ.items()])
        rows, counts = [], {"automate": 0, "augment": 0, "human": 0}
        for tid, t in tasks.items():
            if t["soc"] not in occ:
                continue
            m = metrics.get(tid, {})
            p = pen.get(t["statement"])
            bucket, prio = classify(imp.get(tid), p, m.get("usage_pct"), m.get("automation_pct"),
                                    m.get("human_only_time_hours"), m.get("human_with_ai_time_min"))
            counts[bucket] += 1
            profile = {k[8:]: round(v, 2) for k, v in m.items() if k.startswith("profile:")}
            rows.append((tid, t["soc"], t["statement"], t["type"], imp.get(tid), p, m.get("usage_pct"),
                         m.get("automation_pct"), m.get("augmentation_pct"), m.get("ai_autonomy_mean"),
                         (m["human_only_time_hours"] * 60.0) if "human_only_time_hours" in m else None,
                         m.get("human_with_ai_time_min"), bucket, prio,
                         json.dumps(profile) if profile else None))
        cur.executemany("""INSERT INTO work.task (task_id, soc_code, statement, task_type, importance, penetration,
                             usage_pct, automation_pct, augmentation_pct, ai_autonomy_mean,
                             human_only_time_min, human_with_ai_time_min, bucket, priority, aei_profile, loaded_at)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb, now())
                           ON CONFLICT (task_id) DO UPDATE SET
                             soc_code = EXCLUDED.soc_code, statement = EXCLUDED.statement,
                             task_type = EXCLUDED.task_type, importance = EXCLUDED.importance,
                             penetration = EXCLUDED.penetration, usage_pct = EXCLUDED.usage_pct,
                             automation_pct = EXCLUDED.automation_pct, augmentation_pct = EXCLUDED.augmentation_pct,
                             ai_autonomy_mean = EXCLUDED.ai_autonomy_mean,
                             human_only_time_min = EXCLUDED.human_only_time_min,
                             human_with_ai_time_min = EXCLUDED.human_with_ai_time_min,
                             bucket = EXCLUDED.bucket, priority = EXCLUDED.priority,
                             aei_profile = EXCLUDED.aei_profile, loaded_at = now()""", rows)
    conn.commit()
    summary = {"occupations": len(occ), "tasks": len(rows), "with_aei_metrics": sum(1 for r in rows if r[6] is not None),
               "with_penetration": sum(1 for r in rows if r[5] is not None), **counts}
    log(f"  loaded {summary}")
    if summary["with_aei_metrics"] == 0:
        raise RuntimeError("0 tasks carry AEI metrics — the join or the release path is wrong, not an empty index")
    return summary


def norm_title(t: str) -> str:
    """Pure: the matcher key. Lowercase, punctuation to spaces, single spaces, no leading/trailing."""
    import re
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (t or "").lower())).strip()


def parse_titles(folder: Path) -> list[tuple[str, str, str, str]]:
    """(soc, title, title_norm, source) from Occupation Data, Alternate Titles, Sample of Reported Titles."""
    out, seen = [], set()

    def add(code, title, source):
        t = (title or "").strip()
        n = norm_title(t)
        if not n or len(n) < 3:
            return
        key = (soc(code), n, source)
        if key in seen:
            return
        seen.add(key)
        out.append((soc(code), t[:200], n[:200], source))

    with open(folder / "Occupation Data.txt", encoding="utf-8") as f:
        for row in csv.DictReader(f, delimiter="\t"):
            add(row["O*NET-SOC Code"], row["Title"], "onet")
    with open(folder / "Alternate Titles.txt", encoding="utf-8") as f:
        for row in csv.DictReader(f, delimiter="\t"):
            add(row["O*NET-SOC Code"], row["Alternate Title"], "alternate")
            if row.get("Short Title") and row["Short Title"] != "n/a":
                add(row["O*NET-SOC Code"], row["Short Title"], "alternate")
    with open(folder / "Sample of Reported Titles.txt", encoding="utf-8") as f:
        for row in csv.DictReader(f, delimiter="\t"):
            add(row["O*NET-SOC Code"], row["Reported Job Title"], "reported")
    return out


def load_titles(conn, log) -> int:
    rows = parse_titles(DATA / "onet")
    with conn.cursor() as cur:
        cur.execute("SELECT soc_code FROM work.occupation")
        known = {r[0] for r in cur.fetchall()}
        rows = [r for r in rows if r[0] in known]
        cur.executemany("""INSERT INTO work.title (soc_code, title, title_norm, source) VALUES (%s, %s, %s, %s)
                           ON CONFLICT (soc_code, title_norm, source) DO NOTHING""", rows)
    conn.commit()
    log(f"  titles: {len(rows)} known titles across {len({r[0] for r in rows})} occupations")
    if len(rows) < 10000:
        raise RuntimeError(f"only {len(rows)} titles parsed — the O*NET title files are missing or truncated")
    return len(rows)


def seed_roles(conn, log) -> int:
    n = 0
    with conn.cursor() as cur:
        for function, lead, title, code in ROLE_SEED:
            cur.execute("SELECT 1 FROM work.occupation WHERE soc_code = %s", (code,))
            if not cur.fetchone():
                log(f"  seed: {code} {title} is not in work.occupation — skipped (fix the code)")
                continue
            cur.execute("""INSERT INTO work.role (function, lead, title, soc_code)
                           VALUES (%s, %s, %s, %s) ON CONFLICT (function, soc_code) DO NOTHING""",
                        (function, lead, title, code))
            n += cur.rowcount
    conn.commit()
    log(f"  seeded {n} new role(s)")
    return n


def main(argv: list[str]) -> int:
    stage = argv[1] if len(argv) > 1 else "all"
    if stage not in ("fetch", "load", "seed-roles", "load-titles", "all"):
        print(__doc__, file=sys.stderr)
        return 2
    log = lambda s: print(s, file=sys.stderr)  # noqa: E731
    if stage in ("fetch", "all"):
        fetch(log)
    if stage in ("load", "seed-roles", "load-titles", "all"):
        with psycopg.connect(os.environ["PG_DSN"]) as conn:
            if stage in ("load", "all"):
                load(conn, log)
            if stage in ("seed-roles", "all"):
                seed_roles(conn, log)
            if stage in ("load-titles", "all"):
                load_titles(conn, log)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
