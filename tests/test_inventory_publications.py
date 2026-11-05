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

from telethon.tl.types import MessageEntityStrike, MessageEntityUrl
from tg_marketplace_parser.market_data import MarketDataMixin
from tg_marketplace_parser.message_parsing import extract_product_url_from_text
from tg_marketplace_parser.config import Settings
from tg_marketplace_parser.admin import AdminMixin
from tg_marketplace_parser.common import (
    convert_image_bytes_to_jpeg,
    has_expanded_product_url,
    has_product_sku,
    ozon_product_path,
)
from tg_marketplace_parser.marketplace_parsing import (
    merge_card_data,
    parse_wb_detail_payload,
)
from tg_marketplace_parser.models import ProductCardData, ProductInfo
from tg_marketplace_parser.pipeline import PipelineMixin
from tg_marketplace_parser.presentation import build_publication_text, parse_product_info
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
    def test_ozon_sold_out_redirect_path_is_recognized_as_product(self):
        url = "https://ozon.ru/p/o4279370?short=cF1FVro"

        self.assertTrue(has_product_sku(url))
        self.assertFalse(has_expanded_product_url(url))
        product = parse_product_info(
            "https://ozon.by/t/cF1FVro",
            url,
            canonical_host_override="ozon.by",
        )
        self.assertEqual(product.platform, "Ozon")
        self.assertEqual(product.sku, "o4279370")
        self.assertEqual(product.sku_key, "ozon:short:o4279370")
        self.assertEqual(product.canonical_url, "https://ozon.by/p/o4279370")
        self.assertEqual(ozon_product_path(product.sku), "/p/o4279370")
        self.assertEqual(
            ozon_product_path(
                "1440302046",
                "https://ozon.by/product/1440302046/",
            ),
            "/product/1440302046/",
        )
        self.assertEqual(
            ozon_product_path(
                "1440302046",
                "https://example.com/product/1440302046/",
            ),
            "/product/1440302046/",
        )
        self.assertIsNone(
            extract_product_url_from_text(
                '<link rel="canonical" href="https://ozon.ru/p/o4279370?short=cF1FVro">',
                "https://ozon.ru/",
            )
        )

    async def test_unexpanded_ozon_product_path_is_not_returned_for_publication(self):
        short_url = "https://ozon.ru/p/o4279370?short=cF1FVro"

        class FakeMarketData(MarketDataMixin):
            settings = SimpleNamespace(max_redirects=0)
            curl_session = None
            http_session = object()
            state = SimpleNamespace(http_resolved=0, browser_resolved=0)
            host_has_only_public_ips = AsyncMock(return_value=True)
            _request_step = AsyncMock(return_value=(403, None, None, b""))
            upgrade_to_product_url = AsyncMock(return_value=short_url)

        parser = FakeMarketData()
        resolved = await MarketDataMixin.resolve_url(
            parser,
            short_url,
        )

        self.assertIsNone(resolved)
        parser.upgrade_to_product_url.assert_awaited_once()

    def test_publication_text_omits_unexpanded_ozon_links(self):
        product = parse_product_info(
            "https://ozon.by/t/cF1FVro",
            "https://ozon.ru/p/o4279370",
            canonical_host_override="ozon.by",
        )

        self.assertEqual(build_publication_text("", [product]), "")

    def test_publication_text_keeps_expanded_ozon_links(self):
        product = parse_product_info(
            "https://ozon.by/t/cF1FVro",
            "https://ozon.ru/product/1440302046/",
            canonical_host_override="ozon.by",
        )

        text = build_publication_text("", [product])
        self.assertIn("https://ozon.by/product/1440302046/", text)

    async def test_ozon_card_lookup_uses_saved_canonical_product_path(self):
        requested_urls = []

        async def fetch_json(url, **kwargs):
            requested_urls.append(url)
            return {"_http_status": 404}

        parser = SimpleNamespace(
            settings=SimpleNamespace(ozon_preferred_storefronts=()),
            browser_resolver=SimpleNamespace(is_available=False),
            curl_session=None,
            http_session=object(),
            get_rub_to_byn_rate=AsyncMock(return_value=0.0345),
            _fetch_json_url=AsyncMock(side_effect=fetch_json),
        )

        card = await MarketDataMixin.fetch_ozon_card_data(
            parser,
            "1440302046",
            canonical_url="https://ozon.by/product/1440302046/",
        )

        self.assertFalse(card.in_stock)
        self.assertTrue(requested_urls)
        self.assertIn("url=/product/1440302046/", requested_urls[0])

    async def test_published_product_check_passes_its_canonical_url(self):
        fetch_ozon_card = AsyncMock(return_value=ProductCardData(in_stock=False))
        parser = SimpleNamespace(fetch_ozon_card_data=fetch_ozon_card)
        product = ProductInfo(
            original_url="https://ozon.by/t/example",
            resolved_url="https://ozon.ru/product/1440302046/",
            platform="Ozon",
            sku="1440302046",
            canonical_url="https://ozon.by/product/1440302046/",
            sku_key="ozon:1440302046",
        )

        await PipelineMixin.fetch_card_for_product(parser, product)

        fetch_ozon_card.assert_awaited_once_with(
            "1440302046",
            original_url="https://ozon.by/t/example",
            messages=(),
            canonical_url="https://ozon.by/product/1440302046/",
        )

    async def test_wb_batch_check_falls_back_when_stock_is_unknown(self):
        product = make_product()
        detailed_card = ProductCardData(in_stock=False)
        fetch_card = AsyncMock(return_value=detailed_card)
        parser = SimpleNamespace(fetch_card_for_product=fetch_card)

        result = await PipelineMixin.resolve_wb_batch_card(
            parser,
            product,
            ProductCardData(in_stock=None),
        )

        self.assertIs(result, detailed_card)
        fetch_card.assert_awaited_once_with(product, source_text="")

    async def test_wb_batch_check_does_not_repeat_confirmed_result(self):
        product = make_product()
        confirmed_card = ProductCardData(in_stock=False)
        fetch_card = AsyncMock()
        parser = SimpleNamespace(fetch_card_for_product=fetch_card)

        result = await PipelineMixin.resolve_wb_batch_card(
            parser,
            product,
            confirmed_card,
        )

        self.assertIs(result, confirmed_card)
        fetch_card.assert_not_awaited()

    async def test_wb_batch_check_falls_back_when_card_is_missing(self):
        product = make_product()
        detailed_card = ProductCardData(in_stock=True)
        fetch_card = AsyncMock(return_value=detailed_card)
        parser = SimpleNamespace(fetch_card_for_product=fetch_card)

        result = await PipelineMixin.resolve_wb_batch_card(parser, product, None)

        self.assertIs(result, detailed_card)
        fetch_card.assert_awaited_once_with(product, source_text="")

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

    def test_admin_keyboard_exposes_persistent_quiet_hours_toggle(self):
        settings = {
            "quiet_hours_enabled": "true",
            "ozon_discount_filter": "true",
            "publication_target": "main",
        }
        admin = SimpleNamespace(
            state=SimpleNamespace(is_paused=False),
            settings=SimpleNamespace(test_channel="test"),
            db=SimpleNamespace(
                get_setting=lambda key, default: settings.get(key, default),
            ),
        )

        keyboard = AdminMixin.get_admin_keyboard(admin)
        button = next(
            button
            for row in keyboard
            for button in row
            if button.type.data == b"toggle_quiet_hours"
        )

        self.assertIn("ВКЛ", button.text)

    def test_admin_keyboard_exposes_wb_minimum_stock_toggle(self):
        settings = {"wb_min_stock_filter": "true"}
        admin = SimpleNamespace(
            state=SimpleNamespace(is_paused=False),
            settings=SimpleNamespace(test_channel=None),
            db=SimpleNamespace(
                get_setting=lambda key, default: settings.get(key, default),
            ),
        )

        keyboard = AdminMixin.get_admin_keyboard(admin)
        button = next(
            button
            for row in keyboard
            for button in row
            if button.type.data == b"toggle_wb_stock_filter"
        )

        self.assertIn("ВКЛ", button.text)

    async def test_wb_enrichment_rejects_below_three_and_accepts_three(self):
        class FakeMarketData:
            settings = SimpleNamespace(
                require_sku=True,
                skip_out_of_stock=True,
                require_price=True,
            )
            state = SimpleNamespace(cards_parsed=0)
            quantity = 2
            db = SimpleNamespace(get_setting=lambda key, default: "true")

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

    async def test_wb_minimum_stock_filter_can_be_disabled(self):
        class FakeMarketData:
            settings = SimpleNamespace(
                require_sku=True,
                skip_out_of_stock=True,
                require_price=True,
            )
            state = SimpleNamespace(cards_parsed=0)
            db = SimpleNamespace(get_setting=lambda key, default: "false")

            async def fetch_wb_card_data(self, *args, **kwargs):
                return ProductCardData(in_stock=True, total_quantity=2, price_byn=10)

        enriched = await MarketDataMixin.enrich_product_with_card(
            FakeMarketData(),
            make_product(),
        )

        self.assertIsNotNone(enriched)
        self.assertEqual(enriched.total_quantity, 2)


class PersistenceAndSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(handle)
        self.db = DuplicateDB(self.db_path, cooldown_hours=48, pending_ttl_minutes=20)

    async def asyncTearDown(self):
        self.db.close()
        os.unlink(self.db_path)

    def test_admin_settings_persist(self):
        self.assertEqual(self.db.get_setting("wb_min_stock_filter", "true"), "true")
        self.assertEqual(self.db.get_setting("quiet_hours_enabled", "true"), "true")
        self.db.set_setting("quiet_hours_start", "22:15")
        self.db.set_setting("quiet_hours_end", "06:45")
        self.db.set_setting("quiet_hours_enabled", "false")
        self.db.set_setting("ozon_discount_filter", "false")
        self.db.set_setting("wb_min_stock_filter", "false")
        self.assertEqual(self.db.get_setting("quiet_hours_start", "01:30"), "22:15")
        self.assertEqual(self.db.get_setting("quiet_hours_end", "07:00"), "06:45")
        self.assertEqual(self.db.get_setting("quiet_hours_enabled", "true"), "false")
        self.assertEqual(self.db.get_setting("ozon_discount_filter", "true"), "false")
        self.assertEqual(self.db.get_setting("wb_min_stock_filter", "true"), "false")

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
        self.assertEqual(migrated[0]["check_interval_seconds"], 3600)

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
        self.assertEqual(due[0]["check_interval_seconds"], 3600)

    async def test_stock_check_interval_doubles_and_stops_before_auto_deletion(self):
        product = make_product("Ozon", price_byn=40, avg_month_price_byn=100)
        self.db.reserve_fresh([product], 1, 2)
        self.db.save_published_post(product, "post", [44], 60, published_at=1000)
        client = SimpleNamespace(edit_message=AsyncMock(), delete_messages=AsyncMock())
        parser = SimpleNamespace(
            user_client=client,
            db=self.db,
            record_admin_error=lambda message: None,
        )
        parser.get_channel_entity = AsyncMock(return_value="target")

        for checked_at, expected_next_at, expected_interval in (
            (1900, 5500, 2 * 3600),
            (5500, 12700, 4 * 3600),
            (12700, 27100, 8 * 3600),
            (27100, 55900, 8 * 3600),
            (55900, 84700, 8 * 3600),
        ):
            row = self.db.get_due_published_posts(checked_at)[0]
            await PipelineMixin.handle_published_post_check(
                parser,
                row,
                product,
                ProductCardData(in_stock=True),
                checked_at,
            )
            next_row = self.db.get_due_published_posts(expected_next_at)
            self.assertEqual(next_row[0]["next_check_at"], expected_next_at)
            self.assertEqual(next_row[0]["check_interval_seconds"], expected_interval)

        row = self.db.get_due_published_posts(84700)[0]
        await PipelineMixin.handle_published_post_check(
            parser,
            row,
            product,
            ProductCardData(in_stock=True),
            84700,
        )
        status = self.db._conn.execute(
            "SELECT status FROM published_posts WHERE post_id = ?",
            ("main:44",),
        ).fetchone()
        self.assertEqual(status[0], "expired")
        self.assertEqual(
            self.db.get_due_published_posts(100000),
            [],
        )

    def test_auto_deleted_posts_are_removed_from_check_schedule(self):
        product = make_product()
        self.db.reserve_fresh([product], 1, 2)
        self.db.save_published_post(product, "post", [47], None, published_at=1000)

        expired = self.db.expire_old_published_posts(1000 + 24 * 3600)

        self.assertEqual(expired, 1)
        self.assertEqual(self.db.get_due_published_posts(1000 + 24 * 3600), [])

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
        self.db.set_setting("quiet_hours_enabled", "false")
        self.assertEqual(PipelineMixin.quiet_period_remaining(parser, overnight), 0)

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
        publication_text = f"🔷 Ozon\n\n🔗 {product.canonical_url}"
        self.db.reserve_fresh([product], 1, 2)
        self.db.save_published_post(product, publication_text, [44, 45], 55, published_at=1000)
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
        edit_call = client.edit_message.await_args
        edited_text = edit_call.args[2]
        self.assertTrue(edited_text.endswith("❌ Неактуально"))
        url_offset = publication_text.index(product.canonical_url)
        expected_offset = len(publication_text[:url_offset].encode("utf-16-le")) // 2
        expected_length = len(product.canonical_url.encode("utf-16-le")) // 2
        self.assertEqual(
            edit_call.kwargs["formatting_entities"],
            [
                MessageEntityUrl(offset=expected_offset, length=expected_length),
                MessageEntityStrike(offset=expected_offset, length=expected_length),
            ],
        )
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

    def test_convert_image_bytes_to_jpeg_rejects_truncated_jpeg(self):
        import io
        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (64, 64), (120, 180, 240)).save(buf, format="JPEG", quality=90)
        valid_jpeg = buf.getvalue()

        converted = convert_image_bytes_to_jpeg(valid_jpeg)
        self.assertIsNotNone(converted)
        self.assertTrue(converted.getvalue().endswith(b"\xff\xd9"))

        truncated_jpeg = valid_jpeg[:-20]
        self.assertIsNone(convert_image_bytes_to_jpeg(truncated_jpeg))

    async def test_download_product_image_retries_when_first_response_is_truncated(self):
        import asyncio
        import io
        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (64, 64), (200, 100, 50)).save(buf, format="JPEG", quality=90)
        valid_jpeg = buf.getvalue()
        truncated_jpeg = valid_jpeg[: len(valid_jpeg) // 2]

        responses = [
            SimpleNamespace(
                status_code=200,
                content=truncated_jpeg,
                headers={"Content-Length": str(len(valid_jpeg))},
            ),
            SimpleNamespace(
                status_code=200,
                content=valid_jpeg,
                headers={"Content-Length": str(len(valid_jpeg))},
            ),
        ]

        class FakeMarketData(MarketDataMixin):
            settings = SimpleNamespace(request_timeout_seconds=5)
            resolve_semaphore = asyncio.Semaphore(2)
            curl_session = SimpleNamespace(get=AsyncMock(side_effect=responses))
            http_session = None

        downloader = FakeMarketData()
        result = await downloader.download_product_image(
            "https://ir.ozone.ru/s3/multimedia-1/wc1000/12345.jpg"
        )

        self.assertIsNotNone(result)
        self.assertEqual(downloader.curl_session.get.await_count, 2)


if __name__ == "__main__":
    unittest.main()
