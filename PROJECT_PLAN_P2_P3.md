# Dev Second Brain — Plan bổ sung: Phase 2/3 deep-dive + roadmap tận dụng

> Tài liệu nối tiếp [PROJECT_PLAN.md](PROJECT_PLAN.md). Đọc [CLAUDE.md](CLAUDE.md) (golden rules) trước.
> Phạm vi: thiết kế chi tiết Phase 2 (AI narrative + search) và Phase 3 (pyramid + roadmap),
> các quyết định stack đã chốt, và 7 hướng phát triển sau P3 để **thật sự tận dụng** second brain.

| | |
|---|---|
| **Phiên bản** | 0.1 — Draft |
| **Trạng thái** | Phase 1 đã verify (tests + dry-run trên git history thật); P2/P3 chưa code |
| **Build ở** | Mac Mini (giữ tiến độ liền mạch) |

---

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
- **`EMBED_DIM=1536` đã đúng; `vector(1536)` trong schema đã đúng → KHÔNG migrate.** Việc duy nhất: rewrite `embed()` từ Voyage sang Gemini. (Đây cũng là cách giải mismatch `embed()` gọi voyage-3=1024 cũ.)
- **Vì sao 1536, không 3072:** pgvector HNSW chỉ index ≤2000 dim; 3072 phải dùng `halfvec`. 1536 dùng `vector` chuẩn, lọt dưới ngưỡng.
- **Giới hạn input ~2048 token/lần** → docs dài phải **chunk** trước khi embed (PR summary ngắn thì thừa sức).
- **Lợi VN+EN miễn phí:** đa ngôn ngữ → query tiếng Việt tìm ra code/doc tiếng Anh và ngược lại.
- `.env`: thêm `GEMINI_API_KEY` (giữ `VOYAGE_API_KEY` cũng được để fallback).

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
| 1 | Enrichment | worker | Enrich trong worker, **không** receiver; cache content vào `payload` (replay khỏi gọi lại API) |
| 2 | Narrative projector | worker | LLM call **NGOÀI** transaction; write projection + advance cursor **TRONG** 1 transaction |
| 3 | Embeddings | worker | content-hash skip; `EMBED_DIM` khớp `vector(N)` |
| 4 | Semantic search | Next.js + RPC | Kết quả **link ngược source event** để verify |
| 5 | Entity-confirm UI | Next.js | UPSERT on-conflict **chỉ** bump `last_seen_at` — không đè `display_name`/`pyramid_layer`/`resolution_status` |

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
- [ ] PR merged (qua webhook + enrich) → diary entry tự sinh.
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
