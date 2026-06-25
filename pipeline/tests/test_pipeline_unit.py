"""Unit tests for the receiver's normalization + payload readers.

These lock the live (webhook) deterministic path: occurred_at must be a real
datetime (it lands in a timestamptz column), and a GitHub `push` payload must
yield its changed files even though it isn't PR-shaped.
"""
import datetime as dt

import pipeline


def _push_payload() -> dict:
    return {
        "head_commit": {"timestamp": "2026-06-24T12:00:00Z"},
        "pusher": {"name": "alice"},
        "compare": "https://github.com/o/r/compare/a...b",
        "commits": [
            {"added": ["a.py"], "modified": ["b.py"], "removed": []},
            {"added": [], "modified": ["b.py", "c.py"], "removed": ["d.py"]},
        ],
    }


# ---- normalize_github: occurred_at must be a tz-aware datetime --------------
def test_normalize_push_returns_datetime_and_actor():
    et, occurred, actor, url = pipeline.normalize_github("push", _push_payload())
    assert et == "commit.pushed"
    assert isinstance(occurred, dt.datetime)
    assert occurred.tzinfo is not None
    assert occurred == dt.datetime(2026, 6, 24, 12, 0, 0, tzinfo=dt.timezone.utc)
    assert actor == "alice"


def test_normalize_pr_merged_returns_datetime():
    payload = {
        "action": "closed",
        "pull_request": {
            "merged": True,
            "merged_at": "2026-01-02T03:04:05Z",
            "user": {"login": "bob"},
            "html_url": "https://github.com/o/r/pull/7",
        },
    }
    et, occurred, actor, url = pipeline.normalize_github("pull_request", payload)
    assert et == "pr.merged"
    assert isinstance(occurred, dt.datetime)
    assert occurred == dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.timezone.utc)
    assert actor == "bob"
    assert url.endswith("/pull/7")


def test_normalize_push_missing_head_commit_falls_back_to_datetime():
    p = _push_payload()
    p["head_commit"] = None  # branch delete / tag push → no head_commit
    et, occurred, actor, url = pipeline.normalize_github("push", p)
    assert et == "commit.pushed"
    assert isinstance(occurred, dt.datetime)
    assert actor == "alice"


# ---- changed_files / file_churn across payload shapes ----------------------
def test_changed_files_push_unions_added_modified_removed_deduped():
    assert pipeline.changed_files(_push_payload()) == ["a.py", "b.py", "c.py", "d.py"]


def test_changed_files_files_shape_backfill():
    payload = {"files": [{"filename": "x.py", "additions": 3, "deletions": 1}]}
    assert pipeline.changed_files(payload) == ["x.py"]


def test_file_churn_files_shape():
    payload = {"files": [{"filename": "x.py", "additions": 3, "deletions": 1}]}
    assert pipeline.file_churn(payload, "x.py") == (3, 1)


def test_file_churn_push_has_no_line_counts():
    # Push payloads carry no per-file line counts → churn 0 (commit_count still
    # increments in apply_metrics).
    assert pipeline.file_churn(_push_payload(), "a.py") == (0, 0)


# ---- §5 co-change cap: big commits (initial import / large refactor) skip ---
def test_cochange_skip_threshold():
    cap = pipeline.COCHANGE_MAX_FILES
    assert pipeline.cochange_skip(["f"] * (cap + 1)) is True
    assert pipeline.cochange_skip(["f"] * cap) is False
    assert pipeline.cochange_skip(["a", "b"]) is False
    assert pipeline.cochange_skip([]) is False


# ---- §7.6 transport-independent commit identity: push fan-out per commit ----
def test_fan_out_push_one_event_per_commit_keyed_by_sha():
    import datetime as dt
    p = {
        "repository": {"full_name": "o/r"},
        "commits": [
            {"id": "sha1", "url": "u1", "timestamp": "2026-06-24T12:00:00Z",
             "author": {"email": "a@x.com", "username": "a"},
             "added": ["x.py"], "modified": ["y.py"], "removed": []},
            {"id": "sha2", "url": "u2", "timestamp": "2026-06-24T12:05:00Z",
             "author": {"email": "b@x.com"}, "added": [], "modified": ["y.py"], "removed": ["z.py"]},
        ],
    }
    evs = pipeline.fan_out_github("push", p, "delivery-123")
    assert len(evs) == 2
    assert all(e["source"] == "git" for e in evs)
    assert [e["source_event_id"] for e in evs] == ["sha1", "sha2"]
    assert all(e["event_type"] == "commit.pushed" for e in evs)
    assert isinstance(evs[0]["occurred_at"], dt.datetime)
    assert evs[0]["actor"] == "a@x.com"
    assert pipeline.changed_files(evs[0]["payload"]) == ["x.py", "y.py"]
    assert pipeline.changed_files(evs[1]["payload"]) == ["y.py", "z.py"]


def test_fan_out_non_push_single_event_keyed_by_delivery():
    p = {"action": "closed", "pull_request": {"merged": True, "merged_at": "2026-01-02T03:04:05Z",
         "user": {"login": "bob"}, "html_url": "h", "number": 7}}
    evs = pipeline.fan_out_github("pull_request", p, "delivery-xyz")
    assert len(evs) == 1
    assert evs[0]["source"] == "github"
    assert evs[0]["source_event_id"] == "delivery-xyz"
    assert evs[0]["event_type"] == "pr.merged"


def test_fan_out_push_skips_commit_without_sha():
    p = {"commits": [
        {"id": "sha1", "timestamp": "2026-06-24T12:00:00Z", "added": ["a"], "modified": [], "removed": []},
        {"added": ["b"]},  # no id → skipped
    ]}
    evs = pipeline.fan_out_github("push", p, "d")
    assert [e["source_event_id"] for e in evs] == ["sha1"]
