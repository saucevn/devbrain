"""Unit tests for PR backfill — pure pr_to_event mapping (no network/keys).

Mirrors test_backfill_gitlog.py style: characterize the deterministic shaping
of a GitHub PR object into a `pr.merged` event row. The AI work (enrich →
summarize → embed) is the running worker's job and is NOT exercised here.
"""
from datetime import datetime, timezone

import backfill_prs


def _sample_pr():
    return {
        "number": 47,
        "title": "feat(bbf): schema da ky income_settlements",
        "body": "Sprint 7. Fixes #12",
        "merged_at": "2026-06-17T08:30:00Z",
        "html_url": "https://github.com/saucevn/bbf/pull/47",
        "user": {"login": "thao"},
    }


def test_pr_to_event_shape():
    ev = backfill_prs.pr_to_event(_sample_pr(), "saucevn/bbf")
    assert ev["source"] == "github"
    # Stable, transport-independent id (NOT a random delivery uuid) → re-runs
    # and a later real webhook for the same merge both dedupe to one narrative.
    assert ev["source_event_id"] == "pr-merged:saucevn/bbf#47"
    assert ev["event_type"] == "pr.merged"
    assert ev["actor"] == "thao"
    assert ev["source_url"] == "https://github.com/saucevn/bbf/pull/47"

    pr = ev["payload"]["pull_request"]
    assert pr["number"] == 47
    assert pr["title"].startswith("feat(bbf)")
    assert pr["body"] == "Sprint 7. Fixes #12"
    assert pr["merged"] is True
    assert ev["payload"]["repository"]["full_name"] == "saucevn/bbf"


def test_pr_to_event_occurred_at_is_tz_aware():
    ev = backfill_prs.pr_to_event(_sample_pr(), "saucevn/bbf")
    assert ev["occurred_at"] == datetime(2026, 6, 17, 8, 30, tzinfo=timezone.utc)


def test_pr_to_event_tolerates_missing_body_and_user():
    pr = {"number": 5, "title": "t", "merged_at": "2026-01-01T00:00:00Z",
          "html_url": "https://github.com/o/r/pull/5"}
    ev = backfill_prs.pr_to_event(pr, "o/r")
    # parse_pr / enrich expect strings, not None.
    assert ev["payload"]["pull_request"]["body"] == ""
    assert ev["actor"] is None
    assert ev["payload"]["pull_request"]["user"] == {}


def test_pr_to_event_payload_feeds_enrichment_and_gate():
    """The shaped payload must satisfy the exact fields the worker's narrative
    lane reads: enrich_pr_payload needs repository.full_name + pull_request.number;
    passes_ai_gate keys on event_type=='pr.merged' + actor (bot filter)."""
    import pipeline
    ev = backfill_prs.pr_to_event(_sample_pr(), "saucevn/bbf")
    p = ev["payload"]
    assert (p["repository"]["full_name"]) == "saucevn/bbf"
    assert p["pull_request"]["number"] == 47
    # Un-enriched pr.merged carries no files → deterministic lanes no-op (no
    # double-count with commit-level backfill).
    assert pipeline.changed_files(p) == []


def test_only_merged_prs_kept():
    closed_not_merged = {"number": 9, "merged_at": None}
    merged = {"number": 10, "merged_at": "2026-01-01T00:00:00Z"}
    kept = backfill_prs.filter_merged([closed_not_merged, merged])
    assert [p["number"] for p in kept] == [10]
