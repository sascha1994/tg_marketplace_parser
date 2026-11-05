import json
import logging
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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


if __name__ == "__main__":
    unittest.main()
