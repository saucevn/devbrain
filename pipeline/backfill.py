"""
=======================================================================
DEV SECOND BRAIN — Backfill  (Phase 1, DETERMINISTIC lane only)
=======================================================================
Seed `events` from a git repo's history, then drive the deterministic
projectors (metrics + co-change) by REUSING the exact worker code path in
pipeline.py. No webhook, no LLM, no embeddings → every projection row is
derived_by='git'.

Golden rules honoured:
  - events immutable & idempotent: source='backfill', source_event_id=<SHA>,
    `on conflict do nothing` → re-running never duplicates.
  - projection write + cursor advance happen in ONE transaction (inside
    pipeline.process_event) → no double-count on crash/replay.
  - canonical_key for a file entity = the file path; entity UPSERT only bumps
    last_seen_at (human fields untouched) — all via the shared upsert_entity.

The "payload adapter" is simply shaping each commit as {"files": [...]} —
the same shape changed_files()/file_churn() already read for a PR — so the
existing apply_metrics / apply_cochange run unchanged.

Usage:
    python pipeline/backfill.py <repo-path-or-clone-url>
=======================================================================
"""
from __future__ import annotations

import asyncio
import re
import subprocess
import sys
import tempfile
from datetime import datetime

from pipeline import make_pool, run_one_projector

# Only the two deterministic projectors. 'narrative'/'embeddings' (AI lane)
# are intentionally NOT driven here — backfill stays $0 LLM.
DETERMINISTIC_PROJECTORS = ("metrics", "graph")

# COMMIT<TAB>sha<TAB>author-email<TAB>author-date(strict ISO). numstat lines
# (add<TAB>del<TAB>path) follow each header until the next COMMIT.
_GIT_PRETTY = "COMMIT\t%H\t%ae\t%aI"


# ---------------------------------------------------------------------
# GIT  — walk history at COMMIT granularity (deterministic facts only)
# ---------------------------------------------------------------------
def _git(repo: str, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", repo, *args],
        check=True, capture_output=True, text=True,
    ).stdout


def read_commits(repo: str) -> list[dict]:
    """Parse `git log --numstat` into chronological (oldest-first) commits so
    `events.seq` ascends with time. --no-renames keeps numstat to a clean
    3-column `add<TAB>del<TAB>path` (renames become add+delete of full paths)."""
    out = _git(
        repo, "log", "--no-renames", "--numstat",
        "--date=iso-strict", f"--pretty=format:{_GIT_PRETTY}",
    )
    commits: list[dict] = []
    cur: dict | None = None
    for line in out.splitlines():
        if line.startswith("COMMIT\t"):
            if cur is not None:
                commits.append(cur)
            _, sha, email, date = line.split("\t", 3)
            cur = {"sha": sha, "author": email, "date": date, "files": []}
        elif line.strip() and cur is not None:
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            add, dele, path = parts
            cur["files"].append({
                "filename": path,
                # Binary files report '-' in numstat → treat as 0 churn.
                "additions": 0 if add == "-" else int(add),
                "deletions": 0 if dele == "-" else int(dele),
            })
    if cur is not None:
        commits.append(cur)
    commits.reverse()   # git log is newest-first; we want oldest-first
    return commits


def github_base(repo: str, clone_url: str | None) -> str | None:
    """Best-effort canonical commit-URL base (for events.source_url). Prefer the
    clone URL the user passed; fall back to origin remote. Returns None if not
    a recognisable GitHub remote."""
    candidate = clone_url
    if candidate is None:
        try:
            candidate = _git(repo, "remote", "get-url", "origin").strip()
        except subprocess.CalledProcessError:
            return None
    m = re.search(r"github\.com[:/](.+?)(?:\.git)?/?$", candidate)
    return f"https://github.com/{m.group(1)}" if m else None


def looks_like_url(s: str) -> bool:
    return s.startswith(("http://", "https://", "git@", "ssh://")) or s.endswith(".git")


# ---------------------------------------------------------------------
# DB  — idempotent event insert + reuse of the worker projector sweep
# ---------------------------------------------------------------------
async def insert_events(pool, commits: list[dict], url_base: str | None) -> int:
    """UPSERT one event per commit. Returns count of NEWLY inserted rows
    (conflicts on (source, source_event_id) are silently skipped → idempotent)."""
    inserted = 0
    async with pool.acquire() as conn:
        for c in commits:
            payload = {"sha": c["sha"], "author": c["author"], "files": c["files"]}
            occurred = datetime.fromisoformat(c["date"])
            url = f"{url_base}/commit/{c['sha']}" if url_base else None
            seq = await conn.fetchval(
                """
                insert into events
                  (source, source_event_id, event_type, actor, source_url, payload, occurred_at)
                values ('git', $1, 'commit.pushed', $2, $3, $4, $5)
                on conflict (source, source_event_id) do nothing
                returning seq
                """,
                c["sha"], c["author"], url, payload, occurred,
            )
            if seq is not None:
                inserted += 1
    return inserted


async def _cursor(pool, name: str) -> int:
    return await pool.fetchval(
        "select last_seq from projection_checkpoints where projector_name=$1", name
    )


async def drive_projector(pool, name: str) -> int:
    """Repeatedly run the shared worker sweep (one SWEEP_BATCH per call) until
    its cursor stops advancing. process_event handles the atomic
    projection-write + cursor-advance, so this is crash/replay-safe."""
    while True:
        before = await _cursor(pool, name)
        await run_one_projector(pool, name)
        after = await _cursor(pool, name)
        if after == before:
            return after


# ---------------------------------------------------------------------
# ENTRYPOINT
# ---------------------------------------------------------------------
async def backfill(repo_arg: str) -> None:
    clone_url: str | None = None
    repo = repo_arg
    if looks_like_url(repo_arg):
        clone_url = repo_arg
        repo = tempfile.mkdtemp(prefix="devbrain-backfill-")
        print(f"cloning {repo_arg} → {repo}")
        subprocess.run(["git", "clone", "--quiet", repo_arg, repo], check=True)

    commits = read_commits(repo)
    url_base = github_base(repo, clone_url)
    print(f"parsed {len(commits)} commits from {repo}")

    pool = await make_pool()
    try:
        new = await insert_events(pool, commits, url_base)
        print(f"events: +{new} new ({len(commits) - new} already present)")
        for name in DETERMINISTIC_PROJECTORS:
            seq = await drive_projector(pool, name)
            print(f"projector '{name}': caught up to seq {seq}")
    finally:
        await pool.close()
    print("backfill done (deterministic lane only — $0 LLM).")


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: python pipeline/backfill.py <repo-path-or-clone-url>", file=sys.stderr)
        raise SystemExit(2)
    asyncio.run(backfill(sys.argv[1]))


if __name__ == "__main__":
    main()
