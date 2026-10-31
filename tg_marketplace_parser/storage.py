import sqlite3
import time
from contextlib import suppress
from typing import Iterable

from .models import ProductInfo

class DuplicateDB:
    """SQLite-дедупликация с постоянным соединением и краткоживущими резервированиями pending."""

    def __init__(self, db_path: str, cooldown_hours: int, pending_ttl_minutes: int):
        self.db_path = db_path
        self.cooldown_seconds = cooldown_hours * 3600
        self.pending_ttl_seconds = pending_ttl_minutes * 60
        self._conn = self._connect()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            timeout=10,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _init_db(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS seen_items (
                sku_key TEXT PRIMARY KEY,
                platform TEXT NOT NULL,
                sku TEXT,
                canonical_url TEXT NOT NULL,
                source_chat_id INTEGER,
                source_message_id INTEGER,
                status TEXT NOT NULL CHECK(status IN ('pending', 'published')),
                created_at INTEGER NOT NULL,
                published_at INTEGER
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_seen_items_status_time "
            "ON seen_items(status, published_at, created_at)"
        )

    def reserve_fresh(
        self,
        products: Iterable[ProductInfo],
        source_chat_id: int,
        source_message_id: int,
    ) -> set[str]:
        products_by_key = {product.sku_key: product for product in products}
        if not products_by_key:
            return set()

        now = int(time.time())
        published_cutoff = now - self.cooldown_seconds
        pending_cutoff = now - self.pending_ttl_seconds

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                "DELETE FROM seen_items WHERE status = 'published' AND published_at < ?",
                (published_cutoff,),
            )
            self._conn.execute(
                "DELETE FROM seen_items WHERE status = 'pending' AND created_at < ?",
                (pending_cutoff,),
            )
            placeholders = ",".join("?" for _ in products_by_key)
            rows = self._conn.execute(
                f"SELECT sku_key FROM seen_items WHERE sku_key IN ({placeholders})",
                tuple(products_by_key),
            ).fetchall()
            occupied = {row[0] for row in rows}
            fresh_keys = set(products_by_key) - occupied
            self._conn.executemany(
                """
                INSERT INTO seen_items (
                    sku_key, platform, sku, canonical_url, source_chat_id,
                    source_message_id, status, created_at, published_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, NULL)
                """,
                [
                    (
                        product.sku_key,
                        product.platform,
                        product.sku,
                        product.canonical_url,
                        source_chat_id,
                        source_message_id,
                        now,
                    )
                    for key, product in products_by_key.items()
                    if key in fresh_keys
                ],
            )
            self._conn.execute("COMMIT")
            return fresh_keys
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def mark_published(self, sku_keys: Iterable[str]) -> None:
        keys = tuple(set(sku_keys))
        if not keys:
            return
        now = int(time.time())
        placeholders = ",".join("?" for _ in keys)
        self._conn.execute(
            f"UPDATE seen_items SET status = 'published', published_at = ? "
            f"WHERE status = 'pending' AND sku_key IN ({placeholders})",
            (now, *keys),
        )

    def release_reservations(self, sku_keys: Iterable[str]) -> None:
        keys = tuple(set(sku_keys))
        if not keys:
            return
        placeholders = ",".join("?" for _ in keys)
        self._conn.execute(
            f"DELETE FROM seen_items WHERE status = 'pending' AND sku_key IN ({placeholders})",
            keys,
        )

    def clear_published(self) -> None:
        self._conn.execute("DELETE FROM seen_items")

    def count_by_status(self) -> tuple[int, int]:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) FROM seen_items GROUP BY status"
        ).fetchall()
        counts = {status: count for status, count in rows}
        return counts.get("published", 0), counts.get("pending", 0)

    def close(self) -> None:
        with suppress(Exception):
            self._conn.close()

