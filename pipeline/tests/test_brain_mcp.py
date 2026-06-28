"""Unit tests for the ask-the-brain MCP server's pure helpers (no SDK/DB).

build_server() lazily imports `mcp`, so importing brain_mcp here is safe even
when the MCP SDK isn't installed — we only touch the formatting helpers.
"""
import brain_mcp


def test_strip_project_removes_namespace_prefix():
    assert brain_mcp._strip_project("saucevn/bbf:apps/api/x.py") == "apps/api/x.py"


def test_strip_project_passthrough_when_no_prefix():
    assert brain_mcp._strip_project("apps/api/x.py") == "apps/api/x.py"


def test_strip_project_only_splits_first_colon():
    # a path could contain a colon after the namespace; keep the remainder intact
    assert brain_mcp._strip_project("o/r:a/b:c") == "a/b:c"


def test_format_hit_rounds_and_shapes():
    hit = brain_mcp._format_hit(
        0.255123, "FCT cross-verify", "summary text", "50",
        ["https://github.com/o/r/pull/50"],
    )
    assert hit == {
        "similarity": 0.255,
        "title": "FCT cross-verify",
        "summary": "summary text",
        "pr": "#50",
        "citations": ["https://github.com/o/r/pull/50"],
    }


def test_format_hit_drops_null_citations_and_missing_pr():
    hit = brain_mcp._format_hit(0.9, "t", "s", None, [None, "u", None])
    assert hit["pr"] is None
    assert hit["citations"] == ["u"]
