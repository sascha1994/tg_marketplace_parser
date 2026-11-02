import unittest

from tg_marketplace_parser.models import ProductInfo
from tg_marketplace_parser.presentation import build_publication_text


class PublicationStoreBadgeTests(unittest.TestCase):
    def product(self, platform: str, sku: str) -> ProductInfo:
        canonical_url = (
            f"https://ozon.by/product/{int(sku) + 1_440_302_046}/"
            if platform == "Ozon"
            else f"https://www.wildberries.by/catalog/{int(sku) + 123_456}/detail.aspx"
        )
        return ProductInfo(
            original_url=canonical_url,
            resolved_url=canonical_url,
            platform=platform,
            sku=sku,
            canonical_url=canonical_url,
            sku_key=f"{platform}:{sku}",
            title=f"Товар {sku}",
        )

    def test_ozon_badge_is_on_its_own_first_line_without_extra_letters(self):
        text = build_publication_text("", [self.product("Ozon", "1")])

        self.assertTrue(text.startswith("🔷 Ozon\n\n🛍 Товар 1"))
        self.assertNotIn("🔵", text)
        self.assertNotIn("🟣", text)

    def test_wildberries_badge_is_on_its_own_first_line_without_extra_letters(self):
        text = build_publication_text("", [self.product("Wildberries", "2")])

        self.assertTrue(text.startswith("🟪 Wildberries\n\n🛍 Товар 2"))
        self.assertNotIn("🔵", text)
        self.assertNotIn("🟣", text)

    def test_badges_remain_on_the_first_line_of_each_product_in_selections(self):
        text = build_publication_text(
            "",
            [self.product("Ozon", "1"), self.product("Wildberries", "2")],
        )

        self.assertIn("1. 🔷 Ozon\n\n🛍 Товар 1", text)
        self.assertIn("2. 🟪 Wildberries\n\n🛍 Товар 2", text)


if __name__ == "__main__":
    unittest.main()
