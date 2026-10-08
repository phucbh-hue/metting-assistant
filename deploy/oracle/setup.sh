#!/usr/bin/env bash
# Cài Meeting Copilot lên máy ảo Ubuntu (Oracle Cloud Always Free, ARM hoặc x86). Chạy trên máy ảo:
#   curl -fsSLO https://raw.githubusercontent.com/phucbh-hue/metting-assistant/release/deploy/oracle/setup.sh
#   sudo bash setup.sh
# Chạy lại được: cấu hình đã nhập (prod.env, caddy.env) được giữ nguyên, chỉ cập nhật mã nguồn và chạy lại server.
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/phucbh-hue/metting-assistant.git}"
BRANCH="${BRANCH:-release}"
DIR="${DIR:-/opt/meeting-copilot}"
CONF="$DIR/deploy/oracle"

say() { printf '\n\033[1;35m[meeting-copilot]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[meeting-copilot] %s\033[0m\n' "$*" >&2; exit 1; }
ask() { local v; read -r -p "$1: " v </dev/tty; printf '%s' "$v"; }
ask_secret() { local v; read -r -s -p "$1 (không hiện khi gõ/dán): " v </dev/tty; printf '\n' >&2; printf '%s' "$v"; }

[ "$(id -u)" = 0 ] || die "Chạy bằng sudo: sudo bash setup.sh"
. /etc/os-release
[ "${ID:-}" = ubuntu ] || die "Script dành cho Ubuntu (khi tạo máy ảo chọn image Canonical Ubuntu)"

say "1/6 Cập nhật hệ thống, bật tự cài bản vá bảo mật"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get upgrade -y
echo "iptables-persistent iptables-persistent/autosave_v4 boolean true" | debconf-set-selections
echo "iptables-persistent iptables-persistent/autosave_v6 boolean true" | debconf-set-selections
apt-get install -y ca-certificates curl git openssl unattended-upgrades iptables-persistent
dpkg-reconfigure -f noninteractive unattended-upgrades || true

say "2/6 Mở cổng 80, 443 trong tường lửa của máy (image Ubuntu của Oracle chặn mọi cổng trừ SSH)"
for port in 80 443; do
  if ! iptables -C INPUT -p tcp -m state --state NEW --dport "$port" -j ACCEPT 2>/dev/null; then
    n=$(iptables -L INPUT --line-numbers -n | awk '$2 == "REJECT" { print $1; exit }')
    if [ -n "$n" ]; then
      iptables -I INPUT "$n" -p tcp -m state --state NEW --dport "$port" -j ACCEPT
    else
      iptables -A INPUT -p tcp -m state --state NEW --dport "$port" -j ACCEPT
    fi
  fi
done
netfilter-persistent save

say "3/6 Cài Docker"
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker

say "4/6 Lấy mã nguồn (nhánh $BRANCH)"
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" fetch -q origin "$BRANCH"
  git -C "$DIR" checkout -q "$BRANCH"
  git -C "$DIR" pull -q --ff-only origin "$BRANCH"
else
  git clone -q --branch "$BRANCH" "$REPO_URL" "$DIR"
fi

say "5/6 Cấu hình"
if [ ! -f "$CONF/prod.env" ] || [ ! -f "$CONF/caddy.env" ]; then
  echo "Nhập thông tin (các khóa lấy từ tệp .env trên máy anh). Khóa bí mật không hiện khi dán, dán xong bấm Enter."
  DOMAIN=$(ask "Tên miền đã trỏ về máy này (ví dụ urbox-meeting.duckdns.org)")
  [ -n "$DOMAIN" ] || die "Cần tên miền để có HTTPS (trình duyệt chỉ cho dùng micro trên https)"
  ACME_EMAIL=$(ask "Email nhận thông báo chứng chỉ HTTPS")
  GCID=$(ask "GOOGLE_OAUTH_CLIENT_ID")
  ADMINS=$(ask "Email quản trị, cách nhau dấu phẩy (AUTH_ADMIN_EMAILS)")
  SONIOX=$(ask_secret "SONIOX_API_KEY")
  ANTHROPIC=$(ask_secret "ANTHROPIC_API_KEY")
  MONGO=$(ask_secret "MONGODB_URL")
  umask 077
  cat > "$CONF/prod.env" <<EOF
# Cấu hình server (tạo bởi setup.sh $(date +%d/%m/%Y)). Không commit, không gửi tệp này cho ai.
AUTH_SECRET='$(openssl rand -hex 32)'
GOOGLE_OAUTH_CLIENT_ID='$GCID'
ALLOWED_DOMAIN='urbox.vn'
AUTH_ADMIN_EMAILS='$ADMINS'
CORS_ORIGINS='https://$DOMAIN'
SONIOX_API_KEY='$SONIOX'
ANTHROPIC_API_KEY='$ANTHROPIC'
MONGODB_URL='$MONGO'
MONGODB_DB_NAME='meeting_assistant'
LLM_PROVIDER='claude'
CLAUDE_MODEL='claude-sonnet-5-5'
RECORDING_RETENTION_DAYS='30'
EOF
  cat > "$CONF/caddy.env" <<EOF
DOMAIN=$DOMAIN
ACME_EMAIL=$ACME_EMAIL
EOF
  umask 022
else
  DOMAIN=$(sed -n 's/^DOMAIN=//p' "$CONF/caddy.env")
  echo "Giữ cấu hình đã có (sửa: sudo nano $CONF/prod.env rồi chạy lại script)."
fi

say "6/6 Dựng và chạy server (lần đầu khoảng 10-20 phút: cài thư viện, tải model giọng nói)"
cd "$CONF"
docker compose up -d --build
printf 'Chờ server khởi động'
ok=0
for _ in $(seq 1 90); do
  if docker compose exec -T app curl -fsS http://127.0.0.1:8080/healthz >/dev/null 2>&1; then ok=1; break; fi
  printf '.'; sleep 5
done
echo
[ "$ok" = 1 ] || die "Server chưa chạy được. Xem lỗi: cd $CONF && sudo docker compose logs --tail 100 app"
docker image prune -f >/dev/null || true

IP=$(curl -fsS --max-time 10 https://api.ipify.org || echo "<IP công khai của máy>")
say "Xong. Server chạy ở https://$DOMAIN"
cat <<EOF
Còn 3 việc:
  1. MongoDB Atlas > Network Access > Add IP Address: $IP/32
     Sau đó chạy: cd $CONF && sudo docker compose restart app
  2. Google Cloud Console > Credentials > OAuth client > Authorized JavaScript origins: https://$DOMAIN
  3. Mở https://$DOMAIN và đăng nhập bằng tài khoản @urbox.vn
Xem log:   cd $CONF && sudo docker compose logs -f app
Cập nhật:  sudo bash $CONF/update.sh
EOF
