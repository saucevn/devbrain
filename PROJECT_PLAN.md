# Dev Second Brain — Project Plan

> Bộ não thứ 2 / nhật ký thời gian thực cho team dev: roadmap theo năm, kim tự tháp heatmap, knowledge graph, semantic search — dựng trên kiến trúc event-sourced.

| | |
|---|---|
| **Phiên bản** | 0.2 |
| **Ngày** | 2026-06-25 |
| **Trạng thái** | ✅ **Phases 0–3 shipped** · live ingest (Cloudflare Tunnel + GitHub webhook/milestone) · multi-project namespace. Kế: Phase 4 graph · Phase 5 rollup+ops · Lark/docs ingest. (Deep-dive P2/P3 đã hợp nhất ở Phụ lục cuối file.) |
| **Hạ tầng nền** | Docker Compose all-in-one (VPS *hoặc* Mac) · Cloudflare Tunnel · Postgres local + backup R2 |

---

## 1. Mục tiêu & phạm vi

### Mục tiêu
Một website tổng hợp tiến độ toàn team dev theo thời gian, hoạt động như cuốn nhật ký sống: tự bóc tách từ git/PR/docs, dựng roadmap, kim tự tháp năng lực (heatmap), knowledge graph, và quan trọng nhất — **semantic search** để truy hồi "quyết định/doc/PR cho feature X nằm đâu".

### Trong phạm vi v1
- Ingestion từ GitHub (PR, release, commit) + backfill từ git history.
- Lớp deterministic: metrics theo ngày, co-change graph, roadmap từ status transition.
- Lớp AI: tóm tắt PR thành nhật ký, trích entity, embeddings cho search.
- 5 view (đã build): activity heat · semantic search · entity-confirm · pyramid · roadmap theo năm. (knowledge graph = Phase 4.)

### Ngoài phạm vi v1 (non-goals — có chủ đích)
- **KHÔNG** chấm điểm / xếp hạng contribution bằng AI (non-deterministic + Goodhart's law).
- **KHÔNG** realtime (tiến độ dev không phải real-time domain — dùng ISR).
- **KHÔNG** đa-nguồn phức tạp ngay (Jira/Notion là adapter mở rộng sau Phase 2).

---

## 2. Kiến trúc tổng quan

Event-sourced. `events` là source of truth **immutable**; mọi thứ khác là **projection** derive được và rebuild được.

```
Sources (webhook + backfill)
        ↓
  events  ── immutable, append-only, source of truth
        ↓
  Projector (worker)  ── cursor per-projector + logic_version
        ↓
  ┌─────────────── projections (disposable, replayable) ───────────────┐
  │  deterministic (git facts)        │  AI (probabilistic)            │
  │  metrics_daily · entity_edges     │  narratives · embeddings       │
  │  status_history                   │                                │
  └────────────────────────────────────────────────────────────────────┘
        ↓
  Next.js dashboard (ISR, read-only)
```

**Nguyên tắc cốt lõi:**
1. Event bất biến → mọi projection rebuild được tại bất kỳ thời điểm (time-travel + replay khi nâng prompt).
2. Tách rạch ròi **deterministic** (tính từ git, reproduce được, drive heatmap/roadmap) vs **probabilistic** (AI summarize/extract, luôn kèm source link để verify). Cột `derived_by` đánh dấu ở mọi projection.
3. Entity neo vào **canonical ID** (file path / module / epic key), không dùng tên AI bịa → giải entity resolution.

---

## 3. Tech stack

### 3.1 Frontend

| Hạng mục | Lựa chọn | Ghi chú |
|---|---|---|
| Framework | **Next.js 15** (App Router) | Server Components query thẳng Postgres; ISR cho refresh định kỳ |
| UI | **Shadcn UI + Tailwind CSS** | Dark-mode, clean, đã quen |
| Pyramid heatmap | Custom **Tailwind grid** | Khối do người định nghĩa; màu = metric deterministic (commit 30 ngày) |
| Knowledge graph | **react-force-graph** (MVP) → **sigma.js** (scale) | Render **subgraph-by-query**, cap node hiển thị — KHÔNG render cả graph |
| Roadmap | Custom timeline (Tailwind) | Đọc từ `status_history`, nhóm theo năm |
| Semantic search | Search box → RPC `match_embeddings` | Con ngựa thồ của "second brain" |
| Data fetching | Server Components + ISR; TanStack Query cho client interaction | **Không** Supabase Realtime (YAGNI) |

### 3.2 Backend

| Hạng mục | Lựa chọn | Ghi chú |
|---|---|---|
| Webhook receiver | **FastAPI** (Python) | HMAC verify, idempotent insert, ACK <10s |
| Worker / queue | **ARQ + Redis** | Sweep theo cursor, debounce bằng `_job_id` coalescing; khớp kinh nghiệm lark-multica-bridge |
| Database | **Postgres 16 + pgvector** (`pgvector/pgvector:pg16`) | Local trong compose; schema event-sourced (migration 001 + 002) |
| AI — summarize | **Claude Haiku 4.5** | Bulk PR summary, structured output qua forced tool_use |
| AI — rollup | **Claude Sonnet 4.6** | Nhật ký tổng hợp tuần/release |
| Embeddings | **Gemini `gemini-embedding-001` @1536** (L2-normalized; `RETRIEVAL_DOCUMENT`/`_QUERY`) | ✅ Đã chốt & triển khai (P2A). Khớp `vector(1536)` → không migrate. VN+EN multilingual |
| Enrichment | **GitHub API** | Fetch diff + linked issues (webhook gốc không kèm đủ) |

> **Vì sao hai ngôn ngữ?** Frontend TS/Next, pipeline Python — vì hệ sinh thái AI/data-processing là Python-native (Pydantic, ARQ) và khớp kỹ năng sẵn có. Split có chủ đích, không phải tình cờ.

### 3.3 Deploy & infra

| Thành phần | Nơi chạy | Ghi chú |
|---|---|---|
| db (Postgres 16 + pgvector) | **Trong compose** | Local, sát worker; init tự apply 000+001+002 lần đầu |
| redis · receiver · worker · backup · cloudflared | **Docker Compose** (VPS *hoặc* Mac) | Một lệnh `up`; image multi-arch nên chạy cả amd64/arm64 |
| Webhook endpoint | **Cloudflare Tunnel** | Không cần public IP; route → `receiver:8000` |
| Backup | **pg_dump → Cloudflare R2** | Sidecar dump định kỳ; retention bằng R2 lifecycle rule |
| Frontend | **Cloudflare Pages** *hoặc* compose profile `frontend` | Pages cho zero-ops; hoặc self-host cùng stack nếu muốn all-in-one |

---

## 4. Cấu trúc repo

Single repo, hai ngôn ngữ tách thư mục:

```
dev-second-brain/
├── supabase/
│   └── migrations/
│       ├── 000_local_roles.sql       # shim role 'authenticated' cho Postgres local
│       ├── 001_schema.sql            # event log + projections + entity registry + pgvector
│       └── 002_idempotency.sql       # input_hash, content_hash, natural keys
├── pipeline/                         # FastAPI receiver + ARQ worker
│   ├── pipeline.py                   # receiver + sweep + filter + structured summarize
│   ├── backfill.py                   # seed events từ git history (Phase 1)
│   ├── rollup.py                     # nhật ký tổng hợp Sonnet (Phase 5)
│   ├── requirements.txt
│   └── Dockerfile
├── backup/                           # sidecar pg_dump → R2
│   ├── Dockerfile
│   └── backup.sh
├── web/                              # Next.js 15 + Shadcn
│   ├── app/
│   │   ├── dashboard/
│   │   └── api/
│   ├── lib/db.ts
│   └── Dockerfile                    # chỉ cần nếu self-host (profile frontend)
├── docker-compose.yml
├── .env.example
└── README.md
```

---

## 5. Docker setup (all-in-one)

Toàn bộ stack trong một `docker-compose.yml`, host-agnostic — `up` được trên VPS hay Mac local. File chuẩn nằm ở repo: `docker-compose.yml`, `backup/Dockerfile`, `backup/backup.sh`, `pipeline/Dockerfile`, `.env.example`.

### 5.1 Services trong compose

| Service | Image / build | Vai trò |
|---|---|---|
| `db` | `pgvector/pgvector:pg16` | Postgres + pgvector; init tự apply `000→001→002` lần đầu |
| `redis` | `redis:7-alpine` | Queue cho ARQ |
| `receiver` | build `./pipeline` | Webhook FastAPI (HMAC, idempotent insert, ACK) |
| `worker` | build `./pipeline` | Projector ARQ sweep |
| `backup` | build `./backup` | Sidecar `pg_dump → R2` mỗi `BACKUP_INTERVAL` |
| `cloudflared` | `cloudflare/cloudflared` | Tunnel → `receiver:8000`, không cần public IP |
| `web` | build `./web` | Frontend — **opt-in** (`--profile frontend`); bỏ nếu dùng Pages |

`DATABASE_URL` được compose dựng từ `POSTGRES_*` (một nguồn mật khẩu duy nhất). Trong mạng compose, host của DB là `db`, của redis là `redis`.

### 5.2 Quy trình deploy (VPS hoặc Mac — y hệt nhau)

```bash
# 1. Docker: VPS → Docker Engine; Mac → OrbStack / Docker Desktop / colima
# 2. Chuẩn bị env
cp .env.example .env && $EDITOR .env          # điền secrets + R2 + TUNNEL_TOKEN
# 3. Up backend (web để cho Pages):  docker compose up -d --build
#    Hoặc self-host cả frontend:      docker compose --profile frontend up -d --build
# 4. Cloudflare: tạo Tunnel route → receiver:8000; bật Access cho dashboard
# 5. Verify: backup container chạy 1 lần ngay khi start → kiểm tra R2 có file
```

Image đều multi-arch nên chuyển host = copy `.env` sang máy mới rồi `up` lại. Khác biệt VPS vs Mac chỉ là vận hành: **Mac local** miễn phí (phần cứng sẵn) nhưng phụ thuộc uptime/đường mạng nhà; **VPS** tốn phí tháng nhưng uptime/bandwidth tốt hơn và off-site sẵn. Tunnel lo phần không-cần-public-IP cho cả hai.

### 5.3 Backup & restore (R2)

- Sidecar `backup` dump `pg_dump --no-owner --no-privileges | gzip` → `s3://$R2_BUCKET/backups/`, chạy ngay khi start rồi lặp theo `BACKUP_INTERVAL`.
- Retention: đặt **R2 Lifecycle Rule** (xoá object > N ngày) trong Cloudflare dashboard.
- Khôi phục:
  ```bash
  aws s3 cp s3://$R2_BUCKET/backups/<file>.sql.gz - --endpoint-url $R2_ENDPOINT \
    | gunzip | psql "$DATABASE_URL"
  ```

> **Lưu ý RLS:** bản local dùng Postgres thuần nên `000_local_roles.sql` tạo role `authenticated` (no-op) để Section 8 trong `001` không lỗi lúc init. RLS thực tế vô hiệu (worker/frontend connect bằng superuser → bypass); auth thật cho dashboard là **Cloudflare Access**. Init-scripts chỉ chạy khi volume DB còn rỗng — đổi schema về sau phải migrate tay.

---

## 6. Lộ trình theo phase

> **Tiến độ (2026-06-25):** ✅ Phase 0 · ✅ Phase 1 · ✅ Phase 2 (A narrative+Gemini · B search · C entity-confirm) · ✅ Phase 3 (rich pyramid + roadmap milestones) · ✅ live ingest · ✅ multi-project namespace · ✅ §5 co-change cap · ✅ §7.6 commit identity. ⏳ Phase 4 graph · Phase 5 rollup+ops · Lark/docs ingest.

Nguyên tắc thứ tự: **làm cái hữu ích trước cái long lanh**. Event log + dashboard deterministic chạy được *trước khi* tốn một đồng LLM nào. Graph 3D làm **cuối cùng**, sau khi entity resolution đã chắc.

| Phase | Mục tiêu | Deliverable chính | Exit criteria | Ước lượng* |
|---|---|---|---|---|
| **0 — Foundation** | Hạ tầng + schema sống | `docker compose up` (db init 000+001+002); Cloudflare Tunnel + Access | Webhook nhận & lưu event vào log; ACK <10s verify; init chạy sạch; backup R2 có file | ~2–3 ngày |
| **1 — Deterministic backbone** | Dashboard hữu ích, $0 LLM | `backfill.py`; metrics + co-change projector; frontend skeleton + pyramid heatmap | Heatmap hiện commit activity từ git history thật; zero LLM cost | ~1 tuần |
| **2 — AI narrative + search** | "Second brain" thật | Narrative projector (Haiku, structured); enrichment GitHub API; semantic search box; entity-confirm UI | PR merged → diary entry; search ngữ nghĩa trả kết quả; dev confirm được entity | ~1.5–2 tuần |
| **3 — Pyramid + Roadmap** | Cấu trúc + thời gian | Pyramid layer/block thủ công + heat overlay; roadmap theo năm; capture status transition | Kim tự tháp render từ structure người định nghĩa; roadmap tua được theo năm | ~1 tuần |
| **4 — Knowledge graph** | Mạng tri thức | Graph subgraph-by-query (sigma.js nếu cần); edge confirm; clustering/LOD | Graph render mượt từ entity đã confirmed; không lag ở scale | ~1–1.5 tuần |
| **5 — Rollup + ops** | Nhật ký tổng hợp + vận hành | `rollup.py` (Sonnet) viết diary tuần/release; cost dashboard; replay runbook | Nhật ký tuần đọc được; theo dõi được cost; replay 1 projector thành công | ~1 tuần |

<sub>*Ước lượng giả định workflow solo + Claude Code. Điều chỉnh theo team size thực tế.*</sub>

**Tổng:** ~6–8 tuần. Sau Phase 1 đã có thứ demo được; sau Phase 2 đã có giá trị cốt lõi (nhật ký + search).

---

## 7. Cost & ops

### Đòn bẩy cắt cost (theo thứ tự tác động)
1. **Filter funnel** — AI chỉ chạm `pr.merged`/`release.tagged`/`doc.updated`, loại bot & file rác (~10–20× ít hơn). Đây là đòn lớn nhất.
2. **PR-level (squash)** — một summary/PR, không phải một/commit.
3. **Model tiering** — Haiku cho bulk summarize, Sonnet chỉ cho rollup định kỳ.
4. **Diff đọc một lần** — rollup đọc PR-summary (rẻ), không đọc lại diff.
5. **Content-hash cache** — reprocess/replay không trả tiền LLM cho input cũ.
6. **Prompt caching** — cache system+tool prefix qua hàng ngàn call.

### Vận hành
- **Monitoring:** cost dashboard (token/ngày theo model) — tái dùng pattern logging-proxy + Metabase đã làm cho THÍCH CAY.
- **Replay runbook:** nâng prompt → `truncate` projection tương ứng + bump `logic_version` + reset cursor → worker tự rebuild. (Recipe ở cuối `001_schema.sql`.)
- **Backup:** sidecar `backup` dump `pg_dump → R2` định kỳ (`--no-owner` để restore portable); retention bằng R2 lifecycle rule. Khôi phục: `aws s3 cp s3://$R2_BUCKET/backups/<file> - --endpoint-url $R2_ENDPOINT | gunzip | psql $DATABASE_URL`. Thêm: phần lớn `events` đến từ GitHub (durable upstream) nên mất DB local = backfill + re-derive, **không** mất vĩnh viễn; chỉ dữ liệu người curate (entity confirmed, layer/block, status note) là không tái tạo được — chính là thứ R2 backup bảo vệ. Events bất biến nên worker chết chỉ cần khởi động lại là sweep tiếp.

---

## 8. Quyết định kiến trúc (decision log)

| Quyết định | Lý do | Phương án đã loại |
|---|---|---|
| Event-sourcing (events bất biến, projection derive) | Time-travel + replay khi nâng prompt | Lưu LLM output làm dữ liệu chính |
| **Không** AI scoring; heat tính deterministic | Non-deterministic + Goodhart; thưởng code phình | `contribution_score` 1–10 |
| Tách deterministic/probabilistic (`derived_by`) | Reproducibility + cost + trust | Trộn AI vào mọi metric |
| Drop realtime, dùng ISR | Dev progress không phải real-time domain | Supabase Realtime |
| Entity neo canonical ID (không tên AI) | Giải entity resolution, tránh node gần-trùng | Để AI tự đặt tên free-text |
| Cursor sweep (không job-per-event) | Ordered + idempotent + replayable | Per-event ARQ job |
| 4-gate idempotency + content-hash | Webhook retry/crash/replay safe + cost | Insert thẳng, không dedup |
| pgvector semantic search là ưu tiên | Đây mới là "second brain" thật, hơn graph 3D | Bỏ qua retrieval |
| Graph làm cuối, subgraph-by-query, WebGL | react-force-graph không scale qua ~150 node | Render cả graph mọi lúc |
| Docker Compose all-in-one, host-agnostic (VPS/Mac) | Một stack chạy mọi nơi; image multi-arch; chuyển host = copy `.env` + `up` | Vercel-everything (đắt, rời infra) |
| Postgres local trong compose thay Supabase managed | Worker sát DB (write path nhanh hơn nhiều); kiến trúc này không dùng PostgREST/Auth (Server Components query thẳng) | Supabase managed (chỉ đáng nếu muốn off-load ops) |
| Backup `pg_dump → R2` + events durable upstream | Tự lo durability không cần managed; stakes thấp vì event tái tạo được từ GitHub | Dựa vào managed PITR |
| Python pipeline + TS frontend | Hệ sinh thái AI Python + khớp ARQ/Pydantic | Toàn bộ Node/BullMQ |

---

## 9. Rủi ro & giảm thiểu

| Rủi ro | Mức | Giảm thiểu |
|---|---|---|
| AI trích entity bịa (hallucination) | Cao | `guard_canonical_key` + human confirm + replay khi nâng prompt |
| Graph degrade ở scale (nhiều năm) | Trung bình | Subgraph-by-query + WebGL (sigma.js) + clustering/LOD |
| Cost LLM phình | Trung bình | Filter funnel + Haiku + content-hash cache + cost dashboard |
| GitHub API rate limit (enrich diff) | Trung bình | Token + backoff + cache diff vào event payload |
| Host (VPS/Mac) là single point of failure | Trung bình | R2 backup định kỳ + events tái tạo được từ GitHub; CF tunnel auto-reconnect; chuyển host = copy `.env` + `up` |
| Khoá nhà cung cấp embedding / đổi dimension | Thấp | Lưu `content` gốc → re-embed qua replay nếu đổi model |

---

## 10. Câu hỏi mở / cần chốt

1. **Person identity resolution** — team nhỏ & ổn định thì map thẳng `actor` thô được; cần thì mở rộng `person` entity + aliases sau (không phải sửa schema).
2. ✅ **Embedding provider — CHỐT: Gemini `gemini-embedding-001` @1536** (L2-normalized, `RETRIEVAL_DOCUMENT`/`_QUERY`). Khớp `vector(1536)` sẵn có → không migrate; đã triển khai trong `embed()` (P2A).
3. **Frontend host** — Cloudflare Pages (zero-ops, mặc định) hay compose profile `frontend` để self-host cùng stack?
4. **Nguồn ngoài git** — sau Phase 2 cần adapter cho Jira/Linear? Notion/Obsidian/Lark Wiki? (quyết định ingestion adapters).
5. **Team size / velocity** — để chốt lại timeline ở Mục 6.

---

*Plan này hợp nhất toàn bộ thiết kế qua các vòng: schema event-sourced, pipeline queue/filter/structured-output, và stack + deploy all-in-one. Artifact kèm theo: `001_schema.sql`, `002_idempotency.sql`, `000_local_roles.sql`, `pipeline.py`, `docker-compose.yml`, `backup/Dockerfile`, `backup/backup.sh`, `.env.example`.*


---

# Phụ lục — Deep-dive Phase 2/3 + Leverage roadmap (đã hợp nhất)

> Hợp nhất từ PROJECT_PLAN_P2_P3.md (2026-06-25). Các phần Phase 2/3 ở đây **đã ship**; §3 (leverage #1–7) và §7 (devbrain CLI) là hướng tương lai.

## 0. Quyết định đã chốt (locked)

### 0.1 Connectors / nguồn ingest

| Stack | Cơ chế | `source` · `event_type` | Lane | Ghi chú |
|---|---|---|---|---|
| **GitHub** | Webhook + REST enrichment | `github` · `pr.merged`/`commit.pushed`/`release.tagged`/**`issue.resolved`** | Deterministic + AI | Thêm subscribe `issues`(closed) → `issue.resolved` (nuôi AI gate + roadmap) |
| **Lark** (Docs/Wiki) | Event subscription + Open API fetch | `lark` · `doc.updated` | AI | Endpoint `/webhooks/lark` mới (challenge + AES Encrypt Key, **khác** HMAC GitHub) |
| **Docs import rời** | CLI batch (`import_docs.py`) | `import` · `doc.updated` | AI | `source_event_id = sha256(nội dung)` → idempotent; PDF đọc qua Claude Files API |
| **Claude Code** | *Không phải nguồn* | — | — | Hoạt động kết tinh thành commit/PR (đã bắt qua GitHub). Claude Code là **consumer** (xem §3 hướng #1) |

### 0.2 Model LLM (giữ tiering trong `pipeline.py`)

| Vai trò | Model ID | $/1M (in/out) | Dùng cho |
|---|---|---|---|
| `MODEL_SUMMARY` | `claude-haiku-4-5-20251001` | $1 / $5 | Bulk summary + entity extract (PR/doc), forced `tool_use` |
| `MODEL_ROLLUP` | `claude-sonnet-4-6` | $3 / $15 | Rollup diary tuần/release (Phase 5) |

Opus 4.8 / Fable 5 = **thừa** cho pipeline cost-sensitive này.

### 0.3 Embeddings — Gemini (khóa schema)

- **Model:** `gemini-embedding-001`, `output_dimensionality=1536` (cắt MRL), **L2-normalize sau khi cắt**.
- **task_type:** `RETRIEVAL_DOCUMENT` khi index, `RETRIEVAL_QUERY` khi search (asymmetric).
- **`EMBED_DIM=1536` đã đúng; `vector(1536)` trong schema đã đúng → KHÔNG migrate.** ✅ **Done (P2A):** `embed()` đã rewrite Voyage → Gemini (cũng giải mismatch voyage-3=1024 cũ).
- **Vì sao 1536, không 3072:** pgvector HNSW chỉ index ≤2000 dim; 3072 phải dùng `halfvec`. 1536 dùng `vector` chuẩn, lọt dưới ngưỡng.
- **Giới hạn input ~2048 token/lần** → docs dài phải **chunk** trước khi embed (PR summary ngắn thì thừa sức).
- **Lợi VN+EN miễn phí:** đa ngôn ngữ → query tiếng Việt tìm ra code/doc tiếng Anh và ngược lại.
- `.env`: ✅ đã thêm `GEMINI_API_KEY`; **đã bỏ** `VOYAGE_API_KEY` khỏi `.env.example` (không còn fallback Voyage). Cũng cần `GITHUB_TOKEN` thật (enrichment) — đã set từ `gh auth token`.

### 0.4 Đòn cost

1. **Batches API (−50%)** cho mọi việc latency-tolerant: backfill summary lịch sử, import docs hàng loạt, rollup ban đêm.
2. **input-hash cache** (đã có ở [pipeline.py:493](pipeline/pipeline.py)) — replay không trả tiền lại cho input cũ.
3. ⚠️ **Prompt caching trên Haiku cần prefix ≥ 4096 token.** `PR_SYSTEM_PROMPT` + tool schema hiện ngắn → `cache_control` rất có thể **âm thầm không cache** (`cache_creation_input_tokens: 0`). Verify bằng `usage.cache_read_input_tokens`; nếu = 0 thì **Batches + funnel** mới là đòn tiết kiệm thật, không phải caching.

### 0.5 Architecture delta (Lark + import nối vào)

```
GitHub webhook ─┐
Lark events    ─┼─► receiver (verify per-source) ─► events (immutable) ─► sweep (cursor/projector)
import_docs.py ─┘                                         │ split
                                          deterministic lane          AI lane (gated: pr.merged/release/doc.updated)
                                          metrics_daily               narratives · embeddings (Gemini@1536)
                                          entity_edges                        │
                                                                       semantic search + entity-confirm UI
```

---

## 1. Phase 2 — AI narrative + semantic search

Lane AI bật lên. Chuỗi 5 mắt xích, **build theo thứ tự** (enrichment trước, nếu không AI lane đói dữ liệu).

| # | Mắt xích | Chạy ở | Golden rule then chốt |
|---|---|---|---|
| 1 ✅ | Enrichment | worker | **Done (P2A):** `enrich_pr_payload` fetch files/diff/issues → inject `_files/_diff/_linked_issues` vào BẢN SAO payload (event bất biến, không cache vào event gốc) |
| 2 ✅ | Narrative projector | worker | **Done (P2A):** LLM **NGOÀI** tx; write+cursor **TRONG** 1 tx; `_coerce_analysis` chịu output lệch enum (không poison PR) |
| 3 ✅ | Embeddings | worker | **Done (P2A):** Gemini@1536 (normalized); content-hash skip; khớp `vector(1536)` |
| 4 ⏳ | Semantic search | Next.js + RPC | Kết quả **link ngược source event** để verify (← next) |
| 5 ⏳ | Entity-confirm UI | Next.js | UPSERT on-conflict **chỉ** bump `last_seen_at` — không đè `display_name`/`pyramid_layer`/`resolution_status` |

### 1.1 Enrichment (tiền đề)
Webhook gốc thiếu nội dung → worker fetch và nhồi vào `payload` (đúng shape `parse_pr`/`changed_files` đã đọc):
- **GitHub:** REST → `pull_request._files` (additions/deletions), `_diff` (cắt `DIFF_CHAR_CAP`), `_linked_issues`. Backoff + token (`GITHUB_TOKEN`).
- **Lark:** Open API fetch nội dung doc/wiki khi `doc.updated` → `_doc_body`, `_doc_title`. (Có thể dùng skill `lark-docx`/`lark-wiki`.)
- **Import:** parser MD/Docx; **PDF → Claude Files API document block** (Haiku đọc trực tiếp, khỏi tự parse).

> **Generalize lane AI:** `summarize_pr`/`parse_pr` đang **PR-shaped**. Thêm nhánh `doc`-shaped (`summarize_doc`, scope `'doc'`) để Lark + import đi chung. `passes_ai_gate` **đã** chứa `doc.updated` → filter không phải sửa. `PRAnalysis` schema có thể tái dùng (summary_md + entities + edges) cho doc.

### 1.2 Narrative + embeddings (đã scaffold)
[pipeline.py `maybe_summarize`](pipeline/pipeline.py) đã đúng khung: gate → input-hash cache → `summarize_pr` (Haiku, forced `tool_use`) → `embed`. P2 cần:
- Rewrite `embed()` → Gemini (§0.3). Embed bản `summary_md` (rẻ hơn embed cả diff); với search docs có thể embed thêm chunk nội dung.
- Backfill/import hàng loạt → **Batches API**.

### 1.3 Semantic search (payoff)
Search box → embed query (`RETRIEVAL_QUERY`) → RPC `match_embeddings` (cosine `<=>`) → trả kết quả + citation source event. Đây là "con ngựa thồ" của second brain.

### 1.4 Entity-confirm UI
AI đề xuất entity `resolution_status='proposed'`; `guard_canonical_key` chặn hallucination (file phải khớp path thật, module phải là prefix, epic phải khớp issue key). UI để dev confirm/rename → ghi field người sở hữu. Mỗi lần AI gặp lại entity **không** đè nhãn người đã sửa.

### 1.5 Exit criteria P2
- [x] PR merged (qua webhook + enrich) → diary entry tự sinh. ✅ **(P2A, verified PR #1)**
- [ ] Lark doc / import doc → summary + embedding.
- [ ] Search ngữ nghĩa trả kết quả **có citation**; query VN tìm được doc/code EN.
- [ ] Dev confirm 1 entity; **replay projector không phá nhãn đó** (test `guard_canonical_key` + UPSERT).
- [ ] Cost: bulk chạy qua Batches; `usage.cache_read_input_tokens` được kiểm (xác nhận caching có/không hiệu lực).

---

## 2. Phase 3 — Pyramid + Roadmap (deterministic + human, KHÔNG AI)

P3 **không gọi LLM**. Giá trị = cấu trúc người định nghĩa + heat từ git. Golden rule #3: heat deterministic, **không bao giờ** AI chấm điểm người/đóng góp.

### 2.1 Pyramid capability heatmap

| Thành phần | Nguồn | Loại dữ liệu |
|---|---|---|
| Layer + Block (năng lực) | Người định nghĩa | **Curated** — registry `pyramid_blocks` (tên, layer, thứ tự) |
| Entity → Block mapping | Người gán (qua confirm UI) | Curated — `entities.pyramid_layer` + block ref |
| **Heat overlay** | `metrics_daily` (commit 30d) | **Deterministic** (`derived_by='git'`) |

- Block tô màu = metric tổng hợp các entity thành viên → **dùng lại y nguyên** số học "Activity heat" của [web/app/page.tsx](web/app/page.tsx), thêm tầng nhóm-theo-block. Render Tailwind grid.
- ⚠️ `pyramid_blocks` + mapping là **dữ liệu người curate → KHÔNG tái tạo từ events** → đây chính là thứ R2 backup bảo vệ (golden rule #10). Tách rõ khỏi projection disposable; không bao giờ truncate khi replay.

### 2.2 Roadmap theo năm

- Nguồn status: GitHub milestone/label/project-column, Lark wiki status, hoặc set tay → mỗi lần đổi = **event** → projector ghi `status_history(entity_id, status, occurred_at, derived_by)`.
- Entity kind `'epic'`/`'feature'` (đã có trong `EntityKind`), `canonical_key` neo vào epic/milestone/wiki-node thật.
- Roadmap đọc `status_history` nhóm theo năm, **tua được**. Phần derive từ label = `'git'`; note tay = `'human'`.
- ⚠️ Xác nhận/ thêm bảng `status_history` trong `001_schema.sql` (đối chiếu lúc build). Đây là projection **rebuildable** từ events — khác `pyramid_blocks` (curated).

### 2.3 Exit criteria P3
- [ ] Pyramid render từ structure người định nghĩa + heat overlay deterministic.
- [ ] Roadmap tua được theo năm từ status transition.
- [ ] `pyramid_blocks` + mapping nằm trong phạm vi R2 backup (curated, không re-derive).

---

## 3. Sau P2/P3 — 7 hướng để THẬT SỰ tận dụng

Plan gốc còn **Phase 4** (knowledge graph subgraph-by-query, sigma.js) và **Phase 5** (Sonnet rollup + cost/replay ops) — vẫn làm. Dưới đây là hướng **net-new**, xếp theo value/effort, đều dựng trên projection sẵn có (chi phí biên thấp). ⭐ = ưu tiên, khớp nhất stack của bạn.

### ⭐ #1 — devbrain như MCP server ("ask-the-brain" trong Claude Code)
Expose `search_brain(query)` + `get_context(entity)` qua MCP → dev hỏi ngay trong IDE: *"quyết định/doc/PR cho feature X ở đâu?"* → semantic search + Sonnet tổng hợp **kèm citation source event**. Đóng vòng "Claude Code là consumer". Dựng trên: search P2 + narratives. **Golden rule:** luôn trả citation để verify.

### ⭐ #2 — Weekly digest đẩy ngược vào Lark
Biến Lark từ *nguồn* thành *kênh giao*: rollup Sonnet (P5) tự post group Lark — *"Tuần này team ship X, quyết định Y, doc Z cập nhật"* + link. Biến second brain từ "trang phải vào xem" → "tự đến với người". Dựng trên: `rollup.py` + Lark Open API send-message.

### ⭐ #3 — Stale-doc / knowledge-drift detection (deterministic, high-signal)
Tín hiệu thuần git, **không chấm điểm người**: doc (Lark/import) lâu chưa update *trong khi* code liên quan (co-change neighbors) churn mạnh 30d → cờ *"doc có thể lỗi thời"*. Dựng trên: `metrics_daily` + entity links + doc `last_updated`.

### #4 — Onboarding dossier cho mỗi entity
Với file/module/service bất kỳ, tự lắp hồ sơ: làm gì (narratives) · đụng-cùng-gì (co-change) · doc liên quan (Lark/import) · thay đổi gần đây. Người mới có context tức thì. Chỉ tổng hợp projection sẵn có.

### #5 — Related-work / dedup tại ingest
PR/doc mới → pgvector nearest-neighbor → *"việc tương tự đã có ở đây"*. Chống làm lại, gợi prior art. Gần như free vì embeddings đã có.

### #6 — Blast-radius / impact lookup
*"Đổi file X thì gì co-change, doc/epic nào tham chiếu?"* — co-change (deterministic) + AI edges + doc links. Context trước khi sửa chỗ rủi ro.

### #7 — Time-travel "ta đã biết gì vào ngày D"
Quà free từ event-sourcing: events bất biến + projection replayable → tái dựng trạng thái brain tại mốc quá khứ. Dùng cho retro/audit.

---

## 4. Build order gợi ý (Mac Mini)

1. **Mở connector trước AI:** thêm GitHub `issues`; dựng `/webhooks/lark` (challenge + AES); `import_docs.py`.
2. **Enrichment** (GitHub REST → Lark API → import parser/Files API) — nạp `payload` đầy đủ.
3. **Rewrite `embed()` → Gemini@1536** (giữ `EMBED_DIM=1536`/`vector(1536)`); thêm `GEMINI_API_KEY`.
4. **Generalize AI lane** doc-shaped (`summarize_doc`, scope `'doc'`); chạy Batches cho backfill/import.
5. **Semantic search** (RPC + search box + citation) → **Exit P2**.
6. **Phase 3:** `pyramid_blocks` registry + mapping UI + heat overlay; `status_history` + roadmap → **Exit P3**.
7. **Tận dụng:** #1 MCP → #2 Lark digest → #3 stale-doc (bộ ba ưu tiên), rồi Phase 4 graph / Phase 5 ops.

## 5. Phụ lục — tuning mở (từ verify Phase 1)

- **Co-change phình ở commit lớn:** commit "initial import" sinh C(n,2) cạnh weight=1. Cân nhắc bỏ qua co-change cho commit đụng > N file (vd 50) trong `apply_cochange`. Dashboard order theo `weight desc` nên cạnh có ý nghĩa vẫn nổi — đây là tối ưu, không phải bug.
- **`embed()` mismatch cũ** (voyage-3=1024 vs `EMBED_DIM`/`vector`=1536): **giải bằng chính việc rewrite sang Gemini@1536** ở bước 3 — không cần migrate schema.
- **⚠️ Latent dup webhook↔backfill (cần fix):** webhook `push` key theo `X-GitHub-Delivery` (1 event/push, gộp N commit), còn `backfill.py` key theo `sha` (1 event/commit) → cùng commit nạp **cả hai đường** bị **đếm 2 lần** vào `metrics_daily`/co-change — `unique(source, source_event_id)` không chặn vì `source` lẫn key đều khác. Đây là latent bug **có sẵn** (không do CLI; CLI chỉ làm dễ xảy ra hơn). Fix = **transport-independent commit identity** (xem §7.6): fan-out webhook per-commit + dùng chung `source='git'`/`sha`.

---

## 6. Bổ sung từ prototype "Dev Team Brain"

Trích từ prototype UI (artifact). Phân loại: ✅ **fold** vào PLAN · ❌ **rejected** (vi phạm golden rule). Mục này **cập nhật/mở rộng** §0–§3 ở trên.

### 6.1 Nguồn mới (mở rộng §0.1)

| Nguồn | Tín hiệu prototype | event_type | Lane |
|---|---|---|---|
| **CI/CD + Deploy** | "Build health 94% · CI/CD success rate"; block "Build Health"; "Deploy · Production v1.8.2" | `github` · `ci.run` / `deploy.succeeded` | Deterministic (build-health) |
| **Bug** | "Bug cycle 2.1d · detect → fixed"; "Bug Intelligence"; node "Bugs Fixed"; "BUG-421" | `github` · `bug.opened`/`bug.fixed` (hoặc dùng `issue.*` có label `bug`) | Deterministic (cycle-time) |

- CI/CD **không phải vendor mới** — vẫn là GitHub Actions webhook: subscribe `workflow_run` / `check_suite` / `deployment_status`. Enrich/normalize trong **worker** (golden rule #9). Metric = CI success rate (deterministic).
- Bug lane: theo dõi open→close → ghi `status_history` + tính **cycle-time** (detect→fixed). "Rule cảnh báo regression" = optional, để sau.

### 6.2 Metric / KPI (deterministic, mở rộng §2/§3)

- **Grain "sprint"**: cấu hình ranh giới sprint → cửa sổ cho KPI + rollup. Đổi rollup Phase 5 từ "tuần" → **per-sprint** ("Báo cáo sprint").
- **KPI scorecard band** (4 số top-line, trên heatmap): **Velocity** (throughput vs previous sprint) · **Build health** (CI success rate) · **Bug cycle** (mean detect→fixed) · **Knowledge coverage** (% feature có link doc).
- **Knowledge coverage** = `#feature có ≥1 link doc / #feature`, đếm deterministic từ entity↔doc (link do lane AI tạo, đếm thuần). Cũng phản ánh ở block "Docs Sync"/"Feature Registry".

> ⚠️ **GUARDRAIL Golden Rule #3 — ghi ngay cạnh KPI/Velocity:** mọi KPI là **team-aggregate, throughput/process, deterministic**. TUYỆT ĐỐI **không** per-person, **không** AI-score, **không** ranking. "Velocity" = lượng PR/feature ship được ở mức team, không quy về cá nhân.

### 6.3 Pyramid — mở rộng §2.1 (P3)

- Mỗi block thêm: **maturity %** + **risk class** (`Low risk` / `In progress` / `Critical asset`) — **human-curated** (nằm trong `pyramid_blocks`, không AI-score, không re-derive → R2-backup-protected).
- **Toggle chiều heat:** `Maturity` (curated) ↔ `Activity` (commit 30d, deterministic).
- Block names tham khảo từ prototype: Source Index · Docs Sync · Observability · Release Ledger · Bug Intelligence · Build Health · Feature Registry · AI Summary · Knowledge Graph · Team Brain.

### 6.4 Knowledge graph — mở rộng Phase 4

- Bổ sung node kind: **`release`** · **`plan`** · **`person`**.
- **`person`** ("Contributors") = chạm câu hỏi mở [PROJECT_PLAN §10.1](PROJECT_PLAN.md) (person identity): map thẳng `actor` thô trước, mở rộng `person` entity + aliases sau. **Person KHÔNG kèm điểm/score** (golden rule #3) — chỉ là node liên kết, không xếp hạng.

### 6.5 Evidence Ledger — surface citation hạng nhất (✅ rất nên)

- "Link & nguồn tham chiếu": mỗi insight/narrative liên kết **source thật** với nút **Open**. Source types: **GitHub** (PR) · **Deploy** (release) · **Docs** (ADR) · **Bug**.
- Đây là **hiện thực của golden rule** "AI luôn kèm source link để verify" và là **nền cho hướng #1 (MCP ask-the-brain có dẫn chứng)**. Dữ liệu đã sẵn: `narratives.source_event_ids`, `embeddings`→event, `events.source_url`.

### 6.6 AI report — mở rộng Phase 5 rollup

- Cấu trúc **2 mục: Highlights + Risks & next actions** (Sonnet, scope per-sprint). **Mọi dòng kèm citation** từ Evidence Ledger.
- **"source confidence score cho từng insight AI"** = độ tin cậy **output AI** (✅ hợp lệ — KHÔNG phải chấm người); hiển thị cạnh narrative, optional.

### 6.7 ADR doc-type

- **ADR** (Architecture Decision Record, vd `ADR-009`) là doc-type hạng nhất trong nguồn docs (Lark/import/GitHub) → **decision-log / ADR registry** (khớp hướng leverage). `canonical_key` neo vào ADR id.

### 6.8 ❌ Rejected — Contribution scoring (vi phạm Golden Rule #3)

- Panel **"Đóng góp theo người / module"** với điểm so sánh (Backend **86** · Frontend **78** · DevOps **73** · QA **69**) và roadmap **"Contribution map"** = **chấm/xếp hạng đóng góp** → **KHÔNG đưa vào**. Đây đúng **non-goal #1** của PROJECT_PLAN.
- **Reframe deterministic (nếu cần, mặc định bỏ):** "Activity by area" = đếm số file/PR chạm theo **module** ở mức **team**, **không điểm so sánh**, **không quy về người**.

### 6.9 Tự xác nhận từ chính prototype

- "Risks & next actions" của prototype tự nhắc *"tách event ingestion worker để tránh nghẽn khi import lịch sử commit lớn"* → trùng **golden rule #9** + phụ lục §5 (co-change cap). Giữ nguyên hướng.
- Roadmap tương lai của prototype (AI report engine · Predictive roadmap · Auto release narrative · **Architecture copilot**) — "Architecture copilot" ≈ **hướng #1 (MCP cho Claude Code)**. Khớp tầm nhìn §3.

---

## 7. devbrain CLI (lấy cảm hứng từ codegraph)

Nguồn: [codegraph](https://github.com/colbymchenry/codegraph) — code knowledge graph **local-first** (tree-sitter → SQLite `.codegraph/codegraph.db` WAL+FTS5, FS-watcher sync debounce 2s, MCP `codegraph_explore` cho AI agent: surgical context, call paths, blast-radius; publish npm). Mượn **delivery model (CLI publish + MCP)** và **granularity AST-symbol**, nhưng phải hòa giải với event-sourcing.

> **⚠️ Điểm hòa giải cốt lõi:** central event log lưu *"what happened"* (commit/PR/doc facts), **KHÔNG lưu source code** → **không rebuild được symbol graph từ central events**. Symbol graph là *"what is"* (snapshot AST), cần source thật → **dựng tại local (CLI)**. Nó là **projection deterministic, disposable** (Golden Rule #2); central brain chỉ nhận **activity events** + (optional) edge đã tổng hợp. Đây là cách mượn codegraph mà **không** tạo source-of-truth thứ hai.

### 7.1 Mô hình: CLI = ingestion client + local AST companion
- CLI **không chạm DB**. Hai vai: **(1) ingestion client** — push activity events lên backend qua ingest API (= backfill, không cần webhook); **(2) local AST companion** — dựng `.devbrain/graph.db` (tree-sitter) cho `explore`/`affected`/MCP.
- Local symbol graph = **"Phase 4 graph nhưng local-first + AST-sâu + hướng AI-agent"** — *bổ sung*, không thay graph central.

### 7.2 Command surface

| `devbrain <cmd>` | Làm gì | ↔ codegraph |
|---|---|---|
| `init` | Tạo `.devbrain/config.toml` (backend tunnel, repo id, token ref) + gitignore | `init` |
| `index` | Walk git history → **push activity events** (backfill qua API); **dựng local `.devbrain/graph.db`** (tree-sitter symbols + `depends_on`/`calls`) | `init` build |
| `sync` | Incremental: commit mới từ cursor → push events + update local graph (FS watcher, debounce) | `sync` |
| `explore` / `search <q>` | **Hybrid**: local symbol graph (call paths, blast-radius) **+** remote semantic search (narratives/docs đa nguồn, có citation) | `explore`/`query` |
| `affected <files>` | Blast-radius: symbol/test/doc/epic bị ảnh hưởng (= leverage **#6**) | `affected` |
| `status` | Cursor, freshness, staleness banner | `status` |
| `mcp` / `install` | Đăng ký devbrain làm **MCP server** cho Claude Code/Cursor (= leverage **#1**, ask-the-brain có dẫn chứng) | `install` + MCP |
| `watch` | Daemon FS-watcher auto-sync | (auto-sync) |
| `upgrade` | Cập nhật (pip) | `upgrade` |

### 7.3 Packaging
- **Python + PyPI + pipx** (`pipx install devbrain`, entry point `devbrain`) — tái dùng `backfill.py` + `tree-sitter` Python bindings, **giữ một ngôn ngữ** (golden rule "không introduce style thứ 2"). Codegraph chọn npm/TS vì nhắm JS-heavy + bundle Node; pipeline devbrain là Python nên Python hợp hơn. Chỉ npm-hoá nếu sau cần tối ưu install cho dev non-Python.
- Config `.devbrain/config.toml` (gitignored); auth `DEVBRAIN_TOKEN` sau Cloudflare Access.

### 7.4 Backend delta — ingest API (kênh ingest hạng nhất)
- **`POST /ingest/events`** trên receiver: batch, **token/HMAC auth**, cùng insert idempotent `unique(source,source_event_id)` + enqueue sweep, **ACK <10s** (golden rule #9). Tổng quát hóa webhook GitHub/Lark thành kênh ingest chung; CLI push qua đây, không chạm DB.
- (Optional) reuse search RPC làm endpoint cho `explore`/`mcp` (gộp leverage #1).

### 7.5 Local store `.devbrain/graph.db`
- SQLite (WAL) + tree-sitter: nodes (function/class/method/route), edges (`calls`/`imports`/`extends`/`implements`), FTS5 full-text.
- Sync: FS watcher (FSEvents/inotify), debounce; reconcile `(size, mtime)` + content-hash khi mở (như codegraph).
- Bắt đầu **hẹp**: chỉ ngôn ngữ team dùng (Python/TS) — **không** cố match 20+ lang & 17 framework của codegraph ngày một.

### 7.6 Golden-rule & rủi ro
- ✅ Symbol graph = facts tree-sitter → `derived_by='git'`, **không AI-score, không chấm người**.
- ✅ `.devbrain/graph.db` = projection **disposable, rebuildable** (Rule #2).
- ✅ **Transport-independent commit identity:** danh tính của một commit là **`sha`**, **không** phụ thuộc đường vận chuyển (webhook / backfill / CLI). Chuẩn hóa **mọi** event nguồn-commit về **1 event / commit**, dùng chung namespace — `source='git'` + `source_event_id=sha` — để `unique(source, source_event_id)` dedup **xuyên đường**; repo bật cả webhook + CLI vẫn **không** double-count (cursor + UPSERT lo phần còn lại).
  - Việc cần làm: đổi handler webhook `push` để **fan-out 1 event / commit theo `sha`** (payload đã có `commits[].id`) thay vì 1 event / delivery. Fan-out chỉ vài insert → giữ ACK <10s (push lớn thì enqueue). PR/release/doc **không bị** (chỉ đến từ webhook, key tự nhiên riêng) — vấn đề **chỉ ở commit**.
- ⚠️ **Không push từng symbol làm event** vào central log (nổ volume, sai bản chất "facts"). Symbol graph ở **local**; central chỉ nhận activity events + (optional) edge `depends_on` mức **module** đã tổng hợp.
- ⚠️ Không để local graph thành source-of-truth thứ hai.

### 7.7 Sequencing (mở rộng sau core; hợp nhất Phase 4 + leverage #1/#6)
1. **Ingest API** (push không-webhook) → 2. `init`/`index`/`sync` (local backfill client) → 3. **local tree-sitter symbol graph** + `explore`/`affected` → 4. **`devbrain mcp`** (gộp leverage #1) → **publish PyPI**.
