-- =====================================================================
-- 000_local_roles.sql  — CHẠY TRƯỚC 001 (thứ tự alphabet trong init)
-- =====================================================================
-- Bản local dùng Postgres thuần, KHÔNG có Supabase Auth. Section 8 (RLS)
-- trong 001_schema.sql tham chiếu role 'authenticated' → sẽ lỗi khi init.
--
-- File này tạo role đó như NO-OP để 001 chạy sạch. RLS thực tế KHÔNG có
-- tác dụng vì worker/frontend đều connect bằng superuser 'postgres'
-- (superuser bypass RLS). Auth thật cho dashboard là Cloudflare Access.
--
-- Guard 'if not exists' → an toàn cả khi chạy trên Supabase managed
-- (role đã tồn tại sẵn ở đó).
-- =====================================================================
do $$
begin
  if not exists (select from pg_roles where rolname = 'authenticated') then
    create role authenticated nologin;
  end if;
end
$$;
