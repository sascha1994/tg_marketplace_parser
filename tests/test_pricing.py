import unittest

from tg_marketplace_parser.pricing import (
    _find_math_trio_avg_price,
    _is_plausible_avg_price,
    convert_to_byn,
    extract_avg_month_price_from_text,
    format_byn_price,
    parse_price_value_and_currency,
)


class FormatPriceTests(unittest.TestCase):
    def test_formats_whole_and_fractional_prices(self):
        self.assertEqual(format_byn_price(24.5), "24,50 бел. руб.")
        self.assertEqual(format_byn_price(120), "120 бел. руб.")
        self.assertEqual(format_byn_price(1234.5), "1 234,50 бел. руб.")

    def test_rejects_missing_and_non_positive_prices(self):
        self.assertIsNone(format_byn_price(None))
        self.assertIsNone(format_byn_price(0))
        self.assertIsNone(format_byn_price(-5))


class ParsePriceTests(unittest.TestCase):
    def test_detects_byn_and_rub_currency(self):
        self.assertEqual(parse_price_value_and_currency("10,92 BYN"), (10.92, "BYN"))
        self.assertEqual(parse_price_value_and_currency("320 ₽"), (320.0, "RUB"))
        self.assertEqual(parse_price_value_and_currency("1 299 бел. руб."), (1299.0, "BYN"))

    def test_numbers_use_default_currency(self):
        self.assertEqual(parse_price_value_and_currency(15.5), (15.5, "BYN"))
        self.assertEqual(parse_price_value_and_currency(15.5, "RUB"), (15.5, "RUB"))

    def test_require_currency_rejects_bare_numbers_and_style_names(self):
        self.assertEqual(parse_price_value_and_currency("400", require_currency=True)[0], None)
        self.assertEqual(parse_price_value_and_currency("tsBody400Small", require_currency=True)[0], None)
        self.assertEqual(parse_price_value_and_currency(250, require_currency=True)[0], None)

    def test_empty_and_non_positive_values(self):
        self.assertIsNone(parse_price_value_and_currency(None)[0])
        self.assertIsNone(parse_price_value_and_currency("")[0])
        self.assertIsNone(parse_price_value_and_currency(0)[0])


class ConvertTests(unittest.TestCase):
    def test_converts_rub_and_keeps_byn(self):
        self.assertEqual(convert_to_byn(100, "RUB", 0.0345), 3.45)
        self.assertEqual(convert_to_byn(12.345, "BYN", 0.0345), 12.35)

    def test_invalid_amounts_return_none(self):
        self.assertIsNone(convert_to_byn(None, "BYN", 0.0345))
        self.assertIsNone(convert_to_byn(0, "RUB", 0.0345))


class AveragePriceTests(unittest.TestCase):
    def test_plausibility_bounds(self):
        self.assertTrue(_is_plausible_avg_price(150, 100))
        self.assertFalse(_is_plausible_avg_price(100, 100))
        self.assertFalse(_is_plausible_avg_price(1000, 100))
        self.assertFalse(_is_plausible_avg_price(None, 100))
        self.assertFalse(_is_plausible_avg_price(120, 100, old_price_byn=120))

    def test_math_trio_uses_current_plus_drop_equals_average(self):
        self.assertEqual(_find_math_trio_avg_price([29.15, 8.28, 37.43], actual_price_byn=29.15), 37.43)
        self.assertIsNone(_find_math_trio_avg_price([29.15]))

    def test_extracts_average_from_cheaper_block_text(self):
        text = "29,15 BYN Стало дешевле 8,28 BYN Средняя за сентябрь 37,43 BYN"
        self.assertEqual(extract_avg_month_price_from_text(text, actual_price_byn=29.15), 37.43)

    def test_uses_price_drop_badge_when_average_is_missing(self):
        text = "Цена 29,15 BYN ↓ 8,28 BYN"
        self.assertEqual(extract_avg_month_price_from_text(text, actual_price_byn=29.15), 37.43)

    def test_empty_text_has_no_average(self):
        self.assertIsNone(extract_avg_month_price_from_text(""))


if __name__ == "__main__":
    unittest.main()
