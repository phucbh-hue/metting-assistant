#!/bin/sh
# Khởi động server trong Docker. Ổ lưu lâu dài gắn vào /app/data (Render Disk, docker volume) có thể thuộc root:
# cấp quyền cho người dùng "app" rồi mới chạy server bằng người dùng đó (không chạy server bằng root).
set -e
if [ "$(id -u)" = "0" ]; then
  mkdir -p /app/data
  if [ "$(stat -c %u /app/data)" != "$(id -u app)" ]; then
    chown -R app:app /app/data
  fi
  # Hạ quyền bằng Python có sẵn trong image (không cần cài gosu / setpriv)
  exec python -c 'import os, pwd, sys
u = pwd.getpwnam("app")
os.setgroups([])
os.setgid(u.pw_gid)
os.setuid(u.pw_uid)
os.environ["HOME"] = u.pw_dir
os.execvp(sys.argv[1], sys.argv[1:])' "$@"
fi
exec "$@"
