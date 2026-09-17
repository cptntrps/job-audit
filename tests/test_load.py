"""Unit tests for the pure parts of job-audit load: SOC normalization, the decide rule,
and the O*NET / AEI parsers on tiny fixtures. No DB, no network."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import load  # noqa: E402


def test_soc_strips_onet_suffix():
    assert load.soc("15-1244.00") == "15-1244"
    assert load.soc("15-1299.08") == "15-1299"
    assert load.soc("13-1071") == "13-1071"


def test_classify_buckets():
    # no AEI row, no penetration -> human, tiny priority
    assert load.classify(4.0, None, None, None, None, None) == ("human", 0.04)
    # automation-led -> automate
    b, p = load.classify(4.0, 0.5, 0.2, 70.0, 1.0, 10.0)
    assert b == "automate" and p > 0
    # augmentation-led -> augment
    assert load.classify(4.0, 0.5, 0.2, 30.0, None, None)[0] == "augment"
    # penetration alone (no AEI usage row) still counts as evidence
    assert load.classify(3.0, 0.3, None, None, None, None)[0] == "augment"


def test_priority_grows_with_importance_evidence_and_time_saved():
    low = load.classify(2.0, 0.2, None, 70.0, None, None)[1]
    high_imp = load.classify(5.0, 0.2, None, 70.0, None, None)[1]
    high_evd = load.classify(2.0, 0.9, None, 70.0, None, None)[1]
    saved = load.classify(2.0, 0.2, None, 70.0, 2.0, 10.0)[1]      # 110 min saved -> factor 2.83
    assert high_imp > low and high_evd > low and saved > low
    # time factor is capped at 4
    capped = load.classify(5.0, 1.0, None, 70.0, 100.0, 1.0)[1]
    assert capped == 4.0


def test_parsers_on_fixtures(tmp_path):
    onet = tmp_path / "onet"
    onet.mkdir()
    (onet / "Occupation Data.txt").write_text("O*NET-SOC Code\tTitle\tDescription\n13-1071.00\tHuman Resources Specialists\tx\n")
    (onet / "Task Statements.txt").write_text("O*NET-SOC Code\tTask ID\tTask\tTask Type\n13-1071.00\t1\tScreen applicants.\tCore\n")
    (onet / "Task Ratings.txt").write_text("O*NET-SOC Code\tTask ID\tScale ID\tCategory\tData Value\n13-1071.00\t1\tIM\tn/a\t4.2\n13-1071.00\t1\tFT\t1\t5.0\n")
    occ, tasks, imp = load.parse_onet(onet)
    assert occ == {"13-1071": "Human Resources Specialists"}
    assert tasks[1]["statement"] == "Screen applicants." and imp[1] == 4.2
    (tmp_path / "job_exposure.csv").write_text("occ_code,title,observed_exposure\n13-1071,HR,0.31\n")
    (tmp_path / "task_penetration.csv").write_text('task,penetration\n"Screen applicants.",0.42\n')
    hdr = "date_start,date_end,geo_id,geo_level,category_name,hierarchy_level,metric_id,value,node_name,node_external_id\n"
    (tmp_path / "aei_1p_api.csv").write_text(
        hdr + "2026-04-01,2026-05-01,GLOBAL,global,onet,0,pct,0.30,Screen applicants.,1\n"
              "2026-05-01,2026-06-01,GLOBAL,global,onet,0,pct,0.25,Screen applicants.,1\n"
              "2026-05-01,2026-06-01,GLOBAL,global,onet,0,collaboration_bucket_automation_pct,61.0,Screen applicants.,1\n"
              "2026-05-01,2026-06-01,USA,country,onet,0,pct,9.9,Screen applicants.,1\n"
              "2026-05-01,2026-06-01,GLOBAL,global,onet,1,pct,9.9,DWA,4.A.1\n")
    exposure, pen, metrics = load.parse_aei(tmp_path)
    assert exposure == {"13-1071": 0.31} and pen == {"Screen applicants.": 0.42}
    assert metrics == {1: {"usage_pct": 0.25, "automation_pct": 61.0}}   # latest period, GLOBAL, level 0 only
    # an unmapped metric rides along as a profile entry for the pilot builder
    with open(tmp_path / "aei_1p_api.csv", "a") as f:
        f.write("2026-05-01,2026-06-01,GLOBAL,global,onet,0,artifact_document_or_report_pct,44.0,Screen applicants.,1\n")
    assert load.parse_aei(tmp_path)[2][1]["profile:artifact_document_or_report_pct"] == 44.0
    # a second source blends: automation is the mean, usage the max, a metric one source lacks passes through
    (tmp_path / "aei_claude_ai.csv").write_text(
        hdr + "2026-05-01,2026-06-01,GLOBAL,global,onet,0,pct,0.10,Screen applicants.,1\n"
              "2026-05-01,2026-06-01,GLOBAL,global,onet,0,collaboration_bucket_automation_pct,41.0,Screen applicants.,1\n"
              "2026-05-01,2026-06-01,GLOBAL,global,onet,0,ai_autonomy_mean,3.0,Screen applicants.,1\n")
    _, _, blended = load.parse_aei(tmp_path)
    assert blended == {1: {"usage_pct": 0.25, "automation_pct": 51.0, "ai_autonomy_mean": 3.0,
                           "profile:artifact_document_or_report_pct": 44.0}}


def test_blend_is_mean_except_usage_max():
    a = {1: {"usage_pct": 0.2, "automation_pct": 80.0}}
    b = {1: {"usage_pct": 0.5, "automation_pct": 40.0}, 2: {"automation_pct": 10.0}}
    assert load.blend([a, b]) == {1: {"usage_pct": 0.5, "automation_pct": 60.0}, 2: {"automation_pct": 10.0}}


def test_norm_title_and_parse_titles(tmp_path):
    assert load.norm_title("  Sr. HR Business-Partner (EMEA) ") == "sr hr business partner emea"
    onet = tmp_path / "onet"; onet.mkdir()
    (onet / "Occupation Data.txt").write_text("O*NET-SOC Code\tTitle\tDescription\n13-1071.00\tHuman Resources Specialists\tx\n")
    (onet / "Alternate Titles.txt").write_text("O*NET-SOC Code\tAlternate Title\tShort Title\tSource(s)\n13-1071.00\tHR Generalist\tn/a\t08\n13-1071.00\tHuman Resources Coordinator\tHR Coordinator\t08\n")
    (onet / "Sample of Reported Titles.txt").write_text("O*NET-SOC Code\tReported Job Title\tShown in My Next Move\n13-1071.00\tHR Generalist\tY\n13-1071.00\tRecruiter\tY\n")
    rows = load.parse_titles(onet)
    assert ("13-1071", "HR Coordinator", "hr coordinator", "alternate") in rows
    assert sum(1 for r in rows if r[2] == "hr generalist") == 2          # same title from two sources both kept
    assert len(rows) == 6
