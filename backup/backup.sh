#!/bin/sh
# Dump toàn DB → gzip → verify → upload R2. --no-owner/--no-privileges để
# restore portable trên bất kỳ role nào (tránh vướng ownership/RLS lúc khôi phục).
#
# P0-2: /bin/sh KHÔNG có pipefail → `pg_dump | gzip` nuốt lỗi pg_dump và
# upload một gzip rỗng 20-byte "thành công". Tách pipe + verify trước upload.
set -eu

TS=$(date -u +%Y%m%dT%H%M%SZ)
FILE="secondbrain_${TS}.sql.gz"
TMP="/tmp/${FILE}"
TMP_SQL="/tmp/secondbrain_${TS}.sql"
trap 'rm -f "${TMP_SQL}" "${TMP}"' EXIT

echo "[backup ${TS}] pg_dump..."
pg_dump "${DATABASE_URL}" --no-owner --no-privileges > "${TMP_SQL}"
gzip -9 < "${TMP_SQL}" > "${TMP}"
rm -f "${TMP_SQL}"

# Verify: gzip toàn vẹn + kích thước tối thiểu (schema thật không bao giờ <1KB).
gzip -t "${TMP}"
SIZE_BYTES=$(wc -c < "${TMP}")
if [ "${SIZE_BYTES}" -lt 1024 ]; then
  echo "[backup ${TS}] ABORT: dump ${SIZE_BYTES}B < 1KB — nghi rỗng/corrupt, KHÔNG upload"
  exit 1
fi

SIZE=$(du -h "${TMP}" | cut -f1)
echo "[backup ${TS}] upload → s3://${R2_BUCKET}/backups/${FILE} (${SIZE})"
aws s3 cp "${TMP}" "s3://${R2_BUCKET}/backups/${FILE}" --endpoint-url "${R2_ENDPOINT}"

echo "[backup ${TS}] done"

# Retention: nên đặt R2 Lifecycle Rule (xoá object > N ngày) trong
# Cloudflare dashboard — sạch hơn là tự prune bằng script.
