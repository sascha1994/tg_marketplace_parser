import json
import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

from tg_marketplace_parser.market_data import MarketDataMixin
from tg_marketplace_parser.config import Settings
from tg_marketplace_parser.admin import AdminMixin
from tg_marketplace_parser.marketplace_parsing import (
    merge_card_data,
    parse_wb_detail_payload,
)
from tg_marketplace_parser.models import ProductCardData, ProductInfo
from tg_marketplace_parser.pipeline import PipelineMixin
from tg_marketplace_parser.storage import DuplicateDB


def make_product(platform="Wildberries", sku="12345", **kwargs):
    return ProductInfo(
        original_url=f"https://example.test/{sku}",
        resolved_url=f"https://example.test/{sku}",
        platform=platform,
        sku=sku,
        canonical_url=f"https://example.test/{sku}",
        sku_key=f"{platform.lower()}:{sku}",
        **kwargs,
    )


class InventoryTests(unittest.IsolatedAsyncioTestCase):
    def parse_stock(self, quantity):
        return parse_wb_detail_payload(
            {
                "data": {
                    "products": [
                        {
                            "id": 12345,
                            "totalQuantity": quantity,
                            "sizes": [
                                {
                                    "price": {"product": 10000, "basic": 15000},
                                    "stocks": [{"qty": quantity}],
                                }
                            ],
                        }
                    ]
                }
            },
            sku="12345",
        )

    def test_wb_parser_preserves_reported_total_quantity(self):
        self.assertEqual(self.parse_stock(2).total_quantity, 2)
        self.assertEqual(self.parse_stock(3).total_quantity, 3)

    def test_wb_parser_falls_back_to_sum_of_stock_entries(self):
        payload = {
            "data": {
                "products": [
                    {
                        "id": 12345,
                        "sizes": [
                            {
                                "price": {"product": 10000},
                                "stocks": [{"qty": 1}, {"qty": 2}],
                            }
                        ],
                    }
                ]
            }
        }
        self.assertEqual(parse_wb_detail_payload(payload, sku="12345").total_quantity, 3)

    def test_merge_prefers_larger_known_wb_quantity_across_regions(self):
        merged = merge_card_data(
            ProductCardData(total_quantity=1),
            ProductCardData(total_quantity=4),
        )
        self.assertEqual(merged.total_quantity, 4)

    def test_monthly_discount_calculation_and_unknown_price(self):
        discounted = make_product(
            "Ozon",
            price_byn=70,
            avg_month_price_byn=100,
        )
        self.assertEqual(discounted.discount_percent, 30)
        unavailable = make_product("Ozon", price_byn=70)
        self.assertIsNone(unavailable.discount_percent)

    def test_test_channel_is_configured_without_printing_its_value(self):
        with patch.dict(
            os.environ,
            {
                "TG_API_ID": "1",
                "TG_API_HASH": "hash",
                "BOT_TOKEN": "token",
                "ADMIN_ID": "2",
                "TG_TARGET_CHANNEL": "main",
                "TG_TEST_CHANNEL": "test",
            },
        ):
            settings = Settings.from_env()
        self.assertEqual(settings.test_channel, "test")

    async def test_forwarded_admin_post_is_forced_to_test_channel(self):
        queued = AsyncMock()
        response = AsyncMock()
        admin = SimpleNamespace(
            settings=SimpleNamespace(test_channel="configured"),
            enqueue=queued,
        )
        event = SimpleNamespace(
            message=SimpleNamespace(fwd_from=object(), id=12),
            chat_id=34,
            respond=response,
        )

        handled = await AdminMixin.enqueue_admin_test_message(admin, event)

        self.assertTrue(handled)
        queued.assert_awaited_once()
        self.assertEqual(queued.await_args.args[0].target_channel, "test")
        response.assert_awaited_once()

    async def test_admin_marketplace_link_is_queued_for_test_channel(self):
        queued = AsyncMock()
        response = AsyncMock()
        admin = SimpleNamespace(
            settings=SimpleNamespace(test_channel="configured"),
            enqueue=queued,
        )
        message = SimpleNamespace(
            fwd_from=None,
            raw_text="https://ozon.by/t/Axo0YGA",
            entities=[],
            id=13,
        )
        event = SimpleNamespace(message=message, chat_id=34, respond=response)

        handled = await AdminMixin.enqueue_admin_test_message(admin, event)

        self.assertTrue(handled)
        queued.assert_awaited_once()
        job = queued.await_args.args[0]
        self.assertEqual(job.messages, [message])
        self.assertEqual(job.target_channel, "test")
        response.assert_awaited_once()

    async def test_non_marketplace_admin_message_is_not_queued(self):
        queued = AsyncMock()
        admin = SimpleNamespace(
            settings=SimpleNamespace(test_channel="configured"),
            enqueue=queued,
        )
        message = SimpleNamespace(
            fwd_from=None,
            raw_text="Добрый вечер",
            entities=[],
            id=14,
        )
        event = SimpleNamespace(message=message, chat_id=34, respond=AsyncMock())

        handled = await AdminMixin.enqueue_admin_test_message(admin, event)

        self.assertFalse(handled)
        queued.assert_not_awaited()

    async def test_wb_enrichment_rejects_below_three_and_accepts_three(self):
        class FakeMarketData:
            settings = SimpleNamespace(
                require_sku=True,
                skip_out_of_stock=True,
                require_price=True,
            )
            state = SimpleNamespace(cards_parsed=0)
            quantity = 2

            async def fetch_wb_card_data(self, *args, **kwargs):
                return ProductCardData(
                    in_stock=True,
                    total_quantity=self.quantity,
                    price_byn=10,
                )

        fake = FakeMarketData()
        self.assertIsNone(await MarketDataMixin.enrich_product_with_card(fake, make_product()))
        fake.quantity = 3
        self.assertIsNotNone(await MarketDataMixin.enrich_product_with_card(fake, make_product()))


class PersistenceAndSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(handle)
        self.db = DuplicateDB(self.db_path, cooldown_hours=48, pending_ttl_minutes=20)

    async def asyncTearDown(self):
        self.db.close()
        os.unlink(self.db_path)

    def test_admin_settings_persist(self):
        self.db.set_setting("quiet_hours_start", "22:15")
        self.db.set_setting("quiet_hours_end", "06:45")
        self.db.set_setting("ozon_discount_filter", "false")
        self.assertEqual(self.db.get_setting("quiet_hours_start", "01:30"), "22:15")
        self.assertEqual(self.db.get_setting("quiet_hours_end", "07:00"), "06:45")
        self.assertEqual(self.db.get_setting("ozon_discount_filter", "true"), "false")

    async def test_old_published_post_table_is_migrated(self):
        self.db.close()
        connection = sqlite3.connect(self.db_path)
        connection.execute("DROP TABLE published_posts")
        connection.execute(
            """
            CREATE TABLE published_posts (
                sku_key TEXT PRIMARY KEY,
                product_json TEXT NOT NULL,
                publication_text TEXT NOT NULL,
                message_ids TEXT NOT NULL,
                published_at INTEGER NOT NULL,
                next_check_at INTEGER NOT NULL,
                discount_percent REAL,
                status TEXT NOT NULL DEFAULT 'active'
            )
            """
        )
        product = make_product("Ozon", "legacy")
        connection.execute(
            "INSERT INTO published_posts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                product.sku_key,
                json.dumps(product.__dict__),
                "legacy post",
                "[900]",
                1000,
                1900,
                55,
                "active",
            ),
        )
        connection.commit()
        connection.close()

        self.db = DuplicateDB(self.db_path, cooldown_hours=48, pending_ttl_minutes=20)

        migrated = self.db.get_due_published_posts(1900)
        self.assertEqual(migrated[0]["post_id"], "main:900")
        self.assertEqual(migrated[0]["target_channel"], "main")

    def test_scheduled_reservation_survives_pending_ttl(self):
        product = make_product()
        self.db.reserve_fresh([product], 1, 2)
        self.db.save_scheduled_publication(product, "source", 1, 1)
        self.db._conn.execute("UPDATE seen_items SET created_at = 1")
        self.assertEqual(self.db.reserve_fresh([product], 1, 3), set())
        self.assertEqual(len(self.db.get_due_scheduled_publications(2)), 1)

    def test_published_post_first_check_is_after_fifteen_minutes(self):
        product = make_product("Ozon", price_byn=40, avg_month_price_byn=100)
        self.db.reserve_fresh([product], 1, 2)
        self.db.save_published_post(product, "post", [44], 60, published_at=1000)
        due = self.db.get_due_published_posts(1899)
        self.assertEqual(due, [])
        due = self.db.get_due_published_posts(1900)
        self.assertEqual(due[0]["message_ids"], "[44]")
        self.assertEqual(due[0]["discount_percent"], 60)

    def test_multiple_posts_for_same_sku_keep_independent_message_tracking(self):
        product = make_product()
        now = int(time.time())
        self.db.reserve_fresh([product], 1, 2)
        self.db.save_published_post(product, "first", [51], None, published_at=now - 49 * 3600)
        self.db._conn.execute(
            "UPDATE seen_items SET published_at = ? WHERE sku_key = ?",
            (now - 49 * 3600, product.sku_key),
        )
        self.assertEqual(self.db.reserve_fresh([product], 1, 3), {product.sku_key})
        self.db.save_published_post(product, "second", [52], None, published_at=now)

        due = self.db.get_due_published_posts(now + 900, limit=10)
        self.assertEqual({row["post_id"] for row in due}, {"main:51", "main:52"})

    def test_quiet_period_uses_configured_timezone_and_handles_midnight(self):
        parser = SimpleNamespace(
            settings=SimpleNamespace(timezone="Europe/Minsk"),
            db=self.db,
        )
        now = datetime(2026, 11, 1, 1, 45, tzinfo=ZoneInfo("Europe/Minsk")).timestamp()
        self.assertEqual(PipelineMixin.quiet_period_remaining(parser, now), 5 * 3600 + 15 * 60)
        after = datetime(2026, 11, 1, 7, 0, tzinfo=ZoneInfo("Europe/Minsk")).timestamp()
        self.assertEqual(PipelineMixin.quiet_period_remaining(parser, after), 0)

        self.db.set_setting("quiet_hours_start", "22:00")
        self.db.set_setting("quiet_hours_end", "06:00")
        overnight = datetime(2026, 11, 1, 23, 0, tzinfo=ZoneInfo("Europe/Minsk")).timestamp()
        self.assertEqual(PipelineMixin.quiet_period_remaining(parser, overnight), 7 * 3600)

    async def test_unavailable_check_error_does_not_change_post(self):
        product = make_product("Ozon", sku="oz-1")
        self.db.reserve_fresh([product], 1, 2)
        self.db.save_published_post(product, "post", [44], 60, published_at=1000)
        row = self.db.get_due_published_posts(1900)[0]
        self.db.defer_published_post_check = MagicMock(wraps=self.db.defer_published_post_check)
        client = SimpleNamespace(edit_message=AsyncMock(), delete_messages=AsyncMock())
        parser = SimpleNamespace(
            target_entity="target",
            user_client=client,
            db=self.db,
            record_admin_error=lambda message: None,
        )
        parser.get_channel_entity = AsyncMock(return_value="target")

        await PipelineMixin.handle_published_post_check(parser, row, product, None, 1900)

        self.db.defer_published_post_check.assert_called_once_with("main:44", 1900 + 15 * 60)
        client.edit_message.assert_not_awaited()
        client.delete_messages.assert_not_awaited()

    async def test_unavailable_high_discount_post_is_marked(self):
        product = make_product("Ozon", sku="oz-high")
        self.db.reserve_fresh([product], 1, 2)
        self.db.save_published_post(product, "post body", [44, 45], 55, published_at=1000)
        row = self.db.get_due_published_posts(1900)[0]
        client = SimpleNamespace(edit_message=AsyncMock(), delete_messages=AsyncMock())
        parser = SimpleNamespace(
            target_entity="target",
            user_client=client,
            db=self.db,
            record_admin_error=lambda message: None,
        )
        parser.get_channel_entity = AsyncMock(return_value="target")

        await PipelineMixin.handle_published_post_check(
            parser,
            row,
            product,
            ProductCardData(in_stock=False),
            1900,
        )

        client.edit_message.assert_awaited_once()
        client.delete_messages.assert_awaited_once_with("target", [45])
        status = self.db._conn.execute(
            "SELECT status FROM published_posts WHERE post_id = ?",
            ("main:44",),
        ).fetchone()
        self.assertEqual(status[0], "unavailable_marked")

    async def test_unavailable_low_discount_post_is_deleted(self):
        product = make_product("Ozon", sku="oz-low")
        self.db.reserve_fresh([product], 1, 2)
        self.db.save_published_post(product, "post body", [46], 50, published_at=1000)
        row = self.db.get_due_published_posts(1900)[0]
        client = SimpleNamespace(edit_message=AsyncMock(), delete_messages=AsyncMock())
        parser = SimpleNamespace(
            target_entity="target",
            user_client=client,
            db=self.db,
            record_admin_error=lambda message: None,
        )
        parser.get_channel_entity = AsyncMock(return_value="target")

        await PipelineMixin.handle_published_post_check(
            parser,
            row,
            product,
            ProductCardData(in_stock=False),
            1900,
        )

        client.delete_messages.assert_awaited_once_with("target", [46])
        client.edit_message.assert_not_awaited()
        status = self.db._conn.execute(
            "SELECT status FROM published_posts WHERE post_id = ?",
            ("main:46",),
        ).fetchone()
        self.assertEqual(status[0], "deleted")

    async def test_ozon_filter_threshold_and_admin_toggle(self):
        parser = type("Pipeline", (PipelineMixin,), {"db": self.db})()
        self.db.set_setting("ozon_discount_filter", "true")
        exactly_thirty = make_product("Ozon", "oz-30", price_byn=70, avg_month_price_byn=100)
        below_thirty = make_product("Ozon", "oz-29", price_byn=71, avg_month_price_byn=100)
        self.assertTrue(PipelineMixin.passes_ozon_discount_filter(parser, exactly_thirty))
        self.assertFalse(PipelineMixin.passes_ozon_discount_filter(parser, below_thirty))
        unknown = make_product("Ozon", "oz-unknown-on", price_byn=70)
        self.assertTrue(PipelineMixin.passes_ozon_discount_filter(parser, unknown))

        self.db.set_setting("ozon_discount_filter", "false")
        unknown = make_product("Ozon", "oz-unknown", price_byn=70)
        self.assertTrue(PipelineMixin.passes_ozon_discount_filter(parser, unknown))

    async def test_wb_batch_uses_one_request_for_many_skus(self):
        class FakeMarketData(MarketDataMixin):
            settings = SimpleNamespace(wb_dest_byn=-59208, wb_spp=30)
            _rub_to_byn_rate = 0.0345
            _fetch_json_url = AsyncMock(
                return_value={
                    "data": {
                        "products": [
                            {
                                "id": sku,
                                "totalQuantity": 3,
                                "sizes": [{"price": {"product": 10000}, "stocks": [{"qty": 3}]}],
                            }
                            for sku in (101, 202)
                        ]
                    }
                }
            )

        checker = FakeMarketData()
        cards = await checker.fetch_wb_cards_batch(["101", "202", "101"])

        checker._fetch_json_url.assert_awaited_once()
        self.assertEqual(set(cards), {"101", "202"})
        self.assertEqual(cards["101"].total_quantity, 3)


if __name__ == "__main__":
    unittest.main()
