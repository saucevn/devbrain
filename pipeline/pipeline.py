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
from datetime import datetime, timezone
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

PROJECTORS = ("metrics", "graph", "narrative", "status")   # mỗi cái có cursor riêng

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

    # --- G2: idempotent insert. §7.6: push fan-out 1 event/commit
    #   (source='git'/sha) → dedup xuyên webhook↔backfill; event khác giữ
    #   source='github'/delivery. on conflict do nothing → retry/trùng bỏ qua.
    new_events = fan_out_github(x_github_event, payload, x_github_delivery)
    stored = 0
    async with app.state.pool.acquire() as conn:
        for e in new_events:
            row = await conn.fetchval(
                """
                insert into events (source, source_event_id, event_type, actor, source_url, payload, occurred_at)
                values ($1, $2, $3, $4, $5, $6, $7)
                on conflict (source, source_event_id) do nothing
                returning seq
                """,
                e["source"], e["source_event_id"], e["event_type"], e["actor"],
                e["source_url"], e["payload"], e["occurred_at"],
            )
            if row is not None:
                stored += 1

    # --- enqueue sweep có DEBOUNCE (nhiều commit/push → 1 sweep) ----------
    if stored:
        await app.state.arq.enqueue_job(
            "run_projectors", _job_id="sweep", _defer_by=DEBOUNCE_SEC
        )

    # --- ACK ngay (< 10s timeout của GitHub); việc nặng ở worker ---------
    return {"ok": True, "stored": stored}


def normalize_github(event: str, p: dict) -> tuple[str, datetime, str | None, str | None]:
    """Map webhook GitHub → (event_type 'noun.verb', occurred_at, actor, url).
    occurred_at is a tz-aware datetime — it lands in a timestamptz column, so it
    must NOT be a string."""
    if event == "pull_request" and p.get("action") == "closed" and p["pull_request"].get("merged"):
        pr = p["pull_request"]
        return "pr.merged", _parse_ts(pr.get("merged_at")), pr["user"]["login"], pr["html_url"]
    if event == "release" and p.get("action") == "published":
        r = p["release"]
        return "release.tagged", _parse_ts(r.get("published_at")), r["author"]["login"], r["html_url"]
    if event == "push":
        # head_commit is null on branch-delete / tag pushes → guard.
        hc = p.get("head_commit") or {}
        actor = (p.get("pusher") or {}).get("name")
        return "commit.pushed", _parse_ts(hc.get("timestamp")), actor, p.get("compare")
    if event == "milestone":
        ms = p.get("milestone") or {}
        action = p.get("action") or "updated"
        return (f"milestone.{action}", _parse_ts(ms.get("updated_at")),
                (p.get("sender") or {}).get("login"), ms.get("html_url"))
    # ... thêm issue.resolved, doc.updated tuỳ nguồn
    return f"{event}.raw", _parse_ts(p.get("created_at")), None, None


def milestone_status(action: str, state: str) -> str:
    """Map milestone action/state → roadmap status (planned/in_progress/shipped)."""
    if action == "closed" or state == "closed":
        return "shipped"
    if action == "created":
        return "planned"
    return "in_progress"


def _parse_ts(s: str | None) -> datetime:
    """Parse an ISO-8601 timestamp (GitHub uses a trailing 'Z') into a tz-aware
    datetime; fall back to now(UTC) when missing or unparseable."""
    if not s:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


def fan_out_github(event: str, p: dict, delivery_id: str) -> list[dict]:
    """§7.6 transport-independent commit identity: a `push` fans out to ONE
    event PER COMMIT, keyed source='git' / source_event_id=sha → dedup xuyên
    webhook/backfill/CLI qua unique(source, source_event_id). Event khác
    (pr/release) giữ 1 event/delivery, source='github'."""
    if event == "push":
        repo = p.get("repository")
        out: list[dict] = []
        for c in p.get("commits") or []:
            sha = c.get("id")
            if not sha:
                continue
            author = c.get("author") or {}
            out.append({
                "source": "git",
                "source_event_id": sha,
                "event_type": "commit.pushed",
                "actor": author.get("email") or author.get("username"),
                "source_url": c.get("url"),
                "payload": {"commits": [c], "repository": repo, "sha": sha},
                "occurred_at": _parse_ts(c.get("timestamp")),
            })
        return out
    event_type, occurred_at, actor, url = normalize_github(event, p)
    return [{
        "source": "github",
        "source_event_id": delivery_id,
        "event_type": event_type,
        "actor": actor,
        "source_url": url,
        "payload": p,
        "occurred_at": occurred_at,
    }]


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
    """File paths touched by an event. Handles three payload shapes:
    enriched PR (`pull_request._files`), backfill (`files`), and a raw GitHub
    `push` (union of each commit's added/modified/removed, de-duped in order)."""
    pr = payload.get("pull_request") or {}
    files = pr.get("_files") or payload.get("files")
    if files:
        return [f["filename"] for f in files]
    commits = payload.get("commits")
    if commits:
        seen: set[str] = set()
        paths: list[str] = []
        for c in commits:
            for key in ("added", "modified", "removed"):
                for path in c.get(key) or []:
                    if path not in seen:
                        seen.add(path)
                        paths.append(path)
        return paths
    return []


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
_ALLOWED_KINDS = {"module", "file", "service", "feature", "epic", "doc"}
_ALLOWED_RELATIONS = {"created", "modified", "fixed_bug_in", "documented", "deprecated"}
_ALLOWED_EDGES = {"depends_on", "documented_by", "implements"}
_EDGE_SYNONYMS = {"documents": "documented_by", "documented": "documented_by",
                  "depends": "depends_on", "implement": "implements"}
_RELATION_SYNONYMS = {"documents": "documented", "document": "documented",
                      "fixed": "fixed_bug_in", "fix": "fixed_bug_in",
                      "create": "created", "modify": "modified", "update": "modified"}


def _coerce_analysis(raw: dict) -> dict:
    """Forced tool_use KHÔNG ép cứng enum → một edge_type/relation lạ sẽ làm
    model_validate fail và poison cả PR. Remap synonym đã biết, DROP edge/entity
    còn invalid (giữ phần hợp lệ) thay vì hỏng toàn bộ."""
    raw = dict(raw)
    edges = []
    for e in raw.get("edges") or []:
        et = _EDGE_SYNONYMS.get(str(e.get("edge_type", "")).lower(), e.get("edge_type"))
        if et in _ALLOWED_EDGES:
            edges.append({**e, "edge_type": et})
    raw["edges"] = edges
    ents = []
    for en in raw.get("entities") or []:
        if en.get("kind") not in _ALLOWED_KINDS:
            continue
        rel = _RELATION_SYNONYMS.get(str(en.get("relation", "")).lower(), en.get("relation"))
        if rel not in _ALLOWED_RELATIONS:
            rel = "modified"
        ents.append({**en, "relation": rel})
    raw["entities"] = ents
    return raw


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
    return PRAnalysis.model_validate(_coerce_analysis(block.input))


async def embed(text: str, task_type: str = "RETRIEVAL_DOCUMENT") -> list[float]:
    """Gemini embeddings (gemini-embedding-001) ở EMBED_DIM dims → khớp
    vector(EMBED_DIM) trong schema (VN+EN multilingual). task_type:
    RETRIEVAL_DOCUMENT khi lưu, RETRIEVAL_QUERY khi search. Dims <3072 nên
    normalize về unit length cho cosine (<=>)."""
    import httpx
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
        return _normalize(_parse_gemini_embedding(r.json()))


def _parse_gemini_embedding(data: dict) -> list[float]:
    """embedContent response shape: {"embedding": {"values": [...]}}."""
    return data["embedding"]["values"]


def _normalize(vec: list[float]) -> list[float]:
    import math
    n = math.sqrt(sum(x * x for x in vec))
    return [x / n for x in vec] if n > 0 else vec


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
        elif name == "status":
            await apply_status(conn, ev, version)   # roadmap: milestone → status_history

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


COCHANGE_MAX_FILES = 50  # commit/PR đụng > N file (initial import / refactor lớn)
                         # → bỏ co-change tránh clique C(n,2) nhiễu; metrics vẫn đếm.


def cochange_skip(paths: list[str]) -> bool:
    return len(paths) > COCHANGE_MAX_FILES


async def apply_cochange(conn, ev, version):
    """File đổi cùng nhau trong 1 commit/PR ⇒ cạnh co_changed_with, weight+=1.
    Hoàn toàn deterministic, KHÔNG cần LLM."""
    paths = [p for p in changed_files(ev["payload"]) if not is_ignored(p)]
    if cochange_skip(paths):
        return   # §5: commit quá lớn → skip co-change (entities vẫn do metrics lane tạo)
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


# ---- status projector (roadmap: milestone → entity_status_history) ----
async def apply_status(conn, ev, version):
    """Milestone event → epic entity + 1 transition vào entity_status_history.
    Deterministic (derived từ milestone facts), KHÔNG AI. Projection rebuildable
    → truncate trước khi replay."""
    if not ev["event_type"].startswith("milestone."):
        return
    p = ev["payload"]
    ms = p.get("milestone") or {}
    number = ms.get("number")
    repo = (p.get("repository") or {}).get("full_name")
    if number is None or not repo:
        return
    status = milestone_status(p.get("action") or "", ms.get("state") or "")
    eid = await upsert_entity(conn, "epic", f"milestone:{repo}#{number}", ms.get("title") or f"#{number}")
    await conn.execute(
        """
        insert into entity_status_history (entity_id, status, changed_at, source_event_id)
        values ($1, $2, $3, $4)
        """,
        eid, status, ev["occurred_at"], ev["id"],
    )


# ---- AI projector (đắt, đã lọc + cache) ------------------------------
async def maybe_summarize(pool, ev, version: int):
    """Trả (analysis, input_hash, embedding) hoặc None.
    G4/C5: nếu input_hash đã tồn tại ⇒ SKIP luôn LLM (cache hit)."""
    if not passes_ai_gate(ev):                      # C1 funnel
        return None
    enriched = await enrich_pr_payload(ev["payload"])   # diff/files/issues qua GitHub API (ngoài tx)
    pr = parse_pr(enriched)
    ih = sha256(str(PROMPT_VERSION), pr["title"], pr.get("body", ""), pr["diff"][:DIFF_CHAR_CAP])

    exists = await pool.fetchval("select 1 from narratives where input_hash=$1 limit 1", ih)
    if exists:                                       # C5: đã tóm tắt input y hệt → khỏi trả tiền lại
        return None

    analysis = await summarize_pr(pr)                # ← LLM call duy nhất (Haiku)
    vec = await embed(analysis.summary_md)           # embed bản summary (rẻ hơn embed cả diff)
    return analysis, ih, vec, enriched


async def apply_narrative(conn, ev, version: int, analysis: PRAnalysis, input_hash: str,
                          vec: list[float], enriched_payload: dict):
    pr = parse_pr(enriched_payload)            # enriched ở maybe_summarize (event gốc bất biến)
    known_paths = changed_files(enriched_payload)
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
        str(pr["number"]),
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


# ---- PR enrichment (GitHub API, chạy trong worker — KHÔNG ở receiver) ----
ISSUE_REF_RE = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)", re.I)


def extract_linked_issues(body: str | None) -> list[str]:
    """Linked issues từ PR body theo closing-keyword của GitHub (Fixes #N...)."""
    if not body:
        return []
    seen, out = set(), []
    for m in ISSUE_REF_RE.finditer(body):
        key = "#" + m.group(1)
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _build_enriched_pr(payload: dict, files: list[dict], diff: str, issues: list[str]) -> dict:
    """Inject _files/_diff/_linked_issues vào BẢN SAO payload (event gốc bất
    biến). parse_pr/changed_files/file_churn đọc các field _ này."""
    import copy
    p = copy.deepcopy(payload)
    pr = p.setdefault("pull_request", {})
    pr["_files"] = [
        {"filename": f["filename"], "additions": f.get("additions", 0), "deletions": f.get("deletions", 0)}
        for f in files
    ]
    pr["_diff"] = diff
    pr["_linked_issues"] = issues
    return p


async def enrich_pr_payload(payload: dict) -> dict:
    """Webhook PR thiếu diff/files → fetch qua GitHub API. Trả BẢN SAO đã
    enrich; thiếu repo/number thì trả nguyên payload."""
    import httpx
    pr = payload.get("pull_request") or {}
    repo = (payload.get("repository") or {}).get("full_name")
    number = pr.get("number")
    if not repo or not number:
        return payload
    token = os.environ.get("GITHUB_TOKEN", "")
    auth = {"Authorization": f"Bearer {token}"} if token else {}
    base = f"https://api.github.com/repos/{repo}/pulls/{number}"
    async with httpx.AsyncClient(timeout=30) as c:
        fr = await c.get(f"{base}/files", params={"per_page": 100},
                         headers={**auth, "Accept": "application/vnd.github+json"})
        fr.raise_for_status()
        files = fr.json()
        dr = await c.get(base, headers={**auth, "Accept": "application/vnd.github.v3.diff"})
        dr.raise_for_status()
        diff = dr.text
    return _build_enriched_pr(payload, files, diff, extract_linked_issues(pr.get("body")))


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
