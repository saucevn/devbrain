-- =====================================================================
-- 004_entities_project.sql — multi-project namespace
-- =====================================================================
-- Thêm chiều project/workspace (repo full_name) cho entity. canonical_key của
-- file/doc/module được namespace '{project}:{key}' TRONG projector → 2 repo
-- cùng path KHÔNG còn bị gộp. KHÔNG đổi unique(entity_kind, canonical_key)
-- (namespace nằm ngay trong canonical_key). `project` = denormalized để filter.
-- Theo golden rule #2: KHÔNG fix-up data in-place; entity namespaced được dựng
-- lại qua replay / fresh-init (backfill payload nay kèm repository.full_name).
-- =====================================================================
alter table entities add column if not exists project text;
create index if not exists entities_project_idx on entities (project);
