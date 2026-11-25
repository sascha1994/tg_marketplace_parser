import json
import time
import unittest
from datetime import date
from unittest.mock import AsyncMock
from types import SimpleNamespace

from telethon.errors import FloodWaitError
from telethon.tl import types
from telethon.tl.types import MessageEntityStrike, MessageEntityUrl

from tg_marketplace_parser import topics
from tg_marketplace_parser.constants import POST_AUTO_DELETE_SECONDS
from tg_marketplace_parser.models import ParserState, ProductCardData, ProductInfo
from tg_marketplace_parser.pipeline import PipelineMixin
from tg_marketplace_parser.storage import DuplicateDB

FORUM_ID = -1001234567890
SOURCE = types.InputPeerChannel(channel_id=9876543210, access_hash=2)


class TopicTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolve_forum_missing_from_session_cache(self):
        forum = object()

        async def dialogs(*, ignore_migrated):
            self.assertTrue(ignore_migrated)
            yield SimpleNamespace(id=FORUM_ID, entity=forum)

        client = SimpleNamespace(
            get_input_entity=AsyncMock(side_effect=[ValueError("missing"), forum]),
            iter_dialogs=dialogs,
        )
        self.assertIs(await topics.resolve_forum(client, FORUM_ID), forum)
        client.get_input_entity.assert_any_await(FORUM_ID)
        client.get_input_entity.assert_awaited_with(forum)

    def test_category_and_title(self):
        for category, title, expected in [
            ("Детская одежда", "Куртка", "Детская одежда и обувь"),
            ("", "Шорты мужские", "Одежда и обувь для взрослых"),
            ("", "Легинсы Женский AQ Pro черный Хлопок, Мех искусственный Спортивный, размер 40, 46",
             "Одежда и обувь для взрослых"),
            ("Женская одежда > Леггинсы", "AQ Pro артикул 123", "Одежда и обувь для взрослых"),
            ("", "Лосины для девочки", "Детская одежда и обувь"),
            ("Детская одежда > Колготки", "Колготки утеплённые", "Детская одежда и обувь"),
            ("", "Женские спортивные тайтсы", "Одежда и обувь для взрослых"),
            ("", "Кардиган женский", "Одежда и обувь для взрослых"),
            ("", "Игрушечный пылесос", "Игрушки и товары для детей"),
            ("Книги", "Многоразовая раскраска, Буква-Ленд, набор раскрасок для детей, 3 штуки",
             "Игрушки и товары для детей"),
            ("", "Набор многоразовых раскрасок", "Игрушки и товары для детей"),
            ("Раскраски для детей", "Буква-Ленд, 3 книжки", "Игрушки и товары для детей"),
            ("", "Набор для аппликации для детей", "Игрушки и товары для детей"),
            ("Детское творчество", "Набор фломастеров", "Игрушки и товары для детей"),
            ("", "Пальчиковые краски для малышей", "Игрушки и товары для детей"),
            ("", "Раскраска для взрослых", None),
            ("", "Раскраска-антистресс", None),
            ("", "Профессиональные маркеры для художников", None),
            ("", "Набор полотенец", "Дом и кухня"),
            ("", "Мечта — Казан для плова 4л", "Дом и кухня"),
            ("", "Crafy — Пергамент для аэрогриля 100 штук", "Дом и кухня"),
            ("Дом / Кухня / Выпечка и запекание", "Crafy, 20 × 20 см, 100 штук", "Дом и кухня"),
            ("", "Бумажные вкладыши для аэрогриля", "Дом и кухня"),
            ("", "Пергаментная бумага в рулоне", "Дом и кухня"),
            ("", "Фольга пищевая алюминиевая", "Дом и кухня"),
            ("", "Рукав для запекания курицы", "Дом и кухня"),
            ("", "Пищевая плёнка", "Дом и кухня"),
            ("", "Силиконовый коврик для выпечки", "Дом и кухня"),
            ("Дом и сад • Посуда и кухонные принадлежности • Посуда для приготовления • Посуда для духовки",
             "Форма для выпечки, Форма для запекания, Для аэрогриля, Силиконовая, Круглая", "Дом и кухня"),
            ("Дом и сад > Посуда и кухонные принадлежности > Формочки силиконовые",
             "Круглая форма 20 см", "Дом и кухня"),
            ("", "Аэрогриль с бумажными вкладышами в комплекте", "Техника и электроника"),
            ("", "Пергамент для каллиграфии", None),
            ("Посуда > Казаны", "Мечта, 4 л", "Дом и кухня"),
            ("", "Набор чугунных казанов", "Дом и кухня"),
            ("", "Магнит сувенирный Казань", None),
            ("", "Моющий пылесос", "Техника и электроника"),
            ("Строительство > Электроинструменты", "Bosch GSR 12V-35", "Техника и электроника"),
            ("", "Аккумуляторная дрель-шуруповёрт", "Техника и электроника"),
            ("", "Перфоратор SDS-Plus", "Техника и электроника"),
            ("", "Угловая шлифовальная машина УШМ", "Техника и электроника"),
            ("", "Аккумуляторная цепная пила", "Техника и электроника"),
            ("", "Сетевой фрезер", "Техника и электроника"),
            ("Электроинструменты", "Игрушечный шуруповёрт", "Игрушки и товары для детей"),
            ("Ручной инструмент", "Ручная пила по дереву", "Ремонт и инструменты"),
            ("", "Крестовая отвёртка с набором бит", "Ремонт и инструменты"),
            ("", "Тушь для ресниц EXTRA VOLUME", "Красота и уход"),
            ("", "Фен Day Sonic ICON Air Expert HD18P, 3в1 профессиональный 1200 Вт", "Красота и уход"),
            ("Бытовая техника", "Фен Day Sonic ICON Air Expert HD18P", "Красота и уход"),
            ("", "Расческа-выпрямитель Day Sonic ICON DuoBrush VPR-04, утюжок для волос Day Sonic",
             "Красота и уход"),
            ("Бытовая техника", "Щипцы для завивки волос", "Красота и уход"),
            ("Бытовая техника > Фены и стайлеры", "Day Sonic HD18P", "Красота и уход"),
            ("Строительство и ремонт", "Строительный фен 2000 Вт", "Техника и электроника"),
            ("", "Фен для пайки SMD", "Техника и электроника"),
            ("", "Выпрямитель напряжения 12 В", None),
            ("", "PROFILAX — Зубная щетка средней жесткости для всей семьи, 2 шт в наборе",
             "Красота и уход"),
            ("Красота > Гигиена полости рта", "PROFILAX, набор 2 шт", "Красота и уход"),
            ("Электроника", "Детская электрическая зубная щётка", "Красота и уход"),
            ("", "Сменные насадки для звуковой зубной щётки", "Красота и уход"),
            ("", "Зубная паста с мятой", "Красота и уход"),
            ("", "Зубная нить вощёная", "Красота и уход"),
            ("", "Межзубные ёршики", "Красота и уход"),
            ("", "Ополаскиватель для полости рта", "Красота и уход"),
            ("", "Ирригатор портативный", "Красота и уход"),
            ("", "Скребок для языка", "Красота и уход"),
            ("", "Отбеливающие полоски для зубов", "Красота и уход"),
            ("", "Щётка для обуви", "Одежда и обувь для взрослых"),
            ("", "Крем для глаз GRACEFACE с ПДРН и экстрактом розы", "Красота и уход"),
            ("", "Mirrolla SKIN PLUS Крем воск от трещин для очень сухой кожи рук и ног, пяток",
             "Красота и уход"),
            ("", "Колье на шею жемчуг + подарок", "Красота и уход"),
            ("Украшения > Бижутерия", "Набор с жемчугом", "Красота и уход"),
            ("", "Серьги серебряные с фианитами", "Красота и уход"),
            ("", "Фитнес-браслет с пульсометром", "Техника и электроника"),
            ("", "Крем для обуви водоотталкивающий", "Одежда и обувь для взрослых"),
            ("", "Крем-суп грибной", "Продукты и напитки"),
            ("", "Ёлочные украшения", "Дом и кухня"),
            ("", "Гель для душа", "Красота и уход"),
            ("", "Чай чёрный Winter Crush", "Продукты и напитки"),
            ("Бакалея", "Tess Winter Crush", "Продукты и напитки"),
            ("", "Кондиционер для белья White Musk", "Бытовая химия"),
            ("", "Средство от засоров труб", "Бытовая химия"),
            ("", "Игрушка для собак с пищалкой", "Зоотовары"),
            ("", "Сухой корм для кошек", "Зоотовары"),
            ("", "Автомобильный компрессор 12 В", "Автотовары"),
            ("", "Складная палатка", "Спорт и туризм"),
            ("", "Туристический рюкзак", "Спорт и туризм"),
            ("", "Саморезы по дереву", "Ремонт и инструменты"),
            ("", "Сверла для дрели", "Ремонт и инструменты"),
            ("", "Кофемашина", "Техника и электроника"),
            ("Обувь", "Угги Девочки SAGGEZZA Текстиль", "Детская одежда и обувь"),
            ("Одежда и обувь > Детская обувь", "SAGGEZZA Текстиль", "Детская одежда и обувь"),
            ("", "Сумка планшет, на плечо", "Одежда и обувь для взрослых"),
            ("Аксессуары", "Сумка-планшет", "Одежда и обувь для взрослых"),
            ("", "Планшет с сумкой в комплекте", "Техника и электроника"),
            ("Игрушки", "Ёлочные игрушки шары 26 шт", "Дом и кухня"),
            ("", "Перламутровые сосульки на ёлку", "Дом и кухня"),
            ("", "Мягкая игрушка брелок / Обвес на сумку", "Игрушки и товары для детей"),
            ("", "Салфетница на стол для кухни", "Дом и кухня"),
            ("", "Комплект трусов Женский брифы", "Одежда и обувь для взрослых"),
            ("", "Панама", None),
            ("Детская одежда", "Панама", "Детская одежда и обувь"),
            ("", "Панама мужская", "Одежда и обувь для взрослых"),
            ("", "Товар со скидкой", None),
            ("Электроника > Аксессуары для смартфонов", "Модель AB-12", "Техника и электроника"),
            ("Дом и сад > Мебель > Банкетки", "Модель с мягким сиденьем", "Дом и кухня"),
            ("Спорт и отдых > Волейбол", "Мяч Classic", "Спорт и туризм"),
            ("Детям > Обувь > Ботинки", "Зимняя модель", "Детская одежда и обувь"),
            ("Детские товары > Коляски", "Модель City", "Игрушки и товары для детей"),
            ("Бытовая техника > Гигиена полости рта", "Модель Smart", "Красота и уход"),
            ("Строительство и ремонт > Гайковёрты", "Аккумуляторный DEKO", "Техника и электроника"),
            ("Строительство и ремонт > Оснастка", "Сверла для дрели", "Ремонт и инструменты"),
            ("Женщинам > Бижутерия", "Набор из трех изделий", "Красота и уход"),
            ("", "Slimberg — Аппарат для маникюра и педикюра профессиональный с фрезами", "Красота и уход"),
            ("Красота > Косметические аппараты и аксессуары > slimberg",
             "Slimberg — Аппарат для маникюра и педикюра профессиональный с фрезами", "Красота и уход"),
            ("Бытовая техника", "Аппарат для маникюра и педикюра 20000 об/мин", "Красота и уход"),
            ("Косметические аппараты и аксессуары > slimberg", "Модель M-20", "Красота и уход"),
            ("", "Пылесос для маникюра настольный", "Красота и уход"),
            ("", "Лампа для сушки ногтей UV/LED 48 Вт", "Красота и уход"),
            ("", "Электрическая роликовая пилка для пяток", "Красота и уход"),
            ("", "Аккумуляторный триммер для бороды и усов", "Красота и уход"),
            ("Бытовая техника", "Эпилятор женский беспроводной", "Красота и уход"),
            ("", "Аккумуляторный триммер для травы", "Техника и электроника"),
            ("", "Смарт-часы с AMOLED экраном", "Техника и электроника"),
            ("", "Коврик для компьютерной мыши XXL", "Техника и электроника"),
            ("", "Увлажнитель воздуха ультразвуковой", "Техника и электроника"),
            ("", "Микроволновая печь 20 л", "Техника и электроника"),
            ("", "Рюкзак туристический походный 65 л", "Спорт и туризм"),
            ("", "Термосумка для пикника 25 л", "Спорт и туризм"),
            ("", "Спиннинг карбоновый 2.4 м", "Спорт и туризм"),
            ("", "Сумка для инструмента строительная", "Ремонт и инструменты"),
            ("", "Смеситель для кухни с гибким изливом", "Ремонт и инструменты"),
            ("", "Набор кухонных ножей на подставке", "Дом и кухня"),
            ("", "Комплект постельного белья 2 спальный", "Дом и кухня"),
            ("", "Таблетки для посудомоечной машины 100 шт", "Бытовая химия"),
            ("", "Масло оливковое Extra Virgin 500 мл", "Продукты и напитки"),
            ("", "Фисташки жареные солёные 500 г", "Продукты и напитки"),
            ("", "Видеорегистратор автомобильный с камерой заднего вида", "Автотовары"),
            ("", "Расчёска для кошек и собак от колтунов", "Зоотовары"),
            ("Детям > Головные уборы", "Бейсболка летняя", "Детская одежда и обувь"),
            ("", "Пуховик зимний женский", "Одежда и обувь для взрослых"),
            ("", "Настольная игра для всей семьи", "Игрушки и товары для детей"),
        ]:
            with self.subTest(title=title):
                self.assertEqual(topics.product_topic(category, title, today=date(2027, 1, 15)), expected)

    def test_holiday_topic_routes_products_only_during_the_season(self):
        last_day = date(2027, 1, 14)
        for category, title in [
            ("Дом и кухня", "Елочные игрушки 24 шт"),
            ("", "Сосульки винтовые прозрачные на ёлку"),
            ("", "Искусственная ель 180 см"),
            ("Электроника", "Новогодняя светодиодная гирлянда"),
            ("", "Рождественский венок"),
            ("", "Адвент-календарь с шоколадом"),
            ("", "Костюм Деда Мороза"),
            ("Праздники > Новый год", "Светодиодная гирлянда"),
        ]:
            with self.subTest(title=title):
                self.assertEqual(topics.product_topic(category, title, today=last_day), topics.HOLIDAY_TOPIC)

        self.assertEqual(topics.product_topic("", "Ёлочные игрушки", today=date(2027, 1, 15)), topics.HOME_TOPIC)
        self.assertEqual(topics.product_topic("", "Наушники в подарок на Новый год", today=last_day), topics.TECH_TOPIC)

    async def test_forward_and_track_copy(self):
        product = ProductInfo("", "", "Ozon", "1", "", "ozon:1")
        db = DuplicateDB(":memory:", 1, 1)
        self.addCleanup(db.close)
        name = "Техника и электроника"
        db.save_published_post(product, "post", [10, 11], 50, 100, "test", topic=name)
        post_id, topic, raw_ids, source_channel = db.due_topic_delivery(101)
        self.assertEqual(source_channel, "test")
        client = AsyncMock()
        group = types.InputPeerChannel(channel_id=1234567890, access_hash=1)
        client.get_input_entity.side_effect = [SOURCE, group]

        def telegram(request):
            if hasattr(request, "q"):
                return SimpleNamespace(topics=[SimpleNamespace(id=5, title=name)])
            self.assertEqual(request.top_msg_id, 5)
            self.assertEqual(request.id, [10, 11])
            self.assertEqual(request.to_peer, group)
            self.assertEqual(request.from_peer, SOURCE)
            return SimpleNamespace(updates=[
                types.UpdateMessageID(id=mid + 20, random_id=rid)
                for mid, rid in zip(request.id, request.random_id)
            ])

        client.side_effect = telegram
        destination, ids = await topics.forward_to_topic(client, SOURCE, FORUM_ID, topic, json.loads(raw_ids))
        client.get_input_entity.assert_any_await(FORUM_ID)
        db.complete_topic_delivery(post_id, destination, ids)
        self.assertIsNone(db.due_topic_delivery(101))
        tracked = db.get_due_published_posts(999999, limit=10)
        self.assertEqual({r["target_channel"]: json.loads(r["message_ids"]) for r in tracked},
                         {"test": [10, 11], "forum:-1001234567890": [30, 31]})

    async def test_forward_to_renamed_clothing_topic(self):
        client = AsyncMock()
        client.get_input_entity.return_value = SOURCE
        alternate_title = "Одежда, обувь и аксессуары"
        client.side_effect = [
            SimpleNamespace(topics=[]),
            SimpleNamespace(topics=[SimpleNamespace(id=28, title=alternate_title)]),
            SimpleNamespace(updates=[]),
        ]
        await topics.forward_to_topic(client, SOURCE, FORUM_ID, topics.ADULT_CLOTHING_TOPIC, [])
        first, second, forward = (call.args[0] for call in client.await_args_list)
        self.assertEqual(first.q, topics.ADULT_CLOTHING_TOPIC)
        self.assertEqual(second.q, alternate_title)
        self.assertEqual(forward.top_msg_id, 28)

    async def test_queueing_uses_selected_destination_and_forum_switch(self):
        import asyncio

        parser = PipelineMixin()
        parser.db = DuplicateDB(":memory:", 1, 1)
        self.addCleanup(parser.db.close)
        parser.settings = SimpleNamespace(test_forum_id=FORUM_ID, include_source_notes=False)
        parser.publication_gate_lock = asyncio.Lock()
        parser.quiet_period_remaining = lambda: 0
        parser.publish = AsyncMock(side_effect=[[10], [11], [12]])
        url = "https://ozon.by/product/123456/"
        product = ProductInfo(url, url, "Ozon", "123456", url, "ozon:123456", title="Планшет")

        for channel, message_id in (("main", 10), ("test", 11)):
            parser.db.set_setting("publication_target", channel)
            await parser.publish_product(product, "", [], 1, 1)
            post_id, _, _, source = parser.db.due_topic_delivery(int(time.time()))
            self.assertEqual((post_id, source), (f"{channel}:{message_id}", channel))
            parser.db.complete_topic_delivery(post_id, f"forum:{FORUM_ID}", [message_id + 100])

        parser.db.set_setting("forum_publication_enabled", "false")
        await parser.publish_product(product, "", [], 1, 1)
        self.assertIsNone(parser.db.due_topic_delivery(int(time.time())))
        self.assertEqual(parser.publish.await_args.args[-1], "test")

    async def test_forum_copy_is_marked_unavailable_synchronously_with_main_post(self):
        db = DuplicateDB(":memory:", 48, 20)
        self.addCleanup(db.close)
        url = "https://ozon.by/product/123456/"
        product = ProductInfo(
            url, url, "Ozon", "123456", url, "ozon:123456",
            title="Кофемашина", price_byn=40.0, avg_month_price_byn=100.0,
        )
        publication_text = f"🔷 Ozon\n\n🔗 {url}"
        db.reserve_fresh([product], 1, 1)
        db.save_published_post(
            product, publication_text, [10], product.discount_percent, 1000, "main", topic=topics.TECH_TOPIC,
        )
        db.complete_topic_delivery("main:10", f"forum:{FORUM_ID}", [30, 31], delivered_at=1005)
        self.assertEqual(db.count_published_post_checks(now=1900), (1, 1))

        parser = PipelineMixin()
        parser.db = db
        parser.state = ParserState()
        parser.settings = SimpleNamespace(ozon_check_interval_seconds=60)
        parser._last_ozon_stock_check_at = 0.0
        parser.quiet_period_remaining = lambda: 0
        parser.record_admin_error = lambda msg: None
        parser.fetch_card_for_product = AsyncMock(return_value=ProductCardData(in_stock=False))
        entities = {"main": "main_entity", f"forum:{FORUM_ID}": "forum_entity"}
        parser.get_channel_entity = AsyncMock(side_effect=lambda key: entities[key])
        parser.user_client = SimpleNamespace(edit_message=AsyncMock(), delete_messages=AsyncMock())

        await parser.run_scheduled_publication_step(now=1900)

        parser.fetch_card_for_product.assert_awaited_once()
        self.assertEqual(parser.user_client.edit_message.await_count, 2)
        edited_targets = [
            (call.args[0], call.args[1], call.args[2], call.kwargs["formatting_entities"])
            for call in parser.user_client.edit_message.await_args_list
        ]
        url_offset = publication_text.index(url)
        expected_offset = len(publication_text[:url_offset].encode("utf-16-le")) // 2
        expected_length = len(url.encode("utf-16-le")) // 2
        expected_entities = [
            MessageEntityUrl(offset=expected_offset, length=expected_length),
            MessageEntityStrike(offset=expected_offset, length=expected_length),
        ]
        self.assertEqual(
            edited_targets,
            [
                ("main_entity", 10, publication_text + "\n\n❌ Неактуально", expected_entities),
                ("forum_entity", 30, publication_text + "\n\n❌ Неактуально", expected_entities),
            ],
        )
        parser.user_client.delete_messages.assert_awaited_once_with("forum_entity", [31])
        self.assertEqual(db.get_published_post_status("main:10"), "unavailable_marked")
        self.assertEqual(db.get_published_post_status(f"forum:{FORUM_ID}:30"), "unavailable_marked")
        self.assertEqual((parser.state.posts_marked_unavailable, parser.state.marked_oos), (1, 1))
        self.assertEqual(
            (parser.state.forum_posts_marked_unavailable, parser.state.forum_marked_oos),
            (1, 1),
        )

    async def test_forum_copy_is_deleted_synchronously_on_price_increase(self):
        db = DuplicateDB(":memory:", 48, 20)
        self.addCleanup(db.close)
        url = "https://ozon.by/product/654321/"
        product = ProductInfo(
            url, url, "Ozon", "654321", url, "ozon:654321",
            title="Пылесос", price_byn=60.0, avg_month_price_byn=100.0,
        )
        publication_text = f"🔷 Ozon\n\n🔗 {url}"
        db.reserve_fresh([product], 1, 2)
        db.save_published_post(
            product, publication_text, [20], product.discount_percent, 1000, "main", topic=topics.TECH_TOPIC,
        )
        db.complete_topic_delivery("main:20", f"forum:{FORUM_ID}", [40], delivered_at=1005)

        parser = PipelineMixin()
        parser.db = db
        parser.state = ParserState()
        parser.settings = SimpleNamespace(ozon_check_interval_seconds=60)
        parser._last_ozon_stock_check_at = 0.0
        parser.quiet_period_remaining = lambda: 0
        parser.record_admin_error = lambda msg: None
        parser.fetch_card_for_product = AsyncMock(
            return_value=ProductCardData(in_stock=True, price_byn=75.0),
        )
        entities = {"main": "main_entity", f"forum:{FORUM_ID}": "forum_entity"}
        parser.get_channel_entity = AsyncMock(side_effect=lambda key: entities[key])
        parser.user_client = SimpleNamespace(edit_message=AsyncMock(), delete_messages=AsyncMock())

        await parser.run_scheduled_publication_step(now=1900)

        parser.fetch_card_for_product.assert_awaited_once()
        parser.user_client.edit_message.assert_not_awaited()
        self.assertEqual(
            [call.args for call in parser.user_client.delete_messages.await_args_list],
            [("main_entity", [20]), ("forum_entity", [40])],
        )
        self.assertEqual(db.get_published_post_status("main:20"), "deleted")
        self.assertEqual(db.get_published_post_status(f"forum:{FORUM_ID}:40"), "deleted")
        self.assertEqual((parser.state.posts_deleted, parser.state.deleted_price_up), (1, 1))
        self.assertEqual(
            (parser.state.forum_posts_deleted, parser.state.forum_deleted_price_up),
            (1, 1),
        )

    async def test_forum_copy_advances_schedule_and_expires_after_24h_with_main_post(self):
        db = DuplicateDB(":memory:", 48, 20)
        self.addCleanup(db.close)
        url = "https://www.wildberries.by/catalog/777777/detail.aspx"
        product = ProductInfo(
            url, url, "Wildberries", "777777", url, "wildberries:777777",
            title="Набор полотенец", price_byn=25.0, avg_month_price_byn=50.0,
        )
        db.reserve_fresh([product], 1, 3)
        db.save_published_post(product, "post", [50], 50.0, 1000, "main", topic=topics.HOME_TOPIC)
        db.complete_topic_delivery("main:50", f"forum:{FORUM_ID}", [150], delivered_at=1010)

        parser = PipelineMixin()
        parser.db = db
        parser.state = ParserState()
        parser.settings = SimpleNamespace(ozon_check_interval_seconds=60)
        parser._last_ozon_stock_check_at = 0.0
        parser.quiet_period_remaining = lambda: 0
        parser.record_admin_error = lambda msg: None
        parser.fetch_card_for_product = AsyncMock(
            return_value=ProductCardData(in_stock=True, price_byn=25.0),
        )
        parser.user_client = SimpleNamespace(edit_message=AsyncMock(), delete_messages=AsyncMock())

        # Хотя в БД две строки для одного товара, пакетная проверка WB не запускается зря,
        # а оба поста синхронно переносятся на следующий интервал.
        await parser.run_scheduled_publication_step(now=1900)
        parser.fetch_card_for_product.assert_awaited_once()
        self.assertEqual(db.get_due_published_posts(5499, limit=10), [])
        due_later = db.get_due_published_posts(5500, limit=10)
        self.assertEqual(
            {row["post_id"]: row["check_interval_seconds"] for row in due_later},
            {"main:50": 7200, f"forum:{FORUM_ID}:150": 7200},
        )

        expired = db.expire_old_published_posts(1000 + POST_AUTO_DELETE_SECONDS)
        self.assertEqual(expired, 2)
        self.assertEqual(db.get_published_post_status("main:50"), "expired")
        self.assertEqual(db.get_published_post_status(f"forum:{FORUM_ID}:150"), "expired")

    async def test_forum_copy_retries_after_flood_wait_and_cancels_undelivered_on_delete(self):
        db = DuplicateDB(":memory:", 48, 20)
        self.addCleanup(db.close)
        url = "https://ozon.by/product/999888/"
        product = ProductInfo(
            url, url, "Ozon", "999888", url, "ozon:999888",
            title="Смартфон", price_byn=300.0, avg_month_price_byn=700.0,
        )
        publication_text = f"🔷 Ozon\n\n🔗 {url}"
        db.reserve_fresh([product], 1, 4)
        db.save_published_post(
            product, publication_text, [60], product.discount_percent, 1000, "main", topic=topics.TECH_TOPIC,
        )
        db.complete_topic_delivery("main:60", f"forum:{FORUM_ID}", [160], delivered_at=1005)

        flood_err = FloodWaitError(request=None, capture=120)
        edit_calls = 0

        async def edit_side_effect(entity, msg_id, *args, **kwargs):
            nonlocal edit_calls
            edit_calls += 1
            if entity == "forum_entity" and edit_calls == 2:
                raise flood_err

        parser = PipelineMixin()
        parser.db = db
        parser.state = ParserState()
        parser.settings = SimpleNamespace(ozon_check_interval_seconds=60)
        parser._last_ozon_stock_check_at = 0.0
        parser.quiet_period_remaining = lambda: 0
        parser.record_admin_error = lambda msg: None
        entities = {"main": "main_entity", f"forum:{FORUM_ID}": "forum_entity"}
        parser.get_channel_entity = AsyncMock(side_effect=lambda key: entities[key])
        parser.user_client = SimpleNamespace(
            edit_message=AsyncMock(side_effect=edit_side_effect),
            delete_messages=AsyncMock(),
        )

        row = db.get_due_published_posts(1900, limit=1, include_linked_copies=False)[0]
        await parser.handle_published_post_check(row, product, ProductCardData(in_stock=False), 1900)
        self.assertEqual(db.get_published_post_status("main:60"), "unavailable_marked")
        self.assertEqual(db.get_published_post_status(f"forum:{FORUM_ID}:160"), "active")

        # При повторной проверке даже если карточка временно вернула None, копия наследует статус основного поста.
        retry_rows = db.get_due_published_posts(1900 + 900, limit=5, include_linked_copies=False)
        self.assertEqual([r["post_id"] for r in retry_rows], [f"forum:{FORUM_ID}:160"])
        await parser.handle_published_post_check(retry_rows[0], product, None, 1900 + 900)
        self.assertEqual(db.get_published_post_status(f"forum:{FORUM_ID}:160"), "unavailable_marked")
        self.assertEqual(parser.state.forum_posts_marked_unavailable, 1)

        # Если основной пост удалён до пересылки в тему, задача доставки очищается.
        db.save_published_post(product, publication_text, [61], 35.0, 2000, "main", topic=topics.TECH_TOPIC)
        self.assertIsNotNone(db.due_topic_delivery(2001))
        db.set_published_post_status("main:61", "deleted")
        self.assertIsNone(db.due_topic_delivery(2001))
