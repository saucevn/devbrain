"""
=======================================================================
DEV SECOND BRAIN — Ingestion → Queue → Projector pipeline
=======================================================================
Giải quyết 2 vấn đề: COST và IDEMPOTENCY.

Hai entrypoint (2 process riêng):
  1. uvicorn pipeline:app          → webhook receiver (FastAPI)
  2. arq pipeline.WorkerSettings   → projector worker (ARQ)

4 GATE IDEMPOTENCY (đi từ ngoài vào):
  G1  HMAC verify          → loại payload giả mạo                (receiver)
  G2  unique(source,sid)   → webhook retry trùng → skip enqueue  (receiver)
  G3  cursor (last_seq)    → mỗi event xử lý đúng 1 lần          (sweep)
  G4  content-hash + UPSERT→ replay/crash không tạo bản trùng,
                             và không gọi lại LLM khi input đổi   (AI lane)

6 CỘT CẮT COST:
  C1  filter funnel        → AI chỉ chạm pr.merged/release/doc (~10-20× ít hơn)
  C2  PR-level (squash)    → 1 summary/PR, KHÔNG phải 1/commit
  C3  model tiering        → Haiku cho summarize hàng loạt; Sonnet cho rollup
  C4  diff đọc 1 lần       → rollup đọc PR-summary (rẻ), không đọc lại diff
  C5  content-hash cache   → reprocess không trả tiền lại cho input cũ
  C6  prompt caching       → cache system+tool prefix qua hàng ngàn call

Phụ thuộc: fastapi, uvicorn, arq, asyncpg, pgvector, anthropic, httpx, pydantic
=======================================================================
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from typing import Any, Literal

import asyncpg
from anthropic import AsyncAnthropic
from arq import cron
from arq.connections import RedisSettings
from fastapi import FastAPI, Header, HTTPException, Request
from pgvector.asyncpg import register_vector
from pydantic import BaseModel, Field


# =====================================================================
# CONFIG
# =====================================================================
DATABASE_URL   = os.environ["DATABASE_URL"]            # Supabase Postgres (pooler hoặc direct)
REDIS_URL      = os.environ.get("REDIS_URL", "redis://localhost:6379")
# Receiver/worker secrets are read tolerantly so this module is importable by
# deterministic tools (vd backfill.py) that don't need the webhook/AI lanes.
# The processes that actually use them (receiver HMAC, AI summarize) still set
# them via env in compose.
GITHUB_SECRET  = os.environ.get("GITHUB_WEBHOOK_SECRET", "").encode()
ANTHROPIC_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")

MODEL_SUMMARY  = "claude-haiku-4-5-20251001"   # C3: rẻ/nhanh cho bulk PR summary
MODEL_ROLLUP   = "claude-sonnet-4-6"           # C3: mạnh cho nhật ký định kỳ
PROMPT_VERSION = 1                              # bump khi đổi prompt → input_hash đổi → re-summarize
EMBED_DIM      = 1536                           # PHẢI khớp vector(N) trong schema

SWEEP_BATCH    = 200          # số event đọc mỗi lượt sweep
DEBOUNCE_SEC   = 30           # gom nhiều webhook trong cửa sổ này thành 1 sweep
DIFF_CHAR_CAP  = 12_000       # cắt diff để khống chế token (C2/C5)

PROJECTORS = ("metrics", "graph", "narrative")   # mỗi cái có cursor riêng

anthropic = AsyncAnthropic(api_key=ANTHROPIC_KEY)


# =====================================================================
# DB POOL  (đăng ký codec pgvector để truyền list[float] thẳng vào cột vector)
# =====================================================================
async def _init_connection(conn: asyncpg.Connection) -> None:
    # pgvector codec: truyền list[float] thẳng vào cột vector.
    await register_vector(conn)
    # jsonb codec: truyền/nhận dict cho cột jsonb (events.payload, ...).
    # KHÔNG có codec này thì asyncpg để jsonb dạng text → insert dict lỗi và
    # projector không .get() được payload khi đọc. Bắt buộc cho cả 2 lane.
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )


async def make_pool() -> asyncpg.Pool:
    return await asyncpg.create_pool(
        DATABASE_URL, min_size=2, max_size=10, init=_init_connection
    )


# =====================================================================
# HELPERS
# =====================================================================
def sha256(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode())
        h.update(b"\x00")
    return h.hexdigest()


def advisory_key(name: str) -> int:
    # khoá advisory 1 runner/projector (số 63-bit signed)
    return int.from_bytes(hashlib.blake2b(name.encode(), digest_size=8).digest(), "big", signed=True)


# =====================================================================
# ENTRYPOINT 1 — WEBHOOK RECEIVER  (gate G1 + G2, ACK nhanh)
# =====================================================================
app = FastAPI()


@app.on_event("startup")
async def _startup():
    app.state.pool = await make_pool()
    from arq import create_pool as arq_pool
    app.state.arq = await arq_pool(RedisSettings.from_dsn(REDIS_URL))


@app.get("/health")
async def health():
    """Liveness/readiness cho receiver. Ping DB rẻ; ACK thừa sức < 10s."""
    db_ok = False
    try:
        db_ok = await app.state.pool.fetchval("select 1") == 1
    except Exception:
        db_ok = False
    return {"ok": True, "db": db_ok}


@app.post("/webhooks/github")
async def github_webhook(
    request: Request,
    x_hub_signature_256: str = Header(""),
    x_github_delivery: str = Header(""),
    x_github_event: str = Header(""),
):
    body = await request.body()

    # --- G1: HMAC verify (constant-time, chống giả mạo) -----------------
    digest = "sha256=" + hmac.new(GITHUB_SECRET, body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(digest, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="bad signature")

    payload = await request.json()
    event_type, occurred_at, actor, url = normalize_github(x_github_event, payload)

    # --- G2: idempotent insert (webhook retry dùng cùng delivery ID) ----
    #   on conflict do nothing → nếu KHÔNG trả về id ⇒ đây là retry trùng
    #   ⇒ KHÔNG enqueue lại (tránh xử lý 2 lần).
    row = await app.state.pool.fetchrow(
        """
        insert into events (source, source_event_id, event_type, actor, source_url, payload, occurred_at)
        values ('github', $1, $2, $3, $4, $5, $6)
        on conflict (source, source_event_id) do nothing
        returning seq
        """,
        x_github_delivery, event_type, actor, url, payload, occurred_at,
    )

    # --- enqueue sweep có DEBOUNCE: cùng _job_id 'sweep' trong cửa sổ →
    #     ARQ coalesce thành 1 job (nhiều push = 1 sweep). ----------------
    if row is not None:
        await app.state.arq.enqueue_job(
            "run_projectors", _job_id="sweep", _defer_by=DEBOUNCE_SEC
        )

    # --- ACK ngay (< 10s timeout của GitHub); mọi việc nặng ở worker ----
    return {"ok": True, "stored": row is not None}


def normalize_github(event: str, p: dict) -> tuple[str, str, str | None, str | None]:
    """Map webhook GitHub → (event_type 'noun.verb', occurred_at, actor, url)."""
    if event == "pull_request" and p.get("action") == "closed" and p["pull_request"].get("merged"):
        pr = p["pull_request"]
        return "pr.merged", pr["merged_at"], pr["user"]["login"], pr["html_url"]
    if event == "release" and p.get("action") == "published":
        r = p["release"]
        return "release.tagged", r["published_at"], r["author"]["login"], r["html_url"]
    if event == "push":
        return "commit.pushed", p["head_commit"]["timestamp"], p["pusher"]["name"], p["compare"]
    # ... thêm issue.resolved, doc.updated tuỳ nguồn
    return f"{event}.raw", p.get("created_at") or _now_iso(), None, None


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# =====================================================================
# FILTERS  (C1 — funnel cắt cost TRƯỚC khi chạm LLM)
# =====================================================================
AI_EVENT_TYPES = {"pr.merged", "release.tagged", "issue.resolved", "doc.updated"}
NOISE_MSG = re.compile(r"^(wip|fixup!|squash!|merge branch|merge pull request)", re.I)
IGNORED_PATHS = (
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "dist/", "build/", ".generated.", "node_modules/",
)


def is_bot(actor: str | None) -> bool:
    return bool(actor) and (actor.endswith("[bot]") or actor in {"dependabot", "renovate"})


def is_ignored(path: str) -> bool:
    return any(seg in path for seg in IGNORED_PATHS)


def passes_deterministic(ev: asyncpg.Record) -> bool:
    """Lane rẻ: chạy gần như mọi event, chỉ bỏ bot. Merge commit vẫn tính
    metric (rẻ) nhưng KHÔNG cho qua AI lane bên dưới."""
    return not is_bot(ev["actor"])


def passes_ai_gate(ev: asyncpg.Record) -> bool:
    """Lane đắt: thắt chặt tối đa. Đây là chỗ tiết kiệm tiền lớn nhất."""
    if ev["event_type"] not in AI_EVENT_TYPES:
        return False
    if is_bot(ev["actor"]):
        return False
    files = changed_files(ev["payload"])
    if files and all(is_ignored(f) for f in files):   # PR chỉ đụng file rác
        return False
    return True


def changed_files(payload: dict) -> list[str]:
    pr = payload.get("pull_request") or {}
    return [f["filename"] for f in (pr.get("_files") or payload.get("files") or [])]


# =====================================================================
# STRUCTURED OUTPUT SCHEMA  (Pydantic = single source of truth cho tool schema)
# =====================================================================
EntityKind = Literal["module", "file", "service", "feature", "epic", "doc"]
Relation = Literal["created", "modified", "fixed_bug_in", "documented", "deprecated"]
EdgeType = Literal["depends_on", "documented_by", "implements"]


class ExtractedEntity(BaseModel):
    canonical_key: str = Field(
        description="NEO vào ID có thật từ diff: file path, module path, hoặc linked issue key "
                    "(vd 'src/auth/', 'JIRA-1234'). TUYỆT ĐỐI KHÔNG bịa tên mới."
    )
    kind: EntityKind
    display_name: str
    relation: Relation


class ExtractedEdge(BaseModel):
    from_key: str
    to_key: str
    edge_type: EdgeType


class PRAnalysis(BaseModel):
    summary_md: str = Field(description="2-4 câu highlight cho nhật ký dev, viết bằng ngôn ngữ của repo.")
    highlights: list[str] = Field(default_factory=list, max_length=5)
    entities: list[ExtractedEntity] = Field(default_factory=list)
    edges: list[ExtractedEdge] = Field(default_factory=list)


PR_SYSTEM_PROMPT = (
    "Bạn là trợ lý phân tích pull request cho 'bộ não thứ 2' của team dev. "
    "Nhiệm vụ: tóm tắt PR thành highlight ngắn cho nhật ký, và TRÍCH entity + quan hệ. "
    "QUY TẮC BẮT BUỘC về canonical_key: chỉ được chọn từ danh sách file path và issue key "
    "ĐƯỢC CUNG CẤP trong message; không suy diễn tên không có trong dữ liệu. "
    "Nếu không chắc một entity, đừng trích nó. KHÔNG chấm điểm, KHÔNG xếp hạng người."
)


# =====================================================================
# AI CALLS  (C3 model tiering · C6 prompt caching · structured via tool_use)
# =====================================================================
async def summarize_pr(pr: dict) -> PRAnalysis:
    """1 LLM call/PR, output ép theo schema bằng forced tool_use."""
    file_list = "\n".join(pr["files"]) or "(none)"
    diff = pr["diff"][:DIFF_CHAR_CAP]                      # C2/C5: cắt diff
    user_block = (
        f"PR #{pr['number']}: {pr['title']}\n\n"
        f"Body:\n{pr.get('body','')}\n\n"
        f"Linked issues: {', '.join(pr.get('issues', [])) or '(none)'}\n\n"
        f"Changed files (chọn canonical_key từ đây):\n{file_list}\n\n"
        f"Diff (đã cắt):\n{diff}"
    )

    resp = await anthropic.messages.create(
        model=MODEL_SUMMARY,
        max_tokens=2000,
        # C6: cache_control trên system + tool → prefix tĩnh được cache qua
        # hàng ngàn PR call ⇒ giảm mạnh input token.
        system=[{"type": "text", "text": PR_SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        tools=[{
            "name": "record_pr_analysis",
            "description": "Ghi lại phân tích có cấu trúc của một PR đã merge.",
            "input_schema": PRAnalysis.model_json_schema(),
            "cache_control": {"type": "ephemeral"},
        }],
        tool_choice={"type": "tool", "name": "record_pr_analysis"},   # ép trả JSON đúng schema
        messages=[{"role": "user", "content": user_block}],
    )
    block = next(b for b in resp.content if b.type == "tool_use")
    return PRAnalysis.model_validate(block.input)


async def embed(text: str) -> list[float]:
    """Embedding provider-agnostic. PHẢI trả vector đúng EMBED_DIM.
    Với VN+EN trộn nên dùng model multilingual mạnh (Voyage/Cohere/OpenAI)."""
    import httpx
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            "https://api.voyageai.com/v1/embeddings",
            headers={"Authorization": f"Bearer {os.environ['VOYAGE_API_KEY']}"},
            json={"input": [text], "model": "voyage-3", "input_type": "document"},
        )
        r.raise_for_status()
        return r.json()["data"][0]["embedding"]


# =====================================================================
# CANONICAL ENTITY UPSERT  (giữ human edit BẤT KHẢ XÂM PHẠM)
# =====================================================================
async def upsert_entity(conn, kind: str, canonical_key: str, display_name: str) -> str:
    """on conflict CHỈ bump last_seen_at. KHÔNG ghi đè display_name /
    pyramid_layer / resolution_status (những field người đã sửa)."""
    return await conn.fetchval(
        """
        insert into entities (entity_kind, canonical_key, display_name, resolution_status,
                              first_seen_at, last_seen_at)
        values ($1, $2, $3, 'proposed', now(), now())
        on conflict (entity_kind, canonical_key) do update
          set last_seen_at = now()
        returning id
        """,
        kind, canonical_key, display_name,
    )


def guard_canonical_key(ent: ExtractedEntity, known_paths: list[str], known_issues: list[str]) -> bool:
    """Chốt chặn hallucination: 'file' phải khớp path có thật; 'module' phải
    là prefix của 1 path; 'epic' phải khớp issue key. Còn lại tạm chấp nhận
    nhưng để resolution_status='proposed' chờ người confirm."""
    if ent.kind == "file":
        return ent.canonical_key in known_paths
    if ent.kind == "module":
        return any(p.startswith(ent.canonical_key) for p in known_paths)
    if ent.kind == "epic":
        return ent.canonical_key in known_issues
    return True


# =====================================================================
# ENTRYPOINT 2 — PROJECTOR SWEEP  (cursor G3 · atomic write+advance · G4)
# =====================================================================
async def run_projectors(ctx: dict):
    """Job ARQ. Mỗi projector chạy độc lập với cursor riêng → AI (chậm)
    không chặn metrics (nhanh)."""
    pool: asyncpg.Pool = ctx["pool"]
    for name in PROJECTORS:
        await run_one_projector(pool, name)


async def run_one_projector(pool: asyncpg.Pool, name: str):
    async with pool.acquire() as lock_conn:
        # 1 runner/projector (chống 2 sweep chạy đè dù đã debounce)
        got = await lock_conn.fetchval("select pg_try_advisory_lock($1)", advisory_key(name))
        if not got:
            return
        try:
            cp = await pool.fetchrow(
                "select last_seq, logic_version from projection_checkpoints where projector_name=$1", name
            )
            rows = await pool.fetch(
                """
                select seq, id, source, source_event_id, event_type, actor, source_url, payload, occurred_at
                from events where seq > $1 order by seq asc limit $2
                """,
                cp["last_seq"], SWEEP_BATCH,
            )
            for ev in rows:
                await process_event(pool, name, ev, cp["logic_version"])
        finally:
            await lock_conn.fetchval("select pg_advisory_unlock($1)", advisory_key(name))


async def process_event(pool, name: str, ev, version: int):
    """G3: write projection + advance cursor trong CÙNG 1 transaction →
    crash giữa chừng không double-count (footgun #1 của event-sourcing)."""

    if name == "narrative":
        # AI lane: LLM call PHẢI ở NGOÀI transaction (network). Hàm này tự
        # cache-skip (G4/C5) và trả None nếu bị filter/đã có.
        precomputed = await maybe_summarize(pool, ev, version)

    async with pool.acquire() as conn, conn.transaction():
        if name == "metrics" and passes_deterministic(ev):
            await apply_metrics(conn, ev)            # C: thuần git, += an toàn vì atomic
        elif name == "graph" and passes_deterministic(ev):
            await apply_cochange(conn, ev, version)  # co_changed_with: tín hiệu graph MIỄN PHÍ
        elif name == "narrative" and precomputed is not None:
            await apply_narrative(conn, ev, version, *precomputed)

        # advance cursor per-event → crash chỉ phải xem lại event đang dở
        await conn.execute(
            "update projection_checkpoints set last_seq=$2, updated_at=now() where projector_name=$1",
            name, ev["seq"],
        )


# ---- deterministic projectors (rẻ, không AI) -------------------------
async def apply_metrics(conn, ev):
    day = ev["occurred_at"].date() if hasattr(ev["occurred_at"], "date") else None
    paths = changed_files(ev["payload"])
    # map path → entity 'file', cộng dồn metric ngày (UPSERT +=, atomic).
    for path in paths:
        if is_ignored(path):
            continue
        eid = await upsert_entity(conn, "file", path, path.split("/")[-1])
        adds, dels = file_churn(ev["payload"], path)
        await conn.execute(
            """
            insert into metrics_daily (entity_id, day, commit_count, lines_added, lines_removed)
            values ($1, $2, 1, $3, $4)
            on conflict (entity_id, day) do update
              set commit_count = metrics_daily.commit_count + 1,
                  lines_added  = metrics_daily.lines_added  + excluded.lines_added,
                  lines_removed= metrics_daily.lines_removed+ excluded.lines_removed
            """,
            eid, day, adds, dels,
        )


async def apply_cochange(conn, ev, version):
    """File đổi cùng nhau trong 1 commit/PR ⇒ cạnh co_changed_with, weight+=1.
    Hoàn toàn deterministic, KHÔNG cần LLM."""
    paths = [p for p in changed_files(ev["payload"]) if not is_ignored(p)]
    ids = [await upsert_entity(conn, "file", p, p.split("/")[-1]) for p in paths]
    ts = ev["occurred_at"]
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a, b = sorted((ids[i], ids[j]))   # chuẩn hoá hướng để tránh cạnh đôi
            await conn.execute(
                """
                insert into entity_edges (from_entity, to_entity, edge_type, weight,
                                          first_observed_at, last_observed_at, derived_by, derived_by_version)
                values ($1, $2, 'co_changed_with', 1, $3, $3, 'git', $4)
                on conflict (from_entity, to_entity, edge_type) do update
                  set weight = entity_edges.weight + 1, last_observed_at = excluded.last_observed_at
                """,
                a, b, ts, version,
            )


# ---- AI projector (đắt, đã lọc + cache) ------------------------------
async def maybe_summarize(pool, ev, version: int):
    """Trả (analysis, input_hash, embedding) hoặc None.
    G4/C5: nếu input_hash đã tồn tại ⇒ SKIP luôn LLM (cache hit)."""
    if not passes_ai_gate(ev):                      # C1 funnel
        return None
    pr = parse_pr(ev["payload"])
    ih = sha256(str(PROMPT_VERSION), pr["title"], pr.get("body", ""), pr["diff"][:DIFF_CHAR_CAP])

    exists = await pool.fetchval("select 1 from narratives where input_hash=$1 limit 1", ih)
    if exists:                                       # C5: đã tóm tắt input y hệt → khỏi trả tiền lại
        return None

    analysis = await summarize_pr(pr)                # ← LLM call duy nhất (Haiku)
    vec = await embed(analysis.summary_md)           # embed bản summary (rẻ hơn embed cả diff)
    return analysis, ih, vec


async def apply_narrative(conn, ev, version: int, analysis: PRAnalysis, input_hash: str, vec: list[float]):
    pr = parse_pr(ev["payload"])
    known_paths = changed_files(ev["payload"])
    known_issues = pr.get("issues", [])

    # 1) UPSERT entities (giữ human edit) + contributions (chống trùng) ---
    key_to_id: dict[str, str] = {}
    for ent in analysis.entities:
        if not guard_canonical_key(ent, known_paths, known_issues):   # chốt hallucination
            continue
        eid = await upsert_entity(conn, ent.kind, ent.canonical_key, ent.display_name)
        key_to_id[ent.canonical_key] = eid
        await conn.execute(
            """
            insert into contributions (event_id, entity_id, relation, occurred_at, derived_by, derived_by_version)
            values ($1, $2, $3, $4, 'ai', $5)
            on conflict (event_id, entity_id, relation) do nothing
            """,
            ev["id"], eid, ent.relation, ev["occurred_at"], version,
        )

    # 2) UPSERT edges do AI trích (chỉ giữ cạnh nối 2 entity đã neo) ------
    for e in analysis.edges:
        a, b = key_to_id.get(e.from_key), key_to_id.get(e.to_key)
        if not a or not b or a == b:
            continue
        await conn.execute(
            """
            insert into entity_edges (from_entity, to_entity, edge_type, weight,
                                      first_observed_at, last_observed_at, derived_by, derived_by_version)
            values ($1, $2, $3, 1, $4, $4, 'ai', $5)
            on conflict (from_entity, to_entity, edge_type) do update
              set last_observed_at = excluded.last_observed_at
            """,
            a, b, e.edge_type, ev["occurred_at"], version,
        )

    # 3) UPSERT narrative theo natural key (G4) --------------------------
    nid = await conn.fetchval(
        """
        insert into narratives (scope, scope_ref, title, body_md, highlights, source_event_ids,
                                model, derived_by_version, input_hash, period_start, period_end)
        values ('pr', $1, $2, $3, $4, $5, $6, $7, $8, $9, $9)
        on conflict (scope, scope_ref, derived_by_version) do update
          set body_md=excluded.body_md, highlights=excluded.highlights, input_hash=excluded.input_hash
        returning id
        """,
        str(parse_pr(ev["payload"])["number"]),
        pr["title"], analysis.summary_md, _json(analysis.highlights), [ev["id"]],
        MODEL_SUMMARY, version, input_hash, ev["occurred_at"],
    )

    # 4) UPSERT embedding theo natural key + content_hash ----------------
    ch = sha256(analysis.summary_md)
    await conn.execute(
        """
        insert into embeddings (source_kind, source_id, content, embedding, occurred_at,
                                derived_by_version, content_hash)
        values ('pr_summary', $1, $2, $3, $4, $5, $6)
        on conflict (source_kind, source_id, derived_by_version) do update
          set embedding=excluded.embedding, content=excluded.content, content_hash=excluded.content_hash
        """,
        nid, analysis.summary_md, vec, ev["occurred_at"], version, ch,
    )


# ---- payload parsers / utils (rút gọn — điền theo schema webhook thật) ----
def parse_pr(payload: dict) -> dict:
    pr = payload["pull_request"]
    return {
        "number": pr["number"], "title": pr["title"], "body": pr.get("body") or "",
        "files": [f for f in changed_files(payload) if not is_ignored(f)],
        "issues": pr.get("_linked_issues", []),
        "diff": pr.get("_diff", ""),     # fetch riêng qua GitHub API khi enrich
    }


def file_churn(payload: dict, path: str) -> tuple[int, int]:
    for f in (payload.get("pull_request", {}).get("_files") or payload.get("files") or []):
        if f["filename"] == path:
            return f.get("additions", 0), f.get("deletions", 0)
    return 0, 0


def _json(obj) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False)


# =====================================================================
# ARQ WORKER SETTINGS
# =====================================================================
async def on_startup(ctx):
    ctx["pool"] = await make_pool()


async def on_shutdown(ctx):
    await ctx["pool"].close()


class WorkerSettings:
    functions = [run_projectors]
    # Cron an toàn: sweep mỗi 5 phút phòng khi webhook-triggered sweep lỗi.
    cron_jobs = [cron(run_projectors, minute=set(range(0, 60, 5)))]
    on_startup = on_startup
    on_shutdown = on_shutdown
    redis_settings = RedisSettings.from_dsn(REDIS_URL)
    max_jobs = 4
    job_timeout = 600
