#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

for v in TELEGRAM_BOT_TOKEN TELEGRAM_API_ID TELEGRAM_API_HASH; do
  if [ -z "${!v}" ]; then echo "❌ Falta la variable $v"; exit 1; fi
done

sudo mkdir -p /var/lib/telegram-bot-api

echo "🐳 Levantando servidor Bot API local..."
docker compose up -d
sleep 3

# Solo la primera vez: sacar el bot del servidor oficial
if [ ! -f .logged_out ]; then
  echo "🔌 Desvinculando bot del servidor oficial..."
  curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/logOut" || true
  echo
  touch .logged_out
  sleep 5
fi

export TELEGRAM_API_URL=http://localhost:8081
echo "🤖 Iniciando bot..."
exec sudo -E env "PATH=$PATH" python bot.py
