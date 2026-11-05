import json
import os
import time
import unittest

from tg_marketplace_parser.marketplace_parsing import (
    extract_price_from_post_text,
    get_wb_candidate_basket_hosts,
    get_wb_card_image_url,
    merge_card_data,
    parse_json_ld_product,
    parse_wb_detail_payload,
    parse_wb_price_history_avg,
    parse_wb_price_history_latest,
)
from tg_marketplace_parser.models import ProductCardData

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def load_fixture(name: str) -> str:
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as handle:
        return handle.read()


class WildberriesHelpersTests(unittest.TestCase):
    def test_image_url_uses_vol_and_part_of_the_sku(self):
        url = get_wb_card_image_url(12345678)
        self.assertIn("/vol123/part12345/12345678/images/big/1.webp", url)
        self.assertTrue(url.startswith("https://basket-"))

    def test_image_url_for_invalid_sku_is_empty(self):
        self.assertEqual(get_wb_card_image_url("abc"), "")

    def test_candidate_hosts_start_with_primary_and_have_no_duplicates(self):
        hosts = get_wb_candidate_basket_hosts(123)
        self.assertEqual(len(hosts), len(set(hosts)))
        self.assertTrue(all(host.endswith(".wbbasket.ru") for host in hosts))

    def test_price_history_average_and_latest(self):
        now = int(time.time())
        history = [
            {"dt": now - 3600, "price": {"BYN": 10000}},
            {"dt": now - 60, "price": {"BYN": 12000}},
        ]
        self.assertEqual(parse_wb_price_history_avg(history), 110.0)
        self.assertEqual(parse_wb_price_history_latest(history), 120.0)

    def test_price_history_ignores_invalid_data(self):
        self.assertIsNone(parse_wb_price_history_avg([]))
        self.assertIsNone(parse_wb_price_history_avg("bad"))
        self.assertIsNone(parse_wb_price_history_latest([{"dt": 1, "price": "bad"}]))


class WildberriesDetailPayloadTests(unittest.TestCase):
    def test_parses_fixture_card(self):
        payload = json.loads(load_fixture("wb_detail_payload.json"))
        card = parse_wb_detail_payload(payload, sku="12345678")

        self.assertIsNotNone(card)
        self.assertEqual(card.total_quantity, 7)
        self.assertEqual(card.rating, 4.7)
        self.assertEqual(card.reviews_count, 350)
        self.assertTrue(card.image_url.endswith("/12345678/images/big/1.webp"))
        self.assertIn("рюкзак", card.title.casefold())

    def test_empty_product_list_means_out_of_stock(self):
        card = parse_wb_detail_payload({"data": {"products": []}}, sku="1")
        self.assertEqual(card, ProductCardData(in_stock=False))

    def test_other_sku_in_response_is_not_confused_with_requested_one(self):
        payload = json.loads(load_fixture("wb_detail_payload.json"))
        self.assertIsNone(parse_wb_detail_payload(payload, sku="999"))

    def test_non_dict_payload_is_rejected(self):
        self.assertIsNone(parse_wb_detail_payload([], sku="1"))


class JsonLdTests(unittest.TestCase):
    def test_parses_product_from_fixture(self):
        card = parse_json_ld_product(load_fixture("product_json_ld.json"))

        self.assertIsNotNone(card)
        self.assertEqual(card.price_byn, 49.99)
        self.assertTrue(card.in_stock)
        self.assertEqual(card.brand, "Lumo")
        self.assertEqual(card.rating, 4.8)
        self.assertEqual(card.reviews_count, 120)
        self.assertIn("/wc1000/", card.image_url)
        self.assertIn("лампа", card.title.casefold())

    def test_out_of_stock_offer_has_no_price(self):
        raw = json.dumps(
            {
                "@type": "Product",
                "name": "Товар",
                "offers": {"price": 10, "priceCurrency": "BYN", "availability": "OutOfStock"},
            }
        )
        card = parse_json_ld_product(raw)
        self.assertFalse(card.in_stock)
        self.assertIsNone(card.price_byn)

    def test_invalid_json_returns_none(self):
        self.assertIsNone(parse_json_ld_product("{not json"))
        self.assertIsNone(parse_json_ld_product(""))


class PostTextPriceTests(unittest.TestCase):
    def test_extracts_explicit_price_from_post(self):
        self.assertEqual(extract_price_from_post_text("Цена: 199 BYN только сегодня"), 199.0)

    def test_text_without_currency_has_no_price(self):
        self.assertIsNone(extract_price_from_post_text("Скидки до 70% на всё"))


class MergeCardTests(unittest.TestCase):
    def test_none_inputs_return_the_other_side(self):
        card = ProductCardData(title="Товар")
        self.assertIs(merge_card_data(card, None), card)
        self.assertIs(merge_card_data(None, card), card)
        self.assertIsNone(merge_card_data(None, None))

    def test_live_offer_overrides_regional_out_of_stock(self):
        merged = merge_card_data(
            ProductCardData(in_stock=False),
            ProductCardData(in_stock=True, price_byn=10.0),
        )
        self.assertTrue(merged.in_stock)
        self.assertEqual(merged.price_byn, 10.0)

    def test_out_of_stock_clears_price(self):
        merged = merge_card_data(
            ProductCardData(in_stock=False),
            ProductCardData(in_stock=None, price_byn=10.0),
        )
        self.assertFalse(merged.in_stock)
        self.assertIsNone(merged.price_byn)


if __name__ == "__main__":
    unittest.main()
