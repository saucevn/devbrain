"""Regression test for the narrative projector's DB write shaping.

Bug: highlights was inserted via _json(analysis.highlights) (json.dumps → a
text string), which the jsonb codec then re-encoded into a jsonb *string*
scalar, not an array — so `jsonb_array_length(highlights)` failed. The fix
passes the list directly so the codec writes a real jsonb array.

We assert the value handed to the narratives INSERT is a Python list (the
jsonb codec turns a list into a jsonb array). A fake connection captures the
args without touching a real database.
"""
import asyncio
from datetime import datetime, timezone

import pipeline


class _FakeConn:
    """Records (query, args) for every fetchval/execute; returns a stub id."""

    def __init__(self):
        self.fetchvals: list[tuple[str, tuple]] = []
        self.executes: list[tuple[str, tuple]] = []

    async def fetchval(self, query, *args):
        self.fetchvals.append((query, args))
        return "00000000-0000-0000-0000-000000000009"

    async def execute(self, query, *args):
        self.executes.append((query, args))
        return "OK"


def _enriched():
    return {
        "pull_request": {
            "number": 7, "title": "t", "body": "",
            "_files": [], "_linked_issues": [], "_diff": "",
        },
        "repository": {"full_name": "o/r"},
    }


def _run_apply(highlights):
    conn = _FakeConn()
    analysis = pipeline.PRAnalysis(
        summary_md="s", highlights=highlights, entities=[], edges=[]
    )
    ev = {"id": "E1", "occurred_at": datetime(2026, 1, 1, tzinfo=timezone.utc)}
    asyncio.run(pipeline.apply_narrative(
        conn, ev, 1, analysis, "ih", [0.0] * pipeline.EMBED_DIM, _enriched()
    ))
    inserts = [(q, a) for q, a in conn.fetchvals if "insert into narratives" in q]
    assert inserts, "narratives INSERT was not issued"
    return inserts[0][1]  # positional args of the narratives insert


def test_highlights_inserted_as_list_not_json_string():
    # narratives insert args: $1 scope_ref, $2 title, $3 body_md, $4 highlights
    args = _run_apply(["alpha", "beta"])
    highlights_arg = args[3]
    assert isinstance(highlights_arg, list), (
        f"highlights must be a list (→ jsonb array), got {type(highlights_arg).__name__}"
    )
    assert highlights_arg == ["alpha", "beta"]


def test_highlights_empty_list_stays_list():
    args = _run_apply([])
    assert args[3] == []
    assert isinstance(args[3], list)
