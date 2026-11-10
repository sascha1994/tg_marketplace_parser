import json
import sqlite3
import time
from dataclasses import asdict
from contextlib import suppress
from typing import Iterable

from .constants import (
    INITIAL_POST_CHECK_INTERVAL_SECONDS,
    INITIAL_STOCK_CHECK_INTERVAL_SECONDS,
    POST_AUTO_DELETE_SECONDS,
)
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
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS scheduled_publications (
                sku_key TEXT PRIMARY KEY,
                product_json TEXT NOT NULL,
                source_text TEXT NOT NULL,
                source_chat_id INTEGER NOT NULL,
                target_channel TEXT NOT NULL DEFAULT 'main',
                due_at INTEGER NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_scheduled_publications_due
                ON scheduled_publications(due_at);
            CREATE TABLE IF NOT EXISTS published_posts (
                post_id TEXT PRIMARY KEY,
                sku_key TEXT NOT NULL,
                product_json TEXT NOT NULL,
                publication_text TEXT NOT NULL,
                message_ids TEXT NOT NULL,
                published_at INTEGER NOT NULL,
                next_check_at INTEGER NOT NULL,
                check_interval_seconds INTEGER NOT NULL DEFAULT 3600,
                discount_percent REAL,
                target_channel TEXT NOT NULL DEFAULT 'main',
                status TEXT NOT NULL DEFAULT 'active'
            );
            CREATE INDEX IF NOT EXISTS idx_published_posts_due
                ON published_posts(status, next_check_at);
            CREATE INDEX IF NOT EXISTS idx_published_posts_sku
                ON published_posts(sku_key);
            CREATE TABLE IF NOT EXISTS source_cursors (
                chat_id INTEGER PRIMARY KEY,
                last_message_id INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pending_jobs (
                job_id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_chat_id INTEGER NOT NULL,
                message_ids TEXT NOT NULL,
                target_channel TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_pending_jobs_chat
                ON pending_jobs(source_chat_id);
            CREATE TABLE IF NOT EXISTS blocklist (
                kind TEXT NOT NULL CHECK(kind IN ('brand', 'keyword')),
                value TEXT NOT NULL,
                PRIMARY KEY (kind, value)
            );
            CREATE TABLE IF NOT EXISTS source_stats (
                chat_id INTEGER PRIMARY KEY,
                received INTEGER NOT NULL DEFAULT 0,
                published INTEGER NOT NULL DEFAULT 0,
                rejected INTEGER NOT NULL DEFAULT 0,
                duplicates INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS stats_buckets (
                bucket_ts INTEGER NOT NULL,
                metric TEXT NOT NULL,
                value INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (bucket_ts, metric)
            );
            CREATE INDEX IF NOT EXISTS idx_stats_buckets_ts
                ON stats_buckets(bucket_ts);
            CREATE TABLE IF NOT EXISTS cascade_stats (
                step TEXT PRIMARY KEY,
                ok INTEGER NOT NULL DEFAULT 0,
                fail INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        self._migrate_publication_tables()
        self._backfill_initial_stats_if_needed()

    def _migrate_publication_tables(self) -> None:
        scheduled_columns = {
            row[1] for row in self._conn.execute("PRAGMA table_info(scheduled_publications)")
        }
        if "target_channel" not in scheduled_columns:
            self._conn.execute(
                "ALTER TABLE scheduled_publications ADD COLUMN target_channel "
                "TEXT NOT NULL DEFAULT 'main'"
            )

        published_columns = {
            row[1] for row in self._conn.execute("PRAGMA table_info(published_posts)")
        }
        if "post_id" not in published_columns:
            self._conn.execute("ALTER TABLE published_posts RENAME TO published_posts_legacy")
            self._conn.executescript(
                """
                CREATE TABLE published_posts (
                    post_id TEXT PRIMARY KEY,
                    sku_key TEXT NOT NULL,
                    product_json TEXT NOT NULL,
                    publication_text TEXT NOT NULL,
                    message_ids TEXT NOT NULL,
                    published_at INTEGER NOT NULL,
                    next_check_at INTEGER NOT NULL,
                    check_interval_seconds INTEGER NOT NULL DEFAULT 3600,
                    discount_percent REAL,
                    target_channel TEXT NOT NULL DEFAULT 'main',
                    status TEXT NOT NULL DEFAULT 'active'
                );
                CREATE INDEX IF NOT EXISTS idx_published_posts_due
                    ON published_posts(status, next_check_at);
                CREATE INDEX IF NOT EXISTS idx_published_posts_sku
                    ON published_posts(sku_key);
                """
            )
            legacy_columns = {
                row[1] for row in self._conn.execute("PRAGMA table_info(published_posts_legacy)")
            }
            if {
                "sku_key",
                "product_json",
                "publication_text",
                "message_ids",
                "published_at",
                "next_check_at",
                "discount_percent",
            }.issubset(legacy_columns):
                rows = self._conn.execute("SELECT * FROM published_posts_legacy").fetchall()
                names = [row[1] for row in self._conn.execute("PRAGMA table_info(published_posts_legacy)")]
                for values in rows:
                    old = dict(zip(names, values, strict=False))
                    message_ids = old.get("message_ids") or "[]"
                    try:
                        parsed_ids = json.loads(message_ids)
                    except (TypeError, ValueError):
                        parsed_ids = []
                    post_id = f"main:{parsed_ids[0]}" if parsed_ids else f"main:{old['sku_key']}"
                    self._conn.execute(
                        """
                        INSERT OR IGNORE INTO published_posts(
                            post_id, sku_key, product_json, publication_text, message_ids,
                            published_at, next_check_at, discount_percent, target_channel, status
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'main', ?)
                        """,
                        (
                            post_id,
                            old["sku_key"],
                            old["product_json"],
                            old["publication_text"],
                            message_ids,
                            old["published_at"],
                            old["next_check_at"],
                            old["discount_percent"],
                            old.get("status", "active"),
                        ),
                    )
            self._conn.execute("DROP TABLE published_posts_legacy")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_published_posts_due "
                "ON published_posts(status, next_check_at)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_published_posts_sku "
                "ON published_posts(sku_key)"
            )
        elif "target_channel" not in published_columns:
            self._conn.execute(
                "ALTER TABLE published_posts ADD COLUMN target_channel "
                "TEXT NOT NULL DEFAULT 'main'"
            )

        published_columns = {
            row[1] for row in self._conn.execute("PRAGMA table_info(published_posts)")
        }
        if "check_interval_seconds" not in published_columns:
            self._conn.execute(
                "ALTER TABLE published_posts ADD COLUMN check_interval_seconds "
                f"INTEGER NOT NULL DEFAULT {INITIAL_STOCK_CHECK_INTERVAL_SECONDS}"
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
                "DELETE FROM seen_items WHERE status = 'pending' AND created_at < ? "
                "AND sku_key NOT IN (SELECT sku_key FROM scheduled_publications)",
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

    def get_setting(self, key: str, default: str) -> str:
        row = self._conn.execute(
            "SELECT value FROM app_settings WHERE key = ?",
            (key,),
        ).fetchone()
        return row[0] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO app_settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def save_scheduled_publication(
        self,
        product: ProductInfo,
        source_text: str,
        source_chat_id: int,
        due_at: int,
        target_channel: str = "main",
        attempts: int = 0,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO scheduled_publications(
                sku_key, product_json, source_text, source_chat_id, target_channel, due_at, attempts
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(sku_key) DO UPDATE SET
                product_json = excluded.product_json,
                source_text = excluded.source_text,
                source_chat_id = excluded.source_chat_id,
                target_channel = excluded.target_channel,
                due_at = excluded.due_at,
                attempts = excluded.attempts
            """,
            (
                product.sku_key,
                json.dumps(asdict(product), ensure_ascii=False),
                source_text,
                source_chat_id,
                target_channel,
                due_at,
                attempts,
            ),
        )

    def get_due_scheduled_publications(self, now: int, limit: int = 1) -> list[dict]:
        rows = self._conn.execute(
            "SELECT sku_key, product_json, source_text, source_chat_id, target_channel, due_at, attempts "
            "FROM scheduled_publications WHERE due_at <= ? ORDER BY due_at LIMIT ?",
            (now, limit),
        ).fetchall()
        return [
            {
                "sku_key": row[0],
                "product_json": row[1],
                "source_text": row[2],
                "source_chat_id": row[3],
                "target_channel": row[4],
                "due_at": row[5],
                "attempts": row[6],
            }
            for row in rows
        ]

    def reschedule_publication(self, sku_key: str, due_at: int, *, increment_attempts: bool = True) -> None:
        self._conn.execute(
            "UPDATE scheduled_publications SET due_at = ?, "
            "attempts = attempts + ? WHERE sku_key = ?",
            (due_at, int(increment_attempts), sku_key),
        )

    def delete_scheduled_publication(self, sku_key: str) -> None:
        self._conn.execute("DELETE FROM scheduled_publications WHERE sku_key = ?", (sku_key,))

    def save_published_post(
        self,
        product: ProductInfo,
        publication_text: str,
        message_ids: Iterable[int],
        discount_percent: float | None,
        published_at: int,
        target_channel: str = "main",
    ) -> None:
        message_id_list = list(message_ids)
        if not message_id_list:
            raise ValueError("published post must contain at least one Telegram message ID")
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                """
                INSERT INTO published_posts(
                    post_id, sku_key, product_json, publication_text, message_ids, published_at,
                    next_check_at, check_interval_seconds, discount_percent, target_channel, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
                """,
                (
                    f"{target_channel}:{message_id_list[0]}",
                    product.sku_key,
                    json.dumps(asdict(product), ensure_ascii=False),
                    publication_text,
                    json.dumps(message_id_list),
                    published_at,
                    published_at + INITIAL_POST_CHECK_INTERVAL_SECONDS,
                    INITIAL_STOCK_CHECK_INTERVAL_SECONDS,
                    discount_percent,
                    target_channel,
                ),
            )
            self._conn.execute(
                "UPDATE seen_items SET status = 'published', published_at = ? "
                "WHERE status = 'pending' AND sku_key = ?",
                (published_at, product.sku_key),
            )
            self._conn.execute(
                "DELETE FROM scheduled_publications WHERE sku_key = ?",
                (product.sku_key,),
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def get_due_published_posts(self, now: int, limit: int = 1) -> list[dict]:
        rows = self._conn.execute(
            "SELECT post_id, sku_key, product_json, publication_text, message_ids, published_at, "
            "next_check_at, check_interval_seconds, discount_percent, target_channel FROM published_posts "
            "WHERE status = 'active' AND next_check_at <= ? "
            "ORDER BY next_check_at ASC, COALESCE(discount_percent, 0) DESC, published_at DESC LIMIT ?",
            (now, limit),
        ).fetchall()
        return [
            {
                "post_id": row[0],
                "sku_key": row[1],
                "product_json": row[2],
                "publication_text": row[3],
                "message_ids": row[4],
                "published_at": row[5],
                "next_check_at": row[6],
                "check_interval_seconds": row[7],
                "discount_percent": row[8],
                "target_channel": row[9],
            }
            for row in rows
        ]

    def count_published_post_checks(self, now: int | None = None) -> tuple[int, int]:
        check_now = int(time.time()) if now is None else now
        active_row = self._conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(CASE WHEN next_check_at <= ? THEN 1 ELSE 0 END), 0) "
            "FROM published_posts WHERE status = 'active'",
            (check_now,),
        ).fetchone()
        if not active_row:
            return 0, 0
        return int(active_row[0] or 0), int(active_row[1] or 0)

    def set_published_post_status(self, post_id: str, status: str) -> None:
        self._conn.execute(
            "UPDATE published_posts SET status = ? WHERE post_id = ?",
            (status, post_id),
        )

    def defer_published_post_check(self, post_id: str, next_check_at: int) -> None:
        self._conn.execute(
            "UPDATE published_posts SET next_check_at = ? WHERE post_id = ? AND status = 'active'",
            (next_check_at, post_id),
        )

    def schedule_next_published_post_check(
        self,
        post_id: str,
        next_check_at: int,
        next_interval_seconds: int,
        expires_at: int,
    ) -> None:
        if next_check_at >= expires_at:
            self._conn.execute(
                "UPDATE published_posts SET status = 'expired' "
                "WHERE post_id = ? AND status = 'active'",
                (post_id,),
            )
            return
        self._conn.execute(
            "UPDATE published_posts SET next_check_at = ?, check_interval_seconds = ? "
            "WHERE post_id = ? AND status = 'active'",
            (next_check_at, next_interval_seconds, post_id),
        )

    def expire_old_published_posts(self, now: int) -> int:
        cursor = self._conn.execute(
            "UPDATE published_posts SET status = 'expired' "
            "WHERE status = 'active' AND published_at <= ?",
            (now - POST_AUTO_DELETE_SECONDS,),
        )
        return cursor.rowcount

    def release_reservations(self, sku_keys: Iterable[str]) -> None:
        keys = tuple(set(sku_keys))
        if not keys:
            return
        placeholders = ",".join("?" for _ in keys)
        self._conn.execute(
            f"DELETE FROM seen_items WHERE status = 'pending' AND sku_key IN ({placeholders})",
            keys,
        )
        placeholders = ",".join("?" for _ in keys)
        self._conn.execute(
            f"DELETE FROM scheduled_publications WHERE sku_key IN ({placeholders})",
            keys,
        )

    def clear_published(self) -> None:
        self._conn.execute("DELETE FROM seen_items")
        self._conn.execute("DELETE FROM scheduled_publications")
        self._conn.execute("DELETE FROM published_posts")

    def count_by_status(self) -> tuple[int, int]:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) FROM seen_items GROUP BY status"
        ).fetchall()
        counts = {status: count for status, count in rows}
        return counts.get("published", 0), counts.get("pending", 0)

    # ---- Устойчивая очередь заданий и курсоры источников ----

    def save_job(
        self,
        source_chat_id: int,
        message_ids: Iterable[int],
        target_channel: str | None = None,
        attempts: int = 0,
    ) -> int:
        cursor = self._conn.execute(
            "INSERT INTO pending_jobs(source_chat_id, message_ids, target_channel, attempts, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                source_chat_id,
                json.dumps(sorted(int(item) for item in message_ids)),
                target_channel,
                attempts,
                int(time.time()),
            ),
        )
        return int(cursor.lastrowid)

    def update_job_attempts(self, job_id: int, attempts: int) -> None:
        self._conn.execute(
            "UPDATE pending_jobs SET attempts = ? WHERE job_id = ?",
            (attempts, job_id),
        )

    def delete_job(self, job_id: int) -> None:
        self._conn.execute("DELETE FROM pending_jobs WHERE job_id = ?", (job_id,))

    def load_pending_jobs(self, limit: int = 500) -> list[dict]:
        rows = self._conn.execute(
            "SELECT job_id, source_chat_id, message_ids, target_channel, attempts "
            "FROM pending_jobs ORDER BY job_id LIMIT ?",
            (limit,),
        ).fetchall()
        jobs: list[dict] = []
        for row in rows:
            try:
                message_ids = [int(item) for item in json.loads(row[2])]
            except (TypeError, ValueError):
                message_ids = []
            jobs.append(
                {
                    "job_id": row[0],
                    "source_chat_id": row[1],
                    "message_ids": message_ids,
                    "target_channel": row[3],
                    "attempts": row[4],
                }
            )
        return jobs

    def pending_message_ids(self, source_chat_id: int) -> set[int]:
        rows = self._conn.execute(
            "SELECT message_ids FROM pending_jobs WHERE source_chat_id = ?",
            (source_chat_id,),
        ).fetchall()
        result: set[int] = set()
        for (raw_ids,) in rows:
            with suppress(TypeError, ValueError):
                result.update(int(item) for item in json.loads(raw_ids))
        return result

    def get_cursor(self, chat_id: int) -> int | None:
        row = self._conn.execute(
            "SELECT last_message_id FROM source_cursors WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        return int(row[0]) if row else None

    def set_cursor(self, chat_id: int, message_id: int) -> None:
        self._conn.execute(
            "INSERT INTO source_cursors(chat_id, last_message_id, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET "
            "last_message_id = MAX(last_message_id, excluded.last_message_id), "
            "updated_at = excluded.updated_at",
            (chat_id, message_id, int(time.time())),
        )

    # ---- Чёрный список ----

    def add_block(self, kind: str, value: str) -> bool:
        normalized = " ".join(value.casefold().split())
        if kind not in {"brand", "keyword"} or not normalized:
            return False
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO blocklist(kind, value) VALUES (?, ?)",
            (kind, normalized),
        )
        return cursor.rowcount > 0

    def remove_block(self, kind: str, value: str) -> bool:
        normalized = " ".join(value.casefold().split())
        cursor = self._conn.execute(
            "DELETE FROM blocklist WHERE kind = ? AND value = ?",
            (kind, normalized),
        )
        return cursor.rowcount > 0

    def list_blocks(self) -> list[tuple[str, str]]:
        rows = self._conn.execute(
            "SELECT kind, value FROM blocklist ORDER BY kind, value"
        ).fetchall()
        return [(row[0], row[1]) for row in rows]

    def find_block_match(self, brand: str, title: str) -> str | None:
        """Возвращает описание сработавшего правила чёрного списка или None."""
        brand_text = " ".join((brand or "").casefold().split())
        haystack = " ".join(f"{brand} {title}".casefold().split())
        for kind, value in self.list_blocks():
            if kind == "brand" and brand_text and value == brand_text:
                return f"бренд «{value}»"
            if kind == "keyword" and value in haystack:
                return f"слово «{value}»"
        return None

    # ---- Статистика источников ----

    def bump_source_stat(self, chat_id: int, field_name: str, amount: int = 1) -> None:
        if field_name not in {"received", "published", "rejected", "duplicates"}:
            raise ValueError(f"unknown source stat: {field_name}")
        self._conn.execute(
            f"INSERT INTO source_stats(chat_id, {field_name}, updated_at) VALUES (?, ?, ?) "
            f"ON CONFLICT(chat_id) DO UPDATE SET "
            f"{field_name} = {field_name} + excluded.{field_name}, updated_at = excluded.updated_at",
            (chat_id, amount, int(time.time())),
        )

    def get_top_sources(self, limit: int = 10) -> list[dict]:
        rows = self._conn.execute(
            "SELECT chat_id, received, published, rejected, duplicates FROM source_stats "
            "ORDER BY published DESC, received DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "chat_id": row[0],
                "received": row[1],
                "published": row[2],
                "rejected": row[3],
                "duplicates": row[4],
            }
            for row in rows
        ]

    # ---- Персистентная статистика по периодам и каскаду ----

    def _backfill_initial_stats_if_needed(self) -> None:
        """Однократно переносит уже накопленные исторические данные в stats_buckets при обновлении БД."""
        if self.get_setting("stats_backfilled_v1", "") == "true":
            return
        has_any_stats = self._conn.execute("SELECT 1 FROM stats_buckets LIMIT 1").fetchone()
        if not has_any_stats:
            src_row = self._conn.execute(
                "SELECT COALESCE(SUM(received), 0), COALESCE(SUM(published), 0), "
                "COALESCE(SUM(rejected), 0), COALESCE(SUM(duplicates), 0), COALESCE(MAX(updated_at), 0) "
                "FROM source_stats"
            ).fetchone()
            if src_row and any(src_row[:4]):
                ts = int(src_row[4] or time.time())
                if src_row[0] > 0:
                    self.bump_stat("incoming", int(src_row[0]), timestamp=ts)
                if src_row[1] > 0:
                    self.bump_stat("published", int(src_row[1]), timestamp=ts)
                if src_row[2] > 0:
                    self.bump_stat("rejected", int(src_row[2]), timestamp=ts)
                if src_row[3] > 0:
                    self.bump_stat("duplicates", int(src_row[3]), timestamp=ts)

            post_rows = self._conn.execute(
                "SELECT status, published_at FROM published_posts WHERE status IN ('deleted', 'unavailable_marked')"
            ).fetchall()
            for status, pub_at in post_rows:
                ts = int(pub_at or time.time())
                if status == "deleted":
                    self.bump_stat("posts_deleted", 1, timestamp=ts)
                elif status == "unavailable_marked":
                    self.bump_stat("posts_marked_unavailable", 1, timestamp=ts)
        self.set_setting("stats_backfilled_v1", "true")

    def bump_stat(self, metric: str, amount: int = 1, timestamp: float | None = None) -> None:
        if amount <= 0 or not metric:
            return
        raw_ts = time.time() if timestamp is None else float(timestamp)
        bucket_ts = (int(raw_ts) // 60) * 60
        self._conn.execute(
            "INSERT INTO stats_buckets(bucket_ts, metric, value) VALUES (?, ?, ?) "
            "ON CONFLICT(bucket_ts, metric) DO UPDATE SET value = value + excluded.value",
            (bucket_ts, metric, int(amount)),
        )

    def get_stats(self, since_ts: int | None = None) -> dict[str, int]:
        if since_ts is None:
            rows = self._conn.execute(
                "SELECT metric, COALESCE(SUM(value), 0) FROM stats_buckets GROUP BY metric"
            ).fetchall()
        else:
            cutoff_bucket = (int(since_ts) // 60) * 60
            rows = self._conn.execute(
                "SELECT metric, COALESCE(SUM(value), 0) FROM stats_buckets "
                "WHERE bucket_ts >= ? GROUP BY metric",
                (cutoff_bucket,),
            ).fetchall()
        return {str(metric): int(total or 0) for metric, total in rows}

    def bump_cascade_stat(self, step: str, success: bool) -> None:
        if not step:
            return
        col = "ok" if success else "fail"
        self._conn.execute(
            f"INSERT INTO cascade_stats(step, {col}) VALUES (?, 1) "
            f"ON CONFLICT(step) DO UPDATE SET {col} = {col} + 1",
            (step,),
        )

    def get_cascade_stats(self) -> dict[str, list[int]]:
        rows = self._conn.execute(
            "SELECT step, ok, fail FROM cascade_stats ORDER BY step"
        ).fetchall()
        return {str(row[0]): [int(row[1] or 0), int(row[2] or 0)] for row in rows}

    def get_last_published_at(self) -> float | None:
        raw = self.get_setting("last_published_at", "")
        if raw:
            with suppress(TypeError, ValueError):
                val = float(raw)
                if val > 0:
                    return val
        row = self._conn.execute(
            "SELECT MAX(published_at) FROM published_posts"
        ).fetchone()
        if row and row[0]:
            return float(row[0])
        seen_row = self._conn.execute(
            "SELECT MAX(published_at) FROM seen_items WHERE status = 'published'"
        ).fetchone()
        if seen_row and seen_row[0]:
            return float(seen_row[0])
        return None

    def set_last_published_at(self, timestamp: float) -> None:
        self.set_setting("last_published_at", str(float(timestamp)))

    def reset_stats(self) -> None:
        """Обнуляет накопленную статистику, статистику источников и каскада, не затрагивая дедупликацию."""
        self._conn.execute("DELETE FROM stats_buckets")
        self._conn.execute("DELETE FROM cascade_stats")
        self._conn.execute("DELETE FROM source_stats")
        self._conn.execute("DELETE FROM app_settings WHERE key = 'last_published_at'")
        self.set_setting("stats_backfilled_v1", "true")

    def close(self) -> None:
        with suppress(Exception):
            self._conn.close()
