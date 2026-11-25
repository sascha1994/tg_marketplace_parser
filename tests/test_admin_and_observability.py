import asyncio
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
from tg_marketplace_parser.models import ParserState, ProductCardData, ProductInfo, record_cascade, record_stat
from tg_marketplace_parser.ozon import OzonMixin
from tg_marketplace_parser.pipeline import PipelineMixin
from tg_marketplace_parser.presentation import parse_product_info
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
        admin.queue = asyncio.Queue(maxsize=500)
        admin.synced_cookie_count = 0
        admin.browser_resolver = SimpleNamespace(is_running=False, is_available=True)
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

    async def test_publication_views_are_updated_hourly_and_preserved_when_a_post_disappears(self):
        url = "https://ozon.by/product/123456/"
        product = parse_product_info(url, url)
        now = 100_000
        self.db.save_published_post(product, "post", [77, 78], None, now - 120)
        self.db.save_published_post(product, "post", [79], None, now - 120)
        self.db.save_published_post(product, "old post", [80], None, now - 24 * 3600)
        self.db.save_published_post(product, "test post", [81], None, now - 120, "test")
        self.db.save_published_post(product, "deleted post", [82], None, now - 120)
        self.db.set_published_post_status("main:82", "deleted")

        runtime = RuntimeMixin()
        runtime.db = self.db
        runtime.target_entity = "target"
        runtime.user_client = SimpleNamespace(get_messages=AsyncMock(return_value=[
            SimpleNamespace(id=79, views=37), SimpleNamespace(id=77, views=0),
        ]))
        await runtime.refresh_publication_views(now)
        self.assertCountEqual(runtime.user_client.get_messages.await_args.kwargs["ids"], [77, 79])
        self.assertEqual(self.db.get_publication_view_stats(None), (4, 2, 37))

        runtime.user_client.get_messages.reset_mock()
        await runtime.refresh_publication_views(now + 3599)
        runtime.user_client.get_messages.assert_not_awaited()

        runtime.user_client.get_messages.return_value = [None, SimpleNamespace(id=77, views=50)]
        await runtime.refresh_publication_views(now + 3600)
        self.assertEqual(self.db.get_publication_view_stats(None), (4, 2, 87))

    def test_admin_publication_view_stats_by_day_and_week(self):
        admin = self.make_admin()
        url = "https://ozon.by/product/123456/"
        product = parse_product_info(url, url)
        day_start, _ = admin._get_period_bounds("day", now_ts=1_800_000_000)
        now = day_start + 3600
        self.db.save_published_post(product, "post", [77], None, now - 100)
        self.db.save_published_post(product, "unmeasured post", [78], None, now - 100)
        self.db.save_published_post(product, "yesterday's post", [79], None, now - 24 * 3600)
        self.db.save_published_post(product, "older post", [80], None, now - 10 * 24 * 3600)
        self.db.save_post_views([("main:77", 51), ("main:79", 80), ("main:80", 10)], now)
        self.db.set_published_post_status("main:79", "deleted")

        with patch("tg_marketplace_parser.admin.time.time", return_value=now):
            daily = admin.get_detailed_stats_text("day")
            weekly = admin.get_detailed_stats_text("week")
        self.assertIn("Постов за период: 2", daily)
        self.assertIn("Просмотров по последним замерам: 51", daily)
        self.assertIn("Постов с замером: 1 из 2", daily)
        self.assertIn("Постов за период: 3", weekly)
        self.assertIn("Просмотров по последним замерам: 131", weekly)
        self.assertIn("Среднее просмотров на пост с замером: 65,5", weekly)

        with patch("tg_marketplace_parser.storage.time.time", return_value=now):
            self.db.reset_stats()
        self.assertEqual(self.db.get_publication_view_stats(None), (0, 0, 0))

    def test_forum_topic_stats_separate_group_posts_from_channel_views(self):
        admin = self.make_admin()
        forum_id = -1001234567890
        admin.settings.test_forum_id = forum_id
        destination = f"forum:{forum_id}"
        url = "https://ozon.by/product/123456/"
        product = parse_product_info(url, url)
        day_start, _ = admin._get_period_bounds("day", now_ts=1_800_000_000)

        self.db.save_published_post(product, "overnight", [77], None, day_start - 60, topic="Дом и кухня")
        self.db.complete_topic_delivery("main:77", destination, [101, 102], delivered_at=day_start + 10)
        self.db.save_published_post(product, "unmeasured", [78], None, day_start + 20, topic="Дом и кухня")
        self.db.complete_topic_delivery("main:78", destination, [103], delivered_at=day_start + 25)
        self.db.save_published_post(product, "test", [77], None, day_start + 25, "test", topic="Дом и кухня")
        self.db.complete_topic_delivery("test:77", destination, [104], delivered_at=day_start + 30)
        self.db.save_published_post(product, "old copy", [105], None, day_start + 25, destination)
        self.db.save_published_post(product, "other group", [101], None, day_start + 25, "forum:-1009876543210")
        self.db.save_post_views([("main:77", 0), ("test:77", 500), (f"{destination}:101", 900)], day_start + 40)

        self.assertEqual(self.db.get_forum_topic_stats(forum_id, day_start), [
            ("Дом и кухня", 3, 2, 1, 0),
            (None, 1, 0, 0, 0),
        ])
        with patch("tg_marketplace_parser.admin.time.time", return_value=day_start + 40):
            daily = admin.get_detailed_stats_text("day")
        self.assertIn("Дом и кухня — постов в группе 3 · просмотры в основном канале 0 (замеров 1/2)", daily)
        self.assertIn("Старые публикации без сохранённой темы — постов в группе 1", daily)
        self.db.bump_stat("posts_deleted", 2, timestamp=day_start + 30)
        self.db.bump_stat("deleted_oos", 2, timestamp=day_start + 30)
        self.db.bump_stat("forum_posts_deleted", 1, timestamp=day_start + 30)
        self.db.bump_stat("forum_deleted_oos", 1, timestamp=day_start + 30)
        self.db.bump_stat("forum_posts_marked_unavailable", 2, timestamp=day_start + 30)
        self.db.bump_stat("forum_marked_price_up", 2, timestamp=day_start + 30)
        with patch("tg_marketplace_parser.admin.time.time", return_value=day_start + 40):
            daily_with_removals = admin.get_detailed_stats_text("day")
        self.assertIn("Удалено из канала: 2 (нет в наличии: 2", daily_with_removals)
        self.assertIn("Доп. группа — удалённые и неактуальные (3):", daily_with_removals)
        self.assertIn("Удалено из группы: 1 (нет в наличии: 1 · рост цены ≥15%: 0)", daily_with_removals)
        self.assertIn("Помечено «Неактуально» в группе: 2 (нет в наличии: 0 · рост цены ≥15%: 2)", daily_with_removals)
        self.db.set_setting("publication_stats_reset_at", str(day_start + 41))
        self.assertEqual(self.db.get_forum_topic_stats(forum_id, None), [])

    async def test_wb_circuit_breaker_and_fallback_price_toggle(self):
        class FakeWb(MarketDataMixin):
            settings = SimpleNamespace(wb_dest_byn=-59208, wb_spp=30, wb_allow_fallback_price=True)
            state = ParserState()
            browser_resolver = SimpleNamespace(is_available=False)
            user_client = SimpleNamespace(is_connected=lambda: False)
            get_rub_to_byn_rate = AsyncMock(return_value=0.0345)
            _fetch_wb_basket_card_extras = AsyncMock(
                return_value=("Кроссовки", "Nike", 120.0, 75.0, "https://img", 4.9, 25, True, "Обувь > Кроссовки")
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
        admin.current_sources = {1, 2}
        self.assertIn("Фильтр скидки Ozon", admin.get_filters_text())
        self.assertIn("Расписание публикаций", admin.get_schedule_text())
        self.assertIn("65%", admin.get_thresholds_text())
        self.assertIn("Мониторинг и диагностика", admin.get_monitoring_text())
        self.assertIn("Сервисное управление", admin.get_service_text())
        self.assertEqual(len(admin.get_admin_keyboard()), 4)
        self.assertEqual(len(admin.get_thresholds_keyboard()), 4)
        self.assertEqual(len(admin.get_monitoring_keyboard()), 3)
        self.assertEqual(len(admin.get_service_keyboard()), 4)

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

    def test_persistent_period_stats_filters_deletions_and_reset(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        admin = self.make_admin()
        admin.current_sources = {10, 20}
        admin.queue = SimpleNamespace(qsize=lambda: 0, maxsize=500)
        admin.browser_resolver = SimpleNamespace(is_running=True, is_available=True)
        admin.synced_cookie_count = 12

        tz = ZoneInfo("Europe/Minsk")
        now_dt = datetime(2026, 11, 10, 15, 30, 0, tzinfo=tz)
        now_ts = now_dt.timestamp()
        three_days_ago_ts = now_ts - 3 * 86400
        twenty_days_ago_ts = now_ts - 20 * 86400
        forty_days_ago_ts = now_ts - 40 * 86400

        # Записываем события в разные периоды
        self.db.bump_stat("incoming", 10, timestamp=now_ts)
        self.db.bump_stat("published", 4, timestamp=now_ts)
        self.db.bump_stat("duplicates", 2, timestamp=now_ts)
        self.db.bump_stat("filtered_out_of_stock", 1, timestamp=now_ts)
        self.db.bump_stat("filtered_wb_stock", 1, timestamp=now_ts)
        self.db.bump_stat("filtered_ozon_discount", 1, timestamp=now_ts)
        self.db.bump_stat("filtered_blocklist", 1, timestamp=now_ts)
        self.db.bump_stat("deleted_oos", 2, timestamp=now_ts)
        self.db.bump_stat("deleted_price_up", 1, timestamp=now_ts)
        self.db.bump_stat("posts_deleted", 3, timestamp=now_ts)
        self.db.bump_stat("marked_oos", 1, timestamp=now_ts)
        self.db.bump_stat("posts_marked_unavailable", 1, timestamp=now_ts)

        # 3 дня назад (попадает в week, month, all, но не в day)
        self.db.bump_stat("incoming", 20, timestamp=three_days_ago_ts)
        self.db.bump_stat("published", 5, timestamp=three_days_ago_ts)

        # 20 дней назад (попадает в month, all)
        self.db.bump_stat("incoming", 30, timestamp=twenty_days_ago_ts)
        self.db.bump_stat("published", 6, timestamp=twenty_days_ago_ts)

        # 40 дней назад (попадает только в all)
        self.db.bump_stat("incoming", 40, timestamp=forty_days_ago_ts)
        self.db.bump_stat("published", 7, timestamp=forty_days_ago_ts)
        self.db.set_last_published_at(now_ts)
        record_stat(admin, "cards_parsed", 5, timestamp=now_ts)
        record_cascade(admin, "wb_card_api", True)

        day_stats = admin._collect_stats_for_period("day", now_ts=now_ts)
        week_stats = admin._collect_stats_for_period("week", now_ts=now_ts)
        month_stats = admin._collect_stats_for_period("month", now_ts=now_ts)
        all_stats = admin._collect_stats_for_period("all", now_ts=now_ts)

        self.assertEqual(day_stats["incoming"], 10)
        self.assertEqual(day_stats["published"], 4)
        self.assertEqual(day_stats["posts_deleted"], 3)
        self.assertEqual(day_stats["deleted_oos"], 2)
        self.assertEqual(day_stats["deleted_price_up"], 1)
        self.assertEqual(day_stats["posts_marked_unavailable"], 1)
        self.assertEqual(day_stats["filtered_wb_stock"], 1)
        self.assertEqual(day_stats["filtered_ozon_discount"], 1)
        self.assertEqual(day_stats["filtered_blocklist"], 1)

        self.assertEqual(week_stats["incoming"], 30)
        self.assertEqual(week_stats["published"], 9)
        self.assertEqual(month_stats["incoming"], 60)
        self.assertEqual(month_stats["published"], 15)
        self.assertEqual(all_stats["incoming"], 100)
        self.assertEqual(all_stats["published"], 22)

        # Проверяем сохранение после переоткрытия БД и восстановления состояния
        self.db.close()
        self.db = DuplicateDB(self.db_path, cooldown_hours=48, pending_ttl_minutes=20)
        restarted_admin = self.make_admin()
        restarted_admin.current_sources = {10, 20}
        restarted_admin.queue = SimpleNamespace(qsize=lambda: 0, maxsize=500)
        restarted_admin.browser_resolver = SimpleNamespace(is_running=True, is_available=True)
        restarted_admin.synced_cookie_count = 12
        restarted_admin.restore_stats_from_db()

        self.assertEqual(restarted_admin.state.incoming, 100)
        self.assertEqual(restarted_admin.state.published, 22)
        self.assertEqual(restarted_admin.state.posts_deleted, 3)
        self.assertEqual(restarted_admin.state.posts_marked_unavailable, 1)
        self.assertIsNotNone(restarted_admin.state.last_published_at)
        self.assertIn("wb_card_api", restarted_admin.state.cascade_stats)

        main_text = restarted_admin.get_stats_text()
        self.assertEqual(main_text, "Статус: 🟢 АКТИВЕН")
        restarted_admin.state.is_paused = True
        self.assertEqual(restarted_admin.get_stats_text(), "Статус: ⏸ НА ПАУЗЕ")
        self.assertIn("Очередь: 0/500", restarted_admin.get_monitoring_text())
        self.assertIn("Канал: основной", restarted_admin.get_service_text())

        detailed_text = restarted_admin.get_detailed_stats_text("all")
        self.assertIn("Удалено из канала: 3 (нет в наличии: 2 · рост цены", detailed_text)
        self.assertIn("Помечено «Неактуально»: 1", detailed_text)
        self.assertIn("Малый остаток WB", detailed_text)
        self.assertIn("Низкая скидка Ozon", detailed_text)
        self.assertIn("Чёрный список", detailed_text)
        self.assertEqual(len(restarted_admin.get_stats_keyboard("day")), 6)

        # Проверяем, что обнуление статистики не затрагивает базу дедупликации seen_items
        p_seen = ProductInfo(
            original_url="https://ozon.by/product/999/",
            resolved_url="https://ozon.by/product/999/",
            platform="Ozon",
            sku="999",
            canonical_url="https://ozon.by/product/999/",
            sku_key="ozon:999",
        )
        self.db.reserve_fresh([p_seen], 1, 1)
        self.db.mark_published(["ozon:999"])

        restarted_admin.reset_all_stats()
        self.assertEqual(restarted_admin.state.incoming, 0)
        self.assertEqual(restarted_admin.state.published, 0)
        self.assertEqual(restarted_admin.state.posts_deleted, 0)
        self.assertIsNone(restarted_admin.state.last_published_at)
        self.assertEqual(self.db.get_stats(), {})
        self.assertEqual(self.db.count_by_status(), (1, 0))

    async def test_process_job_and_post_check_record_filter_and_deletion_stats(self):
        from tg_marketplace_parser.models import Job

        class TestParser(MarketDataMixin, PipelineMixin, AdminMixin):
            pass

        parser = TestParser()
        parser.db = self.db
        parser.state = ParserState()
        parser.settings = SimpleNamespace(
            dry_run=False,
            test_channel="test_chan",
            timezone="Europe/Minsk",
            ozon_min_discount_percent=30.0,
            wb_min_stock=3,
            max_links_per_post=5,
            ozon_canonical_host="ozon.by",
            require_sku=True,
            skip_out_of_stock=True,
            require_price=True,
            include_source_notes=False,
        )
        parser.user_client = SimpleNamespace(
            edit_message=AsyncMock(),
            delete_messages=AsyncMock(),
        )
        parser.get_channel_entity = AsyncMock(return_value="target")

        async def _simple_gather(*coros, return_exceptions=False):
            results = []
            for c in coros:
                try:
                    results.append(await c)
                except Exception as exc:
                    if return_exceptions:
                        results.append(exc)
                    else:
                        raise
            return results

        with patch("tg_marketplace_parser.pipeline.asyncio.gather", side_effect=_simple_gather):
            # 1. Сообщение без ссылок -> rejected_no_links
            msg_no_links = SimpleNamespace(id=1, raw_text="Привет без ссылок", entities=[], media=None)
            await parser.process_job(Job(messages=[msg_no_links], source_chat_id=100))
            self.assertEqual(parser.state.rejected_no_links, 1)
            self.assertEqual(self.db.get_stats()["rejected_no_links"], 1)

            # 2. Сообщение с товаром WB с малым остатком (<3) -> filtered_wb_stock
            parser.fetch_wb_card_data = AsyncMock(
                return_value=ProductCardData(
                    title="Куртка",
                    brand="BrandA",
                    price_byn=50.0,
                    in_stock=True,
                    total_quantity=1,
                )
            )
            msg_wb_low = SimpleNamespace(
                id=2,
                raw_text="https://www.wildberries.by/catalog/111111/detail.aspx",
                entities=[],
                media=None,
            )
            await parser.process_job(Job(messages=[msg_wb_low], source_chat_id=100))
            self.assertEqual(parser.state.filtered_wb_stock, 1)
            self.assertEqual(self.db.get_stats()["filtered_wb_stock"], 1)

            # 3. Сообщение с товаром из блок-листа -> filtered_blocklist
            self.db.add_block("brand", "BannedBrand")
            parser.fetch_wb_card_data = AsyncMock(
                return_value=ProductCardData(
                    title="Кроссовки",
                    brand="BannedBrand",
                    price_byn=50.0,
                    in_stock=True,
                    total_quantity=10,
                )
            )
            msg_blocked = SimpleNamespace(
                id=3,
                raw_text="https://www.wildberries.by/catalog/222222/detail.aspx",
                entities=[],
                media=None,
            )
            await parser.process_job(Job(messages=[msg_blocked], source_chat_id=100))
            self.assertEqual(parser.state.filtered_blocklist, 1)
            self.assertEqual(self.db.get_stats()["filtered_blocklist"], 1)

            # 4. Сообщение Ozon с низкой скидкой (<30%) -> filtered_ozon_discount
            parser.fetch_ozon_card_data = AsyncMock(
                return_value=ProductCardData(
                    title="Наушники",
                    brand="GoodBrand",
                    price_byn=90.0,
                    avg_month_price_byn=100.0,
                    in_stock=True,
                )
            )
            msg_oz_low_disc = SimpleNamespace(
                id=4,
                raw_text="https://ozon.by/product/333333/",
                entities=[],
                media=None,
            )
            await parser.process_job(Job(messages=[msg_oz_low_disc], source_chat_id=100))
            self.assertEqual(parser.state.filtered_ozon_discount, 1)
            self.assertEqual(self.db.get_stats()["filtered_ozon_discount"], 1)

    async def test_adaptive_views_pre_delete_snapshot_and_admin_stats_subsections(self):
        admin = self.make_admin()
        forum_id = -1001234567890
        admin.settings.test_forum_id = forum_id
        day_start, _ = admin._get_period_bounds("day", now_ts=1_800_000_000)
        now = day_start + 7200

        p_high = ProductInfo(
            original_url="https://ozon.by/product/5001/",
            resolved_url="https://ozon.by/product/5001/",
            platform="Ozon",
            sku="5001",
            canonical_url="https://ozon.by/product/5001/",
            sku_key="ozon:5001",
            title="Кофемашина автоматическая",
            price_byn=400.0,
            avg_month_price_byn=1000.0,
        )
        p_low = ProductInfo(
            original_url="https://www.wildberries.by/catalog/5002/detail.aspx",
            resolved_url="https://www.wildberries.by/catalog/5002/detail.aspx",
            platform="Wildberries",
            sku="5002",
            canonical_url="https://www.wildberries.by/catalog/5002/detail.aspx",
            sku_key="wildberries:5002",
            title="Набор сковород",
            price_byn=60.0,
            avg_month_price_byn=100.0,
        )
        self.db.reserve_fresh([p_high, p_low], 1, 1)
        self.db.save_published_post(
            p_high, f"🔷 Ozon\n\n🔗 {p_high.canonical_url}", [501], 60.0, now - 600, "main", topic="Техника и электроника",
        )
        self.db.complete_topic_delivery("main:501", f"forum:{forum_id}", [601], delivered_at=now - 595)
        self.db.save_published_post(
            p_low, f"🟪 Wildberries\n\n🔗 {p_low.canonical_url}", [502], 40.0, now - 600, "main", topic="Дом и кухня",
        )
        self.db.complete_topic_delivery("main:502", f"forum:{forum_id}", [602], delivered_at=now - 595)

        # 1. Адаптивный замер в первый час (через 15 минут повторный замер уже положен) + монотонность
        runtime = RuntimeMixin()
        runtime.db = self.db
        runtime.target_entity = "main_entity"
        runtime.user_client = SimpleNamespace(
            is_connected=lambda: True,
            get_messages=AsyncMock(
                return_value=[SimpleNamespace(id=501, views=120), SimpleNamespace(id=502, views=45)]
            ),
        )
        await runtime.refresh_publication_views(now - 450)
        self.assertEqual(self.db.get_publication_view_stats(day_start), (2, 2, 165))
        # Менее 15 минут для свежего поста (<1ч) — повторный замер ещё не требуется
        self.assertEqual(self.db.get_posts_due_for_views(now - 450 + 899), {})
        # Ровно через 15 минут в пределах первого часа пост снова попадает в выборку замера
        self.assertEqual(set(self.db.get_posts_due_for_views(now - 450 + 900)), {501, 502})
        # Если Telegram вернул меньшее число просмотров или None (удалён вручную), старый максимум сохраняется,
        # а время замера сдвигается, чтобы не опрашивать пустое сообщение каждые 5 минут
        runtime.user_client.get_messages.return_value = [SimpleNamespace(id=501, views=90), None]
        await runtime.refresh_publication_views(now - 450 + 900)
        self.assertEqual(self.db.get_publication_view_stats(day_start), (2, 2, 165))
        self.assertEqual(self.db.get_posts_due_for_views(now - 450 + 901), {})

        # 2. Финальный срез просмотров перед удалением и из ответа edit_message при пометке «Неактуально»
        parser = PipelineMixin()
        parser.db = self.db
        parser.state = ParserState()
        parser.record_admin_error = lambda msg: None
        entities = {"main": "main_entity", f"forum:{forum_id}": "forum_entity"}
        parser.get_channel_entity = AsyncMock(side_effect=lambda key: entities[key])
        parser.user_client = SimpleNamespace(
            edit_message=AsyncMock(return_value=SimpleNamespace(id=501, views=310)),
            get_messages=AsyncMock(return_value=[SimpleNamespace(id=502, views=95)]),
            delete_messages=AsyncMock(),
        )
        due_rows = {
            r["post_id"]: r
            for r in self.db.get_due_published_posts(now + 1000, limit=10, include_linked_copies=False)
        }
        await parser.handle_published_post_check(
            due_rows["main:501"], p_high, ProductCardData(in_stock=False), now,
        )
        await parser.handle_published_post_check(
            due_rows["main:502"], p_low, ProductCardData(in_stock=False), now,
        )
        self.assertEqual(self.db.get_publication_view_stats(day_start), (2, 2, 405))

        # 3. Проверка всех 5 подразделов статистики админ-панели
        with patch("tg_marketplace_parser.admin.time.time", return_value=now):
            overview = admin.get_stats_section_text("day", "overview")
            views_sec = admin.get_stats_section_text("day", "views")
            forum_sec = admin.get_stats_section_text("day", "forum")
            lifecycle_sec = admin.get_stats_section_text("day", "lifecycle")
            filters_sec = admin.get_stats_section_text("day", "filters")

        self.assertIn("Статистика (сводка)", overview)
        self.assertIn("Просмотров: 405", overview)
        self.assertIn("Просмотры основного канала", views_sec)
        self.assertIn("Учтено просмотров до удаления постов: 95 (постов: 1)", views_sec)
        self.assertIn("310 👁 — Кофемашина автоматическая (Ozon, −60%) [❌ неакт.]", views_sec)
        self.assertIn("95 👁 — Набор сковород (Wildberries, −40%) [удалён]", views_sec)
        self.assertIn("Доп. группа и темы", forum_sec)
        self.assertIn("Техника и электроника — 1 шт. (🏷 1)", forum_sec)
        self.assertIn("Дом и кухня — 1 шт. (❌ 1)", forum_sec)
        self.assertIn("Снятие и пометки постов", lifecycle_sec)
        self.assertIn("Удалено из группы: 1", lifecycle_sec)
        self.assertIn("Помечено «Неактуально» в группе: 1", lifecycle_sec)
        self.assertIn("Отсев и система", filters_sec)


if __name__ == "__main__":
    unittest.main()
