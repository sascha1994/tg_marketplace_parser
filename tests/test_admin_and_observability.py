import json
import logging
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telethon.errors import FloodWaitError
from telethon.tl.types import PeerChannel
from tg_marketplace_parser import thresholds
from tg_marketplace_parser.admin import AdminMixin
from tg_marketplace_parser.browser import OzonBrowserResolver
from tg_marketplace_parser.browser_ozon import OzonBrowserMixin
from tg_marketplace_parser.browser_wb import WildberriesBrowserMixin
from tg_marketplace_parser.entrypoint import JsonLogFormatter, configure_logging
from tg_marketplace_parser.http_client import HttpClientMixin
from tg_marketplace_parser.images import ImageDownloadMixin
from tg_marketplace_parser.market_data import MarketDataMixin
from tg_marketplace_parser.models import ParserState, ProductCardData, ProductInfo, record_cascade
from tg_marketplace_parser.ozon import OzonMixin
from tg_marketplace_parser.pipeline import PipelineMixin
from tg_marketplace_parser.runtime import RuntimeMixin
from tg_marketplace_parser.sources import SourcesMixin
from tg_marketplace_parser.storage import DuplicateDB
from tg_marketplace_parser.wildberries import WildberriesMixin


class RefactoringStructureTests(unittest.TestCase):
    def test_market_data_mixin_inherits_split_mixins(self):
        self.assertTrue(issubclass(MarketDataMixin, HttpClientMixin))
        self.assertTrue(issubclass(MarketDataMixin, ImageDownloadMixin))
        self.assertTrue(issubclass(MarketDataMixin, WildberriesMixin))
        self.assertTrue(issubclass(MarketDataMixin, OzonMixin))

    def test_browser_resolver_inherits_split_mixins(self):
        self.assertTrue(issubclass(OzonBrowserResolver, OzonBrowserMixin))
        self.assertTrue(issubclass(OzonBrowserResolver, WildberriesBrowserMixin))


class AdminAndObservabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(handle)
        self.db = DuplicateDB(self.db_path, cooldown_hours=48, pending_ttl_minutes=20)

    async def asyncTearDown(self):
        self.db.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db_path + suffix)
            except OSError:
                pass

    def make_admin(self):
        admin = type("Admin", (AdminMixin,), {})()
        admin.db = self.db
        admin.state = ParserState()
        admin.settings = SimpleNamespace(
            dry_run=False,
            test_channel="test_chan",
            timezone="Europe/Minsk",
            ozon_min_discount_percent=30.0,
            wb_min_stock=3,
        )
        return admin

    def test_admin_block_commands_and_blocklist_view(self):
        admin = self.make_admin()
        self.assertIn("пуст", admin.get_blocklist_text())

        reply1 = admin.handle_admin_block_command("/block brand BadBrand")
        self.assertIn("Добавлено", reply1)
        reply2 = admin.handle_admin_block_command("/block keyword копия")
        self.assertIn("Добавлено", reply2)

        listing = admin.handle_admin_block_command("/blocklist")
        self.assertIn("badbrand", listing)
        self.assertIn("копия", listing)

        reply3 = admin.handle_admin_block_command("/unblock brand badbrand")
        self.assertIn("Удалено", reply3)
        self.assertIsNone(admin.handle_admin_block_command("обычный текст"))

    def test_admin_source_and_cascade_views(self):
        admin = self.make_admin()
        self.assertIn("пуста", admin.get_source_stats_text())
        self.assertIn("пуста", admin.get_cascade_stats_text())

        self.db.bump_source_stat(777, "received", 10)
        self.db.bump_source_stat(777, "published", 4)
        self.assertIn("777", admin.get_source_stats_text())

        record_cascade(admin, "ozon_api", True)
        record_cascade(admin, "ozon_api", False)
        cascade_text = admin.get_cascade_stats_text()
        self.assertIn("ozon_api: 1/2 (50%)", cascade_text)

    def test_json_log_formatter_outputs_valid_json(self):
        formatter = JsonLogFormatter()
        record = logging.LogRecord(
            name="test_logger",
            level=logging.INFO,
            pathname=__file__,
            lineno=10,
            msg="Привет %s",
            args=("мир",),
            exc_info=None,
        )
        parsed = json.loads(formatter.format(record))
        self.assertEqual(parsed["level"], "INFO")
        self.assertEqual(parsed["logger"], "test_logger")
        self.assertEqual(parsed["message"], "Привет мир")
        self.assertIn("timestamp", parsed)

        with patch.dict(os.environ, {"LOG_FORMAT": "json", "LOG_LEVEL": "INFO"}):
            configure_logging()
            root = logging.getLogger()
            self.assertTrue(any(isinstance(h.formatter, JsonLogFormatter) for h in root.handlers))
        with patch.dict(os.environ, {"LOG_FORMAT": "text", "LOG_LEVEL": "INFO"}):
            configure_logging()

    async def test_check_wb_batch_resolves_unknown_stock_items(self):
        p1 = ProductInfo(
            original_url="https://www.wildberries.by/catalog/111/detail.aspx",
            resolved_url="https://www.wildberries.by/catalog/111/detail.aspx",
            platform="Wildberries",
            sku="111",
            canonical_url="https://www.wildberries.by/catalog/111/detail.aspx",
            sku_key="wildberries:111",
        )
        p2 = ProductInfo(
            original_url="https://www.wildberries.by/catalog/222/detail.aspx",
            resolved_url="https://www.wildberries.by/catalog/222/detail.aspx",
            platform="Wildberries",
            sku="222",
            canonical_url="https://www.wildberries.by/catalog/222/detail.aspx",
            sku_key="wildberries:222",
        )
        rows = [
            {"sku_key": p1.sku_key, "product_json": json.dumps(p1.__dict__), "source_text": "t1"},
            {"sku_key": p2.sku_key, "product_json": json.dumps(p2.__dict__), "source_text": "t2"},
        ]

        parser = type("Pipeline", (PipelineMixin,), {})()
        parser.fetch_wb_cards_batch = AsyncMock(
            return_value={
                "111": ProductCardData(in_stock=True, price_byn=15.0),
                "222": ProductCardData(in_stock=None),
            }
        )
        parser.resolve_wb_batch_card = AsyncMock(
            return_value=ProductCardData(in_stock=False)
        )
        parser.record_admin_error = lambda msg: None

        results = await parser.check_wb_batch(
            rows,
            batch_error_label="Пакетная проверка WB",
            item_error_label="Индивидуальная проверка WB",
            include_source_text=True,
        )
        self.assertEqual(len(results), 2)
        self.assertTrue(results[0][2].in_stock)
        self.assertFalse(results[1][2].in_stock)
        parser.resolve_wb_batch_card.assert_awaited_once()

    async def test_fetch_folder_channels_includes_pinned_peers_with_dedup(self):
        from telethon.utils import get_peer_id

        dialog_filter = SimpleNamespace(
            title=SimpleNamespace(text="Скидки"),
            include_peers=[PeerChannel(101), PeerChannel(202)],
            pinned_peers=[PeerChannel(202), PeerChannel(303), PeerChannel(999)],
        )
        source_owner = type("Sources", (SourcesMixin,), {})()
        source_owner.settings = SimpleNamespace(folder_name="Скидки")
        source_owner.target_peer_id = get_peer_id(PeerChannel(999))
        source_owner.user_client = AsyncMock(return_value=SimpleNamespace(filters=[dialog_filter]))

        channels = await source_owner.fetch_folder_channels()
        self.assertEqual(
            channels,
            [
                get_peer_id(PeerChannel(101)),
                get_peer_id(PeerChannel(202)),
                get_peer_id(PeerChannel(303)),
            ],
        )

    async def test_runtime_shutdown_disconnects_clients(self):
        runtime = type("Runtime", (RuntimeMixin,), {})()
        runtime.user_client = SimpleNamespace(disconnect=AsyncMock())
        runtime.bot_client = SimpleNamespace(disconnect=AsyncMock())
        runtime._register_signal_handlers()
        await runtime.shutdown("SIGTERM")
        runtime.user_client.disconnect.assert_awaited_once()
        runtime.bot_client.disconnect.assert_awaited_once()

    async def test_wb_circuit_breaker_and_fallback_price_toggle(self):
        class FakeWb(MarketDataMixin):
            settings = SimpleNamespace(wb_dest_byn=-59208, wb_spp=30, wb_allow_fallback_price=True)
            state = ParserState()
            browser_resolver = SimpleNamespace(is_available=False)
            user_client = SimpleNamespace(is_connected=lambda: False)
            get_rub_to_byn_rate = AsyncMock(return_value=0.0345)
            _fetch_wb_basket_card_extras = AsyncMock(
                return_value=("Кроссовки", "Nike", 120.0, 75.0, "https://img", 4.9, 25, True)
            )
            _fetch_json_url = AsyncMock(return_value={"_http_status": 403})

        wb = FakeWb()
        wb.db = self.db
        card = await wb.fetch_wb_card_data("555", source_text="Супер цена")
        self.assertIsNotNone(card)
        self.assertEqual(card.price_byn, 75.0)
        self.assertEqual(card.price_source, "fallback")
        self.assertTrue(wb._is_wb_host_blocked("card.wb.ru"))

        # При повторном вызове заблокированный card.wb.ru пропускается в пакетной проверке
        wb._fetch_json_url.reset_mock()
        batch_res = await wb.fetch_wb_cards_batch(["555"])
        self.assertEqual(batch_res, {"555": None})
        wb._fetch_json_url.assert_not_awaited()

        # Отключение резервной цены в настройках БД запрещает fallback
        self.db.set_setting("wb_allow_fallback_price", "false")
        card_no_fallback = await wb.fetch_wb_card_data("555", source_text="Супер цена")
        self.assertIsNotNone(card_no_fallback)
        self.assertIsNone(card_no_fallback.price_byn)

    async def test_flood_wait_in_published_post_check_defers_by_flood_seconds(self):
        product = ProductInfo(
            original_url="https://ozon.by/product/123456/",
            resolved_url="https://ozon.by/product/123456/",
            platform="Ozon",
            sku="123456",
            canonical_url="https://ozon.by/product/123456/",
            sku_key="ozon:123456",
            price_byn=50.0,
            avg_month_price_byn=100.0,
        )
        self.db.reserve_fresh([product], 1, 10)
        self.db.save_published_post(product, "post", [77], 50.0, published_at=1000)
        row = self.db.get_due_published_posts(1900)[0]

        flood_err = FloodWaitError(request=None, capture=1200)
        client = SimpleNamespace(
            edit_message=AsyncMock(side_effect=flood_err),
            delete_messages=AsyncMock(side_effect=flood_err),
        )
        parser = SimpleNamespace(
            user_client=client,
            db=self.db,
            _last_ozon_stock_check_at=0.0,
            record_admin_error=lambda msg: None,
            get_channel_entity=AsyncMock(return_value="target"),
        )
        await PipelineMixin.handle_published_post_check(
            parser,
            row,
            product,
            ProductCardData(in_stock=False),
            1900,
        )
        next_check = self.db._conn.execute(
            "SELECT next_check_at FROM published_posts WHERE post_id = 'main:77'"
        ).fetchone()[0]
        self.assertEqual(next_check, 1900 + 1205)
        self.assertGreater(parser._last_ozon_stock_check_at, 0.0)

    async def test_due_posts_prioritize_higher_discount_and_counts_work(self):
        p_low = ProductInfo(
            original_url="https://ozon.by/product/1001/",
            resolved_url="https://ozon.by/product/1001/",
            platform="Ozon",
            sku="1001",
            canonical_url="https://ozon.by/product/1001/",
            sku_key="ozon:1001",
        )
        p_high = ProductInfo(
            original_url="https://ozon.by/product/1002/",
            resolved_url="https://ozon.by/product/1002/",
            platform="Ozon",
            sku="1002",
            canonical_url="https://ozon.by/product/1002/",
            sku_key="ozon:1002",
        )
        self.db.reserve_fresh([p_low, p_high], 1, 1)
        self.db.save_published_post(p_low, "low", [11], 30.0, published_at=1000)
        self.db.save_published_post(p_high, "high", [12], 70.0, published_at=1000)

        active_cnt, due_cnt = self.db.count_published_post_checks(now=1900)
        self.assertEqual((active_cnt, due_cnt), (2, 2))
        due = self.db.get_due_published_posts(1900, limit=2)
        self.assertEqual([r["post_id"] for r in due], ["main:12", "main:11"])

    def test_admin_reset_modes_and_configurable_thresholds(self):
        admin = self.make_admin()
        admin._reset_admin_input_modes()
        self.assertFalse(admin.awaiting_price_increase_input)
        self.assertFalse(admin.awaiting_unavailable_discount_input)
        self.assertFalse(admin.awaiting_ozon_interval_input)

        self.db.set_setting("unavailable_mark_discount_percent", "65")
        self.db.set_setting("ozon_check_interval_seconds", "25")
        self.assertEqual(thresholds.unavailable_mark_discount(admin), 65.0)
        self.assertEqual(thresholds.ozon_check_interval_seconds(admin), 25)

    async def test_run_scheduled_publication_step_switches_to_wb_when_ozon_throttled(self):
        import time as time_mod

        p_oz = ProductInfo(
            original_url="https://ozon.by/product/2001/",
            resolved_url="https://ozon.by/product/2001/",
            platform="Ozon",
            sku="2001",
            canonical_url="https://ozon.by/product/2001/",
            sku_key="ozon:2001",
        )
        p_wb = ProductInfo(
            original_url="https://www.wildberries.by/catalog/3001/detail.aspx",
            resolved_url="https://www.wildberries.by/catalog/3001/detail.aspx",
            platform="Wildberries",
            sku="3001",
            canonical_url="https://www.wildberries.by/catalog/3001/detail.aspx",
            sku_key="wildberries:3001",
        )
        self.db.reserve_fresh([p_oz, p_wb], 1, 1)
        self.db.save_published_post(p_oz, "oz post", [201], 80.0, published_at=1000)
        self.db.save_published_post(p_wb, "wb post", [301], 40.0, published_at=1000)

        parser = type("Pipeline", (PipelineMixin,), {})()
        parser.db = self.db
        parser.settings = SimpleNamespace(ozon_check_interval_seconds=60)
        parser._last_ozon_stock_check_at = time_mod.time()
        parser.quiet_period_remaining = lambda: 0
        parser.fetch_card_for_product = AsyncMock(return_value=ProductCardData(in_stock=True, price_byn=20.0))
        parser.handle_published_post_check = AsyncMock()
        parser.record_admin_error = lambda msg: None

        sleep_sec = await parser.run_scheduled_publication_step(now=1900)
        self.assertEqual(sleep_sec, 1.0)
        parser.fetch_card_for_product.assert_awaited_once()
        checked_product = parser.fetch_card_for_product.await_args.args[0]
        self.assertEqual(checked_product.platform, "Wildberries")




if __name__ == "__main__":
    unittest.main()
