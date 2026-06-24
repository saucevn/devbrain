-- =====================================================================
-- DEV SECOND BRAIN — Event-Sourced Schema for Supabase (Postgres 15+)
-- =====================================================================
-- Triết lý:
--   1. `events` là SOURCE OF TRUTH duy nhất, immutable, append-only.
--   2. Mọi bảng khác (graph, pyramid, metrics, narrative, embeddings)
--      đều là PROJECTION — derive được, rebuild được từ event gốc.
--   3. Tách rạch ròi DETERMINISTIC (git/jira facts) vs PROBABILISTIC (AI).
--      Cột `derived_by` đánh dấu nguồn ở MỌI projection.
--   4. Entity neo vào CANONICAL ID (file path, module, epic key),
--      KHÔNG dùng tên do AI bịa → giải bài entity resolution.
--   5. KHÔNG có "contribution_score" do AI gán. "Heat" tính
--      deterministic từ metrics. AI chỉ summarize + extract + viết diary.
--
-- Worker (ARQ/Redis trên Mac mini hoặc CF) ghi qua service_role.
-- Frontend (Next.js) chỉ READ qua authenticated role (RLS bên dưới).
-- =====================================================================


-- ---------------------------------------------------------------------
-- 0. EXTENSIONS
-- ---------------------------------------------------------------------
create extension if not exists vector;     -- pgvector: semantic search
-- gen_random_uuid() là core từ PG13, không cần pgcrypto trên Supabase.


-- =====================================================================
-- 1. EVENT LOG  — immutable source of truth
-- =====================================================================
-- Mỗi webhook event, mỗi commit backfill, mỗi Jira/Notion update
-- landing ở đây NGUYÊN GỐC. Payload không bao giờ bị sửa (trigger chặn).
-- `seq` cho TOTAL ORDER để projector dùng cursor-based replay.
-- `occurred_at` = lúc xảy ra ở hệ nguồn (KHÁC `ingested_at`).
-- ---------------------------------------------------------------------
create table events (
  seq             bigint generated always as identity primary key,
  id              uuid not null default gen_random_uuid() unique,

  source          text not null,            -- 'github' | 'jira' | 'notion' | 'manual'
  source_event_id text not null,            -- GitHub delivery ID / commit SHA / Jira changelog ID
  event_type      text not null,            -- convention: 'noun.verb'
                                            --   pr.merged | pr.opened | release.tagged
                                            --   commit.pushed | issue.resolved | doc.updated
                                            --   deployment.succeeded
  actor           text,                     -- raw author identity (email/login) từ hệ nguồn
  source_url      text,                     -- canonical link (PR url, commit url, Jira link)
                                            --   → dùng cho "link và nguồn tham chiếu"
  payload         jsonb not null,           -- full raw payload, untouched

  occurred_at     timestamptz not null,     -- thời điểm xảy ra ở hệ nguồn
  ingested_at     timestamptz not null default now(),

  -- IDEMPOTENCY: webhook retry (cùng delivery ID) → conflict → skip.
  -- Backfill dùng commit SHA làm source_event_id → tự nhiên idempotent.
  unique (source, source_event_id)
);

create index events_occurred_at_idx on events (occurred_at desc);
create index events_type_idx        on events (event_type);
create index events_actor_idx       on events (actor);
create index events_payload_gin     on events using gin (payload);

-- Immutability: chặn UPDATE / DELETE trên event log.
create or replace function prevent_event_mutation()
returns trigger language plpgsql as $$
begin
  raise exception 'events is append-only: % is not allowed', tg_op;
end;
$$;
create trigger events_no_update before update on events
  for each row execute function prevent_event_mutation();
create trigger events_no_delete before delete on events
  for each row execute function prevent_event_mutation();


-- =====================================================================
-- 2. PROJECTION CHECKPOINTS  — trái tim của cơ chế replay
-- =====================================================================
-- Mỗi projector (metrics, graph, narrative...) giữ 1 cursor = seq cuối
-- đã xử lý. Khi NÂNG CẤP logic/prompt → bump `logic_version`, reset
-- `last_seq = 0`, TRUNCATE bảng projection tương ứng, worker replay lại
-- TOÀN BỘ từ event gốc. Đây là lý do non-determinism của LLM không còn
-- là rủi ro: luôn re-derive được.
-- ---------------------------------------------------------------------
create table projection_checkpoints (
  projector_name text primary key,          -- 'metrics' | 'graph' | 'narrative' | 'embeddings'
  last_seq       bigint not null default 0, -- seq event cuối cùng đã xử lý
  logic_version  int not null default 1,    -- bump khi đổi prompt/logic → trigger replay
  updated_at     timestamptz not null default now()
);

insert into projection_checkpoints (projector_name) values
  ('metrics'), ('graph'), ('narrative'), ('embeddings')
on conflict do nothing;


-- =====================================================================
-- 3. CANONICAL ENTITY REGISTRY  — giải bài entity resolution
-- =====================================================================
-- Entity = node của knowledge graph + block của kim tự tháp.
-- DEDUPE bằng unique(entity_kind, canonical_key): "auth module" ở 100
-- commit khác nhau đều UPSERT về 1 hàng. canonical_key là ID
-- DETERMINISTIC (file path / module / epic key), KHÔNG phải tên AI bịa.
-- ---------------------------------------------------------------------
create table entities (
  id              uuid primary key default gen_random_uuid(),

  entity_kind     text not null,            -- 'module'|'file'|'service'|'feature'
                                            --   |'epic'|'doc'|'person'
  canonical_key   text not null,            -- ANCHOR dedupe: 'src/auth/' | 'JIRA-1234'
                                            --   | 'service:payment' | 'person:thao@...'

  display_name    text not null,            -- human-readable, có thể sửa tay
  description     text,

  -- Vị trí trên kim tự tháp — STRUCTURE do NGƯỜI định nghĩa (product
  -- architecture), KHÔNG để AI tự suy ra.
  pyramid_layer   int,                      -- 1 = nền móng, lớn dần = lên đỉnh
  pyramid_block   text,                     -- nhóm trong cùng 1 layer

  -- Lifecycle + entity merge (khi phát hiện 2 entity thực ra là 1).
  status          text not null default 'active',  -- 'active'|'deprecated'|'merged'
  merged_into     uuid references entities(id),     -- trỏ về entity gộp; query follow con trỏ này

  -- HUMAN-IN-THE-LOOP: AI propose entity ('proposed'), dev confirm
  -- ('confirmed'). Graph "sạch" chỉ render entity đã confirmed.
  resolution_status text not null default 'proposed', -- 'proposed'|'confirmed'|'rejected'

  first_seen_at   timestamptz not null default now(),
  last_seen_at    timestamptz not null default now(),
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),

  unique (entity_kind, canonical_key)
);

create index entities_kind_idx     on entities (entity_kind);
create index entities_pyramid_idx  on entities (pyramid_layer, pyramid_block);
create index entities_status_idx   on entities (status, resolution_status);

-- Aliases: AI có thể trích "auth" / "authentication" / "AuthModule"
-- cho cùng 1 entity. Resolver tra alias → canonical entity_id.
-- unique(alias) global = ép disambiguation, giữ graph sạch.
-- (Nếu cần 1 alias map nhiều entity theo ngữ cảnh → đổi thành
--  unique(entity_id, alias) và để resolver chấm theo context.)
create table entity_aliases (
  id          uuid primary key default gen_random_uuid(),
  entity_id   uuid not null references entities(id) on delete cascade,
  alias       text not null,
  source      text,                         -- alias này đến từ đâu
  created_at  timestamptz not null default now(),
  unique (alias)
);

-- Lịch sử status — drive ROADMAP theo từng năm + cho phép time-travel.
-- "State ở Q2/2025" = status mới nhất per-entity với changed_at <= mốc.
-- entities.status = bản materialized hiện tại (đọc nhanh);
-- bảng này = full transition log (đọc lịch sử).
create table entity_status_history (
  id              uuid primary key default gen_random_uuid(),
  entity_id       uuid not null references entities(id) on delete cascade,
  status          text not null,            -- 'planned'|'in_progress'|'shipped'|'deprecated'
  changed_at      timestamptz not null,
  source_event_id uuid references events(id),  -- event nào gây ra thay đổi
  note            text
);
create index entity_status_hist_idx on entity_status_history (entity_id, changed_at desc);

-- touch updated_at tự động
create or replace function touch_updated_at()
returns trigger language plpgsql as $$
begin new.updated_at = now(); return new; end;
$$;
create trigger entities_touch before update on entities
  for each row execute function touch_updated_at();


-- =====================================================================
-- 4. PROJECTIONS  — derived, disposable, rebuild từ events
-- =====================================================================
-- LƯU Ý REPLAY: các bảng dưới đây dùng UPSERT tăng dần (+=). An toàn vì:
--   - Live: checkpoint đảm bảo mỗi event xử lý đúng 1 lần.
--   - Rebuild: TRUNCATE trước → replay từ rỗng → không double-count.
-- Footgun #1 của event-sourcing: reprocess KHÔNG truncate → cộng dồn sai.
-- ---------------------------------------------------------------------

-- 4.1 CONTRIBUTIONS — event nào "đụng" entity nào (cầu nối event↔entity).
--     Facts deterministic (lines/files từ git) tách khỏi phần AI (relation
--     suy luận). Cột derived_by phân biệt. KHÔNG có score.
create table contributions (
  id              uuid primary key default gen_random_uuid(),
  event_id        uuid not null references events(id) on delete cascade,
  entity_id       uuid not null references entities(id) on delete cascade,

  -- Deterministic facts (reproducible từ git/jira):
  lines_added     int,
  lines_removed   int,
  files_changed   int,

  relation        text,                     -- 'created'|'modified'|'fixed_bug_in'
                                            --   |'documented'|'deprecated'
  occurred_at     timestamptz not null,     -- denormalized từ event (cho time-query)

  derived_by      text not null,            -- 'git' (deterministic) | 'ai' (extracted)
  derived_by_version int not null default 1,
  created_at      timestamptz not null default now()
);
create index contrib_entity_idx   on contributions (entity_id, occurred_at desc);
create index contrib_event_idx    on contributions (event_id);
create index contrib_relation_idx on contributions (relation);

-- 4.2 ENTITY EDGES — cạnh của knowledge graph. Time-aware (cạnh có thể
--     xuất hiện/biến mất). Tín hiệu MẠNH & MIỄN PHÍ: 'co_changed_with'
--     = file đổi cùng nhau → tính từ commit co-occurrence, KHÔNG cần AI.
create table entity_edges (
  id                uuid primary key default gen_random_uuid(),
  from_entity       uuid not null references entities(id) on delete cascade,
  to_entity         uuid not null references entities(id) on delete cascade,
  edge_type         text not null,          -- 'depends_on'|'documented_by'
                                            --   |'co_changed_with'|'implements'
  weight            numeric not null default 1,  -- vd: số lần co-change (tăng dần)

  first_observed_at timestamptz not null,
  last_observed_at  timestamptz not null,

  derived_by        text not null,          -- 'git' | 'ai'
  derived_by_version int not null default 1,

  unique (from_entity, to_entity, edge_type),
  check (from_entity <> to_entity)
);
create index edges_from_idx on entity_edges (from_entity);
create index edges_to_idx   on entity_edges (to_entity);
create index edges_time_idx on entity_edges (last_observed_at desc);

-- 4.3 METRICS DAILY — aggregate DETERMINISTIC, time-aware, drive heatmap.
--     "Heat" của block = commit activity trong cửa sổ N ngày (tính ở view
--     bên dưới). Hoàn toàn reproducible, KHÔNG AI.
create table metrics_daily (
  entity_id           uuid not null references entities(id) on delete cascade,
  day                 date not null,

  commit_count        int not null default 0,
  pr_count            int not null default 0,
  lines_added         int not null default 0,
  lines_removed       int not null default 0,
  bugs_fixed          int not null default 0,
  unique_contributors int not null default 0,  -- distinct person-entity

  primary key (entity_id, day)
);
create index metrics_day_idx on metrics_daily (day);


-- =====================================================================
-- 5. NARRATIVES  — "nhật ký" do AI viết (probabilistic layer)
-- =====================================================================
-- AI CHỈ summarize/extract ở đây. Luôn kèm source_event_ids để người
-- verify → trust. Model rẻ (Haiku) cho summarize hàng loạt; model mạnh
-- (Sonnet/Opus) cho bản tổng hợp định kỳ (weekly/release).
-- ---------------------------------------------------------------------
create table narratives (
  id               uuid primary key default gen_random_uuid(),
  scope            text not null,           -- 'pr'|'weekly'|'release'|'entity'
  scope_ref        text,                    -- PR number | week id | release tag | entity_id

  title            text,
  body_md          text not null,           -- nội dung markdown của diary entry
  highlights       jsonb,                   -- highlights có cấu trúc (optional)

  source_event_ids uuid[] not null default '{}',  -- events được tóm tắt → verify
  model            text not null,           -- 'claude-haiku-4-5' | 'claude-sonnet-4-6'...
  derived_by_version int not null default 1,

  period_start     timestamptz,
  period_end       timestamptz,
  created_at       timestamptz not null default now()
);
create index narratives_scope_idx  on narratives (scope, scope_ref);
create index narratives_period_idx on narratives (period_end desc);


-- =====================================================================
-- 6. EMBEDDINGS (pgvector)  — con ngựa thồ retrieval / "second brain" thật
-- =====================================================================
-- Embed mọi PR summary + doc + narrative → semantic search:
-- "quyết định/doc cho feature X nằm đâu?".
--
-- CHỌN DIMENSION theo model embedding. Mặc định 1536 (OpenAI 3-small /
-- Voyage-3). Nội dung VN+EN trộn → ưu tiên model multilingual mạnh
-- (Voyage / Cohere multilingual / OpenAI 3-large).
--
-- GOTCHA: HNSW index `vector` chỉ tới 2000 dims. Nếu xài 3072 (3-large),
-- giảm dimension về <=2000, HOẶC đổi cột sang `halfvec(3072)` (pgvector
-- 0.7+, index tới 4000 dims) + dùng halfvec_cosine_ops.
-- ---------------------------------------------------------------------
create table embeddings (
  id              uuid primary key default gen_random_uuid(),

  source_kind     text not null,            -- 'pr_summary'|'doc'|'commit_msg'|'narrative'
  source_id       uuid,                     -- trỏ về hàng projection (vd narratives.id)
  entity_id       uuid references entities(id) on delete set null,

  content         text not null,            -- text đã embed (giữ để display/rerank)
  embedding       vector(1536),             -- ĐỔI dimension theo model bạn chọn

  occurred_at     timestamptz,              -- cho time-filtered search
  derived_by_version int not null default 1,
  created_at      timestamptz not null default now()
);

-- HNSW: ANN nhanh, recall cao. cosine ops khớp embedding đã normalize.
create index embeddings_hnsw_idx
  on embeddings using hnsw (embedding vector_cosine_ops);
create index embeddings_kind_idx on embeddings (source_kind);

-- Hàm search semantic (gọi từ Next.js qua RPC). Trả similarity + lọc
-- theo kind và mốc thời gian.
create or replace function match_embeddings (
  query_embedding vector(1536),
  match_count     int default 10,
  filter_kind     text default null,
  after_date      timestamptz default null
)
returns table (
  id          uuid,
  source_kind text,
  source_id   uuid,
  entity_id   uuid,
  content     text,
  similarity  float,
  occurred_at timestamptz
)
language sql stable as $$
  select
    e.id, e.source_kind, e.source_id, e.entity_id, e.content,
    1 - (e.embedding <=> query_embedding) as similarity,
    e.occurred_at
  from embeddings e
  where (filter_kind is null or e.source_kind = filter_kind)
    and (after_date  is null or e.occurred_at >= after_date)
  order by e.embedding <=> query_embedding
  limit match_count;
$$;


-- =====================================================================
-- 7. CONVENIENCE VIEWS  — frontend đọc thẳng
-- =====================================================================

-- 7.1 ENTITY HEAT — heat của kim tự tháp, tính DETERMINISTIC từ
--     metrics_daily (cửa sổ 30 ngày). Frontend map sang hot/warm/cool/cold.
create or replace view entity_heat as
select
  e.id            as entity_id,
  e.display_name,
  e.pyramid_layer,
  e.pyramid_block,
  e.status,
  coalesce(sum(m.commit_count) filter (where m.day >= current_date - 30), 0) as commits_30d,
  coalesce(sum(m.lines_added + m.lines_removed)
           filter (where m.day >= current_date - 30), 0)                     as churn_30d,
  coalesce(sum(m.bugs_fixed)   filter (where m.day >= current_date - 30), 0)  as bugs_fixed_30d,
  max(m.day)      as last_active_day
from entities e
left join metrics_daily m on m.entity_id = e.id
where e.status <> 'deprecated'
  and e.merged_into is null
group by e.id;

-- 7.2 ROADMAP — transition log → roadmap theo năm. Mỗi dòng = 1 mốc
--     thay đổi trạng thái của 1 block, kèm link event gây ra nó.
create or replace view roadmap as
select
  h.entity_id,
  e.display_name,
  e.pyramid_layer,
  h.status,
  h.changed_at,
  date_part('year', h.changed_at)::int as year,
  ev.source_url   as evidence_url
from entity_status_history h
join entities e on e.id = h.entity_id
left join events ev on ev.id = h.source_event_id
order by h.changed_at;

-- 7.3 GRAPH ACTIVE — edges còn "sống" trong cửa sổ 90 ngày, chỉ giữa các
--     entity đã confirmed → graph sạch để render.
create or replace view graph_active as
select
  ed.from_entity, ef.display_name as from_name,
  ed.to_entity,   et.display_name as to_name,
  ed.edge_type, ed.weight, ed.last_observed_at
from entity_edges ed
join entities ef on ef.id = ed.from_entity and ef.resolution_status = 'confirmed'
join entities et on et.id = ed.to_entity   and et.resolution_status = 'confirmed'
where ed.last_observed_at >= now() - interval '90 days';


-- =====================================================================
-- 8. RLS (Row Level Security)  — scaffold tối thiểu cho internal tool
-- =====================================================================
-- Worker dùng service_role → BYPASS RLS (ghi thoải mái).
-- Dashboard dùng authenticated → CHỈ READ. Không cho client ghi trực tiếp.
-- ---------------------------------------------------------------------
alter table events                enable row level security;
alter table entities              enable row level security;
alter table entity_aliases        enable row level security;
alter table entity_status_history enable row level security;
alter table contributions         enable row level security;
alter table entity_edges          enable row level security;
alter table metrics_daily         enable row level security;
alter table narratives            enable row level security;
alter table embeddings            enable row level security;

do $$
declare t text;
begin
  foreach t in array array[
    'events','entities','entity_aliases','entity_status_history',
    'contributions','entity_edges','metrics_daily','narratives','embeddings'
  ] loop
    execute format(
      'create policy %I on %I for select to authenticated using (true);',
      'read_' || t, t
    );
  end loop;
end $$;


-- =====================================================================
-- 9. REPLAY RECIPE  (chạy tay khi nâng prompt/logic của 1 projector)
-- =====================================================================
-- Ví dụ rebuild lại toàn bộ projection 'graph' sau khi cải tiến extractor:
--
--   begin;
--     truncate entity_edges;
--     -- (KHÔNG truncate entities/events — chỉ projection của projector này)
--     update projection_checkpoints
--       set last_seq = 0, logic_version = logic_version + 1, updated_at = now()
--       where projector_name = 'graph';
--   commit;
--
--   -- Sau đó worker tự đọc từ seq 0, replay toàn bộ event → dựng lại graph.
--   -- Tương tự cho 'metrics' (truncate metrics_daily), 'embeddings'
--   -- (truncate embeddings + re-embed), 'narrative' (truncate narratives).
--
-- Vì events bất biến và còn nguyên, bạn rebuild được BẤT KỲ projection nào,
-- BAO NHIÊU lần tùy thích, mà không mất dữ liệu gốc. Đó là toàn bộ sức
-- mạnh của kiến trúc này.
-- =====================================================================
