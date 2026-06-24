# Dev Second Brain — Project Plan

> Bộ não thứ 2 / nhật ký thời gian thực cho team dev: roadmap theo năm, kim tự tháp heatmap, knowledge graph, semantic search — dựng trên kiến trúc event-sourced.

| | |
|---|---|
| **Phiên bản** | 0.1 — Draft |
| **Ngày** | 2026-06-24 |
| **Trạng thái** | Sẵn sàng khởi động Phase 0 |
| **Hạ tầng nền** | Docker Compose all-in-one (VPS *hoặc* Mac) · Cloudflare Tunnel · Postgres local + backup R2 |

---

## 1. Mục tiêu & phạm vi

### Mục tiêu
Một website tổng hợp tiến độ toàn team dev theo thời gian, hoạt động như cuốn nhật ký sống: tự bóc tách từ git/PR/docs, dựng roadmap, kim tự tháp năng lực (heatmap), knowledge graph, và quan trọng nhất — **semantic search** để truy hồi "quyết định/doc/PR cho feature X nằm đâu".

### Trong phạm vi v1
- Ingestion từ GitHub (PR, release, commit) + backfill từ git history.
- Lớp deterministic: metrics theo ngày, co-change graph, roadmap từ status transition.
- Lớp AI: tóm tắt PR thành nhật ký, trích entity, embeddings cho search.
- 4 view: pyramid heatmap · roadmap theo năm · knowledge graph · semantic search.

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
| Embeddings | **Voyage-3** hoặc **Cohere multilingual** | Quan trọng cho nội dung VN+EN trộn; khớp `vector(N)` trong schema |
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
2. **Embedding provider** — Voyage-3 vs Cohere multilingual vs OpenAI 3-large cho VN+EN? (ảnh hưởng dimension cột `vector(N)`).
3. **Frontend host** — Cloudflare Pages (zero-ops, mặc định) hay compose profile `frontend` để self-host cùng stack?
4. **Nguồn ngoài git** — sau Phase 2 cần adapter cho Jira/Linear? Notion/Obsidian/Lark Wiki? (quyết định ingestion adapters).
5. **Team size / velocity** — để chốt lại timeline ở Mục 6.

---

*Plan này hợp nhất toàn bộ thiết kế qua các vòng: schema event-sourced, pipeline queue/filter/structured-output, và stack + deploy all-in-one. Artifact kèm theo: `001_schema.sql`, `002_idempotency.sql`, `000_local_roles.sql`, `pipeline.py`, `docker-compose.yml`, `backup/Dockerfile`, `backup/backup.sh`, `.env.example`.*
