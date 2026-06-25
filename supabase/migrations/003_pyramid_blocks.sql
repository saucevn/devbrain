-- =====================================================================
-- 003_pyramid_blocks.sql — Phase 3 pyramid (CURATED, human-owned)
-- =====================================================================
-- pyramid_blocks = năng lực do NGƯỜI định nghĩa (tên, layer, thứ tự, maturity,
-- risk). KHÔNG phải projection: bền qua mọi lần replay; được R2 backup bảo vệ
-- (golden rule #10). Heat overlay tính deterministic từ metrics_daily của các
-- entity thành viên (entities.pyramid_block = pyramid_blocks.key).
-- Áp TAY lên DB đang chạy (init-scripts chỉ chạy lúc volume rỗng).
-- =====================================================================
create table if not exists pyramid_blocks (
  id          uuid primary key default gen_random_uuid(),
  key         text not null unique,        -- slug; entities.pyramid_block trỏ vào đây
  name        text not null,
  layer       int  not null,               -- 1 = nền móng, tăng dần lên đỉnh
  position    int  not null default 0,     -- thứ tự trong cùng layer
  maturity    int,                         -- 0..100 (%), human-curated
  risk_class  text,                        -- 'low' | 'in_progress' | 'critical'
  description text,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  check (layer >= 1),
  check (maturity is null or (maturity between 0 and 100)),
  check (risk_class is null or risk_class in ('low','in_progress','critical'))
);
create index if not exists pyramid_blocks_layer_idx on pyramid_blocks (layer, position);

do $$ begin
  if not exists (select 1 from pg_trigger where tgname = 'pyramid_blocks_touch') then
    create trigger pyramid_blocks_touch before update on pyramid_blocks
      for each row execute function touch_updated_at();
  end if;
end $$;

insert into pyramid_blocks (key, name, layer, position, maturity, risk_class, description) values
 ('ingestion','Ingestion & Projectors',1,0,70,'critical','Webhook/backfill to events to metrics/co-change'),
 ('schema','Schema & DB',1,1,85,'low','Event-sourced Postgres + pgvector'),
 ('ai_lane','AI Narrative & Search',2,0,55,'in_progress','Haiku summary + Gemini embeddings + search'),
 ('dashboard','Dashboard',2,1,60,'in_progress','Next.js heat / co-change / search / entities'),
 ('docs','Docs & Plans',3,0,90,'low','PROJECT_PLAN, CLAUDE.md, ADRs')
on conflict (key) do nothing;
