"""Оплата рублями через Tribute (https://tribute.tg): цифровые товары «N звёзд в мешочек».

Покупатель платит в Tribute, Tribute шлёт вебхук new_digital_product на наш сервер,
бот проверяет подпись и начисляет звёзды. Подробности: wiki.tribute.tg → «Integrating Digital Products».

Настройки в /etc/lavk.env:
  TRIBUTE_API_KEY   — ключ из кабинета автора Tribute (Настройки → Ключи API); им же подписаны вебхуки
  TRIBUTE_PRODUCTS  — какие товары сколько звёзд дают: «123456:100,123457:300» (id товара:звёзды)
  TRIBUTE_PORT      — локальный порт приёмника вебхуков (по умолчанию 8090; снаружи его открывает Caddy)
"""
import base64
import hashlib
import hmac
import json
import math
import logging
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

import os

import requests

logger = logging.getLogger("lavk_bot")

API_BASE = "https://tribute.tg/api/v1"
# Внутри Telegram Tribute продаёт только за звёзды: «в рублях через СБП» — это покупка звёзд по курсу Telegram.
# Замер ноября 2026: товар 275 ₽ → 249⭐ → 360 ₽ по СБП; 519 ₽ → 470⭐ → 680 ₽; 145 ₽ → 132⭐.
RUB_PER_STAR = float(os.getenv("TRIBUTE_RUB_PER_STAR", "1.105"))   # цена товара → звёзды (вверх)
SBP_PER_STAR = float(os.getenv("TRIBUTE_SBP_PER_STAR", "1.446"))    # звезда при покупке по СБП


def sbp_price(amount_rub: float, stars: int = 0) -> int:
    """Сколько покупатель заплатит по СБП внутри Telegram за товар с такой ценой."""
    stars = stars or math.ceil(amount_rub / RUB_PER_STAR - 1e-9)
    return round(stars * SBP_PER_STAR)
WEBHOOK_PATH = "/tribute/webhook"


def parse_products(raw: str) -> dict[int, int]:
    """«123456:100, p123457:300» → {123456: 100, 123457: 300}. Ссылку startapp=p123456 тоже понимаем."""
    products = {}
    for part in re.split(r"[,;\s]+", raw or ""):
        m = re.search(r"p?(\d+)\s*[:=]\s*(\d+)$", part.strip())
        if m:
            products[int(m.group(1))] = int(m.group(2))
    return products


def product_link(product_id: int) -> str:
    """Ссылка открывает товар в мини-приложении Tribute внутри Telegram."""
    return f"https://t.me/tribute/app?startapp=p{product_id}"


def signature_ok(body: bytes, header: str, api_key: str) -> bool:
    """trbt-signature — HMAC-SHA256 тела запроса ключом API. Кодировку Tribute не уточняет: принимаем hex и base64."""
    if not header or not api_key:
        return False
    digest = hmac.new(api_key.encode(), body, hashlib.sha256).digest()
    header = header.strip()
    if header.lower().startswith("sha256="):
        header = header[7:]
    candidates = (digest.hex(), base64.b64encode(digest).decode())
    return any(hmac.compare_digest(header, c) for c in candidates)


class Tribute:
    def __init__(self, api_key: str, products: dict[int, int],
                 on_purchase: Callable[[int, int, int, str], None],
                 on_refund: Callable[[int, int, int, str], None]):
        self.api_key = api_key
        self.products = products
        self.on_purchase = on_purchase   # (telegram_user_id, product_id, stars, charge_id, рублей)
        self.on_refund = on_refund
        self._prices: dict[int, str] = {}
        self._links: dict[int, str] = {}
        self._buyer_rub: dict[int, int] = {}
        self._prices_at = 0.0
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.api_key and self.products)

    # ---------- цены для кнопок ----------
    def price_label(self, product_id: int) -> str:
        """«145 ₽» из API Tribute (кешируется на час); пусто, если API недоступен."""
        with self._lock:
            if time.time() - self._prices_at > 3600:
                self._prices_at = time.time()
                threading.Thread(target=self._load_prices, daemon=True).start()
            return self._prices.get(product_id, "")

    def buyer_rub(self, product_id: int, payload: Optional[dict] = None) -> int:
        """Сколько рублей заплатил покупатель: по цене товара (как на кнопке), иначе из вебхука."""
        with self._lock:
            value = self._buyer_rub.get(product_id, 0)
        if not value and payload and str(payload.get("currency", "")).lower() == "rub":
            try:
                value = sbp_price(int(payload["amount"]) / 100)
            except (KeyError, TypeError, ValueError):
                value = 0
        return value

    def link(self, product_id: int) -> str:
        """Ссылка из API Tribute (поле link), а пока её нет — собранная по номеру товара."""
        with self._lock:
            return self._links.get(product_id) or product_link(product_id)

    def _load_prices(self):
        prices, links, buyer = {}, {}, {}
        for product_id in self.products:
            try:
                r = requests.get(f"{API_BASE}/products/{product_id}", headers={"Api-Key": self.api_key}, timeout=15)
                r.raise_for_status()
                data = r.json()
                data = data.get("data", data) if isinstance(data, dict) else data
                amount, currency = int(data["amount"]), str(data.get("currency", "RUB")).upper()
                stars = data.get("starsAmount") if data.get("starsAmountEnabled") else 0
                value = sbp_price(amount / 100, int(stars or 0))
                prices[product_id] = f"{value} {'₽' if currency == 'RUB' else currency}"
                if currency == "RUB":
                    buyer[product_id] = value
                if isinstance(data.get("link"), str) and data["link"].startswith("https://"):
                    links[product_id] = data["link"]
            except Exception as e:
                logger.warning("Tribute: не удалось узнать цену товара %s (%s)", product_id, e)
        with self._lock:
            self._prices.update(prices)
            self._links.update(links)
            self._buyer_rub.update(buyer)

    # ---------- вебхуки ----------
    def handle(self, body: bytes, signature: str) -> int:
        """HTTP-код ответа. 200 — принято (в том числе повтор), 401 — плохая подпись, 400 — непонятный запрос."""
        if not signature_ok(body, signature, self.api_key):
            logger.warning("Tribute: вебхук с неверной подписью отклонён")
            return 401
        try:
            event = json.loads(body)
            name = str(event.get("name", ""))
            payload = event.get("payload") or {}
        except (ValueError, AttributeError):
            return 400
        logger.info("Tribute: вебхук %s: %s", name, json.dumps(payload, ensure_ascii=False)[:500])
        if name not in ("new_digital_product", "digital_product_refunded", "digitalProductRefund"):
            return 200  # другие события нам не нужны, но Tribute не должен их повторять
        try:
            user_id = int(payload["telegram_user_id"])
            product_id = int(payload["product_id"])
        except (KeyError, TypeError, ValueError):
            logger.error("Tribute: в вебхуке нет telegram_user_id/product_id: %s", payload)
            return 200
        stars = self.products.get(product_id)
        if stars is None:
            logger.error("Tribute: товар %s не указан в TRIBUTE_PRODUCTS — звёзды не начислены (%s)",
                         product_id, user_id)
            return 200
        # Уникальный id платежа: защищает от двойного зачисления при повторах вебхука.
        purchase = payload.get("purchase_id") or payload.get("id") or event.get("created_at") or ""
        charge_id = f"tribute:{product_id}:{user_id}:{purchase}"
        if name == "new_digital_product":
            self.on_purchase(user_id, product_id, stars, charge_id, self.buyer_rub(product_id, payload))
        else:
            self.on_refund(user_id, product_id, stars, "refund:" + charge_id)
        return 200

    def serve(self, port: int):
        tribute = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                if self.path.split("?")[0].rstrip("/") != WEBHOOK_PATH:
                    self.send_response(404)
                    self.end_headers()
                    return
                length = min(int(self.headers.get("Content-Length") or 0), 1_000_000)
                body = self.rfile.read(length)
                try:
                    code = tribute.handle(body, self.headers.get("trbt-signature", ""))
                except Exception:
                    logger.exception("Tribute: ошибка обработки вебхука")
                    code = 500  # Tribute повторит позже
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true}' if code == 200 else b'{"ok":false}')

            def do_GET(self):
                self.send_response(200 if self.path.rstrip("/") == WEBHOOK_PATH else 404)
                self.end_headers()
                self.wfile.write(b"lavk tribute webhook")

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        logger.info("Tribute: приём вебхуков на 127.0.0.1:%s%s, товаров: %s", port, WEBHOOK_PATH, len(self.products))
