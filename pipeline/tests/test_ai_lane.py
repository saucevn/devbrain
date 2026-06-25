"""Phase 2A AI-lane helpers — pure functions, no network / no API keys:
Gemini embedding parse+normalize, linked-issue extraction, PR enrichment build."""
import math

import pipeline


def test_normalize_unit_length():
    v = pipeline._normalize([3.0, 4.0])
    assert math.isclose(math.sqrt(sum(x * x for x in v)), 1.0, rel_tol=1e-9)
    assert math.isclose(v[0], 0.6) and math.isclose(v[1], 0.8)


def test_normalize_zero_vector_stays_zero():
    assert pipeline._normalize([0.0, 0.0]) == [0.0, 0.0]


def test_parse_gemini_embedding():
    assert pipeline._parse_gemini_embedding(
        {"embedding": {"values": [0.1, 0.2, 0.3]}}
    ) == [0.1, 0.2, 0.3]


def test_extract_linked_issues_dedupes_and_orders():
    body = "This fixes #12 and Closes #34. Also fix #12 again. resolves #7"
    assert pipeline.extract_linked_issues(body) == ["#12", "#34", "#7"]


def test_extract_linked_issues_empty():
    assert pipeline.extract_linked_issues("") == []
    assert pipeline.extract_linked_issues(None) == []
    assert pipeline.extract_linked_issues("a bare #5 with no keyword") == []


def test_build_enriched_pr_injects_fields_without_mutating_original():
    payload = {
        "pull_request": {"number": 5, "title": "t", "body": "Fixes #9"},
        "repository": {"full_name": "o/r"},
    }
    files = [
        {"filename": "a.py", "additions": 3, "deletions": 1},
        {"filename": "b.py", "additions": 0, "deletions": 2},
    ]
    enriched = pipeline._build_enriched_pr(payload, files, "DIFFTEXT", ["#9"])
    assert "_files" not in payload["pull_request"]          # original untouched
    pr = enriched["pull_request"]
    assert pr["_diff"] == "DIFFTEXT"
    assert pr["_linked_issues"] == ["#9"]
    assert [f["filename"] for f in pr["_files"]] == ["a.py", "b.py"]
    assert pipeline.changed_files(enriched) == ["a.py", "b.py"]
    assert pipeline.file_churn(enriched, "a.py") == (3, 1)
    parsed = pipeline.parse_pr(enriched)
    assert parsed["diff"] == "DIFFTEXT"
    assert parsed["issues"] == ["#9"]
    assert parsed["files"] == ["a.py", "b.py"]
