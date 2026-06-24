# devbrain

A real-time **"second brain" / living changelog** for a dev team. It ingests
git / PR / docs activity and surfaces four views: a pyramid capability heatmap,
a yearly roadmap, a knowledge graph, and semantic search.

It is **event-sourced**: `events` is the single immutable source of truth, and
everything else (metrics, graph edges, narratives, embeddings) is a derived
projection that can be rebuilt from events at any time. Two layers stay cleanly
separated — a **deterministic** lane computed from git facts (cheap, reproducible,
drives the heatmap/roadmap) and a **probabilistic** lane produced by an LLM
(summaries + entity extraction + embeddings, always linked back to source events).

> Full design, phases, and decision log: **[PROJECT_PLAN.md](PROJECT_PLAN.md)**.
> Engineering rules (non-negotiable): **[CLAUDE.md](CLAUDE.md)**.

## Stack

- **Backend:** Python 3.12 · FastAPI (receiver) · ARQ + Redis (worker) · asyncpg (no ORM) · Pydantic v2 · Anthropic SDK
- **DB:** Postgres 16 + pgvector (local, in compose). Hand-written SQL migrations, applied in order.
- **Frontend:** Next.js 15 (opt-in via compose profile) — not built yet.
- **Deploy:** Docker Compose (VPS or Mac) · Cloudflare Tunnel + Access · backup to R2.

## Run

```bash
# 1. Configure env (fill in real secrets for cloudflared/backup/AI later)
cp .env.example .env

# 2. Bring up the backend (frontend stays off — that's for Cloudflare Pages)
docker compose up -d --build
#    Self-host the frontend too:
#    docker compose --profile frontend up -d --build

# Migrations 000 → 001 → 002 auto-apply on the FIRST db boot (empty volume only).
```

The webhook receiver listens at `POST /webhooks/github` (HMAC-verified, ACKs in
<10s) and `GET /health`. Heavy work happens in the worker, never the request path.

## Backfill (Phase 1 — deterministic, $0 LLM)

Seed `events` from a repo's git history and build the deterministic projections
(`metrics_daily`, co-change `entity_edges`):

```bash
python pipeline/backfill.py <repo-path-or-clone-url>
```

Backfill is idempotent (events keyed by commit SHA; projectors advance a cursor),
so re-running it never double-counts. It only drives the deterministic lane — no
LLM, no embeddings.

## Replay a projector

After changing a projector's logic: `truncate` its projection table, bump
`logic_version`, reset `last_seq = 0`, and the worker rebuilds from `seq = 0`.
See the recipe at the bottom of `supabase/migrations/001_schema.sql`.

## Status

Phase 0 → 1. Deterministic backbone first; the AI lane, frontend, and knowledge
graph come later (see PROJECT_PLAN.md §6).
