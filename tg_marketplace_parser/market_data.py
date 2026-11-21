from dataclasses import replace
from typing import Iterable
from . import thresholds
from .common import log
from .http_client import HttpClientMixin
from .images import ImageDownloadMixin
from .models import ProductInfo, record_stat
from .ozon import OzonMixin
from .wildberries import WildberriesMixin


class MarketDataMixin(HttpClientMixin, ImageDownloadMixin, WildberriesMixin, OzonMixin):
    def _set_enrichment_reject_reason(self, sku_key: str, reason: str) -> None:
        reasons = getattr(self, "_enrichment_reject_reasons", None)
        if not isinstance(reasons, dict):
            reasons = {}
            self._enrichment_reject_reasons = reasons
        if sku_key:
            reasons[sku_key] = reason

    def _pop_enrichment_reject_reason(self, sku_key: str, default: str = "out_of_stock") -> str:
        reasons = getattr(self, "_enrichment_reject_reasons", None)
        if isinstance(reasons, dict):
            return reasons.pop(sku_key, default)
        return default

    async def enrich_product_with_card(
        self,
        product: ProductInfo,
        messages: Iterable = (),
        source_text: str = "",
    ) -> ProductInfo | None:
        """Загружает карточку товара, проверяет наличие и обогащает ProductInfo названием, ценой в BYN, средней ценой и фото.

        Возвращает None, если товара нет в наличии или карточка недоступна.
        """
        if not product.sku:
            if self.settings.require_sku:
                MarketDataMixin._set_enrichment_reject_reason(self, product.sku_key, "invalid_url")
                return None
            return product

        if product.platform == "Wildberries":
            card = await self.fetch_wb_card_data(
                product.sku,
                messages=messages,
                source_text=source_text,
            )
        else:
            card = await self.fetch_ozon_card_data(
                product.sku,
                original_url=product.original_url,
                messages=messages,
                canonical_url=product.canonical_url,
            )

        if card is not None:
            record_stat(self, "cards_parsed")

        if card is not None and card.in_stock is False:
            if self.settings.skip_out_of_stock:
                MarketDataMixin._set_enrichment_reject_reason(self, product.sku_key, "out_of_stock")
                log(f"Товар {product.platform} (арт. {product.sku}) отсутствует в наличии — публикация отменена")
                return None

        if (
            card is not None
            and product.platform == "Wildberries"
            and self.db.get_setting("wb_min_stock_filter", "true") == "true"
            and card.total_quantity is not None
            and card.total_quantity < thresholds.wb_min_stock(self)
        ):
            MarketDataMixin._set_enrichment_reject_reason(self, product.sku_key, "wb_stock")
            log(
                f"Товар Wildberries (арт. {product.sku}) отклонён: "
                f"остаток {card.total_quantity}, требуется не менее {thresholds.wb_min_stock(self)}"
            )
            return None

        if self.settings.require_price and (card is None or card.price_byn is None or card.price_byn <= 0):
            MarketDataMixin._set_enrichment_reject_reason(self, product.sku_key, "no_price")
            log(
                f"Для товара {product.platform} (арт. {product.sku}) не найдена актуальная цена / нет в наличии — "
                f"публикация отменена"
            )
            return None

        if card is None:
            return product

        return replace(
            product,
            title=card.title or product.title,
            brand=card.brand or product.brand,
            price_byn=card.price_byn,
            avg_month_price_byn=card.avg_month_price_byn,
            old_price_byn=card.old_price_byn,
            image_url=card.image_url or product.image_url,
            in_stock=card.in_stock is not False,
            total_quantity=card.total_quantity,
            rating=card.rating,
            reviews_count=card.reviews_count,
            price_source=card.price_source or product.price_source,
        )
