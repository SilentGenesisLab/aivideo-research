#!/usr/bin/env bash
# 一键配置域名与 HTTPS：sudo bash deploy/setup-https.sh [域名] [证书通知邮箱(可选)]
# 做的事：安装 nginx 站点配置 → 检查并重载 nginx → certbot 申请 Let's Encrypt 证书（自动续期）并开启 http→https 跳转
set -euo pipefail
DOMAIN="${1:-videodata.sligenai.cn}"
EMAIL="${2:-}"
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/nginx/videodata.sligenai.cn.conf"
DST="/etc/nginx/sites-available/$DOMAIN"

[ "$(id -u)" = 0 ] || { echo "请用 sudo 运行：sudo bash $0"; exit 1; }

echo "① 检查域名解析"
IP_DNS="$(getent hosts "$DOMAIN" | awk '{print $1}' | head -1)"
IP_ME="$(curl -s -4 -m 5 ifconfig.me || true)"
echo "   $DOMAIN → ${IP_DNS:-未解析}，本机公网 IP ${IP_ME:-未知}"
[ -n "$IP_DNS" ] || { echo "   域名还没有解析，请先添加 A 记录指向本机"; exit 1; }

echo "② 安装 nginx 站点配置 → $DST"
sed "s/videodata.sligenai.cn/$DOMAIN/g" "$SRC" > "$DST"
ln -sf "$DST" "/etc/nginx/sites-enabled/$DOMAIN"
nginx -t
systemctl reload nginx

echo "③ 申请证书（Let's Encrypt，certbot 会自动续期）"
if [ -n "$EMAIL" ]; then MAIL=(-m "$EMAIL"); else MAIL=(--register-unsafely-without-email); fi
certbot --nginx -d "$DOMAIN" --redirect --non-interactive --agree-tos "${MAIL[@]}" --keep-until-expiring

echo "④ 验证"
nginx -t && systemctl reload nginx
code="$(curl -s -o /dev/null -w '%{http_code}' "https://$DOMAIN/api/health" || true)"
echo "   https://$DOMAIN/api/health → $code"
[ "$code" = 200 ] && echo "完成：https://$DOMAIN" || echo "HTTPS 已配置，但服务未响应，请确认后端在 127.0.0.1:8770 运行（./start.sh）"
