#!/usr/bin/env bash
# Cập nhật server lên mã mới nhất của nhánh release: sudo bash /opt/meeting-copilot/deploy/oracle/update.sh
# Server dừng khoảng 1 phút khi khởi động lại: đừng chạy lúc đang có cuộc họp.
set -euo pipefail
DIR="${DIR:-/opt/meeting-copilot}"
BRANCH="${BRANCH:-release}"
[ "$(id -u)" = 0 ] || { echo "Chạy bằng sudo: sudo bash $0" >&2; exit 1; }
git -C "$DIR" pull -q --ff-only origin "$BRANCH"
echo "Mã nguồn: $(git -C "$DIR" log --oneline -1)"
cd "$DIR/deploy/oracle"
docker compose up -d --build
docker image prune -f >/dev/null || true
echo "Đã cập nhật. Xem log: cd $DIR/deploy/oracle && sudo docker compose logs -f app"
