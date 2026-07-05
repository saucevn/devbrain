# devbrain — Codebase Audit & Improvement Plan

> Audit date: 2026-07-03 · Scope: full repo (pipeline, migrations, web, infra) at commit `b8406eb`.
> Method: four parallel specialized reviews (Python pipeline, Postgres schema, Next.js frontend, security/infra), findings cross-verified against source. Every item below was confirmed in the actual code — line refs included.
>
> **How to use this file:** work top-down. P0 items are fix-before-anything-else. Each step says *what*, *where*, *why*, and *replay impact* where relevant. Schema changes are grouped into a single proposed `005_hardening.sql` migration (§ P1-1) — per CLAUDE.md, migrations after first boot must be applied manually.

---

## Scorecard — golden-rule compliance (verified)

| Golden rule | Status | Notes |
|---|---|---|
| 1. `events` append-only | ⚠️ PARTIAL | UPDATE/DELETE triggers exist; **no TRUNCATE trigger** (P0-3) |
| 2. Projections rebuildable | ⚠️ PARTIAL | `entity_status_history` replay double-counts without truncate; replay recipe omits it (P0-4) |
| 3. No AI scoring of people | ✅ PASS | Blocked in system prompt; no score fields anywhere |
| 4. Every projection row carries `derived_by` | ❌ FAIL | `metrics_daily`, `entity_status_history` lack the column; `narratives`/`embeddings` only have `derived_by_version` (P1-2) |
| 5. Entity UPSERT only bumps `last_seen_at` | ✅ PASS | `upsert_entity` on-conflict sets only `last_seen_at` |
| 6. Write + cursor in one txn; LLM outside | ✅ PASS | `process_event` structure is correct |
| 7. Layered idempotency, no plain INSERT | ❌ FAIL | `apply_status` is a bare INSERT with no natural key (P0-4) |
| 8. AI gate + PR-level + Haiku | ✅ PASS | `passes_ai_gate` correct; Haiku for bulk |
| 9. Receiver ACKs <10s | ✅ PASS | Heavy work enqueued; but no fail-closed HMAC (P0-1) |
| 10. Replay is recovery | ⚠️ PARTIAL | Backup that replay depends on can silently be corrupt (P0-2) |

---

## P0 — Critical (fix immediately; each is a data-integrity or event-store-poisoning risk)

### P0-1 · Webhook HMAC verifies against an **empty secret** — event-store poisoning
**Where:** [pipeline/pipeline.py:57](pipeline/pipeline.py#L57) (`GITHUB_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "").encode()`) and the verify at [pipeline.py:144-146](pipeline/pipeline.py#L144).
**Why critical:** if the env var is unset, `hmac.new(b"", body, sha256)` produces a digest any attacker can compute. Forged `pr.merged`/`release.tagged` events land in the immutable `events` table and **replay forever** through every projector, including the LLM lane (cost + poisoned narratives/embeddings). This is the single worst failure mode in the whole system because events cannot be deleted.
**Fix:** the module-level comment says the tolerant read is intentional so `backfill.py` can import the module — keep the tolerant read, but **fail closed at the endpoint**:
```python
if not GITHUB_SECRET:
    raise HTTPException(status_code=503, detail="webhook secret not configured")
```
plus a receiver-startup assertion in `_startup()` so misconfiguration is loud on boot. While here (same endpoint, small additions):
- Reject empty `X-GitHub-Delivery` with 400 (empty string currently becomes a valid dedup key `("github","")` — first junk event permanently claims the slot). [pipeline.py:138](pipeline/pipeline.py#L138)
- Reject bodies > 10 MB via `content-length` check before `request.body()` (GitHub caps payloads at 25 MB; no server-side guard exists).

### P0-2 · `backup.sh` silently uploads corrupt backups when `pg_dump` fails
**Where:** [backup/backup.sh:11](backup/backup.sh#L11) — `pg_dump ... | gzip -9 > "$TMP"` under `set -eu` in `/bin/sh`.
**Why critical:** POSIX sh has no `pipefail`; `set -e` only sees the last command in the pipe. If Postgres is unreachable, gzip succeeds on empty input, a ~20-byte gzip uploads to R2, and the entrypoint reports success. The R2 backup is the **only** protection for human-curated data (`pyramid_blocks`, confirmed entities, status notes) that cannot be re-derived from events — the exact thing PROJECT_PLAN §7 says the backup exists to protect.
**Fix (POSIX-safe, no bash needed):** split the pipe and verify:
```sh
pg_dump "${DATABASE_URL}" --no-owner --no-privileges > "${TMP_SQL}"
gzip -9 < "${TMP_SQL}" > "${TMP}" && rm -f "${TMP_SQL}"
gzip -t "${TMP}"
[ "$(wc -c < "${TMP}")" -gt 1024 ] || { echo "[backup] dump suspiciously small, abort"; exit 1; }
```
**Follow-up (P2):** add a `restore_test.sh` that pulls the latest object and pipes it into a scratch DB — restore has never been exercised.

### P0-3 · `events` table has no TRUNCATE guard
**Where:** [supabase/migrations/001_schema.sql:70-73](supabase/migrations/001_schema.sql#L70) — `prevent_event_mutation` covers `BEFORE UPDATE`/`BEFORE DELETE` only. Row-level triggers don't fire on TRUNCATE, so `truncate events` succeeds silently. Especially dangerous here because the documented replay workflow *is* "truncate a projection" — one wrong table name in a replay session deletes the source of truth.
**Fix (goes into `005_hardening.sql`):**
```sql
create trigger events_no_truncate before truncate on events
  for each statement execute function prevent_event_mutation();
```

### P0-4 · `apply_status` bare INSERT — replay double-counts the roadmap
**Where:** [pipeline/pipeline.py:631-637](pipeline/pipeline.py#L631) inserts into `entity_status_history` with no `ON CONFLICT`; the table has no natural key; and the replay recipe in 001 §9 **does not mention** this table (it was added in Phase 3 after the recipe was written).
**Why critical:** resetting the `status` projector cursor without truncating first re-inserts every milestone transition — duplicate roadmap rows, violating golden rules 2 and 7. It is the only projection write without an idempotency guard.
**Fix (paired schema + code change):**
```sql
-- 005_hardening.sql (dedupe first in case duplicates already exist)
delete from entity_status_history a using entity_status_history b
  where a.entity_id = b.entity_id and a.source_event_id = b.source_event_id
    and a.source_event_id is not null and a.ctid > b.ctid;
create unique index entity_status_hist_natural_key
  on entity_status_history (entity_id, source_event_id)
  where source_event_id is not null;
```
```python
# apply_status: add
... on conflict (entity_id, source_event_id) where source_event_id is not null do nothing
```
And update the §9 replay recipe comment in `001_schema.sql` to include the `status` projector (`truncate entity_status_history` + reset its checkpoint).

---

## P1 — High (correctness, robustness, security hardening; do this sprint)

### P1-1 · New migration `005_hardening.sql` — constraints + indexes matching real query patterns
One manually-applied migration bundling P0-3, P0-4, and the following (all verified against actual queries in `pipeline.py` / `web/lib/data.ts`):

**Missing indexes (Seq Scans today):**
```sql
-- getChangelog sorts by period_start; only period_end is indexed (data.ts:329)
create index narratives_period_start_idx on narratives (period_start desc nulls last);
-- searchBrain/getChangelog join narratives via embeddings.source_id (data.ts:154,327) — unindexed
create index embeddings_source_id_idx on embeddings (source_id) where source_id is not null;
-- roadmap view joins events on entity_status_history.source_event_id (001:373-385) — unindexed
create index entity_status_hist_event_idx on entity_status_history (source_event_id)
  where source_event_id is not null;
-- composite filters used by getHotFiles / getProposedEntities (data.ts:78,189)
create index entities_kind_project_idx on entities (entity_kind, project);
create index entities_resolution_project_idx on entities (resolution_status, project);
```

**Missing CHECK constraints (text enums currently unenforced — a typo writes silently-wrong data that layer-separation queries then miss):**
```sql
alter table contributions add constraint contributions_derived_by_check check (derived_by in ('git','ai'));
alter table entity_edges  add constraint entity_edges_derived_by_check  check (derived_by in ('git','ai'));
alter table entities add constraint entities_resolution_status_check
  check (resolution_status in ('proposed','confirmed','rejected'));
alter table entities add constraint entities_status_check
  check (status in ('active','deprecated','merged'));
alter table entity_status_history add constraint entity_status_history_status_check
  check (status in ('planned','in_progress','shipped','deprecated'));
```

**Replay impact:** indexes and checks are additive — no truncate/replay needed. Apply manually (init-scripts only run on an empty volume).

### P1-2 · Golden rule #4 drift: `derived_by` missing from half the projections
**Where:** `metrics_daily` and `entity_status_history` have no `derived_by` column; `narratives`/`embeddings` carry only `derived_by_version` ([001_schema.sql:239-251, 159-166, 273, 308](supabase/migrations/001_schema.sql#L239)). CLAUDE.md rule 4 says *every* projection row carries it.
**Decision needed, recommendation = add the columns** (cheap, keeps the rule intact rather than amending it):
```sql
alter table metrics_daily         add column derived_by text not null default 'git' check (derived_by in ('git','ai'));
alter table entity_status_history add column derived_by text not null default 'git' check (derived_by in ('git','ai'));
alter table narratives            add column derived_by text not null default 'ai'  check (derived_by in ('git','ai'));
alter table embeddings            add column derived_by text not null default 'ai'  check (derived_by in ('git','ai'));
```
Update the INSERTs in `apply_metrics` / `apply_status` / `apply_narrative` to set it explicitly. (Rationale: Phase 3 §2.2 already anticipates human-set status notes — `derived_by='human'` may join the enum later; having the column now makes that a value change, not a schema change.)

### P1-3 · `status` projector crashes on deploys that haven't applied 003
**Where:** [pipeline/pipeline.py:507](pipeline/pipeline.py#L507) `run_one_projector` — `cp = await pool.fetchrow(...)` then `cp["last_seq"]`; the `status` checkpoint row is seeded only by `003_pyramid_blocks.sql:43`. On any DB where 003 wasn't manually applied, every sweep raises `TypeError` for that projector.
**Fix:** guard `if cp is None: return` (graceful skip + log a warning), *and* add a fallback `insert ... on conflict do nothing` for all `PROJECTORS` checkpoints at worker startup so code and schema can't drift again.

### P1-4 · No retry/backoff on Anthropic, Gemini, GitHub calls — one 429 stalls the narrative cursor
**Where:** `summarize_pr` ([pipeline.py:394-422](pipeline/pipeline.py#L394)), `embed` (425-443), `enrich_pr_payload` (777-797) — all `raise_for_status()` with no retry.
**Why high:** a transient 5xx/429 aborts the sweep loop; the cursor sticks at the failing event and each cron tick re-hammers the API with zero backoff. A *permanently* failing event (e.g., PR deleted before enrichment → GitHub 404) stalls the AI lane **forever**.
**Fix:**
1. Wrap the three call sites in a small retry helper (exponential backoff + jitter, ~4 attempts, honor `Retry-After`).
2. Poison-event policy: on non-retryable errors (404/422), log + return `None` from `maybe_summarize` so the cursor advances. The event stays in the log; a future replay with fixed logic can still pick it up — that's exactly what replay is for.

### P1-5 · Compose/network hardening (fail-closed perimeter)
**Where:** [docker-compose.yml](docker-compose.yml)
- **Web port `3000:3000` binds 0.0.0.0** (line 96-97): on a VPS this bypasses Cloudflare Access entirely and exposes the `confirmEntity` write action to anyone with network reach. → `"127.0.0.1:3000:3000"` (or route web through the tunnel).
- **Redis unauthenticated**: add `--requirepass "${REDIS_PASSWORD}"` + update `REDIS_URL`; add an `internal: true` compose network for db/redis/worker.
- **`cloudflare/cloudflared:latest`** (line 81): the internet-facing component floats on a mutable tag → pin to a digest (do the same for `pgvector/pgvector:pg16`, `redis:7-alpine`).
- **All containers run as root**: add `USER app` to pipeline/web/backup Dockerfiles.
- **FastAPI docs exposed through the tunnel**: `FastAPI(docs_url=None, redoc_url=None, openapi_url=None)` (env-gated for dev).

### P1-6 · `confirmEntity` server action: no input validation, no auth-header check
**Where:** [web/lib/actions.ts:6-12](web/lib/actions.ts#L6), [web/lib/data.ts:208-223](web/lib/data.ts#L208)
This action writes the *human-owned* fields (`display_name`, `resolution_status`) — the one thing that can't be rebuilt from events. Currently: no UUID check on `id` (a missing form field becomes the literal string `"null"` → raw Postgres error → 500), no length cap on `displayName`, no error boundary, and — combined with the 0.0.0.0 binding above — reachable unauthenticated.
**Fix:** UUID-regex guard + `displayName.trim().slice(0, 200)` + try/catch returning a structured result; add a root `app/error.tsx`; and (belt-and-braces once P1-5 lands) a `middleware.ts` that requires the `CF-Access-Jwt-Assertion` header on mutating requests.

### P1-7 · Fail-fast on required secrets; honest `/health`
- `GEMINI_API_KEY` is read at call time ([pipeline.py:434](pipeline/pipeline.py#L434)) — the worker boots fine and crashes on the *first AI-lane event*, possibly hours later. Validate at worker startup (same pattern as `DATABASE_URL`). Same for the web app: guard `DATABASE_URL`/`GEMINI_API_KEY` in [web/lib/data.ts:7,113](web/lib/data.ts#L7) (today an unset key sends `?key=undefined` to Gemini).
- `/health` returns `200 {"ok": true}` even when the DB ping fails ([pipeline.py:124-131](pipeline/pipeline.py#L124)) — status-code-based probes never see the outage. Return 503 when `db_ok` is false. Migrate `@app.on_event("startup")` → `lifespan` while touching this (deprecated in FastAPI ≥0.93).

### P1-8 · Escape `ILIKE` wildcards from user input (two places)
- [web/lib/data.ts:368](web/lib/data.ts#L368) `getSubgraph`: `?q=%` matches every entity → full scan holding one of only 5 pool connections.
- [pipeline/brain_mcp.py:106](pipeline/brain_mcp.py#L106): same pattern, amplified by an unbounded `limit` param (clamp `limit = min(limit, 100)`).
**Fix:** `seed.replace(/[\\%_]/g, "\\$&")` + `escape '\'` (TS), mirror in Python. Parameterization is already correct — this is query-widening, not injection.

---

## P2 — Medium (operational quality; next sprint)

1. **Structured logging in the pipeline** — zero `logging` calls today; HMAC failures, projector errors, and LLM usage are invisible. Add `logging` with a warning on signature failures (includes source IP) and info-level cursor/cost lines. This is also the foundation for the Phase 5 cost dashboard.
2. **Content-hash pre-check before Gemini** ([pipeline.py:650-656](pipeline/pipeline.py#L650)): `input_hash` gates the Haiku call, but when a PR re-summarizes to byte-identical `summary_md`, `embed()` still calls Gemini. Check `embeddings.content_hash` first — the column already exists, it's just never used as a skip condition. (Directly serves PROJECT_PLAN §0.4 cost levers.)
3. **Verify prompt caching actually engages** — PROJECT_PLAN §0.4 flags that Haiku needs a ≥4096-token prefix; `PR_SYSTEM_PROMPT` + tool schema is likely below it, so `cache_control` may be a silent no-op. Log `usage.cache_read_input_tokens`; if 0, drop the misleading `cache_control` and lean on Batches + the funnel instead.
4. **Web pool resilience** ([web/lib/data.ts:4-8](web/lib/data.ts#L4)): add `connectionTimeoutMillis: 3000` (today saturation = indefinite hang of request handlers) and `idleTimeoutMillis: 30000`.
5. **`safeUrl()` protocol check** — `events.source_url` / `evidence_url` render as `href` unfiltered in changelog/search/roadmap pages. A poisoned event (see P0-1) could carry `javascript:`. Allow only `https?://`.
6. **Rewrite OR-joins on `entity_edges`** ([data.ts:281,377](web/lib/data.ts#L281)): `on (e.from_entity = d.id or e.to_entity = d.id)` defeats both single-column indexes → UNION ALL form.
7. **Pin dependencies**: `anthropic>=0.40` has no upper bound (`<1` like the others, or lock exact versions); digest-pin `python:3.12-slim`, `node:22-alpine`, `postgres:16-alpine`.
8. **Typed vector param in web search** ([data.ts:147-158](web/lib/data.ts#L147)): the `"[" + vec.join(",") + "]"` literal is safe *today* (floats from Gemini) but architecturally fragile — validate `vec.every(Number.isFinite)` or use the `pgvector` npm codec; also runtime-validate the Gemini response shape before the `number[]` cast (data.ts:129).
9. **Sweep throughput**: `SWEEP_BATCH=200` × LLM latency can exceed the 600s ARQ `job_timeout` during backfill bursts; loop batches until drained (with a time budget) or shrink the AI-lane batch.
10. **Backfill blocking calls**: `subprocess.run` inside `async def backfill()` ([backfill.py:165-167](pipeline/backfill.py#L165)) — wrap in `asyncio.to_thread` (CLI-only today, but it's the pattern the CLI in PROJECT_PLAN §7 will inherit).
11. **N+1 in `brain_mcp.get_context`** ([brain_mcp.py:128-133](pipeline/brain_mcp.py#L128)): one query per neighbor → single `WHERE id = ANY($1::uuid[])`.
12. **Rollup natural-key design note (do before Phase 5)**: `narratives_natural_key (scope, scope_ref, derived_by_version)` doesn't dedupe when `scope_ref IS NULL`. Rule for `rollup.py`: scope_ref is **never null** — use `'2026-W27'` / release tag.

---

## P3 — Low / hygiene (fold into other work)

- **Tests for the two most sensitive functions**: `guard_canonical_key` (hallucination gate) and `passes_ai_gate` (cost gate) have no unit tests. Cases: exact file-path match, module prefix, epic in known-issues, bot filter, ignored-files filter.
- **Dead schema weight**: `metrics_daily.pr_count/bugs_fixed/unique_contributors` are never written; `entity_heat` sums `bugs_fixed` (always 0). Either populate (PROJECT_PLAN §6.1 bug-lane makes them real) or comment them as reserved.
- **Vestigial `embeddings` checkpoint row** (001:93): not in the `PROJECTORS` tuple; remove or document.
- **Frontend hygiene**: add ESLint (`eslint-config-next`) + lint script; bump `tsconfig` target ES2017 → ES2022; `"overrides": {"postcss": ">=8.5.10"}` for the bundled-PostCSS advisory; stable keys instead of array index where a natural key exists.
- **`.env.example`**: replace the dictionary-phrase `POSTGRES_PASSWORD` placeholder with a `$(openssl rand -hex 16)` instruction.
- **CLAUDE.md accuracy**: the frontend is described as ISR but every page is `force-dynamic` (intentional — DB unreachable at build time). Update the doc, or implement time-based revalidation later; don't leave the doc claiming caching that doesn't exist.
- **`MODEL_ROLLUP` constant unused** — tag `# Phase 5` so it reads as intent, not dead code.
- **Log hygiene in web** ([data.ts:18-20](web/lib/data.ts#L18)): log `e.message` + query name, not the full pg error object (contains bind params).
- **HNSW params**: `embeddings_hnsw_idx` uses defaults; make `m`/`ef_construction` explicit when embedding volume grows.
- **Restore drill**: script + runbook entry proving an R2 object restores cleanly (pairs with P0-2).

---

## Sequencing & how this meets the roadmap

```
Week 1  P0-1..4  ─ security fail-closed + backup integrity + 005_hardening.sql (part 1)
Week 2  P1-1..8  ─ migration 005 (part 2), retry/backoff, compose hardening, action validation
Week 3  P2       ─ logging + cost checks (feeds Phase 5), pool/query tuning, pins
ongoing P3       ─ fold into touched files
```

- **Before Phase 4 (knowledge graph):** P1-1 indexes and the P1-8/P2-6 edge-query fixes matter most — the graph view multiplies `entity_edges` query load.
- **Before Phase 5 (rollup + ops):** P2-1 logging (cost dashboard input), P2-3 cache verification, P2-12 scope_ref rule, and the P0-4 replay-recipe fix (the runbook is a Phase 5 exit criterion).
- **Replay impact summary:** only P0-4 (status dedupe) and P1-2 (`derived_by` backfill defaults) touch existing projection rows, and both are additive/dedup-safe — **no full replay required** by anything in this plan. If you want the new `derived_by` values stamped by code rather than defaults, do the standard `truncate → bump logic_version → reset cursor` per projector afterward.

## UI audit — live walkthrough (Chrome, 2026-07-05, localhost:3000)

All 8 routes exercised at 1440×900 and 390×844 (mobile). Semantic search returns ranked results with citations; graph subgraph-by-query, stale-doc table, pyramid, and project chips all work. No failed network requests. Findings, by severity:

### U1 · React hydration error #418 — RESOLVED: environmental, not an app bug
Observed twice in the prod console (`Minified React error #418; args[]=HTML`). **Root-caused 2026-07-05 via `next dev` reproduction attempt:**
- The app has **zero client components** (no `use client` anywhere); all dates are server-formatted fixed ISO strings — nothing in app code can produce a hydration diff.
- Dev build (same code, same live DB via port-forward): **9 loads across all 8 routes, zero hydration warnings**. Prod also stopped reproducing on re-test (8 further loads).
- Live-DOM inspection on :3000 found an element **injected directly into `<body>` by a browser extension** (carrying a JWT; redacted by tooling) — and `curl` of the server HTML confirms it is **absent from the SSR output**. An extension-injected node racing React's shell hydration is the textbook cause of intermittent #418 at the document level.

**Verdict:** no code change. If #418 recurs, verify in a clean Chrome profile/incognito before investigating the app. (Related hygiene: the committed `web/tsconfig.json` has `jsx: "react-jsx"`, which `next dev` rewrites to `"preserve"` on every run, dirtying the tree — commit the Next-normalized version once.)

### U2 · Stat cards ignore the project filter — HIGH (display correctness)
On `/?project=saucevn%2Fdevbrain` the Activity-heat grid re-scopes correctly, but the four header cards (EVENTS 334 · ENTITIES 593 · EDGES 3,334 · LAST ACTIVITY) show the same global numbers as `all`. A reader filtering to one repo sees another repo's totals. **Action:** pass the project param into the stats queries in [web/lib/data.ts](web/lib/data.ts), or label the cards "all projects".

### U3 · `/graph?q=%` — P1-8 confirmed live
The ILIKE-wildcard finding from the code audit reproduces in the running app: `?q=%` silently matches everything and seeds an arbitrary entity ("docs/index.md Bản đồ canonical"). No error, no hint. Fix per P1-8; also show a "no exact match" notice instead of silently picking the first row.

### U4 · Roadmap renders one card per status transition — MEDIUM
"v0.1 — Foundation & deterministic backbone" appears twice (planned 2026-06-20 and shipped 2026-06-24) as visually identical sibling cards — reads as duplicate data. `entity_status_history` is transition history by design; the view should group by milestone and show the latest status with prior transitions as a sub-line/tooltip.

### U5 · Entities page shows "0 touches" on every proposed card — MEDIUM
All visible cards show `0 touches · 2026-06-28` even for clearly active files. If touches comes from a `metrics_daily` window join, it's broken or mis-windowed for these entities — and it's the signal a human uses to decide confirm vs reject. Verify the join in [web/lib/data.ts:189](web/lib/data.ts#L189) (`getProposedEntities`).

### U6 · Changelog shows raw markdown as plain text — LOW/MEDIUM
`summary_md` bullets and body render backticks and `-` literals (e.g. `` `pipeline/backfill_prs.py` ``) as-is. Either render markdown through a sanitizing renderer (no raw HTML — content is LLM-derived) or strip formatting server-side. Also fine to keep plain text as a deliberate choice — then strip the backticks at write time.

### U7 · Pyramid polish — LOW
- "AI Narrative & Search" block: `0 ent · 0 commits` yet 55% maturity — empty blocks could use a subdued "no entities mapped yet" state instead of full-color heat.
- The `critical` risk badge is red-on-amber (low contrast, borderline WCAG); the same badge style on other blocks is fine. Give the badge a solid dark chip background.

**Mobile (390×844):** layout holds — heat grid collapses to 2 columns, nav fits on one line without a hamburger. No horizontal scroll observed. No action needed now; revisit nav if more routes are added.

---

## Verification checklist (definition of done for P0/P1)

- [ ] Receiver returns 503 when `GITHUB_WEBHOOK_SECRET` unset; 401 on bad signature; 400 on missing delivery ID (test all three).
- [ ] `backup.sh` exits non-zero when Postgres is down; no object uploaded (kill db, run once).
- [ ] `truncate events` raises via trigger.
- [ ] Reset `status` cursor to 0 **without** truncating → row count in `entity_status_history` unchanged after sweep.
- [ ] `psql -f 005_hardening.sql` applies cleanly twice (idempotent).
- [ ] `curl :3000` from a non-localhost interface fails; Redis rejects unauthenticated `PING`.
- [ ] `confirmEntity` with a garbage `id` returns a friendly error, not a 500.
- [ ] Worker start with `GEMINI_API_KEY` unset fails at boot, not at first AI event.
- [ ] `ruff check . && ruff format --check .` and existing `pipeline/tests` still green.
