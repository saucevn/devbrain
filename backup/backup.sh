#!/bin/sh
# Dump toàn DB → gzip → upload R2. --no-owner/--no-privileges để restore
# portable trên bất kỳ role nào (tránh vướng ownership/RLS lúc khôi phục).
set -eu

TS=$(date -u +%Y%m%dT%H%M%SZ)
FILE="secondbrain_${TS}.sql.gz"
TMP="/tmp/${FILE}"

echo "[backup ${TS}] pg_dump..."
pg_dump "${DATABASE_URL}" --no-owner --no-privileges | gzip -9 > "${TMP}"
SIZE=$(du -h "${TMP}" | cut -f1)

echo "[backup ${TS}] upload → s3://${R2_BUCKET}/backups/${FILE} (${SIZE})"
aws s3 cp "${TMP}" "s3://${R2_BUCKET}/backups/${FILE}" --endpoint-url "${R2_ENDPOINT}"

rm -f "${TMP}"
echo "[backup ${TS}] done"

# Retention: nên đặt R2 Lifecycle Rule (xoá object > N ngày) trong
# Cloudflare dashboard — sạch hơn là tự prune bằng script.
