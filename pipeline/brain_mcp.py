"""
=======================================================================
DEV SECOND BRAIN — MCP server ("ask-the-brain")   [leverage #1]
=======================================================================
Exposes the brain to an AI agent (Claude Code / Cursor) over MCP stdio:
  - search_brain(query): semantic search over merged-PR & doc narratives,
    ALWAYS returning source citations so the agent can verify.
  - get_context(entity): everything the brain knows about a file / module /
    feature / epic — co-change neighbors, related docs, and the narratives
    that touched it.

Read-only. Reuses pipeline.embed (Gemini RETRIEVAL_QUERY) + the match_embeddings
RPC. Golden rules honoured: AI output links back to a source event (verifiable);
NO scoring/ranking of people or contributions (#3) — it only retrieves facts.

The MCP SDK is imported lazily inside build_server() so the pure helpers (and
their unit tests) don't require `mcp` to be installed.

Run (needs DATABASE_URL + GEMINI_API_KEY):
    pip install mcp        # not in the base image; install where you run this
    python pipeline/brain_mcp.py
Then register it as an MCP server (command = that line) in Claude Code / Cursor.
=======================================================================
"""
from __future__ import annotations

from typing import Any

from pipeline import embed, make_pool

_pool = None


async def _get_pool():
    """Lazily open one shared pool for the server's lifetime."""
    global _pool
    if _pool is None:
        _pool = await make_pool()
    return _pool


# ---------------------------------------------------------------------
# PURE HELPERS (unit-tested; no DB / no network)
# ---------------------------------------------------------------------
def _strip_project(canonical_key: str) -> str:
    """Drop the '{repo}:' namespace prefix for display.
    'saucevn/bbf:apps/api/x.py' → 'apps/api/x.py'."""
    return canonical_key.split(":", 1)[1] if ":" in canonical_key else canonical_key


def _format_hit(similarity: float, title: str, summary: str,
                pr_ref: str | None, urls: list[str]) -> dict[str, Any]:
    """Shape one search hit + its citation(s) for the agent."""
    return {
        "similarity": round(similarity, 3),
        "title": title,
        "summary": summary,
        "pr": f"#{pr_ref}" if pr_ref else None,
        "citations": [u for u in urls if u],
    }


# ---------------------------------------------------------------------
# TOOLS (plain async fns; registered in build_server so they stay testable)
# ---------------------------------------------------------------------
async def search_brain(query: str, limit: int = 8) -> list[dict]:
    """Semantic search the dev second-brain (merged-PR & doc narratives).
    Returns ranked summaries each with source citations to verify."""
    pool = await _get_pool()
    vec = await embed(query, "RETRIEVAL_QUERY")
    rows = await pool.fetch(
        """
        select m.similarity, n.title, n.body_md, n.scope_ref, n.source_event_ids
        from match_embeddings($1, $2, 'pr_summary') m
        join narratives n on n.id = m.source_id
        order by m.similarity desc
        """,
        vec, limit,
    )
    out: list[dict] = []
    for r in rows:
        urls: list[str] = []
        if r["source_event_ids"]:
            ev = await pool.fetch(
                "select source_url from events where id = any($1::uuid[])",
                r["source_event_ids"],
            )
            urls = [e["source_url"] for e in ev]
        out.append(_format_hit(r["similarity"], r["title"], r["body_md"],
                               r["scope_ref"], urls))
    return out


async def get_context(entity: str, limit: int = 10) -> dict:
    """Everything the brain knows about an entity (file / module / feature /
    epic): the matched entity, co-change & AI neighbors, and the narratives
    that touched it. Accepts a file path, module prefix, or display name."""
    pool = await _get_pool()
    matches = await pool.fetch(
        """
        select id, entity_kind, canonical_key, display_name, project, pyramid_layer
        from entities
        where canonical_key = $1                 -- exact key
           or canonical_key like '%:' || $1      -- exact path within a project ('repo:path')
           or canonical_key like '%/' || $1      -- basename of a namespaced path
           or display_name ilike $1              -- AI/curated display name
        order by last_seen_at desc limit 5
        """,
        entity,
    )
    if not matches:
        return {"query": entity, "found": False,
                "hint": "No entity matched. Try a file path, module prefix, or display name."}

    e = matches[0]
    eid = e["id"]
    neighbor_rows = await pool.fetch(
        """
        select case when from_entity = $1 then to_entity else from_entity end as other,
               edge_type, weight
        from entity_edges
        where from_entity = $1 or to_entity = $1
        order by weight desc limit $2
        """,
        eid, limit,
    )
    neighbors: list[dict] = []
    for n in neighbor_rows:
        o = await pool.fetchrow(
            "select display_name, canonical_key, entity_kind from entities where id = $1",
            n["other"],
        )
        if o:
            neighbors.append({
                "name": o["display_name"], "key": _strip_project(o["canonical_key"]),
                # weight is numeric → cast to int so the MCP JSON result serializes.
                "kind": o["entity_kind"], "edge": n["edge_type"], "weight": int(n["weight"]),
            })

    narrs = await pool.fetch(
        """
        select distinct n.scope_ref, n.title, c.relation
        from contributions c
        join narratives n on n.source_event_ids @> array[c.event_id]
        where c.entity_id = $1
        order by n.scope_ref desc limit $2
        """,
        eid, limit,
    )
    return {
        "query": entity, "found": True,
        "entity": {
            "name": e["display_name"], "key": _strip_project(e["canonical_key"]),
            "kind": e["entity_kind"], "project": e["project"], "layer": e["pyramid_layer"],
        },
        "neighbors": neighbors,
        "narratives": [
            {"pr": f"#{x['scope_ref']}", "title": x["title"], "relation": x["relation"]}
            for x in narrs
        ],
    }


def build_server():
    """Create the FastMCP server and register the tools. mcp imported here so
    the module (and its pure-helper tests) load without the SDK installed."""
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("devbrain")
    server.tool()(search_brain)
    server.tool()(get_context)
    return server


def main() -> None:
    build_server().run()


if __name__ == "__main__":
    main()
