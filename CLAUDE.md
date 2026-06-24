# CLAUDE.md — devbrain

Context file for Claude Code. Read this fully before editing. The rules below are **non-negotiable**: they protect an event-sourced architecture that breaks in subtle ways if violated.

Repo: `git@github.com:saucevn/devbrain.git` · Owner: saucevn

---

## What this is

**devbrain** is a real-time "second brain" / living changelog for a dev team. It ingests git/PR/docs activity and surfaces four views: a pyramid capability heatmap, a yearly roadmap, a knowledge graph, and semantic search.

It is **event-sourced**: `events` is the single immutable source of truth; everything else (metrics, graph edges, narratives, embeddings) is a **derived projection** that can be rebuilt from events at any time. There are two clearly separated layers: a **deterministic** layer computed from git facts (reproducible, cheap, drives heatmap/roadmap) and a **probabilistic** layer produced by an LLM (summaries + entity extraction + embeddings, always linked back to source events for verification).

Full design rationale lives in `PROJECT_PLAN.md`. The schema's replay recipe is at the bottom of `supabase/migrations/001_schema.sql`.

---

## Golden rules — NEVER violate

1. **`events` is append-only and immutable.** DB triggers reject UPDATE/DELETE. Never write code that mutates an event's `payload` or `occurred_at`. New facts = new rows.
2. **Projections are derived & disposable.** They are NEVER the source of truth. Any projection must be safely rebuildable from `events` starting at `seq = 0`. When you change projection logic, the path is: bump `logic_version` → `truncate` that projection → replay. Don't add migrations that try to "fix up" projection data in place.
3. **NO AI scoring or ranking of people or contributions. Ever.** "Heat" is computed deterministically from `metrics_daily`. The LLM only summarizes, extracts entities, and writes narrative. If you find yourself adding a `score` an LLM produces, stop.
4. **Every projection row carries `derived_by`** = `'git'` (deterministic, reproducible) or `'ai'` (extracted). Keep the layers separate. The LLM must never write deterministic metrics.
5. **Entity `canonical_key` anchors to a real identifier** (file path / module path / epic key) — never an AI-invented name. The entity UPSERT on-conflict **only** bumps `last_seen_at`; it must NEVER overwrite `display_name`, `pyramid_layer`, or `resolution_status` (human-owned fields). Run `guard_canonical_key` before trusting any AI-extracted entity.
6. **Projection write + cursor advance go in ONE transaction.** Projections use `+=` UPSERTs; a crash between the write and the cursor update double-counts. The LLM call must happen **outside** the transaction (it's network I/O).
7. **Idempotency is layered, don't remove a layer:** webhook delivery-ID dedup (`unique(source, source_event_id)`) → cursor (each event processed once) → content/input-hash to skip the LLM on replay → UPSERT on natural keys. Never use a plain `INSERT` for a projection that could be re-run.
8. **Cost discipline:** the LLM only touches filtered events (`passes_ai_gate`: merged PRs / releases / docs). Summarize at **PR level**, not per commit. Haiku for bulk summaries, Sonnet only for periodic rollup. Keep `cache_control` on the system + tool prefix.
9. **The receiver ACKs in <10s.** Never do heavy work or LLM calls in the request path — enqueue and let the worker handle it.
10. **Replay is the recovery mechanism.** Design every projector to be re-runnable from scratch. This is also why losing the local DB is recoverable: most events originate from GitHub (durable upstream).

---

## Architecture (one screen)

```
webhook + backfill → events (immutable) → ARQ sweep (cursor + logic_version)
        ↓ split
  deterministic lane (every event, cheap, no LLM)   AI lane (filtered, gated)
  metrics_daily · entity_edges(co_changed_with)      narratives · embeddings
        ↓
  Next.js dashboard (ISR, read-only) + pgvector semantic search
```

Webhook receiver and worker are two processes from one image. The sweep reads `events` past each projector's cursor, in `seq` order; each projector has its own checkpoint so the slow AI lane never blocks the fast deterministic lane.

---

## Repo layout

```
supabase/migrations/   000_local_roles · 001_schema · 002_idempotency  (auto-applied on first db boot)
pipeline/              pipeline.py (receiver + sweep + filters + summarize) · backfill.py · rollup.py · Dockerfile · requirements.txt
backup/                Dockerfile · backup.sh   (pg_dump → R2 sidecar)
web/                   Next.js 15 + Shadcn (frontend; opt-in via compose profile)
docker-compose.yml     all-in-one stack (db, redis, receiver, worker, backup, cloudflared, web)
PROJECT_PLAN.md        full plan: phases, decision log, risks
```

---

## Stack

- **Backend:** Python 3.12, FastAPI (receiver), ARQ + Redis (worker), asyncpg (no ORM), Pydantic v2, Anthropic SDK.
- **DB:** Postgres 16 + pgvector, local in compose. Schema is hand-written SQL — no migration framework, just ordered files.
- **AI:** `MODEL_SUMMARY = claude-haiku-4-5-20251001`, `MODEL_ROLLUP = claude-sonnet-4-6`. Structured output via forced `tool_use`. Embeddings provider TBD (locks `EMBED_DIM` ↔ `vector(N)`).
- **Frontend:** Next.js 15 (App Router, Server Components query Postgres directly), Shadcn + Tailwind, ISR (no realtime).
- **Deploy:** Docker Compose, host-agnostic (VPS or Mac), Cloudflare Tunnel + Access, backup to R2.

---

## Commands

```bash
# Bring up backend (web stays off for Cloudflare Pages):
docker compose up -d --build
# Self-host everything incl. frontend:
docker compose --profile frontend up -d --build

# Migrations auto-apply on first db boot (empty volume only). Manual apply:
psql "$DATABASE_URL" -f supabase/migrations/001_schema.sql

# Backfill deterministic data from a repo's git history (Phase 1):
python pipeline/backfill.py <repo-path-or-url>

# Local dev (without docker):
uvicorn pipeline:app --reload          # receiver
arq pipeline.WorkerSettings            # worker

# Replay a projector after changing its logic (see 001_schema.sql §9):
#   truncate <projection>; bump logic_version; reset last_seq=0; worker rebuilds.

# Lint / format:
ruff check . && ruff format .

# Restore from R2:
aws s3 cp s3://$R2_BUCKET/backups/<file>.sql.gz - --endpoint-url $R2_ENDPOINT | gunzip | psql "$DATABASE_URL"
```

---

## Conventions

- Async everywhere; asyncpg with explicit SQL (no ORM, no query builder). Full type hints. Format/lint with `ruff`.
- SQL is `snake_case`; every projection row sets `derived_by` and `derived_by_version`.
- Follow the patterns already in `pipeline.py` (filters, structured `tool_use`, atomic write+cursor, entity UPSERT). Don't introduce a second style.
- Commits: small, logical, imperative subject. Push to `origin main`.
- Secrets only via env / `.env` (gitignored). Never hardcode keys.

---

## Current state & build order

Design is complete; code is at **Phase 0 → 1**. Build the deterministic backbone *before* anything AI or visual.

1. **Phase 0** — foundation: `docker compose up` green, schema applied, webhook stores events.
2. **Phase 1** — `backfill.py` + metrics + co-change projectors → useful dashboard with **$0 LLM**. ← *next*
3. **Phase 2** — AI summarize (Haiku) + entity extraction + pgvector search.
4. **Phase 3** — pyramid (human structure + metric heat) + roadmap from `status_history`.
5. **Phase 4** — knowledge graph (subgraph-by-query; **build last**, after entity resolution is solid).
6. **Phase 5** — Sonnet rollup diary + cost/monitoring + replay runbook.

**Do NOT** start the AI lane, frontend, or graph viz before the deterministic backbone works.

**One open blocker before Phase 2:** choose the embedding provider — it locks `EMBED_DIM` and the `vector(N)` column. Stop and ask before picking one.

---

## Gotchas (already handled — don't "fix" them)

- `000_local_roles.sql` creates a no-op `authenticated` role so the RLS block in `001` doesn't error on plain Postgres. RLS is effectively off (services connect as superuser → bypass); real dashboard auth is **Cloudflare Access**. Don't add Supabase-style RLS enforcement.
- DB init-scripts run **only when the volume is empty**. Schema changes after first boot must be applied manually.
- `EMBED_DIM` must match the `vector(N)` in the schema. Changing embedding model ⇒ re-embed via replay, not an in-place edit.
- GitHub webhook payloads lack the full diff and linked issues. Enrich via the GitHub API **in the worker**, never in the receiver.
- pgvector HNSW indexes vectors ≤2000 dims; for larger, use `halfvec`.
- Backfill operates at **commit** granularity (deterministic lane only). PR-level summaries (AI lane) come from the webhook/enrichment path, not backfill.
