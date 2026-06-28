"""
=======================================================================
DEV SECOND BRAIN — PR backfill  (AI-lane seeder)
=======================================================================
Seed historical `pr.merged` events from a repo's merged pull requests, then
let the ALREADY-RUNNING worker enrich → summarize → embed them via its existing
narrative lane (pipeline.maybe_summarize / apply_narrative). This is what turns
the brain from "a few webhook PRs" into a real, searchable corpus.

Why an injector (not driving the AI lane inline like backfill.py does for the
deterministic lane):
  - The narrative lane needs ANTHROPIC + GEMINI + GITHUB keys and network I/O.
    The worker already has them; the receiver/injector should stay thin and
    keyless (golden rule #9 — ACK fast, heavy work in the worker).
  - So this script only INSERTS events (needs DATABASE_URL) + lists PRs (needs
    GITHUB_TOKEN), then nudges the worker. The worker does every LLM call.

Golden rules honoured:
  - events immutable & idempotent: source='github',
    source_event_id='pr-merged:{repo}#{n}' — a STABLE, transport-independent id
    (NOT a random delivery uuid). Re-running never duplicates; and if a real
    webhook later delivers the same merge (different delivery id → 2nd event),
    the narrative UPSERT on (scope,scope_ref,version) + the input-hash cache
    collapse it to ONE narrative and ZERO extra LLM calls.
  - no LLM here. The AI lane runs in the worker (rules #6/#8/#9).
  - a pr.merged event carries NO file list until the worker enriches it, so the
    deterministic metrics/co-change projectors read changed_files()==[] and stay
    untouched → no double-count with the commit-level backfill.

Cost note: at ~tens of merged PRs the bulk Batches API (−50%) is not worth its
async submit/poll rework — the existing synchronous Haiku lane is reused as-is
(golden rule: don't introduce a second style). Revisit Batches only if seeding
hundreds/thousands of PRs.

Usage (run inside the worker image, where deps + GITHUB_TOKEN live):
    python pipeline/backfill_prs.py <owner/repo> [<owner/repo> ...]
=======================================================================
"""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

from pipeline import REDIS_URL, _parse_ts, make_pool

GITHUB_API = "https://api.github.com"


# ---------------------------------------------------------------------
# GITHUB  — list merged PRs (oldest-first so events.seq ascends with time)
# ---------------------------------------------------------------------
def filter_merged(prs: list[dict]) -> list[dict]:
    """A closed PR is *merged* iff merged_at is non-null (the list endpoint
    returns merged_at=null for closed-but-not-merged)."""
    return [p for p in prs if p.get("merged_at")]


async def fetch_merged_prs(repo: str) -> list[dict]:
    """Page through closed PRs (ascending by creation) and keep the merged ones.
    Uses GITHUB_TOKEN for private repos + higher rate limits."""
    import httpx

    token = os.environ.get("GITHUB_TOKEN", "")
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    out: list[dict] = []
    async with httpx.AsyncClient(timeout=30) as c:
        page = 1
        while True:
            r = await c.get(
                f"{GITHUB_API}/repos/{repo}/pulls",
                params={"state": "closed", "per_page": 100, "page": page,
                        "sort": "created", "direction": "asc"},
                headers=headers,
            )
            r.raise_for_status()
            batch = r.json()
            if not batch:
                break
            out.extend(batch)
            page += 1
    return filter_merged(out)


# ---------------------------------------------------------------------
# SHAPE  — one merged PR → one `pr.merged` event (pure, unit-tested)
# ---------------------------------------------------------------------
def pr_to_event(pr: dict, repo: str) -> dict[str, Any]:
    """Map a GitHub PR object to the event row the worker's narrative lane
    expects. Only the fields enrich_pr_payload / parse_pr / passes_ai_gate read
    are populated; the worker fetches files + diff + linked issues itself."""
    number = pr["number"]
    login = (pr.get("user") or {}).get("login")
    return {
        "source": "github",
        "source_event_id": f"pr-merged:{repo}#{number}",
        "event_type": "pr.merged",
        "actor": login,
        "source_url": pr.get("html_url"),
        "payload": {
            "pull_request": {
                "number": number,
                "title": pr.get("title") or "",
                "body": pr.get("body") or "",
                "merged": True,
                "merged_at": pr.get("merged_at"),
                "html_url": pr.get("html_url"),
                "user": {"login": login} if login else {},
            },
            "repository": {"full_name": repo},
        },
        "occurred_at": _parse_ts(pr.get("merged_at")),
    }


# ---------------------------------------------------------------------
# DB  — idempotent event insert (mirrors backfill.insert_events)
# ---------------------------------------------------------------------
async def insert_pr_events(pool, events: list[dict]) -> int:
    """UPSERT one event per PR. Returns count of NEWLY inserted rows; conflicts
    on (source, source_event_id) are silently skipped → idempotent re-runs."""
    inserted = 0
    async with pool.acquire() as conn:
        for e in events:
            seq = await conn.fetchval(
                """
                insert into events
                  (source, source_event_id, event_type, actor, source_url, payload, occurred_at)
                values ($1, $2, $3, $4, $5, $6, $7)
                on conflict (source, source_event_id) do nothing
                returning seq
                """,
                e["source"], e["source_event_id"], e["event_type"], e["actor"],
                e["source_url"], e["payload"], e["occurred_at"],
            )
            if seq is not None:
                inserted += 1
    return inserted


async def nudge_worker() -> None:
    """Enqueue an immediate projector sweep so the worker picks up the new
    events now instead of waiting for its 5-min cron. Harmless if it overlaps:
    run_one_projector takes a per-projector advisory lock."""
    from arq import create_pool as arq_pool
    from arq.connections import RedisSettings

    redis = await arq_pool(RedisSettings.from_dsn(REDIS_URL))
    try:
        await redis.enqueue_job("run_projectors")
    finally:
        # redis-py asyncio: close() is deprecated since 5.0.1 → aclose().
        await redis.aclose()


# ---------------------------------------------------------------------
# ENTRYPOINT
# ---------------------------------------------------------------------
async def backfill_prs(repos: list[str]) -> None:
    pool = await make_pool()
    try:
        total_new = 0
        for repo in repos:
            prs = await fetch_merged_prs(repo)
            events = [pr_to_event(p, repo) for p in prs]
            new = await insert_pr_events(pool, events)
            total_new += new
            print(f"{repo}: {len(prs)} merged PRs → +{new} new events "
                  f"({len(events) - new} already present)")
    finally:
        await pool.close()

    if total_new:
        await nudge_worker()
        print(f"enqueued sweep → worker will enrich+summarize+embed {total_new} "
              f"new PR events (Haiku narrative + Gemini embedding).")
    else:
        print("no new events — nothing for the worker to do.")
    print("PR backfill done (events injected; AI work runs in the worker).")


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: python pipeline/backfill_prs.py <owner/repo> [<owner/repo> ...]",
              file=sys.stderr)
        raise SystemExit(2)
    asyncio.run(backfill_prs(sys.argv[1:]))


if __name__ == "__main__":
    main()
