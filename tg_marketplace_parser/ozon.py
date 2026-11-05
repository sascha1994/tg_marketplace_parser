from contextlib import suppress
from dataclasses import replace
import json
from telethon.tl.functions.messages import GetWebPageRequest
from typing import Iterable
from urllib.parse import quote
from .common import ozon_product_path
from .constants import HEAD_READ_LIMIT, OZON_APP_HEADERS
from .marketplace_parsing import merge_card_data, parse_html_product_card, parse_ozon_composer_payload, parse_webpage_preview_card
from .message_parsing import match_message_webpage_obj
from .models import ProductCardData, record_cascade
from .pricing import extract_avg_month_price_from_text, extract_ozon_avg_price_from_state, extract_ozon_price_modal_urls


class OzonMixin:
    async def fetch_ozon_card_data(
        self,
        sku: str,
        original_url: str = "",
        messages: Iterable = (),
        canonical_url: str = "",
    ) -> ProductCardData | None:
        """Парсит карточку товара Ozon: красивое название, актуальная цена в BYN, средняя цена за месяц, главное фото и наличие."""
        rub_rate = await self.get_rub_to_byn_rate()
        product_path = ozon_product_path(sku, canonical_url)
        encoded_path = quote(product_path, safe="/")

        card_data: ProductCardData | None = None

        # 1. Пробуем JSON API Ozon (entrypoint-api / composer-api) на витрине ozon.by
        storefronts = ["ozon.by"]
        for d in self.settings.ozon_preferred_storefronts:
            if d not in storefronts:
                storefronts.append(d)

        for host in storefronts:
            default_curr = "BYN" if host == "ozon.by" else "RUB"
            endpoints = (
                f"https://{host}/api/entrypoint-api.bx/page/json/v2?url={encoded_path}",
                f"https://www.{host}/api/entrypoint-api.bx/page/json/v2?url={encoded_path}",
                f"https://api.{host}/composer-api.bx/page/json/v2?url={encoded_path}",
                f"https://www.{host}/api/composer-api.bx/page/json/v2?url={encoded_path}",
            )
            for ep_url in endpoints:
                payload = await self._fetch_json_url(ep_url, headers=OZON_APP_HEADERS)
                if not payload or "_http_status" in payload:
                    if payload and payload.get("_http_status") == 404:
                        return ProductCardData(in_stock=False)
                    continue
                parsed = parse_ozon_composer_payload(
                    payload,
                    default_currency=default_curr,
                    rub_to_byn_rate=rub_rate,
                )
                record_cascade(self, "ozon_api", parsed is not None and parsed.price_byn is not None)
                if parsed is not None:
                    # Если средняя цена не нашлась напрямую, но в widgetStates есть ссылка на модальное окно «Стало дешевле»
                    if parsed.avg_month_price_byn is None:
                        modal_urls = extract_ozon_price_modal_urls(payload)
                        for m_path in modal_urls[:2]:
                            enc_m = quote(m_path, safe="/?=&")
                            m_ep = f"https://{host}/api/entrypoint-api.bx/page/json/v2?url={enc_m}"
                            m_payload = await self._fetch_json_url(m_ep, headers=OZON_APP_HEADERS, timeout_override=4.0)
                            if m_payload and "_http_status" not in m_payload:
                                m_avg = extract_ozon_avg_price_from_state(
                                    m_payload,
                                    actual_price_byn=parsed.price_byn,
                                    old_price_byn=parsed.old_price_byn,
                                    default_currency=default_curr,
                                    rub_to_byn_rate=rub_rate,
                                ) or extract_avg_month_price_from_text(
                                    json.dumps(m_payload, ensure_ascii=False),
                                    actual_price_byn=parsed.price_byn,
                                    old_price_byn=parsed.old_price_byn,
                                    default_currency=default_curr,
                                    rub_to_byn_rate=rub_rate,
                                )
                                if m_avg is not None:
                                    parsed = replace(parsed, avg_month_price_byn=m_avg)
                                    break

                    card_data = merge_card_data(card_data, parsed)
                    if card_data.in_stock is False:
                        return card_data
                    if (
                        card_data.title
                        and card_data.price_byn is not None
                        and card_data.image_url
                        and card_data.avg_month_price_byn is not None
                    ):
                        return card_data

        # 2. Пробуем получить карточку (включая попап «Стало дешевле» и фото) через прогретый браузер Playwright
        if self.browser_resolver.is_available and (
            card_data is None
            or not card_data.title
            or card_data.price_byn is None
            or not card_data.image_url
            or card_data.avg_month_price_byn is None
        ):
            browser_card, fresh_cookies = await self.browser_resolver.fetch_product_card(
                sku,
                rub_to_byn_rate=rub_rate,
                canonical_url=canonical_url,
            )
            if fresh_cookies:
                self._sync_cookies_to_http_clients(fresh_cookies)
            record_cascade(self, "ozon_browser", browser_card is not None and browser_card.price_byn is not None)
            if browser_card is not None:
                card_data = merge_card_data(card_data, browser_card)
                if card_data.in_stock is False:
                    return card_data
                if card_data.title and card_data.price_byn is not None and card_data.image_url:
                    return card_data

        # 3. Пробуем прямой HTML карточки на ozon.by через curl_cffi
        if self.curl_session is not None and (
            card_data is None or not card_data.title or card_data.price_byn is None or not card_data.image_url
        ):
            for host in storefronts[:2]:
                default_curr = "BYN" if host == "ozon.by" else "RUB"
                page_url = (
                    f"https://{host}{product_path}"
                    if host == "ozon.by"
                    else f"https://www.{host}{product_path}"
                )
                with suppress(Exception):
                    async with self.resolve_semaphore:
                        resp = await self.curl_session.get(
                            page_url,
                            headers={"Accept-Language": "ru-RU,ru;q=0.9"},
                            allow_redirects=True,
                            timeout=self.settings.request_timeout_seconds,
                        )
                        if resp.status_code == 404:
                            return ProductCardData(in_stock=False)
                        if resp.content:
                            html_text = resp.content[:HEAD_READ_LIMIT].decode("utf-8", errors="ignore")
                            html_card = parse_html_product_card(
                                html_text,
                                default_currency=default_curr,
                                rub_to_byn_rate=rub_rate,
                            )
                            record_cascade(self, "ozon_curl", html_card is not None and html_card.price_byn is not None)
                            if html_card is not None:
                                card_data = merge_card_data(card_data, html_card)
                                if card_data.in_stock is False or (
                                    card_data.title and card_data.price_byn is not None and card_data.image_url
                                ):
                                    return card_data

        # 4. Пробуем Telegram GetWebPageRequest для https://ozon.by/product/<sku>/ и превью сообщения
        if self.user_client.is_connected() and (
            card_data is None or not card_data.title or card_data.price_byn is None
        ):
            for host in ("ozon.by", "www.ozon.ru"):
                default_curr = "BYN" if "ozon.by" in host else "RUB"
                target_url = f"https://{host}{product_path}"
                with suppress(Exception):
                    res = await self.user_client(GetWebPageRequest(url=target_url, hash=0))
                    webpage = getattr(res, "webpage", res)
                    wp_card = parse_webpage_preview_card(
                        webpage,
                        default_currency=default_curr,
                        rub_to_byn_rate=rub_rate,
                    )
                    record_cascade(self, "telegram_preview", wp_card is not None and wp_card.price_byn is not None)
                    if wp_card is not None:
                        card_data = merge_card_data(card_data, wp_card)
                        if card_data.in_stock is False or (card_data.title and card_data.price_byn is not None):
                            return card_data

        wp_msg_obj = match_message_webpage_obj(messages, original_url, sku, 1)
        if wp_msg_obj:
            wp_card = parse_webpage_preview_card(
                wp_msg_obj,
                default_currency="BYN",
                rub_to_byn_rate=rub_rate,
            )
            card_data = merge_card_data(card_data, wp_card)

        return card_data
