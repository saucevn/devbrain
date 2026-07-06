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
