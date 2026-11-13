#!/usr/bin/env bash
# Установка и обновление бота Лавк на чистом Ubuntu/Debian-сервере.
#   curl -fsSL https://raw.githubusercontent.com/kirillam1/Gwentbot/hoplite/thasos-aa051e98/deploy/install.sh | sudo bash
# Повторный запуск той же команды обновляет код и перезапускает бота; ключи и данные сохраняются.
set -euo pipefail

REPO="https://github.com/kirillam1/Gwentbot.git"
BRANCH="${LAVK_BRANCH:-hoplite/thasos-aa051e98}"
APP_DIR=/opt/lavk
DATA_DIR=/opt/lavk-data
ENV_FILE=/etc/lavk.env
SERVICE=/etc/systemd/system/lavk.service

if [ "$(id -u)" -ne 0 ]; then
  echo "Запусти от root: добавь sudo перед bash" >&2
  exit 1
fi

echo "==> Ставлю Python и git"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git ca-certificates >/dev/null

id lavk >/dev/null 2>&1 || useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin lavk
mkdir -p "$DATA_DIR"
chown -R lavk:lavk "$DATA_DIR"

echo "==> Скачиваю бота ($BRANCH)"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" fetch -q --depth 1 origin "$BRANCH"
  git -C "$APP_DIR" reset -q --hard FETCH_HEAD
else
  rm -rf "$APP_DIR"
  git clone -q --depth 1 --branch "$BRANCH" "$REPO" "$APP_DIR"
fi

echo "==> Ставлю библиотеки (1–3 минуты)"
[ -x "$APP_DIR/venv/bin/python" ] || python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

if [ ! -s "$ENV_FILE" ]; then
  echo
  echo "==> Ключи бота (вставь значение и нажми Enter)"
  ask() {
    local value=""
    while [ -z "$value" ]; do
      read -r -p "$1: " value < /dev/tty
      # При вставке с телефона часто прилипают пробелы и переносы строк.
      value="${value//[[:space:]]/}"
    done
    printf '%s=%s\n' "$2" "$value" >> "$ENV_FILE.tmp"
  }
  rm -f "$ENV_FILE.tmp"
  ask "BOT_TOKEN (токен от @BotFather)" BOT_TOKEN
  ask "GEMINI_API_KEY" GEMINI_API_KEY
  ask "FAL_API_KEY" FAL_API_KEY
  ask "ADMIN_ID (твой Telegram ID, цифрами)" ADMIN_ID
  optional=""
  read -r -p "OPENROUTER_API_KEY (необязательно — Enter, чтобы пропустить): " optional < /dev/tty || true
  optional="${optional//[[:space:]]/}"
  [ -n "$optional" ] && echo "OPENROUTER_API_KEY=$optional" >> "$ENV_FILE.tmp"
  echo "DB_PATH=$DATA_DIR/lavk.db" >> "$ENV_FILE.tmp"
  install -m 600 -o root -g root "$ENV_FILE.tmp" "$ENV_FILE"
  rm -f "$ENV_FILE.tmp"
else
  echo "==> Ключи уже сохранены в $ENV_FILE — оставляю как есть"
fi

cat > "$SERVICE" <<EOF
[Unit]
Description=Lavk Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
User=lavk
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
Environment=PYTHONUNBUFFERED=1
ExecStart=$APP_DIR/venv/bin/python main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable -q lavk
systemctl restart lavk

echo "==> Запускаю, жду 15 секунд…"
sleep 15
journalctl -u lavk -n 15 --no-pager -o cat || true
echo
if systemctl is-active -q lavk; then
  echo "✅ Лавк работает. Логи: journalctl -u lavk -f   Перезапуск: systemctl restart lavk"
else
  echo "❌ Бот не запустился — пришли строки выше."
  exit 1
fi
