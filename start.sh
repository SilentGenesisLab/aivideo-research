#!/usr/bin/env bash
# 后台启动网页服务（对外监听，需已在 .env 配置 APP_PASSWORD）
#   ./start.sh [端口]        默认 8770
#   ./stop.sh               停止
cd "$(dirname "$0")"
PORT="${1:-8770}"
# 监听地址：优先环境变量，其次 .env 里的 HOST；经 nginx/HTTPS 访问时为 127.0.0.1（关闭 IP:端口 直连）
HOST="${HOST:-$(grep -E '^HOST=' .env 2>/dev/null | cut -d= -f2)}"
HOST="${HOST:-0.0.0.0}"
[ -f server.pid ] && kill "$(cat server.pid)" 2>/dev/null
nohup .venv/bin/python server.py --host "$HOST" --port "$PORT" >> server.log 2>&1 &
echo $! > server.pid
sleep 2
if kill -0 "$(cat server.pid)" 2>/dev/null; then
  URL="$(grep -E '^PUBLIC_BASE_URL=' .env 2>/dev/null | cut -d= -f2-)"
  echo "已启动：${URL:-http://$(curl -s -4 -m 5 ifconfig.me || hostname -I | cut -d' ' -f1):$PORT}（监听 $HOST:$PORT）"
else
  echo "启动失败，查看 server.log："; tail -5 server.log
fi
