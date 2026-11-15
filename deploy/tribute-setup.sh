#!/usr/bin/env bash
# Подключает оплату рублями через Tribute: ключ API, товары, HTTPS-адрес для вебхуков (Caddy + sslip.io).
# Запуск:
#   curl -fsSL https://raw.githubusercontent.com/kirillam1/Gwentbot/hoplite/thasos-aa051e98/deploy/tribute-setup.sh | sudo bash
set -euo pipefail

INSTALL_URL="https://raw.githubusercontent.com/kirillam1/Gwentbot/hoplite/thasos-aa051e98/deploy/install.sh"
ENV_FILE=/etc/lavk.env
PORT=8090

# Всё тело — в функции: bash прочитает скрипт целиком до запуска (важно при curl | bash).
main() {
say()  { echo; echo "==> $*"; }
fail() { echo; echo "❌ $*" >&2; exit 1; }
set_env() {  # set_env KEY VALUE — заменить строку или дописать
  if grep -q "^$1=" "$ENV_FILE"; then
    python3 - "$ENV_FILE" "$1" "$2" <<'PY'
import sys
path, key, value = sys.argv[1:]
lines = open(path, encoding="utf-8").read().splitlines()
lines = [f"{key}={value}" if l.startswith(key + "=") else l for l in lines]
open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")
PY
  else
    echo "$1=$2" >> "$ENV_FILE"
  fi
}

[ "$(id -u)" -eq 0 ] || fail "Запусти от root: добавь sudo перед bash"
[ -f "$ENV_FILE" ] || fail "Не нашёл $ENV_FILE — сначала установи бота через install.sh"

say "Шаг 1 из 5. Ключ API Tribute"
echo "   Кабинет автора Tribute → ⋯ → Настройки → «Ключи API». Ключ при вставке НЕ будет виден — так и задумано."
KEY=""
while [ -z "$KEY" ]; do
  read -r -s -p "   Вставь ключ и нажми Enter: " KEY < /dev/tty; echo
  KEY="$(echo "$KEY" | tr -d '[:space:]')"
done

say "Шаг 2 из 5. Товары"
code="$(curl -s -o /tmp/tribute_products.json -w '%{http_code}' -H "Api-Key: $KEY" "https://tribute.tg/api/v1/products?size=100&type=digital" || true)"
if [ "$code" = "401" ] || [ "$code" = "403" ]; then
  fail "Tribute не принял ключ API (код $code). Проверь ключ и запусти скрипт заново."
fi
PRODUCTS=""
if [ "$code" = "200" ]; then
  # Берём товары из Tribute сами: число в начале названия («100 звёзд…») — сколько звёзд он даёт.
  PRODUCTS="$(python3 - /tmp/tribute_products.json <<'PY'
import json, re, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
def find_items(x):
    if isinstance(x, list) and x and isinstance(x[0], dict) and "id" in x[0]:
        return x
    if isinstance(x, dict):
        for v in x.values():
            r = find_items(v)
            if r: return r
    return []
pairs = []
for p in find_items(data):
    m = re.match(r"\s*(\d+)\s*(зв|⭐|star)", str(p.get("name", "")), re.I)
    if m and str(p.get("status", "approved")) != "rejected":
        pairs.append((int(m.group(1)), int(p["id"]), p.get("name"), p.get("amount"), p.get("currency")))
for stars, pid, name, amount, cur in sorted(pairs):
    price = f"{int(amount)/100:.0f} {cur}" if isinstance(amount, (int, float)) else "?"
    print(f"   • {name} — {price} → {stars}⭐ (товар {pid})", file=sys.stderr)
print(",".join(f"{pid}:{stars}" for stars, pid, *_ in sorted(pairs)))
PY
)"
fi
if [ -n "$PRODUCTS" ]; then
  echo "   Нашёл товары выше. Всё верно?"
  read -r -p "   Enter — да, всё верно; n — ввести вручную: " yn < /dev/tty
  if [ "$yn" = "n" ] || [ "$yn" = "N" ]; then PRODUCTS=""; fi
else
  echo "   Не смог получить список товаров автоматически (код $code) — введём вручную."
fi
if [ -z "$PRODUCTS" ]; then
echo "   Для каждого товара: вставь ссылку или номер товара, потом сколько звёзд он даёт."
echo "   Когда товары закончатся — просто нажми Enter на пустой ссылке."
while true; do
  read -r -p "   Ссылка или номер товара (или Enter, чтобы закончить): " LINK < /dev/tty
  LINK="$(echo "$LINK" | tr -d '[:space:]')"
  if [ -z "$LINK" ]; then
    [ -n "$PRODUCTS" ] && break
    echo "   Нужен хотя бы один товар."; continue
  fi
  ID="$(echo "$LINK" | grep -oE 'p[0-9]+' | head -1 | tr -d p || true)"
  [ -n "$ID" ] || ID="$(echo "$LINK" | grep -oE '[0-9]{3,}' | head -1 || true)"
  [ -n "$ID" ] || { echo "   Не нашёл номер товара."; continue; }
  STARS=""
  while ! [[ "$STARS" =~ ^[0-9]+$ ]]; do
    read -r -p "   Сколько звёзд даёт товар $ID? " STARS < /dev/tty
    STARS="$(echo "$STARS" | tr -d '[:space:]')"
  done
  PRODUCTS="${PRODUCTS:+$PRODUCTS,}$ID:$STARS"
  echo "   Добавлен: товар $ID → $STARS⭐"
done
fi
echo "   Товары: $PRODUCTS"

say "Шаг 3 из 5. Обновляю код бота"
curl -fsSL "$INSTALL_URL" | bash
[ -f /opt/lavk/tribute.py ] || fail "В коде бота нет модуля Tribute — обновление не прошло"

say "Шаг 4 из 5. HTTPS-адрес для уведомлений об оплате"
IP="$(curl -4 -s -m 15 https://ipinfo.io/ip || true)"
[[ "$IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "Не смог узнать внешний IP сервера"
DOMAIN="${IP//./-}.sslip.io"
export DEBIAN_FRONTEND=noninteractive
apt-get install -y -qq caddy >/dev/null
[ -f /etc/caddy/Caddyfile ] && cp /etc/caddy/Caddyfile "/etc/caddy/Caddyfile.bak.$(date +%s)"
cat > /etc/caddy/Caddyfile <<EOF
$DOMAIN {
	handle /tribute/* {
		reverse_proxy 127.0.0.1:$PORT
	}
	handle {
		respond 404
	}
}
EOF
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
fi
systemctl enable -q caddy
systemctl restart caddy

say "Шаг 5 из 5. Включаю оплату в боте"
set_env TRIBUTE_API_KEY "$KEY"
set_env TRIBUTE_PRODUCTS "$PRODUCTS"
set_env TRIBUTE_PORT "$PORT"
systemctl restart lavk
echo "   Жду, пока бот запустится и сервер получит HTTPS-сертификат (до 2 минут)…"
URL="https://$DOMAIN/tribute/webhook"
ok=""
for _ in $(seq 1 24); do
  sleep 5
  if [ "$(curl -s -o /dev/null -w '%{http_code}' -m 10 "$URL" || true)" = "200" ]; then ok=1; break; fi
done
echo
journalctl -u lavk -n 30 --no-pager -o cat | grep -E "Tribute|Ошибка|ERROR" || true
echo
if [ -n "$ok" ]; then
  echo "✅ Приёмник оплат работает."
else
  echo "⚠️ Адрес пока не отвечает. Подожди пару минут и проверь: curl -s $URL"
  echo "   Если не заработает — пришли вывод: journalctl -u caddy -n 30 --no-pager"
fi
echo
echo "ПОСЛЕДНИЙ ШАГ: вставь этот адрес в Tribute → Настройки → «Ключи API» → Webhook URL:"
echo
echo "   $URL"
echo
}

main "$@"
