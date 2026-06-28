-- Replay the `narrative` projector after fixing highlights jsonb encoding.
-- §9 recipe (supabase/migrations/001_schema.sql): rebuild from events, never
-- UPDATE projection rows in place (golden rule #2). Run with the worker already
-- on the fixed code so the rebuild writes correct jsonb arrays.
begin;
  -- dependent AI-lane embeddings first (source_id points at narrative ids that
  -- truncate would orphan); only the pr_summary kind is narrative-derived.
  delete from embeddings where source_kind = 'pr_summary';
  truncate narratives;
  -- (do NOT truncate entities/entity_edges/contributions — apply_narrative
  --  re-derives them via idempotent UPSERT / on-conflict-do-nothing.)
  update projection_checkpoints
    set last_seq = 0, logic_version = logic_version + 1, updated_at = now()
    where projector_name = 'narrative';
commit;
