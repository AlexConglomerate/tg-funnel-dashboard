#!/usr/bin/env bash
# Локальный запуск: [SSH SOCKS-туннель к Bot API] + HTTPS-туннель cloudflared + бот.
# Ctrl+C гасит всё.
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -f .env ]]; then
  echo "Нет .env: cp .env.example .env и заполните BOT_TOKEN и ADMIN_IDS" >&2
  exit 1
fi
set -a
# shellcheck disable=SC1091
source .env
set +a

HTTP_PORT="${HTTP_PORT:-8080}"
SOCKS_PORT="${SOCKS_PORT:-1080}"
mkdir -p data

PIDS=()
cleanup() {
  trap - EXIT INT TERM
  for pid in "${PIDS[@]:-}"; do
    [[ -n "$pid" ]] && kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 130' INT TERM

port_open() { nc -z 127.0.0.1 "$1" >/dev/null 2>&1; }

# 1. SOCKS-туннель через сервер с доступом к Telegram.
#    Адрес сервера в вывод не печатаем: вывод ssh уходит в data/ssh.log.
if [[ -n "${PROXY_SSH:-}" ]]; then
  if port_open "$SOCKS_PORT"; then
    echo "Порт $SOCKS_PORT уже занят: освободите его или задайте SOCKS_PORT" >&2
    exit 1
  fi
  ssh -N -D "127.0.0.1:$SOCKS_PORT" \
    -o BatchMode=yes -o ExitOnForwardFailure=yes \
    -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
    "$PROXY_SSH" >data/ssh.log 2>&1 &
  PIDS+=($!)
  ssh_pid=$!
  for _ in $(seq 1 30); do
    port_open "$SOCKS_PORT" && break
    if ! kill -0 "$ssh_pid" 2>/dev/null; then
      echo "SSH-туннель не поднялся, подробности в data/ssh.log" >&2
      exit 1
    fi
    sleep 0.5
  done
  if ! port_open "$SOCKS_PORT"; then
    echo "SSH-туннель не открыл порт $SOCKS_PORT за 15 с, см. data/ssh.log" >&2
    exit 1
  fi
  export PROXY_URL="socks5://127.0.0.1:$SOCKS_PORT"
  echo "→ SOCKS-туннель к Bot API: $PROXY_URL"
fi

# 2. HTTPS-туннель к Mini App.
if ! command -v cloudflared >/dev/null 2>&1; then
  echo "Нет cloudflared: brew install cloudflared" >&2
  exit 1
fi
cloudflared tunnel --no-autoupdate --url "http://localhost:$HTTP_PORT" >data/cloudflared.log 2>&1 &
PIDS+=($!)
cf_pid=$!

WEBAPP_URL=""
for _ in $(seq 1 60); do
  WEBAPP_URL="$(grep -Eo 'https://[a-z0-9-]+\.trycloudflare\.com' data/cloudflared.log | head -n1 || true)"
  [[ -n "$WEBAPP_URL" ]] && break
  if ! kill -0 "$cf_pid" 2>/dev/null; then
    echo "cloudflared завершился, подробности в data/cloudflared.log" >&2
    exit 1
  fi
  sleep 0.5
done
if [[ -z "$WEBAPP_URL" ]]; then
  echo "cloudflared не выдал адрес за 30 с, см. data/cloudflared.log" >&2
  exit 1
fi
export WEBAPP_URL
echo "→ Mini App: $WEBAPP_URL"

# 3. Бот + API + планировщик. Ctrl+C получает и python (корректная остановка), и этот скрипт.
uv run python -m funnel
