#!/usr/bin/env bash
# Переводит бота Лавк на собственный Bot API сервер Telegram (telegram-bot-api в Docker).
# Зачем: общий api.telegram.org иногда «залипает» на getFile, и бот не может скачать фото пользователя.
# Запуск:
#   curl -fsSL https://raw.githubusercontent.com/kirillam1/Gwentbot/hoplite/thasos-aa051e98/deploy/local-bot-api.sh | sudo bash
# Скрипт сам спросит api_id и api_hash (их выдают на my.telegram.org → API development tools).
set -euo pipefail

INSTALL_URL="https://raw.githubusercontent.com/kirillam1/Gwentbot/hoplite/thasos-aa051e98/deploy/install.sh"
ENV_FILE=/etc/lavk.env
WORK_DIR=/var/lib/telegram-bot-api
LOCAL=http://127.0.0.1:8081
CONTAINER=tgapi

# Всё тело — в функции: bash читает скрипт целиком до запуска, и вложенные команды
# не съедят остаток скрипта из stdin при запуске через curl | bash.
main() {
say()  { echo; echo "==> $*"; }
fail() { echo; echo "❌ $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || fail "Запусти от root: добавь sudo перед bash"
[ -f "$ENV_FILE" ] || fail "Не нашёл $ENV_FILE — сначала установи бота через install.sh"
set -a; . "$ENV_FILE"; set +a
[ -n "${BOT_TOKEN:-}" ] || fail "В $ENV_FILE нет BOT_TOKEN"

say "Шаг 1 из 6. Ключи с my.telegram.org"
API_ID=""; API_HASH=""
while ! [[ "$API_ID" =~ ^[0-9]{3,12}$ ]]; do
  read -r -p "Вставь api_id (только цифры) и нажми Enter: " API_ID < /dev/tty
  API_ID="$(echo "$API_ID" | tr -d '[:space:]')"
  [[ "$API_ID" =~ ^[0-9]{3,12}$ ]] || echo "   Это не похоже на api_id — там только цифры. Ещё раз."
done
while ! [[ "$API_HASH" =~ ^[0-9a-fA-F]{32}$ ]]; do
  read -r -p "Вставь api_hash (32 символа: цифры и буквы a–f) и нажми Enter: " API_HASH < /dev/tty
  API_HASH="$(echo "$API_HASH" | tr -d '[:space:]')"
  [[ "$API_HASH" =~ ^[0-9a-fA-F]{32}$ ]] || echo "   Это не похоже на api_hash — должно быть ровно 32 символа. Ещё раз."
done
echo "   Ключи приняты."

say "Шаг 2 из 6. Обновляю код бота"
curl -fsSL "$INSTALL_URL" | bash
grep -q "TG_API_BASE" /opt/lavk/main.py || fail "В коде бота нет поддержки своего сервера — обновление не прошло"

say "Шаг 3 из 6. Ставлю Docker и права на папку"
export DEBIAN_FRONTEND=noninteractive
apt-get install -y -qq docker.io acl >/dev/null
systemctl enable -q --now docker
mkdir -p "$WORK_DIR"
# Бот (пользователь lavk) читает скачанные сервером файлы прямо с диска.
setfacl -R -m u:lavk:rX "$WORK_DIR"
setfacl -R -d -m u:lavk:rX "$WORK_DIR"

say "Шаг 4 из 6. Запускаю сервер Telegram"
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
docker run -d --name "$CONTAINER" --restart always -p 127.0.0.1:8081:8081 \
  -e TELEGRAM_API_ID="$API_ID" -e TELEGRAM_API_HASH="$API_HASH" -e TELEGRAM_LOCAL=1 \
  -v "$WORK_DIR:$WORK_DIR" aiogram/telegram-bot-api:latest >/dev/null
up=""
for _ in $(seq 1 30); do
  code="$(curl -s -o /dev/null -w '%{http_code}' "$LOCAL/" || true)"
  if [ "$code" != "000" ]; then up=1; break; fi
  sleep 2
done
[ -n "$up" ] || { docker logs --tail 20 "$CONTAINER" || true; fail "Сервер не поднялся за минуту. Бот НЕ переключён и работает как раньше."; }
echo "   Сервер отвечает."

say "Шаг 5 из 6. Отключаю бота от общего сервера Telegram (logOut)"
answer="$(curl -s -m 30 "https://api.telegram.org/bot$BOT_TOKEN/logOut" || true)"
echo "   Ответ Telegram: $answer"
if ! echo "$answer" | grep -q '"ok":true'; then
  echo "   ⚠️ Telegram не подтвердил logOut. Если бот уже был отключён раньше — это нормально, продолжаю."
fi

say "Шаг 6 из 6. Переключаю бота на свой сервер"
if grep -q '^TG_API_BASE=' "$ENV_FILE"; then
  sed -i "s|^TG_API_BASE=.*|TG_API_BASE=$LOCAL|" "$ENV_FILE"
else
  echo "TG_API_BASE=$LOCAL" >> "$ENV_FILE"
fi
systemctl restart lavk
echo "   Жду 15 секунд…"
sleep 15

me="$(curl -s -m 60 "$LOCAL/bot$BOT_TOKEN/getMe" || true)"
echo
journalctl -u lavk -n 15 --no-pager -o cat || true
echo
if echo "$me" | grep -q '"ok":true'; then
  echo "✅ Готово: бот работает через свой сервер Telegram. Попробуй генерацию с обычного аккаунта."
else
  echo "⚠️ Свой сервер пока не ответил на getMe: $me"
  echo "   Подожди минуту и проверь: journalctl -u lavk -n 30 --no-pager"
fi
echo
echo "Откат (если понадобится): удали строку TG_API_BASE из $ENV_FILE,"
echo "выполни  curl -s $LOCAL/bot\$BOT_TOKEN/logOut  и через 10 минут  systemctl restart lavk"
}

main "$@"
