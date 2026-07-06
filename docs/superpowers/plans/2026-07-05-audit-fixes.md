# devbrain Audit Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix all P0/P1 findings + confirmed UI data-correctness bugs (U2/U3/U5) from `IMPROVEMENT_PLAN.md` — fail-closed webhook security, backup integrity, schema hardening (migration 005), pipeline robustness, compose hardening, and web input-validation/query fixes.

**Architecture:** Event-sourced system: `events` (immutable) → per-projector cursor sweep → deterministic projections (`metrics_daily`, `entity_edges`, `entity_status_history`) + AI lane (`narratives`, `embeddings`). Fixes are additive: one new migration file (`005_hardening.sql`), guard/retry code in `pipeline/pipeline.py`, hardening in compose/Dockerfiles, validation + SQL fixes in `web/lib/`. **No full replay is required by any task.**

**Tech Stack:** Python 3.12 · FastAPI · ARQ · asyncpg (no ORM) · Postgres 16 + pgvector · Next.js 15 App Router (Server Components, `pg` pool) · Docker Compose.

## Global Constraints (from CLAUDE.md — non-negotiable)

- `events` is append-only; never write code that mutates event rows.
- Every projection row sets `derived_by` (`'git'` deterministic | `'ai'` extracted) — Task 4/5 close the existing gaps.
- Projection write + cursor advance stay in ONE transaction; LLM/network calls stay OUTSIDE transactions.
- No plain `INSERT` for projections — UPSERT on natural keys.
- Entity UPSERT on-conflict only bumps `last_seen_at` (do not touch `upsert_entity`'s conflict clause).
- NO AI scoring/ranking of people, ever.
- Async everywhere; full type hints; `ruff check . && ruff format .` must pass (run from `pipeline/`).
- SQL is `snake_case`; migrations are ordered hand-written files, applied MANUALLY on live DBs (init-scripts only run on an empty volume).
- Secrets only via env / `.env` (gitignored). Never print or commit secret values.
- Follow existing `pipeline.py` patterns; don't introduce a second style. VN comments in code are normal — keep the voice.
- Commits: small, logical, imperative subject.
- Python tests live in `pipeline/tests/`, plain pytest, pure functions preferred (see `test_pipeline_unit.py` style). Run: `cd pipeline && python -m pytest tests/ -q`.
- Web has NO test framework — verification is `cd web && npx tsc --noEmit` and `npm run build`, plus manual curl steps.
- Do NOT apply migrations or restart services on the LIVE stack — that is the human-gated Rollout section at the end.

---

## Epic A — Receiver fail-closed (P0-1, L-2, L-3, H-5, P1-7)

### Task 1: Webhook guard function — empty secret / missing delivery-id / oversize body

**Files:**
- Modify: `pipeline/pipeline.py` (config block ~line 67; `github_webhook` ~lines 134-148)
- Test: `pipeline/tests/test_receiver_guards.py` (create)

**Interfaces:**
- Produces: `check_webhook_guards(secret: bytes, delivery_id: str, content_length: str | None) -> None` (raises `fastapi.HTTPException`), constant `MAX_BODY_BYTES: int`. Task 7 later adds a log line next to the HMAC check — do not restructure the endpoint beyond what is shown here.

- [ ] **Step 1: Write the failing test**

Create `pipeline/tests/test_receiver_guards.py`:

```python
"""Fail-closed guards for the webhook receiver (audit P0-1, L-2, L-3).

An empty GITHUB_WEBHOOK_SECRET must NEVER be used for HMAC verification —
hmac.new(b"", body) is computable by any attacker → event-store poisoning.
"""
import pytest
from fastapi import HTTPException

import pipeline


def test_guards_reject_empty_secret_with_503():
    with pytest.raises(HTTPException) as ei:
        pipeline.check_webhook_guards(b"", "delivery-1", "100")
    assert ei.value.status_code == 503


def test_guards_reject_missing_delivery_id_with_400():
    # empty string would become dedup key ('github','') and permanently claim it
    with pytest.raises(HTTPException) as ei:
        pipeline.check_webhook_guards(b"secret", "", "100")
    assert ei.value.status_code == 400


def test_guards_reject_oversize_body_with_413():
    with pytest.raises(HTTPException) as ei:
        pipeline.check_webhook_guards(b"secret", "d-1", str(pipeline.MAX_BODY_BYTES + 1))
    assert ei.value.status_code == 413


def test_guards_pass_normal_request():
    pipeline.check_webhook_guards(b"secret", "d-1", "12345")
    pipeline.check_webhook_guards(b"secret", "d-1", None)          # no content-length header
    pipeline.check_webhook_guards(b"secret", "d-1", "not-a-number")  # garbage header ignored
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd pipeline && python -m pytest tests/test_receiver_guards.py -q`
Expected: FAIL — `AttributeError: module 'pipeline' has no attribute 'check_webhook_guards'`

- [ ] **Step 3: Implement the guards**

In `pipeline/pipeline.py`, after the `DIFF_CHAR_CAP  = 12_000` line in the CONFIG block, add:

```python
MAX_BODY_BYTES = 10 * 1024 * 1024   # GitHub tự cap payload 25MB; ta chặn sớm ở 10MB
```

Then directly above the `@app.post("/webhooks/github")` decorator, add:

```python
def check_webhook_guards(secret: bytes, delivery_id: str, content_length: str | None) -> None:
    """Fail-closed guards chạy TRƯỚC khi đọc body (audit P0-1/L-2/L-3).
    Secret rỗng ⇒ 503 (KHÔNG BAO GIỜ verify HMAC bằng key rỗng — attacker tự
    tính được digest). Delivery-id rỗng ⇒ 400 (nó là dedup key G2; chuỗi rỗng
    hợp lệ sẽ chiếm slot ('github','') vĩnh viễn). Body quá lớn ⇒ 413."""
    if not secret:
        raise HTTPException(status_code=503, detail="webhook secret not configured")
    if not delivery_id:
        raise HTTPException(status_code=400, detail="missing X-GitHub-Delivery")
    if content_length is not None and content_length.isdigit() and int(content_length) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")
```

In `github_webhook`, change the first line of the body from:

```python
    body = await request.body()
```

to:

```python
    check_webhook_guards(GITHUB_SECRET, x_github_delivery, request.headers.get("content-length"))
    body = await request.body()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd pipeline && python -m pytest tests/ -q && ruff check . && ruff format --check .`
Expected: all PASS (existing tests unaffected), ruff clean.

- [ ] **Step 5: Commit**

```bash
git add pipeline/pipeline.py pipeline/tests/test_receiver_guards.py
git commit -m "fix(receiver): fail closed on empty webhook secret, missing delivery id, oversize body"
```

---

### Task 2: Honest /health (503 on DB-down) + lifespan + disable /docs

**Files:**
- Modify: `pipeline/pipeline.py` (lines ~113-131: `app = FastAPI()`, `_startup`, `health`)
- Test: `pipeline/tests/test_receiver_guards.py` (append)

**Interfaces:**
- Produces: `health_payload(db_ok: bool) -> tuple[int, dict]`. The FastAPI app is constructed with `lifespan=` and `docs_url=None, redoc_url=None, openapi_url=None`.
- Consumes: nothing from other tasks.

- [ ] **Step 1: Write the failing test** — append to `pipeline/tests/test_receiver_guards.py`:

```python
# ---- /health must speak status codes (probes don't parse bodies) ----------
def test_health_payload_ok():
    assert pipeline.health_payload(True) == (200, {"ok": True, "db": True})


def test_health_payload_db_down_is_503():
    code, body = pipeline.health_payload(False)
    assert code == 503
    assert body == {"ok": False, "db": False}


def test_fastapi_docs_disabled():
    # H-5: swagger/openapi must not be served through the Cloudflare tunnel
    assert pipeline.app.docs_url is None
    assert pipeline.app.redoc_url is None
    assert pipeline.app.openapi_url is None
```

- [ ] **Step 2: Run to verify failure**

Run: `cd pipeline && python -m pytest tests/test_receiver_guards.py -q`
Expected: FAIL — no attribute `health_payload`; docs assertions fail (`/docs` is the default).

- [ ] **Step 3: Implement**

In `pipeline/pipeline.py` add to the import block (top of file, after `from datetime import ...`):

```python
from contextlib import asynccontextmanager
```

and after the existing `from fastapi import ...` line:

```python
from fastapi.responses import JSONResponse
```

Replace the block from `app = FastAPI()` through the end of the `health` function (currently lines 113-131) with:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Thay @app.on_event('startup') (deprecated từ FastAPI 0.93)."""
    app.state.pool = await make_pool()
    from arq import create_pool as arq_pool
    app.state.arq = await arq_pool(RedisSettings.from_dsn(REDIS_URL))
    yield
    await app.state.pool.close()


# H-5: không expose swagger/openapi qua Cloudflare tunnel.
app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def health_payload(db_ok: bool) -> tuple[int, dict]:
    """DB chết ⇒ 503 để probe theo status-code thấy được outage (trước đây
    luôn trả 200 {'ok': True} kể cả khi DB down)."""
    return (200 if db_ok else 503), {"ok": db_ok, "db": db_ok}


@app.get("/health")
async def health() -> JSONResponse:
    """Liveness/readiness cho receiver. Ping DB rẻ; ACK thừa sức < 10s."""
    db_ok = False
    try:
        db_ok = await app.state.pool.fetchval("select 1") == 1
    except Exception:
        db_ok = False
    code, body = health_payload(db_ok)
    return JSONResponse(status_code=code, content=body)
```

(The old `@app.on_event("startup")`-decorated `_startup` function is deleted by this replacement.)

- [ ] **Step 4: Run tests**

Run: `cd pipeline && python -m pytest tests/ -q && ruff check .`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add pipeline/pipeline.py pipeline/tests/test_receiver_guards.py
git commit -m "fix(receiver): 503 health on db outage, lifespan startup, disable exposed api docs"
```

---

## Epic B — Backup integrity (P0-2)

### Task 3: backup.sh — split the pipe, verify the artifact, abort loudly

**Files:**
- Modify: `backup/backup.sh` (full rewrite, 21 lines)

**Interfaces:** none (standalone script; Dockerfile entrypoint already echoes `[backup] FAILED` on non-zero exit — that path finally becomes reachable).

- [ ] **Step 1: Replace `backup/backup.sh` with:**

```sh
#!/bin/sh
# Dump toàn DB → gzip → verify → upload R2. --no-owner/--no-privileges để
# restore portable trên bất kỳ role nào (tránh vướng ownership/RLS lúc khôi phục).
#
# P0-2: /bin/sh KHÔNG có pipefail → `pg_dump | gzip` nuốt lỗi pg_dump và
# upload một gzip rỗng 20-byte "thành công". Tách pipe + verify trước upload.
set -eu

TS=$(date -u +%Y%m%dT%H%M%SZ)
FILE="secondbrain_${TS}.sql.gz"
TMP="/tmp/${FILE}"
TMP_SQL="/tmp/secondbrain_${TS}.sql"
trap 'rm -f "${TMP_SQL}" "${TMP}"' EXIT

echo "[backup ${TS}] pg_dump..."
pg_dump "${DATABASE_URL}" --no-owner --no-privileges > "${TMP_SQL}"
gzip -9 < "${TMP_SQL}" > "${TMP}"
rm -f "${TMP_SQL}"

# Verify: gzip toàn vẹn + kích thước tối thiểu (schema thật không bao giờ <1KB).
gzip -t "${TMP}"
SIZE_BYTES=$(wc -c < "${TMP}")
if [ "${SIZE_BYTES}" -lt 1024 ]; then
  echo "[backup ${TS}] ABORT: dump ${SIZE_BYTES}B < 1KB — nghi rỗng/corrupt, KHÔNG upload"
  exit 1
fi

SIZE=$(du -h "${TMP}" | cut -f1)
echo "[backup ${TS}] upload → s3://${R2_BUCKET}/backups/${FILE} (${SIZE})"
aws s3 cp "${TMP}" "s3://${R2_BUCKET}/backups/${FILE}" --endpoint-url "${R2_ENDPOINT}"

echo "[backup ${TS}] done"

# Retention: nên đặt R2 Lifecycle Rule (xoá object > N ngày) trong
# Cloudflare dashboard — sạch hơn là tự prune bằng script.
```

- [ ] **Step 2: Syntax-check**

Run: `sh -n backup/backup.sh`
Expected: no output, exit 0.

- [ ] **Step 3: Prove the failure path (unreachable DB ⇒ non-zero exit, no upload)**

```bash
docker build -t devbrain-backup-test ./backup
docker run --rm \
  -e DATABASE_URL='postgresql://x:x@127.0.0.1:1/x' \
  -e R2_BUCKET=b -e R2_ENDPOINT='http://127.0.0.1:1' \
  -e AWS_ACCESS_KEY_ID=x -e AWS_SECRET_ACCESS_KEY=x -e AWS_DEFAULT_REGION=auto \
  --entrypoint /usr/local/bin/backup.sh devbrain-backup-test; echo "exit=$?"
```

Expected: `pg_dump` connection error printed, **`exit=` non-zero**, and no `upload →` line. (Before this fix the same scenario printed `upload →` and exited 0.)

- [ ] **Step 4: Commit**

```bash
git add backup/backup.sh
git commit -m "fix(backup): abort on pg_dump failure instead of uploading empty gzip"
```

---

## Epic C — Schema hardening (P0-3, P0-4, P1-1, P1-2)

### Task 4: Migration `005_hardening.sql` + replay-recipe doc fix

**Files:**
- Create: `supabase/migrations/005_hardening.sql`
- Modify: `supabase/migrations/001_schema.sql` (§9 comment block only, ~lines 431-450)

**Interfaces:**
- Produces (Task 5 relies on these): unique partial index `entity_status_hist_natural_key (entity_id, source_event_id) where source_event_id is not null`; columns `metrics_daily.derived_by`, `entity_status_history.derived_by`, `narratives.derived_by`, `embeddings.derived_by` (text, defaults `'git'`/`'git'`/`'ai'`/`'ai'`).

- [ ] **Step 1: Create `supabase/migrations/005_hardening.sql`:**

```sql
-- =====================================================================
-- 005_hardening.sql — audit fixes (P0-3, P0-4, P1-1, P1-2)
-- =====================================================================
-- Idempotent (chạy 2 lần vô hại). Áp TAY lên DB đang chạy:
--   docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
--     < supabase/migrations/005_hardening.sql
-- Không yêu cầu replay: mọi thay đổi là additive/dedup-safe.
-- =====================================================================

-- ---------------------------------------------------------------------
-- 1) P0-3: events là append-only nhưng row-trigger KHÔNG bắt TRUNCATE.
--    Nguy hiểm kép vì quy trình replay chính thống là "truncate <projection>"
--    — gõ nhầm tên bảng là mất source of truth.
-- ---------------------------------------------------------------------
drop trigger if exists events_no_truncate on events;
create trigger events_no_truncate before truncate on events
  for each statement execute function prevent_event_mutation();

-- ---------------------------------------------------------------------
-- 2) P0-4: entity_status_history không có natural key → reset cursor mà
--    quên truncate là double-count roadmap. Dedupe trước, rồi khoá lại.
-- ---------------------------------------------------------------------
delete from entity_status_history a
  using entity_status_history b
  where a.entity_id = b.entity_id
    and a.source_event_id = b.source_event_id
    and a.source_event_id is not null
    and a.ctid > b.ctid;

create unique index if not exists entity_status_hist_natural_key
  on entity_status_history (entity_id, source_event_id)
  where source_event_id is not null;

-- ---------------------------------------------------------------------
-- 3) P1-1: index khớp query pattern thật (web/lib/data.ts + views)
-- ---------------------------------------------------------------------
create index if not exists narratives_period_start_idx
  on narratives (period_start desc nulls last);                 -- getChangelog sort
create index if not exists embeddings_source_id_idx
  on embeddings (source_id) where source_id is not null;        -- searchBrain/getChangelog join
create index if not exists entity_status_hist_event_idx
  on entity_status_history (source_event_id) where source_event_id is not null;  -- roadmap view join
create index if not exists entities_kind_project_idx
  on entities (entity_kind, project);                           -- getHotFiles filter
create index if not exists entities_resolution_project_idx
  on entities (resolution_status, project);                     -- getProposedEntities filter

-- ---------------------------------------------------------------------
-- 4) P1-2: golden rule #4 — MỌI projection row mang derived_by.
--    metrics_daily & entity_status_history thuần git; narratives & embeddings
--    thuần ai. entity_status_history cho phép 'human' (PROJECT_PLAN §2.2:
--    status note đặt tay).
-- ---------------------------------------------------------------------
alter table metrics_daily         add column if not exists derived_by text not null default 'git';
alter table entity_status_history add column if not exists derived_by text not null default 'git';
alter table narratives            add column if not exists derived_by text not null default 'ai';
alter table embeddings            add column if not exists derived_by text not null default 'ai';

-- ---------------------------------------------------------------------
-- 5) P1-1: CHECK constraints cho text-enum (typo ghi sai lane sẽ nổ sớm
--    thay vì âm thầm lệch số liệu). drop+add = idempotent.
-- ---------------------------------------------------------------------
alter table contributions drop constraint if exists contributions_derived_by_check;
alter table contributions add constraint contributions_derived_by_check
  check (derived_by in ('git', 'ai'));

alter table entity_edges drop constraint if exists entity_edges_derived_by_check;
alter table entity_edges add constraint entity_edges_derived_by_check
  check (derived_by in ('git', 'ai'));

alter table metrics_daily drop constraint if exists metrics_daily_derived_by_check;
alter table metrics_daily add constraint metrics_daily_derived_by_check
  check (derived_by in ('git', 'ai'));

alter table entity_status_history drop constraint if exists entity_status_history_derived_by_check;
alter table entity_status_history add constraint entity_status_history_derived_by_check
  check (derived_by in ('git', 'ai', 'human'));

alter table narratives drop constraint if exists narratives_derived_by_check;
alter table narratives add constraint narratives_derived_by_check
  check (derived_by in ('git', 'ai'));

alter table embeddings drop constraint if exists embeddings_derived_by_check;
alter table embeddings add constraint embeddings_derived_by_check
  check (derived_by in ('git', 'ai'));

alter table entities drop constraint if exists entities_resolution_status_check;
alter table entities add constraint entities_resolution_status_check
  check (resolution_status in ('proposed', 'confirmed', 'rejected'));

alter table entities drop constraint if exists entities_status_check;
alter table entities add constraint entities_status_check
  check (status in ('active', 'deprecated', 'merged'));

alter table entity_status_history drop constraint if exists entity_status_history_status_check;
alter table entity_status_history add constraint entity_status_history_status_check
  check (status in ('planned', 'in_progress', 'shipped', 'deprecated'));
```

- [ ] **Step 2: Update the §9 replay recipe comment in `001_schema.sql`.** Find this text near the bottom:

```
--   -- Sau đó worker tự đọc từ seq 0, replay toàn bộ event → dựng lại graph.
--   -- Tương tự cho 'metrics' (truncate metrics_daily), 'embeddings'
--   -- (truncate embeddings + re-embed), 'narrative' (truncate narratives).
```

and replace with:

```
--   -- Sau đó worker tự đọc từ seq 0, replay toàn bộ event → dựng lại graph.
--   -- Tương tự cho 'metrics' (truncate metrics_daily), 'embeddings'
--   -- (truncate embeddings + re-embed), 'narrative' (truncate narratives),
--   -- 'status' (truncate entity_status_history — có natural key từ 005 nên
--   -- reset cursor mà quên truncate cũng KHÔNG double-count, nhưng truncate
--   -- vẫn là đường sạch).
```

- [ ] **Step 3: Verify on a scratch DB (init-scripts auto-apply 000→005 on empty volume, then re-apply 005 to prove idempotency, then prove the TRUNCATE guard):**

```bash
docker run --rm -d --name devbrain-mig-test -e POSTGRES_PASSWORD=test \
  -v "$PWD/supabase/migrations:/docker-entrypoint-initdb.d:ro" \
  -v "$PWD/supabase/migrations:/mig:ro" \
  pgvector/pgvector:pg16
sleep 15
docker logs devbrain-mig-test 2>&1 | grep -i error   # expect NO output
docker exec devbrain-mig-test psql -U postgres -f /mig/005_hardening.sql          # 2nd apply
docker exec devbrain-mig-test psql -U postgres -c "truncate events;" ; echo "truncate exit=$?"
```

Expected: no errors from init; second apply of 005 completes without error; the truncate prints `ERROR:  events is append-only: TRUNCATE is not allowed` and `truncate exit=1`.

Tear down: `docker rm -f devbrain-mig-test` (Task 5 recreates it with a published port).

- [ ] **Step 4: Commit**

```bash
git add supabase/migrations/005_hardening.sql supabase/migrations/001_schema.sql
git commit -m "feat(schema): 005 hardening - truncate guard, status natural key, indexes, derived_by, checks"
```

---

### Task 5: Projector code — idempotent status writes, `derived_by`, checkpoint self-heal

**Files:**
- Modify: `pipeline/pipeline.py` (`apply_status` ~line 617, `apply_metrics` ~line 571, `apply_narrative` inserts ~lines 699-731, `run_one_projector` ~line 511, `process_event` ~line 534, worker `on_startup` ~line 810)
- Test: `pipeline/tests/test_status_idempotent_db.py` (create; skipped unless `TEST_DATABASE_URL` set)

**Interfaces:**
- Consumes: Task 4's unique index + `derived_by` columns (integration test needs a scratch DB with migrations applied).
- Produces: no new public names; behavior changes only.

- [ ] **Step 1: Write the (skippable) integration test** — create `pipeline/tests/test_status_idempotent_db.py`:

```python
"""P0-4: apply_status phải idempotent — replay không được double-count roadmap.

Chạy khi có TEST_DATABASE_URL (scratch container, xem Step 2):
  TEST_DATABASE_URL=postgresql://postgres:test@127.0.0.1:15433/postgres \
    python -m pytest tests/test_status_idempotent_db.py -q
"""
import asyncio
import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="needs TEST_DATABASE_URL (scratch db)"
)


def _milestone_payload() -> dict:
    return {
        "action": "created",
        "milestone": {"number": 1, "title": "v9.9 test", "state": "open"},
        "repository": {"full_name": "test/repo"},
    }


def test_apply_status_twice_inserts_once():
    import asyncpg

    import pipeline

    async def run() -> int:
        conn = await asyncpg.connect(os.environ["TEST_DATABASE_URL"])
        await pipeline._init_connection(conn)
        try:
            eid = await conn.fetchval(
                """
                insert into events (source, source_event_id, event_type, actor, payload, occurred_at)
                values ('github', 'it-status-1', 'milestone.created', 'tester', $1, now())
                on conflict (source, source_event_id) do nothing
                returning id
                """,
                _milestone_payload(),
            )
            if eid is None:  # re-run: event already stored
                eid = await conn.fetchval(
                    "select id from events where source='github' and source_event_id='it-status-1'"
                )
            ev = {
                "event_type": "milestone.created",
                "payload": _milestone_payload(),
                "occurred_at": await conn.fetchval("select occurred_at from events where id=$1", eid),
                "id": eid,
            }
            await pipeline.apply_status(conn, ev, 1)
            await pipeline.apply_status(conn, ev, 1)  # replay same event
            return await conn.fetchval(
                "select count(*) from entity_status_history where source_event_id=$1", eid
            )
        finally:
            await conn.close()

    assert asyncio.run(run()) == 1
```

- [ ] **Step 2: Run to verify it fails** (scratch DB with the migrations, Postgres published on loopback):

```bash
docker rm -f devbrain-mig-test 2>/dev/null
docker run --rm -d --name devbrain-mig-test -e POSTGRES_PASSWORD=test \
  -p 127.0.0.1:15433:5432 \
  -v "$PWD/supabase/migrations:/docker-entrypoint-initdb.d:ro" pgvector/pgvector:pg16
sleep 15
cd pipeline && TEST_DATABASE_URL=postgresql://postgres:test@127.0.0.1:15433/postgres \
  python -m pytest tests/test_status_idempotent_db.py -q
```

Expected: FAIL — count is 2 (bare INSERT inserts twice).

- [ ] **Step 3: Implement the pipeline changes.** All in `pipeline/pipeline.py`:

**(a) `apply_status`** — replace the `conn.execute` at the end with:

```python
    await conn.execute(
        """
        insert into entity_status_history (entity_id, status, changed_at, source_event_id, derived_by)
        values ($1, $2, $3, $4, 'git')
        on conflict (entity_id, source_event_id) where source_event_id is not null
          do nothing
        """,
        eid, status, ev["occurred_at"], ev["id"],
    )
```

**(b) `apply_metrics`** — the INSERT gains `derived_by`:

```python
        await conn.execute(
            """
            insert into metrics_daily (entity_id, day, commit_count, lines_added, lines_removed, derived_by)
            values ($1, $2, 1, $3, $4, 'git')
            on conflict (entity_id, day) do update
              set commit_count = metrics_daily.commit_count + 1,
                  lines_added  = metrics_daily.lines_added  + excluded.lines_added,
                  lines_removed= metrics_daily.lines_removed+ excluded.lines_removed
            """,
            eid, day, adds, dels,
        )
```

**(c) `apply_narrative`** — narratives INSERT: add `derived_by` column with value `'ai'`:

```python
    nid = await conn.fetchval(
        """
        insert into narratives (scope, scope_ref, title, body_md, highlights, source_event_ids,
                                model, derived_by, derived_by_version, input_hash, period_start, period_end)
        values ('pr', $1, $2, $3, $4, $5, $6, 'ai', $7, $8, $9, $9)
        on conflict (scope, scope_ref, derived_by_version) do update
          set body_md=excluded.body_md, highlights=excluded.highlights, input_hash=excluded.input_hash
        returning id
        """,
```

(the parameter tuple below it is unchanged). Same for the embeddings INSERT:

```python
    await conn.execute(
        """
        insert into embeddings (source_kind, source_id, content, embedding, occurred_at,
                                derived_by, derived_by_version, content_hash)
        values ('pr_summary', $1, $2, $3, $4, 'ai', $5, $6)
        on conflict (source_kind, source_id, derived_by_version) do update
          set embedding=excluded.embedding, content=excluded.content, content_hash=excluded.content_hash
        """,
        nid, analysis.summary_md, vec, ev["occurred_at"], version, ch,
    )
```

**(d) `run_one_projector`** — P1-3 crash guard. After the `cp = await pool.fetchrow(...)` call add:

```python
            if cp is None:
                # Migration seed cho projector này chưa được áp (vd 'status'
                # chỉ seed ở 003) → skip êm thay vì TypeError mỗi sweep.
                print(f"[sweep] projector '{name}' has no checkpoint row; skipping (apply migrations)")
                return
```

**(e) `process_event`** — initialize `precomputed` explicitly (latent NameError if PROJECTORS grows). Change:

```python
    if name == "narrative":
```

to:

```python
    precomputed = None
    if name == "narrative":
```

**(f) worker `on_startup`** — self-heal checkpoint rows:

```python
async def on_startup(ctx: dict) -> None:
    ctx["pool"] = await make_pool()
    # Self-heal: mỗi projector trong PROJECTORS phải có checkpoint row —
    # code và migration không được phép drift (P1-3).
    async with ctx["pool"].acquire() as conn:
        for name in PROJECTORS:
            await conn.execute(
                "insert into projection_checkpoints (projector_name) values ($1) on conflict do nothing",
                name,
            )
```

- [ ] **Step 4: Run tests**

```bash
cd pipeline && TEST_DATABASE_URL=postgresql://postgres:test@127.0.0.1:15433/postgres \
  python -m pytest tests/ -q && ruff check . && ruff format --check .
docker rm -f devbrain-mig-test
```

Expected: all PASS including the new integration test (count == 1).

- [ ] **Step 5: Commit**

```bash
git add pipeline/pipeline.py pipeline/tests/test_status_idempotent_db.py
git commit -m "fix(projector): idempotent status writes, derived_by on all lanes, checkpoint self-heal"
```

---

## Epic D — Pipeline robustness (P1-4, P1-7, P2-1)

### Task 6: Retry/backoff for Gemini + GitHub; poison-event policy for the AI lane

**Files:**
- Modify: `pipeline/pipeline.py` (imports; `anthropic` client ~line 71; `embed` ~line 425; `enrich_pr_payload` ~line 777; `maybe_summarize` ~line 641)
- Test: `pipeline/tests/test_retries.py` (create)

**Interfaces:**
- Produces: `class PoisonEvent(Exception)`, `RETRYABLE_STATUS: set[int]`, `POISON_STATUS: set[int]`, `async def with_retries(fn, *, attempts: int = 4, base_delay: float = 1.0)`.

- [ ] **Step 1: Write the failing tests** — create `pipeline/tests/test_retries.py`:

```python
"""P1-4: transient API errors must back off and retry; permanently-failing
events (404/410/422) must become PoisonEvent so the cursor can advance."""
import asyncio

import httpx
import pytest

import pipeline


def _status_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "http://x")
    return httpx.HTTPStatusError("boom", request=req, response=httpx.Response(code, request=req))


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch):
    async def instant(_secs: float) -> None:
        return None
    monkeypatch.setattr(asyncio, "sleep", instant)


def test_retries_transient_503_then_succeeds():
    calls = {"n": 0}

    async def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _status_error(503)
        return "ok"

    assert asyncio.run(pipeline.with_retries(fn)) == "ok"
    assert calls["n"] == 3


def test_404_raises_poison_event_immediately():
    calls = {"n": 0}

    async def fn():
        calls["n"] += 1
        raise _status_error(404)

    with pytest.raises(pipeline.PoisonEvent):
        asyncio.run(pipeline.with_retries(fn))
    assert calls["n"] == 1


def test_non_retryable_400_raises_original_error():
    async def fn():
        raise _status_error(400)

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(pipeline.with_retries(fn))


def test_exhausted_retries_raise_last_error():
    async def fn():
        raise _status_error(429)

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(pipeline.with_retries(fn, attempts=2))
```

- [ ] **Step 2: Run to verify failure**

Run: `cd pipeline && python -m pytest tests/test_retries.py -q`
Expected: FAIL — no attribute `with_retries`.

- [ ] **Step 3: Implement.** In `pipeline/pipeline.py`:

**(a)** Add `import asyncio` and `import random` to the stdlib import block at the top (alphabetical, next to `import hashlib`/`import hmac`). Add `import httpx` there too, and delete the two function-local `import httpx` lines inside `embed` and `enrich_pr_payload`. Change the anthropic import to `from anthropic import APIStatusError, AsyncAnthropic`.

**(b)** Change the client construction (line ~71) to use SDK retries:

```python
anthropic = AsyncAnthropic(api_key=ANTHROPIC_KEY, max_retries=4)
```

**(c)** Add above the `AI CALLS` section header:

```python
# =====================================================================
# RETRY / POISON POLICY  (P1-4: 1 lỗi 429 không được ghim cursor mãi mãi)
# =====================================================================
class PoisonEvent(Exception):
    """Event mà AI lane không bao giờ xử lý được (vd PR đã bị xoá → 404).
    maybe_summarize bắt nó → return None → cursor đi tiếp; event vẫn nằm
    nguyên trong log nên một lần replay sau (logic mới) vẫn nhặt lại được."""


RETRYABLE_STATUS = {429, 500, 502, 503, 504}
POISON_STATUS = {404, 410, 422}


async def with_retries(fn, *, attempts: int = 4, base_delay: float = 1.0):
    """Exponential backoff + jitter cho HTTP call (Gemini/GitHub). Status
    poison ⇒ PoisonEvent ngay; status khác ngoài RETRYABLE ⇒ raise nguyên."""
    for attempt in range(attempts):
        try:
            return await fn()
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status in POISON_STATUS:
                raise PoisonEvent(f"http {status}: {e.request.url}") from e
            if status not in RETRYABLE_STATUS or attempt == attempts - 1:
                raise
        except httpx.TransportError:
            if attempt == attempts - 1:
                raise
        await asyncio.sleep(base_delay * (2 ** attempt) + random.random() * 0.2)
```

**(d)** `embed` — wrap the network call:

```python
async def embed(text: str, task_type: str = "RETRIEVAL_DOCUMENT") -> list[float]:
    """Gemini embeddings (gemini-embedding-001) ở EMBED_DIM dims → khớp
    vector(EMBED_DIM) trong schema (VN+EN multilingual). task_type:
    RETRIEVAL_DOCUMENT khi lưu, RETRIEVAL_QUERY khi search. Dims <3072 nên
    normalize về unit length cho cosine (<=>)."""
    async def _call() -> dict:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(
                "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-001:embedContent",
                params={"key": os.environ["GEMINI_API_KEY"]},
                json={
                    "model": "models/gemini-embedding-001",
                    "content": {"parts": [{"text": text}]},
                    "taskType": task_type,
                    "outputDimensionality": EMBED_DIM,
                },
            )
            r.raise_for_status()
            return r.json()

    return _normalize(_parse_gemini_embedding(await with_retries(_call)))
```

**(e)** `enrich_pr_payload` — wrap both GitHub calls (a deleted PR → 404 → PoisonEvent):

```python
async def enrich_pr_payload(payload: dict) -> dict:
    """Webhook PR thiếu diff/files → fetch qua GitHub API. Trả BẢN SAO đã
    enrich; thiếu repo/number thì trả nguyên payload."""
    pr = payload.get("pull_request") or {}
    repo = (payload.get("repository") or {}).get("full_name")
    number = pr.get("number")
    if not repo or not number:
        return payload
    token = os.environ.get("GITHUB_TOKEN", "")
    auth = {"Authorization": f"Bearer {token}"} if token else {}
    base = f"https://api.github.com/repos/{repo}/pulls/{number}"

    async def _fetch() -> tuple[list[dict], str]:
        async with httpx.AsyncClient(timeout=30) as c:
            fr = await c.get(f"{base}/files", params={"per_page": 100},
                             headers={**auth, "Accept": "application/vnd.github+json"})
            fr.raise_for_status()
            dr = await c.get(base, headers={**auth, "Accept": "application/vnd.github.v3.diff"})
            dr.raise_for_status()
            return fr.json(), dr.text

    files, diff = await with_retries(_fetch)
    return _build_enriched_pr(payload, files, diff, extract_linked_issues(pr.get("body")))
```

**(f)** `maybe_summarize` — catch poison and advance:

```python
async def maybe_summarize(pool, ev, version: int):
    """Trả (analysis, input_hash, embedding, enriched) hoặc None.
    G4/C5: nếu input_hash đã tồn tại ⇒ SKIP luôn LLM (cache hit).
    PoisonEvent ⇒ log + None: cursor đi tiếp, event nằm lại log chờ replay."""
    if not passes_ai_gate(ev):                      # C1 funnel
        return None
    try:
        enriched = await enrich_pr_payload(ev["payload"])   # diff/files/issues qua GitHub API (ngoài tx)
        pr = parse_pr(enriched)
        ih = sha256(str(PROMPT_VERSION), pr["title"], pr.get("body", ""), pr["diff"][:DIFF_CHAR_CAP])

        exists = await pool.fetchval("select 1 from narratives where input_hash=$1 limit 1", ih)
        if exists:                                   # C5: đã tóm tắt input y hệt → khỏi trả tiền lại
            return None

        analysis = await summarize_pr(pr)            # ← LLM call duy nhất (Haiku, SDK tự retry)
        vec = await embed(analysis.summary_md)       # embed bản summary (rẻ hơn embed cả diff)
    except PoisonEvent as e:
        print(f"[ai-lane] poison event seq={ev['seq']}: {e} — skipping")
        return None
    except APIStatusError as e:
        if e.status_code in POISON_STATUS:
            print(f"[ai-lane] poison event seq={ev['seq']}: anthropic {e.status_code} — skipping")
            return None
        raise
    return analysis, ih, vec, enriched
```

- [ ] **Step 4: Run tests**

Run: `cd pipeline && python -m pytest tests/ -q && ruff check . && ruff format --check .`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add pipeline/pipeline.py pipeline/tests/test_retries.py
git commit -m "feat(pipeline): retry with backoff on gemini/github, poison-event skip for ai lane"
```

---

### Task 7: Structured logging + fail-fast secret validation

**Files:**
- Modify: `pipeline/pipeline.py` (imports; config block; HMAC check in `github_webhook`; `lifespan`; worker `on_startup`; the `print()` calls added in Tasks 5/6)
- Test: `pipeline/tests/test_receiver_guards.py` (append)

**Interfaces:**
- Produces: `require_env(*names: str) -> None` (raises `RuntimeError` listing missing vars), module-level `logger = logging.getLogger("pipeline")`.

- [ ] **Step 1: Write the failing test** — append to `pipeline/tests/test_receiver_guards.py`:

```python
# ---- fail-fast secrets: worker phải chết lúc boot, không phải lúc event AI đầu tiên
def test_require_env_raises_listing_missing(monkeypatch):
    monkeypatch.delenv("X_AUDIT_A", raising=False)
    monkeypatch.setenv("X_AUDIT_B", "set")
    with pytest.raises(RuntimeError) as ei:
        pipeline.require_env("X_AUDIT_A", "X_AUDIT_B")
    assert "X_AUDIT_A" in str(ei.value)
    assert "X_AUDIT_B" not in str(ei.value)


def test_require_env_passes_when_all_set(monkeypatch):
    monkeypatch.setenv("X_AUDIT_A", "1")
    monkeypatch.setenv("X_AUDIT_B", "2")
    pipeline.require_env("X_AUDIT_A", "X_AUDIT_B")
```

- [ ] **Step 2: Run to verify failure**

Run: `cd pipeline && python -m pytest tests/test_receiver_guards.py -q`
Expected: FAIL — no attribute `require_env`.

- [ ] **Step 3: Implement.** In `pipeline/pipeline.py`:

**(a)** Add `import logging` to the stdlib imports. After the CONFIG block (below `PROJECTORS = (...)`), add:

```python
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("pipeline")


def require_env(*names: str) -> None:
    """Fail-fast lúc boot: thiếu secret thì chết to & sớm, không phải chết
    âm thầm ở event AI đầu tiên sau nhiều giờ chạy (P1-7)."""
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise RuntimeError(f"missing required env: {', '.join(missing)}")
```

**(b)** In `lifespan` (receiver), first line of the body: `require_env("GITHUB_WEBHOOK_SECRET")`.

**(c)** In worker `on_startup`, first line of the body: `require_env("ANTHROPIC_API_KEY", "GEMINI_API_KEY")`.

**(d)** HMAC failure gets a security log. In `github_webhook`:

```python
    if not hmac.compare_digest(digest, x_hub_signature_256):
        logger.warning("webhook HMAC verification failed (delivery=%s)", x_github_delivery)
        raise HTTPException(status_code=401, detail="bad signature")
```

**(e)** Replace the `print(...)` calls added by Tasks 5/6:
- `run_one_projector`: `logger.warning("projector '%s' has no checkpoint row; skipping (apply migrations)", name)`
- `maybe_summarize` (both spots): `logger.warning("ai-lane poison event seq=%s: %s — skipping", ev["seq"], e)` and `logger.warning("ai-lane poison event seq=%s: anthropic %s — skipping", ev["seq"], e.status_code)`.

- [ ] **Step 4: Run tests**

Run: `cd pipeline && python -m pytest tests/ -q && ruff check . && ruff format --check .`
Expected: all PASS. Also confirm no stray prints: `grep -n "print(" pipeline/pipeline.py` → no matches.

- [ ] **Step 5: Commit**

```bash
git add pipeline/pipeline.py pipeline/tests/test_receiver_guards.py
git commit -m "feat(pipeline): structured logging, hmac-failure audit log, fail-fast env validation"
```

---

## Epic E — Infra hardening (P1-5, H-3)

### Task 8: Compose — loopback web port, Redis auth, pinned cloudflared

**Files:**
- Modify: `docker-compose.yml`
- Modify: `.env.example`

**Interfaces:** compose injects `REDIS_URL=redis://:${REDIS_PASSWORD}@redis:6379` into receiver/worker (overrides the `.env` value because `environment:` beats `env_file:`).

- [ ] **Step 1: Edit `docker-compose.yml`:**

**(a)** Extend the shared env anchor:

```yaml
x-db-env: &db-env
  DATABASE_URL: postgresql://${POSTGRES_USER:-postgres}:${POSTGRES_PASSWORD}@db:5432/${POSTGRES_DB:-secondbrain}
  REDIS_URL: redis://:${REDIS_PASSWORD}@redis:6379
```

**(b)** Redis service — require auth (healthcheck needs the var inside the container):

```yaml
  redis:
    image: redis:7-alpine
    command: ["redis-server", "--appendonly", "yes", "--requirepass", "${REDIS_PASSWORD}"]
    environment:
      REDIS_PASSWORD: ${REDIS_PASSWORD}
    volumes:
      - redis-data:/data
    healthcheck:
      test: ["CMD-SHELL", "redis-cli -a \"$$REDIS_PASSWORD\" --no-auth-warning ping | grep PONG"]
      interval: 5s
      timeout: 3s
      retries: 5
    restart: unless-stopped
```

**(c)** Web service ports — loopback only (H-2: `0.0.0.0` bypasses Cloudflare Access):

```yaml
    ports:
      - "127.0.0.1:3000:3000"
```

**(d)** Pin cloudflared. Get the current digest:

```bash
docker pull cloudflare/cloudflared:latest
docker image inspect --format '{{index .RepoDigests 0}}' cloudflare/cloudflared:latest
```

The command prints `cloudflare/cloudflared@sha256:…` — use that exact printed string as the image value:

```yaml
  cloudflared:
    image: cloudflare/cloudflared@sha256:<paste-digest-from-command-above>
```

- [ ] **Step 2: Edit `.env.example`** — in the Postgres block replace the password line with:

```
POSTGRES_PASSWORD=          # bắt buộc — sinh bằng: openssl rand -hex 16
```

and replace the Redis block with:

```
# ----- Redis (compose tự dựng REDIS_URL từ REDIS_PASSWORD) ------------
REDIS_PASSWORD=             # bắt buộc — sinh bằng: openssl rand -hex 16
# REDIS_URL chỉ cần khi chạy ngoài compose: redis://:<REDIS_PASSWORD>@localhost:6379
```

- [ ] **Step 3: Verify config renders and redis auth works (scratch, does not touch the live stack):**

```bash
REDIS_PASSWORD=testpass POSTGRES_PASSWORD=x TUNNEL_TOKEN=x docker compose config -q && echo "config OK"
docker run --rm redis:7-alpine sh -c \
  'redis-server --requirepass testpass --daemonize yes && sleep 1 \
   && (redis-cli ping; true) && redis-cli -a testpass --no-auth-warning ping'
```

Expected: `config OK`; then `NOAUTH Authentication required.` for the bare ping and `PONG` for the authenticated one.

- [ ] **Step 4: Commit**

```bash
git add docker-compose.yml .env.example
git commit -m "fix(compose): loopback web port, redis requirepass, pin cloudflared digest"
```

> **Rollout note (human-gated):** the live `.env` needs `REDIS_PASSWORD=` added before the next `docker compose up`. Listed in the Rollout section.

---

### Task 9: Run containers as non-root

**Files:**
- Modify: `pipeline/Dockerfile`
- Modify: `web/Dockerfile`
- Modify: `backup/Dockerfile`

**Interfaces:** none.

- [ ] **Step 1: `pipeline/Dockerfile`** — after the final `COPY . .` and before `EXPOSE 8000`, add:

```dockerfile
RUN useradd --system --create-home --uid 10001 app \
    && chown -R app:app /app
USER app
```

- [ ] **Step 2: `web/Dockerfile`** — in the `runner` stage, change the three COPY lines and add USER (the `node` user ships with `node:22-alpine`):

```dockerfile
FROM node:22-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1
COPY --from=builder --chown=node:node /app/public ./public
COPY --from=builder --chown=node:node /app/.next/standalone ./
COPY --from=builder --chown=node:node /app/.next/static ./.next/static
USER node
EXPOSE 3000
CMD ["node", "server.js"]
```

- [ ] **Step 3: `backup/Dockerfile`** — the `postgres:16-alpine` base already has a `postgres` user; add directly before `ENTRYPOINT`:

```dockerfile
USER postgres
```

- [ ] **Step 4: Verify each image builds and runs unprivileged:**

```bash
docker build -t t-pipe ./pipeline && docker run --rm --entrypoint id t-pipe
docker build -t t-backup ./backup && docker run --rm --entrypoint id t-backup
docker build -t t-web ./web && docker run --rm --entrypoint id t-web
```

Expected: each `id` prints a non-zero uid (`app`/`postgres`/`node`), never `uid=0(root)`.

- [ ] **Step 5: Commit**

```bash
git add pipeline/Dockerfile web/Dockerfile backup/Dockerfile
git commit -m "fix(docker): run pipeline, web, backup containers as non-root users"
```

---

## Epic F — Web fixes (P1-6, P1-8/U3, U2, U5, H3-web, M1, M6)

### Task 10: `confirmEntity` input validation + root error boundary

**Files:**
- Modify: `web/lib/actions.ts` (whole file, 12 lines)
- Create: `web/app/error.tsx`

**Interfaces:**
- Consumes: `confirmEntityDb(id, action, displayName?)` from `web/lib/data.ts` (unchanged).

- [ ] **Step 1: Replace `web/lib/actions.ts` with:**

```ts
"use server";

import { revalidatePath } from "next/cache";
import { confirmEntityDb } from "./data";

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

// P1-6: action này ghi field HUMAN-OWNED (display_name, resolution_status) —
// thứ duy nhất replay không dựng lại được. Validate chặt, không bao giờ để
// lỗi Postgres thô văng qua RSC boundary thành trang 500.
export async function confirmEntity(formData: FormData) {
  const id = String(formData.get("id") ?? "");
  if (!UUID_RE.test(id)) return; // id thiếu/hỏng ('null') → no-op, không phải 500
  const action = String(formData.get("action")) === "reject" ? "reject" : "confirm";
  const raw = formData.get("display_name");
  const displayName = raw ? String(raw).trim().slice(0, 200) : undefined;
  try {
    await confirmEntityDb(id, action, displayName || undefined);
  } catch (e) {
    console.error("[actions] confirmEntity failed:", e instanceof Error ? e.message : String(e));
    return;
  }
  revalidatePath("/entities");
}
```

- [ ] **Step 2: Create `web/app/error.tsx`:**

```tsx
"use client";

// Root error boundary: lỗi server chưa bắt render trang này thay vì trang
// 500 thô của Next (dev mode lộ stack).
export default function Error({ reset }: { error: Error; reset: () => void }) {
  return (
    <div className="mx-auto max-w-3xl px-5 py-24 text-center">
      <h1 className="display text-3xl text-fg">Có lỗi xảy ra</h1>
      <p className="mt-3 font-mono text-sm text-muted">Trang gặp lỗi tạm thời — thử lại.</p>
      <button
        onClick={reset}
        className="mt-6 rounded-lg border border-accent/50 bg-accent/10 px-5 py-2 font-mono text-sm text-accent transition-colors hover:bg-accent/20"
      >
        retry →
      </button>
    </div>
  );
}
```

- [ ] **Step 3: Verify**

Run: `cd web && npx tsc --noEmit && npm run build`
Expected: both succeed.

Manual check (optional, needs running stack): submit the `/entities` confirm form with a devtools-deleted `id` field → page stays up, no 500.

- [ ] **Step 4: Commit**

```bash
git add web/lib/actions.ts web/app/error.tsx
git commit -m "fix(web): validate confirmEntity input, add root error boundary"
```

---

### Task 11: Escape ILIKE wildcards in `getSubgraph` (U3/P1-8 web half)

**Files:**
- Modify: `web/lib/data.ts` (`getSubgraph`, ~lines 359-381; add helper near `stripFor`)

**Interfaces:**
- Produces: `escapeLike(s: string): string` (module-private). Postgres default LIKE escape char is `\` — no `ESCAPE` clause needed.

- [ ] **Step 1: Add the helper** next to the `stripFor` definition:

```ts
// U3/P1-8: escape LIKE/ILIKE metacharacters in user input — '?q=%' would
// otherwise match every row and silently seed an arbitrary entity.
const escapeLike = (s: string) => s.replace(/[\\%_]/g, "\\$&");
```

- [ ] **Step 2: Change the seed-resolution query** in `getSubgraph` to:

```ts
    const seedRow = seed
      ? (
          await pool.query(
            `select e.id, ${STRIP} as key, e.display_name as name
             from entities e
             where (${STRIP}) = $1 or e.display_name ilike $2 or e.canonical_key like '%/' || $2
             order by e.last_seen_at desc limit 1`,
            [seed, escapeLike(seed)]
          )
        ).rows[0]
```

(the no-seed fallback branch is unchanged). With escaping, `?q=%` now matches nothing → `!seedRow` → the existing empty-subgraph return — the page renders its empty state instead of a random entity.

- [ ] **Step 3: Verify**

Run: `cd web && npx tsc --noEmit && npm run build`
Expected: success.

Manual check (running stack): `/graph?q=%` renders the empty graph panel; `/graph?q=main.py` still resolves.

- [ ] **Step 4: Commit**

```bash
git add web/lib/data.ts
git commit -m "fix(web): escape ilike wildcards in graph seed lookup"
```

---

### Task 12: brain_mcp hardening — clamp limit, escape ILIKE, kill N+1

**Files:**
- Modify: `pipeline/brain_mcp.py` (`search_brain`, `get_context`, pure-helper section)
- Test: `pipeline/tests/test_brain_mcp.py` (append)

**Interfaces:**
- Produces: `MAX_LIMIT = 100`, `_clamp_limit(limit: int) -> int`, `_escape_like(s: str) -> str`.

- [ ] **Step 1: Write the failing tests** — append to `pipeline/tests/test_brain_mcp.py`:

```python
# ---- audit M-3 / L-4: unbounded limit + raw LIKE wildcards from MCP input ---
def test_clamp_limit_bounds():
    import brain_mcp
    assert brain_mcp._clamp_limit(1_000_000) == brain_mcp.MAX_LIMIT
    assert brain_mcp._clamp_limit(0) == 1
    assert brain_mcp._clamp_limit(-5) == 1
    assert brain_mcp._clamp_limit(8) == 8


def test_escape_like_metacharacters():
    import brain_mcp
    assert brain_mcp._escape_like("100%_done") == "100\\%\\_done"
    assert brain_mcp._escape_like("a\\b") == "a\\\\b"
    assert brain_mcp._escape_like("plain") == "plain"
```

(If the existing file imports `brain_mcp` at top level already, match that import style instead of the function-local imports.)

- [ ] **Step 2: Run to verify failure**

Run: `cd pipeline && python -m pytest tests/test_brain_mcp.py -q`
Expected: FAIL — no attribute `_clamp_limit`.

- [ ] **Step 3: Implement.** In `pipeline/brain_mcp.py`:

**(a)** In the PURE HELPERS section add:

```python
MAX_LIMIT = 100  # M-3: MCP client có thể truyền limit=10^6 → DoS DB


def _clamp_limit(limit: int) -> int:
    return max(1, min(int(limit), MAX_LIMIT))


def _escape_like(s: str) -> str:
    """L-4: escape LIKE/ILIKE metachar từ input MCP (mặc định Postgres dùng
    backslash làm escape char nên không cần ESCAPE clause)."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
```

**(b)** First line of `search_brain` body: `limit = _clamp_limit(limit)`.

**(c)** `get_context`: first line `limit = _clamp_limit(limit)`, and the match query becomes:

```python
    matches = await pool.fetch(
        """
        select id, entity_kind, canonical_key, display_name, project, pyramid_layer
        from entities
        where canonical_key = $1                 -- exact key
           or canonical_key like '%:' || $2      -- exact path within a project ('repo:path')
           or canonical_key like '%/' || $2      -- basename of a namespaced path
           or display_name ilike $2              -- AI/curated display name
        order by last_seen_at desc limit 5
        """,
        entity, _escape_like(entity),
    )
```

**(d)** Replace the N+1 neighbor loop (`for n in neighbor_rows: o = await pool.fetchrow(...)`) with one batch query:

```python
    others = await pool.fetch(
        "select id, display_name, canonical_key, entity_kind from entities where id = any($1::uuid[])",
        [n["other"] for n in neighbor_rows],
    )
    by_id = {o["id"]: o for o in others}
    neighbors: list[dict] = []
    for n in neighbor_rows:
        o = by_id.get(n["other"])
        if o:
            neighbors.append({
                "name": o["display_name"], "key": _strip_project(o["canonical_key"]),
                # weight is numeric → cast to int so the MCP JSON result serializes.
                "kind": o["entity_kind"], "edge": n["edge_type"], "weight": int(n["weight"]),
            })
```

- [ ] **Step 4: Run tests**

Run: `cd pipeline && python -m pytest tests/ -q && ruff check . && ruff format --check .`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add pipeline/brain_mcp.py pipeline/tests/test_brain_mcp.py
git commit -m "fix(mcp): clamp limit, escape like wildcards, batch neighbor lookup"
```

---

### Task 13: U2 — stat cards respect the project filter

**Files:**
- Modify: `web/lib/data.ts` (`getSummary`, lines ~45-65)
- Modify: `web/app/page.tsx` (line ~49)

**Interfaces:**
- Produces: `getSummary(project?: string): Promise<Summary>` (signature gains the optional param; `Summary` type unchanged).

- [ ] **Step 1: Replace `getSummary` in `web/lib/data.ts` with:**

```ts
export function getSummary(project?: string): Promise<Summary> {
  return safe(
    async () => {
      // U2: 4 ô stat hero phải theo project chip như heat grid. Events mang
      // repo trong payload; entities/edges qua entities.project.
      const { rows } = await pool.query(
        `select
           (select count(*) from events
             where $1::text is null or payload -> 'repository' ->> 'full_name' = $1)::int as events,
           (select count(*) from entities
             where $1::text is null or project = $1)::int as entities,
           (select count(*) from entity_edges ed
             join entities ef on ef.id = ed.from_entity
             where $1::text is null or ef.project = $1)::int as edges,
           (select max(occurred_at) from events
             where $1::text is null or payload -> 'repository' ->> 'full_name' = $1) as last_event`,
        [project ?? null]
      );
      const r = rows[0];
      return {
        events: r.events,
        entities: r.entities,
        edges: r.edges,
        lastEvent: r.last_event ? new Date(r.last_event).toISOString() : null,
      };
    },
    { events: 0, entities: 0, edges: 0, lastEvent: null }
  );
}
```

- [ ] **Step 2: Pass the param in `web/app/page.tsx`** — inside the `Promise.all` array change `getSummary(),` to `getSummary(project),`.

- [ ] **Step 3: Verify**

Run: `cd web && npx tsc --noEmit && npm run build`
Expected: success.

Manual check (running stack): `/?project=saucevn%2Fdevbrain` shows different EVENTS/ENTITIES/EDGES numbers than `/` (unless the DB genuinely has one project).

- [ ] **Step 4: Commit**

```bash
git add web/lib/data.ts web/app/page.tsx
git commit -m "fix(web): scope hero stat cards to the selected project"
```

---

### Task 14: U5 — "touches" counts deterministic activity too

**Files:**
- Modify: `web/lib/data.ts` (`getProposedEntities`, lines ~182-204)

**Interfaces:** `ProposedEntity` type unchanged; only the SQL for `touches` changes.

- [ ] **Step 1: Replace the query inside `getProposedEntities` with:**

```ts
    const { rows } = await pool.query(
      `select e.id, e.entity_kind, ${STRIP} as canonical_key, e.display_name, e.last_seen_at,
              -- U5: 'touches' đo cả 2 lane. contributions chỉ do AI lane ghi →
              -- entity đề xuất từ lane deterministic luôn 0 (misleading khi
              -- người dùng quyết định confirm/reject). Cộng thêm commit_count.
              ((select count(*) from contributions c where c.entity_id = e.id)
               + coalesce((select sum(m.commit_count) from metrics_daily m where m.entity_id = e.id), 0)
              )::int as touches
       from entities e
       where e.resolution_status = 'proposed' and ($2::text is null or e.project = $2)
       order by e.last_seen_at desc
       limit $1`,
      [limit, project ?? null]
    );
```

(the old `left join contributions` + `group by` disappear — scalar subqueries replace them; the `rows.map` below stays identical.)

- [ ] **Step 2: Verify**

Run: `cd web && npx tsc --noEmit && npm run build`
Expected: success.

Manual check (running stack): `/entities` — active files now show non-zero touches.

- [ ] **Step 3: Commit**

```bash
git add web/lib/data.ts
git commit -m "fix(web): count deterministic commits in proposed-entity touches"
```

---

### Task 15: Env guards + pool timeouts in `web/lib/data.ts`

**Files:**
- Modify: `web/lib/data.ts` (pool construction lines 1-8; `embedQuery` lines ~112-132)

**Interfaces:** none new. NOTE: do NOT throw at module scope — `next build` inside the Docker image evaluates this module with no env vars; a top-level throw breaks the image build.

- [ ] **Step 1: Replace the pool block (lines 1-8) with:**

```ts
import { Pool } from "pg";

// Reuse one pool across HMR reloads in dev.
const g = globalThis as unknown as { _pgPool?: Pool };
// KHÔNG throw ở module scope: `next build` trong Docker image chạy không có
// env → throw sẽ phá build. Cảnh báo to, và pool fail nhanh thay vì treo.
if (!process.env.DATABASE_URL) {
  console.error("[data] DATABASE_URL is not set — every query will fail");
}
const pool =
  g._pgPool ??
  new Pool({
    connectionString: process.env.DATABASE_URL,
    max: 5,
    // M6: khi 5 connection đều bận, query mới phải fail nhanh (safe() đỡ)
    // thay vì treo request handler vô hạn.
    connectionTimeoutMillis: 3000,
    idleTimeoutMillis: 30000,
  });
if (process.env.NODE_ENV !== "production") g._pgPool = pool;
```

- [ ] **Step 2: Guard `embedQuery`** — replace its opening with:

```ts
async function embedQuery(text: string): Promise<number[]> {
  const key = process.env.GEMINI_API_KEY;
  if (!key) throw new Error("GEMINI_API_KEY is not set"); // safe() bắt → kết quả rỗng; không gửi 'key=undefined' ra ngoài
  const res = await fetch(
```

(rest of the function unchanged.)

- [ ] **Step 3: Verify**

Run: `cd web && npx tsc --noEmit && npm run build`
Expected: success (build works without env vars — the guard only logs).

- [ ] **Step 4: Commit**

```bash
git add web/lib/data.ts
git commit -m "fix(web): warn on missing env, fail-fast pool timeouts, guard gemini key"
```

---

### Task 16: `safeUrl` — protocol-check DB-sourced hrefs (M1)

**Files:**
- Modify: `web/lib/data.ts` (add exported helper near `safe()`)
- Modify: `web/app/changelog/page.tsx` (`Entry`, lines ~37-49)
- Modify: `web/app/search/page.tsx` (`Hit`, lines ~53-71)
- Modify: `web/app/roadmap/page.tsx` (`Item`, lines ~48-60)

**Interfaces:**
- Produces: `safeUrl(u: string | null): string | undefined` exported from `web/lib/data.ts`.

- [ ] **Step 1: Add to `web/lib/data.ts`** (after the `safe()` helper):

```ts
// M1: events.source_url / evidence_url render thẳng vào <a href>. Một event
// bị poison (xem P0-1) có thể mang 'javascript:' URL — chỉ cho http(s) qua.
export function safeUrl(u: string | null): string | undefined {
  return u && /^https?:\/\//i.test(u) ? u : undefined;
}
```

- [ ] **Step 2: `web/app/changelog/page.tsx`** — import: `import { getChangelog, safeUrl, type ChangelogEntry } from "@/lib/data";` and in `Entry` replace the header block with:

```tsx
function Entry({ e }: { e: ChangelogEntry }) {
  const url = safeUrl(e.sourceUrl);
  return (
    <article className="lift rounded-xl panel-glass px-5 py-4">
      <div className="flex items-center justify-between gap-3 font-mono text-[11px]">
        <div className="flex items-center gap-2.5">
          {e.date && <span className="italic text-muted" style={{ fontFamily: "var(--font-display)" }}>{e.date}</span>}
          {e.project && <span className="text-muted">{e.project}</span>}
          {e.pr && <span className="rounded-full border border-accent/40 bg-accent/10 px-2 py-0.5 text-accent">{e.pr}</span>}
        </div>
        {url && (
          <a href={url} target="_blank" rel="noreferrer" className="text-accent hover:underline">source ↗</a>
        )}
      </div>
```

(rest of `Entry` unchanged.)

- [ ] **Step 3: `web/app/search/page.tsx`** — import: `import { searchBrain, safeUrl, type SearchHit } from "@/lib/data";` and in `Hit`:

```tsx
function Hit({ h }: { h: SearchHit }) {
  const pct = Math.round(h.similarity * 100);
  const pr = h.scope === "pr" && h.scopeRef ? `PR #${String(h.scopeRef).split("#").pop()} · ` : "";
  const url = safeUrl(h.sourceUrl);
```

then replace `{h.sourceUrl && (<a href={h.sourceUrl} ...)}` with `{url && (<a href={url} ...)}` (same attributes).

- [ ] **Step 4: `web/app/roadmap/page.tsx`** — import: `import { getRoadmap, safeUrl, type RoadmapItem } from "@/lib/data";` and in `Item`:

```tsx
function Item({ it }: { it: RoadmapItem }) {
  const url = safeUrl(it.evidenceUrl);
```

then replace `{it.evidenceUrl && <a href={it.evidenceUrl} ...>}` with `{url && <a href={url} ...>}` (same attributes).

- [ ] **Step 5: Verify**

Run: `cd web && npx tsc --noEmit && npm run build`
Expected: success.

- [ ] **Step 6: Commit**

```bash
git add web/lib/data.ts web/app/changelog/page.tsx web/app/search/page.tsx web/app/roadmap/page.tsx
git commit -m "fix(web): only render http(s) urls from db-sourced link fields"
```

---

## Rollout (HUMAN-GATED — do not execute as part of the tasks)

Execute in order after all tasks are merged; each step touches the live stack.

> **⚠️ ORDERING IS LOAD-BEARING — migration 005 MUST be applied (step 2) BEFORE the new pipeline image runs (step 3).** The updated projector INSERTs (`apply_metrics`, `apply_status`, `apply_narrative`) now name the `derived_by` column and the `entity_status_hist_natural_key` conflict target that only exist after 005. Deploy the new image against a DB that hasn't had 005 applied and **every metrics/status/narrative INSERT fails** with `column "derived_by" does not exist` (loud: the transaction aborts and the cursor does not advance — no data corruption or double-count, but the lane stops making progress until 005 is applied). Do NOT run step 3 before step 2 succeeds.

1. Add `REDIS_PASSWORD=<openssl rand -hex 16>` to the live `.env`.
2. Apply the migration: `docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" < supabase/migrations/005_hardening.sql` (idempotent; additive; no replay needed). Verify it applied: `\d metrics_daily` should show the `derived_by` column before proceeding.
3. Rebuild + restart: `docker compose up -d --build` (and `--profile frontend` if self-hosting web). **Only after step 2 is confirmed.**
4. Smoke: `/health` → 200; forged bad-signature webhook → 401 + a `pipeline` warning log; `truncate events` in psql → error; `/graph?q=%` → empty state; `/?project=<repo>` stats change with the chip; `/entities` shows non-zero touches.
5. Watch one backup cycle: `docker compose logs backup` shows `done` with a plausible size, and the new R2 object is > 1 KB.

## Out of scope (tracked in IMPROVEMENT_PLAN.md, separate plans)

P2-2 embed content-hash pre-check · P2-3 prompt-cache verification · P2-9 sweep batching · P2-10 backfill `asyncio.to_thread` · dependency pinning (P2-7) · U4 roadmap grouping · U6 markdown rendering · U7 pyramid polish · P3 hygiene items · Cloudflare Access JWT middleware.
