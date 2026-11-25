import json
import unittest

from tg_marketplace_parser.marketplace_parsing import (
    merge_card_data,
    parse_html_product_card,
    parse_ozon_composer_payload,
    parse_ozon_visible_price_text,
)
from tg_marketplace_parser.models import ProductCardData
from tg_marketplace_parser.topics import HOME_TOPIC, product_topic


class OzonVisiblePriceTests(unittest.TestCase):
    def test_cookware_breadcrumbs_route_card_without_title_keyword(self):
        title = "Круглая силиконовая форма 20 см"
        names = [
            "Дом и сад", "Посуда и кухонные принадлежности",
            "Посуда для приготовления", "Посуда для духовки",
        ]
        breadcrumbs = {
            "@type": "BreadcrumbList",
            "itemListElement": [{"item": {"name": name}} for name in names],
        }
        scripts = [json.dumps({"@type": "Product", "name": title}), json.dumps(breadcrumbs)]
        html = "".join(f'<script type="application/ld+json">{script}</script>' for script in scripts)
        for card in (
            parse_html_product_card(html),
            parse_ozon_composer_payload({"seo": {"script": [{"innerHTML": script} for script in scripts]}}),
        ):
            with self.subTest(card=card):
                self.assertIsNotNone(card)
                self.assertEqual(card.category, " > ".join(names))
                self.assertEqual(product_topic(card.category, card.title), HOME_TOPIC)

    def test_extracts_current_by_price_from_price_widget(self):
        card = parse_ozon_visible_price_text(
            "10,92 BYN 49,99 BYN Стало дешевле 1,00 BYN за 100 мл"
        )

        self.assertIsNotNone(card)
        self.assertEqual(card.price_byn, 10.92)
        self.assertTrue(card.in_stock)

    def test_converts_visible_ruble_price(self):
        card = parse_ozon_visible_price_text("320 ₽", rub_to_byn_rate=0.0345)

        self.assertIsNotNone(card)
        self.assertEqual(card.price_byn, 11.04)

    def test_rejects_price_widget_without_a_price(self):
        self.assertIsNone(parse_ozon_visible_price_text("В корзину"))

    def test_visible_offer_overrides_stale_unavailable_result(self):
        merged = merge_card_data(
            ProductCardData(in_stock=False),
            parse_ozon_visible_price_text("10,92 BYN"),
        )

        self.assertIsNotNone(merged)
        self.assertEqual(merged.price_byn, 10.92)
        self.assertTrue(merged.in_stock)

    def test_composer_price_overrides_conflicting_price_widget_availability(self):
        card = parse_ozon_composer_payload(
            {
                "widgetStates": {
                    "webPrice-123": json.dumps(
                        {"isAvailable": False, "cardPrice": "10,92 BYN"}
                    )
                }
            }
        )

        self.assertIsNotNone(card)
        self.assertEqual(card.price_byn, 10.92)
        self.assertTrue(card.in_stock)

    def test_explicit_sold_out_widget_still_overrides_a_price(self):
        card = parse_ozon_composer_payload(
            {
                "widgetStates": {
                    "webPrice-123": json.dumps({"cardPrice": "10,92 BYN"}),
                    "webOutOfStock-123": json.dumps({"text": "Нет в наличии"}),
                }
            }
        )

        self.assertIsNotNone(card)
        self.assertIsNone(card.price_byn)
        self.assertFalse(card.in_stock)


if __name__ == "__main__":
    unittest.main()
