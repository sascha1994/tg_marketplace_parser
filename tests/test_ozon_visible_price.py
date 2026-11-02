import json
import unittest

from tg_marketplace_parser.marketplace_parsing import (
    merge_card_data,
    parse_ozon_composer_payload,
    parse_ozon_visible_price_text,
)
from tg_marketplace_parser.models import ProductCardData


class OzonVisiblePriceTests(unittest.TestCase):
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
