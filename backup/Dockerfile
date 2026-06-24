# Backup sidecar: pg_dump (khớp PG16) + aws-cli (nói chuyện R2 qua S3 API).
FROM postgres:16-alpine

# aws-cli để upload lên R2 (S3-compatible). Nếu repo alpine không có sẵn
# 'aws-cli', đổi sang: RUN apk add --no-cache python3 py3-pip && \
#   pip install --break-system-packages awscli
RUN apk add --no-cache aws-cli

COPY backup.sh /usr/local/bin/backup.sh
RUN chmod +x /usr/local/bin/backup.sh

# Loop trong entrypoint (KHÔNG dùng cron) → kế thừa full env của container,
# tránh footgun "cron strip env". Backup chạy NGAY khi start (verify được
# liền), rồi lặp mỗi BACKUP_INTERVAL giây.
ENTRYPOINT ["/bin/sh", "-c", "while true; do /usr/local/bin/backup.sh || echo '[backup] FAILED'; sleep \"${BACKUP_INTERVAL:-86400}\"; done"]
