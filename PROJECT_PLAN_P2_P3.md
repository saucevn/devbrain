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
- ✅ Idempotency xuyên đường: commit event **luôn** dùng `sha` làm `source_event_id` → repo có cả webhook + CLI **không double-count** (cursor + UPSERT lo phần còn lại).
- ⚠️ **Không push từng symbol làm event** vào central log (nổ volume, sai bản chất "facts"). Symbol graph ở **local**; central chỉ nhận activity events + (optional) edge `depends_on` mức **module** đã tổng hợp.
- ⚠️ Không để local graph thành source-of-truth thứ hai.

### 7.7 Sequencing (mở rộng sau core; hợp nhất Phase 4 + leverage #1/#6)
1. **Ingest API** (push không-webhook) → 2. `init`/`index`/`sync` (local backfill client) → 3. **local tree-sitter symbol graph** + `explore`/`affected` → 4. **`devbrain mcp`** (gộp leverage #1) → **publish PyPI**.
