-- =====================================================================
-- Migration 002: idempotency + cost cho AI projectors
-- Áp LÊN TRÊN migration 001 (dev_second_brain_schema.sql).
-- Lý do: các bước gọi LLM/embed là tốn tiền và có side-effect → phải
-- (a) SKIP gọi lại khi input không đổi, (b) UPSERT thay vì INSERT để
-- replay/crash không tạo bản trùng.
-- =====================================================================

-- ---- narratives ------------------------------------------------------
-- input_hash: hash của (prompt_version, title, body, diff) → nếu đã tồn
-- tại thì SKIP cả LLM call (cache hit, tiết kiệm tiền lúc replay).
alter table narratives add column if not exists input_hash text;

-- Natural key: đúng 1 narrative cho mỗi (scope, scope_ref) tại 1 version.
-- → re-summarize cùng PR = UPSERT, không nhân bản.
create unique index if not exists narratives_natural_key
  on narratives (scope, scope_ref, derived_by_version);

create index if not exists narratives_input_hash_idx
  on narratives (input_hash);

-- ---- embeddings ------------------------------------------------------
-- content_hash: skip re-embed (tốn tiền) khi text không đổi.
alter table embeddings add column if not exists content_hash text;

-- Natural key cho UPSERT. Lưu ý: source_id NULL được Postgres coi là
-- distinct → các hàng source_id null sẽ không conflict (chấp nhận được,
-- chỉ áp dụng cho edge case không gắn source).
create unique index if not exists embeddings_natural_key
  on embeddings (source_kind, source_id, derived_by_version);

create index if not exists embeddings_content_hash_idx
  on embeddings (content_hash);

-- ---- contributions ---------------------------------------------------
-- Chống nhân bản khi reprocess 1 event ở AI lane (crash giữa LLM và
-- commit). 1 hàng cho mỗi (event, entity, relation).
create unique index if not exists contributions_natural_key
  on contributions (event_id, entity_id, relation);
