# devbrain

[![ci](https://github.com/saucevn/devbrain/actions/workflows/ci.yml/badge.svg)](https://github.com/saucevn/devbrain/actions/workflows/ci.yml)

A real-time **"second brain" / living changelog** for a dev team — it ingests
git / PR / docs activity and turns it into a set of read-only views: **activity
heat**, **semantic search**, an **AI narrative changelog**, **entity confirm**, a
**capability pyramid**, a **yearly roadmap**, a **knowledge graph**, and a
**stale-doc watch**.

**Event-sourced:** `events` is the single immutable source of truth; everything
else (metrics, graph edges, narratives, embeddings, status history) is a
derived, replayable projection. Two cleanly separated lanes:

- **Deterministic** (git facts, **$0 LLM**, reproducible) → activity heat,
  co-change graph, pyramid heat, roadmap.
- **Probabilistic** (LLM) → PR/doc narratives, entity extraction, embeddings —
  always linked back to source events for verification. **No AI scoring of
  people or contributions, ever.**

> Design, phases & decision log: **[PROJECT_PLAN.md](PROJECT_PLAN.md)** ·
> engineering rules (non-negotiable): **[CLAUDE.md](CLAUDE.md)** ·
> architecture diagrams: [`docs/`](docs/).

## Status (2026-07-06)

Phases 0–3 shipped · live ingest running · multi-project namespacing in place ·
security/robustness hardened + CI on every PR.

| ✅ | Scope |
|---|---|
| Phase 0 | foundation: `compose up`, schema, webhook stores events |
| Phase 1 | backfill + deterministic projectors + dashboard ($0 LLM) |
| Phase 2A | AI narrative (Haiku) + Gemini embeddings + GitHub enrichment |
| Phase 2B | semantic search (pgvector + citation) |
| Phase 2C | entity-confirm UI (human-owned) |
| Phase 3 | rich pyramid (layer/maturity/risk) + roadmap from GitHub milestones |
| Multi-project | entities namespaced `{repo}:{path}` + per-project filter |
| Live ingest | Cloudflare Tunnel → receiver · GitHub webhook (push/PR/release/milestone) |
| Hardening | fail-closed webhook, backup integrity, migration `005` (TRUNCATE guard, idempotent status projector, `derived_by` everywhere), retry/poison AI lane, non-root containers ([#8](https://github.com/saucevn/devbrain/pull/8)) |
| CI | GitHub Actions on every PR: ruff + pytest (with a real Postgres for DB tests) · web build + typecheck |

**Next:** Phase 4 knowledge graph · Phase 5 Sonnet rollup + ops · Lark/docs ingest.

## Stack

- **Backend:** Python 3.12 · FastAPI (receiver) · ARQ + Redis (worker) · asyncpg (no ORM) · Pydantic v2 · Anthropic (Haiku/Sonnet)
- **Embeddings:** Google Gemini `gemini-embedding-001` @1536 (L2-normalized) → `vector(1536)`
- **DB:** Postgres 16 + pgvector (in compose); ordered SQL migrations `000→005`
- **Frontend:** Next.js 15 (App Router; Server Components query Postgres; ISR)
- **Deploy:** Docker Compose · Cloudflare Tunnel + Access · backup → R2

## Run

```bash
cp .env.example .env          # secrets: GITHUB_*, ANTHROPIC_API_KEY, GEMINI_API_KEY, TUNNEL_TOKEN, R2_*
docker compose up -d --build                          # backend (db, redis, receiver, worker, cloudflared, backup)
docker compose --profile frontend up -d --build web   # + dashboard at :3000
python pipeline/backfill.py <repo-url>                # seed deterministic data from git history
```

Migrations `000→005` auto-apply on the **first (empty)** db boot; apply later ones
manually (see the ⚠️ ordering note in [PROJECT_PLAN.md](PROJECT_PLAN.md) — a schema
migration must land before the code that depends on it is deployed).

## Tests & CI

Every PR and every push to `main` runs [`.github/workflows/ci.yml`](.github/workflows/ci.yml):
a **pipeline** job (`ruff check` + `pytest` against a real `pgvector/pgvector:pg16`
service so the DB-integration test runs too) and a **web** job (`next build` with no
env — guarding the "no throw at module scope" invariant — then `tsc --noEmit`).

Run the backend tests locally:

```bash
pip install -r pipeline/requirements-dev.txt
cd pipeline && python -m pytest tests/ -q                 # unit tests ($0, no DB)
# with a Postgres that has migrations 000→005 applied, also runs the DB test:
TEST_DATABASE_URL=postgresql://postgres:test@127.0.0.1:15433/postgres \
  python -m pytest tests/ -q
```

## Views — http://localhost:3000

- `/` — activity heat (commits/file) + co-change + project switcher
- `/search` — semantic search (Gemini query embed → pgvector cosine, with citations)
- `/changelog` — AI narrative diary (newest first, each entry links its source event)
- `/entities` — confirm / rename AI-proposed entities (human-owned, survives replay)
- `/pyramid` — capability pyramid (layer / maturity / risk; heat ↔ maturity toggle)
- `/roadmap` — yearly roadmap from GitHub milestones
- `/graph` — knowledge graph, subgraph-by-query around a seed entity (Phase 4, WIP)
- `/stale` — stale-doc / knowledge-drift watch (deterministic, no AI scoring)

## Ingest & multi-project

One endpoint `POST /webhooks/github` (HMAC-verified, ACKs <10s → ARQ/Redis queue
→ per-projector cursor sweep). **Any number of repos** can point at it; commits
use a transport-independent identity (`source='git'` / sha) so webhook ↔ backfill
never double-count, and entities are namespaced `{repo}:{path}` so projects don't
collide. Lark + docs-upload ingest are designed (see PROJECT_PLAN.md) but not yet built.

## Replay

Change a projector's logic → clear its projection table, bump `logic_version`,
reset `last_seq = 0`; the worker rebuilds from `seq = 0`. Curated data
(`pyramid_blocks`, confirmed entities, status notes) is **not** a projection —
it's protected by the R2 backup and never cleared.
