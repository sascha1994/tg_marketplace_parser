import unittest
from types import SimpleNamespace

from tg_marketplace_parser.message_parsing import (
    extract_links,
    extract_product_url_from_text,
    extract_skus_from_text,
    extract_url_from_webpage_obj,
    resolve_ozon_canonical_host,
)


class ExtractLinksTests(unittest.TestCase):
    def test_collects_text_and_entity_links_without_duplicates(self):
        message = SimpleNamespace(
            raw_text="Смотри https://ozon.by/t/abc123, и ещё https://wb.ru/catalog/1/detail.aspx.",
            entities=[SimpleNamespace(url="https://ozon.by/t/abc123"), SimpleNamespace(url=None)],
        )

        self.assertEqual(
            extract_links(message),
            ["https://ozon.by/t/abc123", "https://wb.ru/catalog/1/detail.aspx"],
        )

    def test_handles_message_without_text_or_entities(self):
        self.assertEqual(extract_links(SimpleNamespace(raw_text=None, entities=None)), [])


class ExtractSkusTests(unittest.TestCase):
    def test_finds_labelled_skus_in_order_without_duplicates(self):
        text = "Артикул: 123456789\nарт. 987654321\nКод товара 123456789"
        self.assertEqual(extract_skus_from_text(text), ["123456789", "987654321"])

    def test_ignores_short_numbers_and_empty_text(self):
        self.assertEqual(extract_skus_from_text("Артикул: 123"), [])
        self.assertEqual(extract_skus_from_text(""), [])


class CanonicalHostTests(unittest.TestCase):
    def test_matches_known_ozon_storefronts_including_subdomains(self):
        self.assertEqual(resolve_ozon_canonical_host("www.ozon.kz"), "ozon.kz")
        self.assertEqual(resolve_ozon_canonical_host("ozon.ru."), "ozon.ru")

    def test_falls_back_to_ozon_by_for_unknown_hosts(self):
        self.assertEqual(resolve_ozon_canonical_host("example.com"), "ozon.by")
        self.assertEqual(resolve_ozon_canonical_host(None), "ozon.by")

    def test_valid_override_wins_and_invalid_override_is_ignored(self):
        self.assertEqual(resolve_ozon_canonical_host("ozon.ru", "ozon.kz"), "ozon.kz")
        self.assertEqual(resolve_ozon_canonical_host("ozon.ru", "evil.example"), "ozon.ru")


class ExtractProductUrlTests(unittest.TestCase):
    def test_wildberries_sku_is_normalized_to_belarus_storefront(self):
        html = '<a href="https://www.wildberries.ru/catalog/12345678/detail.aspx?targetUrl=XS">'
        self.assertEqual(
            extract_product_url_from_text(html, "https://wb.ru/"),
            "https://www.wildberries.by/catalog/12345678/detail.aspx",
        )

    def test_ozon_full_product_link_is_returned(self):
        html = '<a href="https://www.ozon.ru/product/some-name-1440302046/">link</a>'
        result = extract_product_url_from_text(html, "https://ozon.ru/t/xyz")
        self.assertIsNotNone(result)
        self.assertIn("1440302046", result)

    def test_page_without_product_returns_none(self):
        self.assertIsNone(extract_product_url_from_text("<html>Привет</html>", "https://ozon.ru/t/xyz"))
        self.assertIsNone(extract_product_url_from_text("", "https://ozon.ru/t/xyz"))


class WebpageObjectTests(unittest.TestCase):
    def test_product_url_is_taken_from_webpage_url(self):
        webpage = SimpleNamespace(url="https://www.ozon.by/product/1440302046/")
        self.assertEqual(
            extract_url_from_webpage_obj(webpage, "https://ozon.by/t/abc"),
            "https://www.ozon.by/product/1440302046/",
        )

    def test_returns_none_for_missing_webpage(self):
        self.assertIsNone(extract_url_from_webpage_obj(None, "https://ozon.by/t/abc"))


if __name__ == "__main__":
    unittest.main()
