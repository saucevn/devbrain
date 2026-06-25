# devbrain

A real-time **"second brain" / living changelog** for a dev team — it ingests
git / PR / docs activity and turns it into five views: **activity heat**,
**semantic search**, **entity confirm**, a **capability pyramid**, and a
**yearly roadmap**.

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

## Status (2026-06-25)

Phases 0–3 shipped · live ingest running · multi-project namespacing in place.

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

**Next:** Phase 4 knowledge graph · Phase 5 Sonnet rollup + ops · Lark/docs ingest.

## Stack

- **Backend:** Python 3.12 · FastAPI (receiver) · ARQ + Redis (worker) · asyncpg (no ORM) · Pydantic v2 · Anthropic (Haiku/Sonnet)
- **Embeddings:** Google Gemini `gemini-embedding-001` @1536 (L2-normalized) → `vector(1536)`
- **DB:** Postgres 16 + pgvector (in compose); ordered SQL migrations `000→004`
- **Frontend:** Next.js 15 (App Router; Server Components query Postgres; ISR)
- **Deploy:** Docker Compose · Cloudflare Tunnel + Access · backup → R2

## Run

```bash
cp .env.example .env          # secrets: GITHUB_*, ANTHROPIC_API_KEY, GEMINI_API_KEY, TUNNEL_TOKEN, R2_*
docker compose up -d --build                          # backend (db, redis, receiver, worker, cloudflared, backup)
docker compose --profile frontend up -d --build web   # + dashboard at :3000
python pipeline/backfill.py <repo-url>                # seed deterministic data from git history
```

Migrations `000→004` auto-apply on the **first (empty)** db boot; apply later ones
manually. Run the tests in the built image:

```bash
docker run --rm -v "$PWD/pipeline:/app" -w /app -e PYTHONPATH=/app \
  -e DATABASE_URL=postgresql://t:t@localhost/t dev-second-brain-receiver \
  sh -c "pip install -q pytest && python -m pytest -q tests/"
```

## Views — http://localhost:3000

- `/` — activity heat (commits/file) + co-change + project switcher
- `/search` — semantic search (Gemini query embed → pgvector cosine, with citations)
- `/entities` — confirm / rename AI-proposed entities (human-owned, survives replay)
- `/pyramid` — capability pyramid (layer / maturity / risk; heat ↔ maturity toggle)
- `/roadmap` — yearly roadmap from GitHub milestones

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
