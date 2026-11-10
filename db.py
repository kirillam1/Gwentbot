import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    username    TEXT,
    first_name  TEXT,
    balance     INTEGER NOT NULL DEFAULT 0,
    unlimited   INTEGER NOT NULL DEFAULT 0,
    blocked     INTEGER NOT NULL DEFAULT 0,
    referrer_id INTEGER,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_users_referrer ON users(referrer_id);
CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);

CREATE TABLE IF NOT EXISTS cards (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    parent_id  INTEGER,
    data       TEXT NOT NULL,
    file_id    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cards_user ON cards(user_id, id);

-- Журнал всех движений баланса; charge_id — id платежа Telegram, защищает от двойного зачисления.
CREATE TABLE IF NOT EXISTS transactions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    amount     INTEGER NOT NULL,
    kind       TEXT NOT NULL,
    charge_id  TEXT UNIQUE,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tx_user ON transactions(user_id, kind);

CREATE TABLE IF NOT EXISTS promo_codes (
    code       TEXT PRIMARY KEY,
    amount     INTEGER NOT NULL,
    max_uses   INTEGER NOT NULL DEFAULT 0,   -- 0 — без ограничения
    uses       INTEGER NOT NULL DEFAULT 0,
    active     INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS promo_uses (
    code       TEXT NOT NULL,
    user_id    INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (code, user_id)
);
"""

FLAGS = ("blocked", "unlimited")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


class Database:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock, self.conn:
            self.conn.executescript(SCHEMA)
            columns = {row[1] for row in self.conn.execute("PRAGMA table_info(cards)")}
            if "art_file_id" not in columns:
                # Чистый арт без рамки — исходник для правок.
                self.conn.execute("ALTER TABLE cards ADD COLUMN art_file_id TEXT")
            user_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(users)")}
            if "free_used" not in user_columns:
                self.conn.execute("ALTER TABLE users ADD COLUMN free_used INTEGER NOT NULL DEFAULT 0")
                # Пробная картища — только тем, у кого ещё нет ни одной карты.
                self.conn.execute("UPDATE users SET free_used = 1 WHERE user_id IN (SELECT user_id FROM cards)")

    def _one(self, sql: str, params=()):
        with self.lock:
            return self.conn.execute(sql, params).fetchone()

    def _all(self, sql: str, params=()):
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def _scalar(self, sql: str, params=()):
        row = self._one(sql, params)
        return row[0] if row and row[0] is not None else 0

    # ---------- пользователи ----------
    def upsert_user(self, user_id: int, username: Optional[str], first_name: Optional[str]) -> bool:
        """Возвращает True, если пользователь новый."""
        username = username.lower() if username else None
        with self.lock, self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO users (user_id, username, first_name, created_at) VALUES (?, ?, ?, ?)",
                (user_id, username, first_name, now()),
            )
            if cur.rowcount:
                return True
            self.conn.execute(
                "UPDATE users SET username = ?, first_name = ? WHERE user_id = ?", (username, first_name, user_id)
            )
            return False

    def get_user(self, user_id: int):
        return self._one("SELECT * FROM users WHERE user_id = ?", (user_id,))

    def find_user(self, query: str):
        query = query.strip()
        if query.lstrip("-").isdigit():
            return self.get_user(int(query))
        return self._one("SELECT * FROM users WHERE username = ?", (query.lstrip("@").lower(),))

    def set_flag(self, user_id: int, flag: str, value: bool):
        if flag not in FLAGS:
            raise ValueError(flag)
        with self.lock, self.conn:
            self.conn.execute(f"UPDATE users SET {flag} = ? WHERE user_id = ?", (int(value), user_id))

    def set_referrer(self, user_id: int, referrer_id: int) -> bool:
        if user_id == referrer_id:
            return False
        with self.lock, self.conn:
            cur = self.conn.execute(
                "UPDATE users SET referrer_id = ? WHERE user_id = ? AND referrer_id IS NULL "
                "AND EXISTS (SELECT 1 FROM users WHERE user_id = ?)",
                (referrer_id, user_id, referrer_id),
            )
            return cur.rowcount > 0

    def count_referrals(self, user_id: int) -> int:
        return self._scalar("SELECT COUNT(*) FROM users WHERE referrer_id = ?", (user_id,))

    def referral_earnings(self, user_id: int) -> int:
        return self._scalar("SELECT SUM(amount) FROM transactions WHERE user_id = ? AND kind = 'referral'", (user_id,))

    def active_user_ids(self) -> list[int]:
        return [row[0] for row in self._all("SELECT user_id FROM users WHERE blocked = 0")]

    # ---------- баланс ----------
    def add_balance(self, user_id: int, amount: int, kind: str, charge_id: Optional[str] = None) -> bool:
        """Начисляет (или списывает при amount < 0, но не ниже нуля). False — платёж уже был зачислен."""
        with self.lock, self.conn:
            try:
                self.conn.execute(
                    "INSERT INTO transactions (user_id, amount, kind, charge_id, created_at) VALUES (?, ?, ?, ?, ?)",
                    (user_id, amount, kind, charge_id, now()),
                )
            except sqlite3.IntegrityError:
                return False
            self.conn.execute(
                "UPDATE users SET balance = MAX(0, balance + ?) WHERE user_id = ?", (amount, user_id)
            )
            return True

    def spend(self, user_id: int, amount: int, kind: str) -> bool:
        with self.lock, self.conn:
            cur = self.conn.execute(
                "UPDATE users SET balance = balance - ? WHERE user_id = ? AND balance >= ?", (amount, user_id, amount)
            )
            if not cur.rowcount:
                return False
            self.conn.execute(
                "INSERT INTO transactions (user_id, amount, kind, created_at) VALUES (?, ?, ?, ?)",
                (user_id, -amount, kind, now()),
            )
            return True

    # ---------- пробная картища ----------
    def claim_trial(self, user_id: int) -> bool:
        with self.lock, self.conn:
            cur = self.conn.execute("UPDATE users SET free_used = 1 WHERE user_id = ? AND free_used = 0", (user_id,))
            return cur.rowcount > 0

    def release_trial(self, user_id: int):
        with self.lock, self.conn:
            self.conn.execute("UPDATE users SET free_used = 0 WHERE user_id = ?", (user_id,))

    # ---------- промокоды ----------
    def create_promo(self, code: str, amount: int, max_uses: int) -> bool:
        with self.lock, self.conn:
            try:
                self.conn.execute(
                    "INSERT INTO promo_codes (code, amount, max_uses, created_at) VALUES (?, ?, ?, ?)",
                    (code, amount, max_uses, now()),
                )
            except sqlite3.IntegrityError:
                return False
            return True

    def list_promos(self):
        return self._all("SELECT * FROM promo_codes WHERE active = 1 ORDER BY created_at DESC")

    def deactivate_promo(self, code: str):
        with self.lock, self.conn:
            self.conn.execute("UPDATE promo_codes SET active = 0 WHERE code = ?", (code,))

    def redeem_promo(self, code: str, user_id: int) -> tuple[str, int]:
        """Статус: ok / not_found / used / exhausted. Звёзды начисляются в той же транзакции."""
        with self.lock, self.conn:
            promo = self.conn.execute("SELECT * FROM promo_codes WHERE code = ? AND active = 1", (code,)).fetchone()
            if promo is None:
                return "not_found", 0
            if self.conn.execute("SELECT 1 FROM promo_uses WHERE code = ? AND user_id = ?", (code, user_id)).fetchone():
                return "used", 0
            if promo["max_uses"] and promo["uses"] >= promo["max_uses"]:
                return "exhausted", 0
            self.conn.execute("INSERT INTO promo_uses (code, user_id, created_at) VALUES (?, ?, ?)", (code, user_id, now()))
            self.conn.execute("UPDATE promo_codes SET uses = uses + 1 WHERE code = ?", (code,))
            self.conn.execute(
                "INSERT INTO transactions (user_id, amount, kind, created_at) VALUES (?, ?, 'promo', ?)",
                (user_id, promo["amount"], now()),
            )
            self.conn.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (promo["amount"], user_id))
            return "ok", promo["amount"]

    # ---------- картищи ----------
    def add_card(self, user_id: int, data: dict, file_id: str, art_file_id: Optional[str] = None,
                 parent_id: Optional[int] = None) -> int:
        with self.lock, self.conn:
            cur = self.conn.execute(
                "INSERT INTO cards (user_id, parent_id, data, file_id, art_file_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, parent_id, json.dumps(data, ensure_ascii=False), file_id, art_file_id, now()),
            )
            return cur.lastrowid

    @staticmethod
    def _card(row):
        if row is None:
            return None
        card = dict(row)
        card["data"] = json.loads(card["data"])
        return card

    def get_card(self, card_id: int, user_id: int):
        return self._card(self._one("SELECT * FROM cards WHERE id = ? AND user_id = ?", (card_id, user_id)))

    def list_cards(self, user_id: int, offset: int, limit: int):
        rows = self._all(
            "SELECT * FROM cards WHERE user_id = ? ORDER BY id DESC LIMIT ? OFFSET ?", (user_id, limit, offset)
        )
        return [self._card(row) for row in rows]

    def count_cards(self, user_id: int) -> int:
        return self._scalar("SELECT COUNT(*) FROM cards WHERE user_id = ?", (user_id,))

    # ---------- статистика ----------
    def stats(self) -> dict:
        day = today()
        return {
            "users": self._scalar("SELECT COUNT(*) FROM users"),
            "users_today": self._scalar("SELECT COUNT(*) FROM users WHERE created_at >= ?", (day,)),
            "blocked": self._scalar("SELECT COUNT(*) FROM users WHERE blocked = 1"),
            "unlimited": self._scalar("SELECT COUNT(*) FROM users WHERE unlimited = 1"),
            "cards": self._scalar("SELECT COUNT(*) FROM cards"),
            "cards_today": self._scalar("SELECT COUNT(*) FROM cards WHERE created_at >= ?", (day,)),
            "stars": self._scalar("SELECT SUM(amount) FROM transactions WHERE kind = 'topup'"),
            "stars_today": self._scalar(
                "SELECT SUM(amount) FROM transactions WHERE kind = 'topup' AND created_at >= ?", (day,)
            ),
        }
