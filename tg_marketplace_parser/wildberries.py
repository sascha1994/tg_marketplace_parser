import asyncio
from contextlib import suppress
from dataclasses import replace
from telethon.tl.functions.messages import GetWebPageRequest
from typing import Any, Iterable
from urllib.parse import urlparse
from .common import clean_product_title, log
from .constants import BROWSER_USER_AGENT, WB_BASKET_VOL_CACHE
from .marketplace_parsing import extract_price_from_post_text, get_wb_candidate_basket_hosts, merge_card_data, parse_wb_detail_payload, parse_wb_price_history_avg, parse_wb_price_history_latest, parse_webpage_preview_card
from .message_parsing import match_message_webpage_obj
from .models import ProductCardData, record_cascade


class WildberriesMixin:
    async def _resolve_wb_basket_host(
        self,
        sku_int: int,
        req_headers: dict[str, str],
    ) -> tuple[str, dict[str, Any] | None]:
        """Определяет рабочий шард basket-XX.wbbasket.ru для артикула WB и возвращает (basket_host, card_json).

        Если расчётный шард возвращает 404 (например, для новых 9- и 10-значных артикулов),
        выполняет быстрый параллельный пробинг соседних шардов и кэширует найденный хост для vol.
        """
        vol = sku_int // 100_000
        part = sku_int // 1_000
        candidates = get_wb_candidate_basket_hosts(vol)

        # 1. Проверяем первый (кэшированный или расчётный) хост
        primary_host = candidates[0]
        primary_url = f"https://{primary_host}/vol{vol}/part{part}/{sku_int}/info/ru/card.json"
        card_data = await self._fetch_json_url(primary_url, headers=req_headers, timeout_override=4.0)
        if isinstance(card_data, dict) and "_http_status" not in card_data and (
            card_data.get("imt_name") or card_data.get("subj_name") or card_data.get("nm_id") or card_data.get("imt_id")
        ):
            if WB_BASKET_VOL_CACHE.get(vol) != primary_host:
                WB_BASKET_VOL_CACHE[vol] = primary_host
                self._save_wb_baskets_cache_to_disk()
            return primary_host, card_data

        # 2. Если расчётный баскет вернул 404 / пусто, пробируем остальные шарды батчами по 10
        remaining = candidates[1:]
        batch_size = 10
        for i in range(0, len(remaining), batch_size):
            batch = remaining[i:i + batch_size]

            async def _probe_host(host: str) -> tuple[str, dict[str, Any] | None]:
                u = f"https://{host}/vol{vol}/part{part}/{sku_int}/info/ru/card.json"
                res = await self._fetch_json_url(u, headers=req_headers, timeout_override=3.2)
                if isinstance(res, dict) and "_http_status" not in res and (
                    res.get("imt_name") or res.get("subj_name") or res.get("nm_id") or res.get("imt_id")
                ):
                    return host, res
                return host, None

            results = await asyncio.gather(*(_probe_host(h) for h in batch), return_exceptions=True)
            for item in results:
                if isinstance(item, tuple) and item[1] is not None:
                    found_host, found_card = item
                    WB_BASKET_VOL_CACHE[vol] = found_host
                    self._save_wb_baskets_cache_to_disk()
                    log(f"Найден рабочий шард корзины WB для vol={vol} (арт. {sku_int}): {found_host}")
                    return found_host, found_card

        return primary_host, None
    async def _fetch_wb_basket_card_extras(
        self,
        sku: str,
        *,
        rub_to_byn_rate: float = 0.0345,
    ) -> tuple[str, str, float | None, float | None, str, float | None, int | None, bool]:
        """Получает с сервера корзины Wildberries (basket-XX.wbbasket.ru):
        - полное название и бренд (info/ru/card.json)
        - среднюю цену за месяц и последнюю известную цену (info/price-history.json)
        - ссылку на главное фото карточки товара (images/big/1.webp)
        - рейтинг и число отзывов через публичный CDN отзывов (feedbacks1/2.wb.ru) при наличии imt_id
        - флаг card_exists (подтверждение существования карточки товара на CDN)
        """
        try:
            sku_int = int(sku)
        except ValueError:
            return "", "", None, None, "", None, None, False

        vol = sku_int // 100_000
        part = sku_int // 1_000

        req_headers = {
            "User-Agent": BROWSER_USER_AGENT,
            "Accept": "application/json",
            "Origin": "https://www.wildberries.by",
            "Referer": f"https://www.wildberries.by/catalog/{sku_int}/detail.aspx",
        }

        basket_host, card_data = await self._resolve_wb_basket_host(sku_int, req_headers)
        base_path = f"https://{basket_host}/vol{vol}/part{part}/{sku_int}"
        history_json_url = f"{base_path}/info/price-history.json"
        image_url = f"{base_path}/images/big/1.webp"

        history_data = await self._fetch_json_any(history_json_url, headers=req_headers, timeout_override=4.0)

        title = ""
        brand_name = ""
        imt_id = None
        card_exists = False

        if isinstance(card_data, dict) and "_http_status" not in card_data:
            card_exists = True
            imt_id = card_data.get("imt_id")
            imt_name = str(card_data.get("imt_name") or card_data.get("subj_name") or "").strip()
            selling = card_data.get("selling")
            if isinstance(selling, dict):
                brand_name = str(selling.get("brand_name") or "").strip()
            if not brand_name:
                brand_name = str(card_data.get("brand_name") or "").strip()
            title = clean_product_title(imt_name, brand_name)

        avg_month_price_byn = None
        latest_history_price_byn = None
        if isinstance(history_data, list):
            avg_month_price_byn = parse_wb_price_history_avg(
                history_data,
                rub_to_byn_rate=rub_to_byn_rate,
            )
            latest_history_price_byn = parse_wb_price_history_latest(
                history_data,
                rub_to_byn_rate=rub_to_byn_rate,
            )

        rating: float | None = None
        reviews_count: int | None = None
        if imt_id:
            for fb_host in ("feedbacks1.wb.ru", "feedbacks2.wb.ru"):
                fb_url = f"https://{fb_host}/feedbacks/v2/{imt_id}"
                fb_data = await self._fetch_json_url(fb_url, headers=req_headers, timeout_override=3.5)
                if isinstance(fb_data, dict) and "_http_status" not in fb_data:
                    with suppress(TypeError, ValueError):
                        val = fb_data.get("valuation")
                        if val is not None and float(val) > 0:
                            rating = float(val)
                    with suppress(TypeError, ValueError):
                        cnt = fb_data.get("feedbackCount") or fb_data.get("feedbackCountWithText")
                        if cnt is not None and int(cnt) >= 0:
                            reviews_count = int(cnt)
                    if rating is not None or reviews_count is not None:
                        break

        return (
            title,
            brand_name,
            avg_month_price_byn,
            latest_history_price_byn,
            image_url,
            rating,
            reviews_count,
            card_exists,
        )
    async def fetch_wb_card_data(
        self,
        sku: str,
        messages: Iterable = (),
        source_text: str = "",
    ) -> ProductCardData | None:
        """Парсит карточку товара Wildberries: название, актуальная цена в BYN, средняя цена за месяц, фото и наличие.

        Использует многоуровневый каскад:
        1. CDN корзины WB (динамический поиск basket-XX.wbbasket.ru, card.json, price-history.json, feedbacks).
        2. Прямые и внутренние JSON API карточек (card.wb.ru v4/v2, __internal/card/cards/v4/detail, u-card.wb.ru)
           по регионам Беларуси и РФ без преждевременного обрыва цикла при региональном отсутствии.
        3. Поисковый индекс WB (search.wb.ru/exactmatch/ru/{shard}/v18/search и v9/search по артикулу).
        4. Браузерный резолвер Playwright (перехват XHR + чтение DOM страницы wildberries.by / wildberries.ru).
        5. Telegram WebPage-превью (GetWebPageRequest + медиа сообщения).
        6. Резервная цена из истории цен WB (price-history.json) или текста исходного поста, если API цен закрыт 403.
        """
        rub_rate = await self.get_rub_to_byn_rate()
        wb_headers = {
            "User-Agent": BROWSER_USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ru-RU,ru;q=0.9",
            "Origin": "https://www.wildberries.by",
            "Referer": f"https://www.wildberries.by/catalog/{sku}/detail.aspx",
            "x-requested-with": "XMLHttpRequest",
        }

        # 1. Сразу получаем открытые данные с Basket CDN (название, бренд, историю цен, фото, рейтинг)
        (
            basket_title,
            basket_brand,
            avg_month_byn,
            latest_history_byn,
            basket_img,
            basket_rating,
            basket_reviews,
            card_exists,
        ) = await self._fetch_wb_basket_card_extras(
            sku,
            rub_to_byn_rate=rub_rate,
        )

        card_data: ProductCardData | None = None
        record_cascade(self, "wb_basket", bool(basket_title or basket_img or avg_month_byn is not None))
        if basket_title or basket_img or avg_month_byn is not None:
            card_data = ProductCardData(
                title=basket_title,
                brand=basket_brand,
                avg_month_price_byn=avg_month_byn,
                image_url=basket_img,
                rating=basket_rating,
                reviews_count=basket_reviews,
            )

        # 2. Опрашиваем JSON API карточек (v4 / v2) на всех хостах и регионах (BYN и RUB)
        dest_candidates = (self.settings.wb_dest_byn, -59208, -59202, -1257786)
        unique_dests = list(dict.fromkeys(dest_candidates))

        api_hosts = (
            ("https://card.wb.ru/cards/v4/detail", "https://www.wildberries.by"),
            ("https://card.wb.ru/cards/v2/detail", "https://www.wildberries.by"),
            ("https://www.wildberries.by/__internal/card/cards/v4/detail", "https://www.wildberries.by"),
            ("https://www.wildberries.ru/__internal/card/cards/v4/detail", "https://www.wildberries.ru"),
            ("https://u-card.wb.ru/cards/v4/detail", "https://www.wildberries.by"),
        )

        explicit_oos_responses = 0
        successful_api_responses = 0
        blocked_hosts: set[str] = set()

        for base_endpoint, origin_url in api_hosts:
            host_key = urlparse(base_endpoint).netloc
            if host_key in blocked_hosts:
                continue

            headers = dict(wb_headers)
            headers["Origin"] = origin_url
            headers["Referer"] = f"{origin_url}/catalog/{sku}/detail.aspx"

            for dest in unique_dests:
                detail_url = (
                    f"{base_endpoint}"
                    f"?appType=1&curr=byn&dest={dest}&spp={self.settings.wb_spp}&nm={sku}"
                )
                payload = await self._fetch_json_url(detail_url, headers=headers, timeout_override=5.0)
                if not payload:
                    continue
                http_status = payload.get("_http_status")
                if http_status in {403, 429, 404}:
                    if http_status in {403, 429}:
                        blocked_hosts.add(host_key)
                    break

                parsed = parse_wb_detail_payload(
                    payload,
                    sku=sku,
                    currency="BYN",
                    rub_to_byn_rate=rub_rate,
                )
                record_cascade(self, "wb_card_api", parsed is not None and parsed.price_byn is not None)
                if parsed is not None:
                    successful_api_responses += 1
                    if parsed.in_stock is False:
                        explicit_oos_responses += 1
                    else:
                        card_data = merge_card_data(parsed, card_data)
                        if card_data and card_data.title and card_data.price_byn is not None:
                            return card_data

            if card_data and card_data.title and card_data.price_byn is not None:
                return card_data

            # Пробуем также RUB-витрину (Москва -1257786 и 123585595), если для BYN цены не оказалось
            if host_key not in blocked_hosts:
                for rub_dest in (-1257786, 123585595):
                    rub_url = (
                        f"{base_endpoint}"
                        f"?appType=1&curr=rub&dest={rub_dest}&spp={self.settings.wb_spp}&nm={sku}"
                    )
                    payload_rub = await self._fetch_json_url(rub_url, headers=headers, timeout_override=5.0)
                    if not payload_rub:
                        continue
                    if payload_rub.get("_http_status") in {403, 429, 404}:
                        if payload_rub.get("_http_status") in {403, 429}:
                            blocked_hosts.add(host_key)
                        break
                    parsed_rub = parse_wb_detail_payload(
                        payload_rub,
                        sku=sku,
                        currency="RUB",
                        rub_to_byn_rate=rub_rate,
                    )
                    if parsed_rub is not None:
                        successful_api_responses += 1
                        if parsed_rub.in_stock is False:
                            explicit_oos_responses += 1
                        else:
                            card_data = merge_card_data(parsed_rub, card_data)
                            if card_data and card_data.title and card_data.price_byn is not None:
                                return card_data

        # 3. Пробуем поисковый API WB (search.wb.ru/exactmatch/ru/{shard}/v18/search и v9/search) по артикулу
        if card_data is None or card_data.price_byn is None:
            search_shards = ("common", "preset", "msk")
            search_blocked = False
            for api_v in ("v18", "v9"):
                if search_blocked:
                    break
                for shard in search_shards:
                    for curr_code, s_dest in (("byn", self.settings.wb_dest_byn), ("rub", -1257786)):
                        search_url = (
                            f"https://search.wb.ru/exactmatch/ru/{shard}/{api_v}/search"
                            f"?appType=1&curr={curr_code}&dest={s_dest}&page=1&query={sku}"
                            f"&resultset=catalog&sort=popular&spp={self.settings.wb_spp}"
                        )
                        s_payload = await self._fetch_json_url(search_url, headers=wb_headers, timeout_override=5.0)
                        if not s_payload:
                            continue
                        s_status = s_payload.get("_http_status")
                        if s_status == 403:
                            search_blocked = True
                            break
                        if s_status in {429, 404}:
                            continue
                        parsed_search = parse_wb_detail_payload(
                            s_payload,
                            sku=sku,
                            currency=curr_code.upper(),
                            rub_to_byn_rate=rub_rate,
                        )
                        record_cascade(self, "wb_search", parsed_search is not None and parsed_search.price_byn is not None)
                        if parsed_search is not None and parsed_search.price_byn is not None:
                            card_data = merge_card_data(parsed_search, card_data)
                            if card_data and card_data.title and card_data.price_byn is not None:
                                return card_data

        # Если API карточек успешно ответили (HTTP 200) на нескольких регионах и везде подтвердили отсутствие товара
        if successful_api_responses >= 2 and explicit_oos_responses == successful_api_responses and (
            card_data is None or card_data.price_byn is None
        ):
            return replace(card_data, in_stock=False, price_byn=None) if card_data else ProductCardData(in_stock=False)

        # 4. Браузерный резолвер Playwright (обходит блокировку 403 на card.wb.ru и читает живой DOM карточки WB)
        if self.browser_resolver.is_available and (card_data is None or card_data.price_byn is None):
            browser_card, fresh_cookies = await self.browser_resolver.fetch_wb_product_card(
                sku,
                rub_to_byn_rate=rub_rate,
            )
            if fresh_cookies:
                self._sync_cookies_to_http_clients(fresh_cookies)
            record_cascade(self, "wb_browser", browser_card is not None and browser_card.price_byn is not None)
            if browser_card is not None:
                if browser_card.in_stock is False and (card_data is None or card_data.price_byn is None):
                    return replace(
                        merge_card_data(card_data, browser_card),
                        in_stock=False,
                        price_byn=None,
                    )
                card_data = merge_card_data(browser_card, card_data)
                if card_data and card_data.title and card_data.price_byn is not None:
                    return card_data

        # 5. Проверяем Telegram GetWebPageRequest и WebPage-превью исходного сообщения
        if self.user_client.is_connected() and (
            card_data is None or not card_data.title or card_data.price_byn is None
        ):
            for wb_host, def_curr in (("www.wildberries.by", "BYN"), ("www.wildberries.ru", "RUB")):
                target_url = f"https://{wb_host}/catalog/{sku}/detail.aspx"
                with suppress(Exception):
                    res = await self.user_client(GetWebPageRequest(url=target_url, hash=0))
                    webpage = getattr(res, "webpage", res)
                    wp_card = parse_webpage_preview_card(
                        webpage,
                        default_currency=def_curr,
                        rub_to_byn_rate=rub_rate,
                    )
                    if wp_card is not None:
                        if wp_card.in_stock is False and (card_data is None or card_data.price_byn is None):
                            return replace(merge_card_data(card_data, wp_card), in_stock=False, price_byn=None)
                        card_data = merge_card_data(card_data, wp_card)
                        if card_data and card_data.title and card_data.price_byn is not None:
                            return card_data

        wp_obj = match_message_webpage_obj(messages, "", sku, 1)
        if wp_obj:
            wp_card = parse_webpage_preview_card(wp_obj, default_currency="BYN", rub_to_byn_rate=rub_rate)
            if wp_card is not None:
                if wp_card.in_stock is False and (card_data is None or card_data.price_byn is None):
                    return replace(merge_card_data(card_data, wp_card), in_stock=False, price_byn=None)
                card_data = merge_card_data(card_data, wp_card)

        # 6. Если живой эндпоинт цен закрыт антиботом (403), но карточка существует и не помечена как «нет в наличии»:
        # используем последнюю известную цену из price-history.json или цену из текста поста
        if card_data is not None and card_data.in_stock is not False and card_data.price_byn is None:
            fallback_price = latest_history_byn
            if fallback_price is None and source_text:
                fallback_price = extract_price_from_post_text(source_text, rub_to_byn_rate=rub_rate)
            if fallback_price is not None and fallback_price > 0:
                log(
                    f"Для товара Wildberries (арт. {sku}) использована резервная цена {fallback_price} BYN "
                    f"(история цен / текст поста)"
                )
                card_data = replace(card_data, price_byn=fallback_price, in_stock=True)
            elif card_exists and card_data.title:
                card_data = replace(card_data, in_stock=True)

        return card_data
    async def fetch_wb_cards_batch(
        self,
        skus: Iterable[str],
    ) -> dict[str, ProductCardData | None]:
        """Checks a bounded set of WB SKUs in one detail request."""
        requested = list(dict.fromkeys(str(sku).strip() for sku in skus if str(sku).strip()))[:20]
        if not requested:
            return {}
        encoded_skus = ",".join(requested)
        url = (
            "https://card.wb.ru/cards/v4/detail"
            f"?appType=1&curr=byn&dest={self.settings.wb_dest_byn}"
            f"&spp={self.settings.wb_spp}&nm={encoded_skus}"
        )
        payload = await self._fetch_json_url(
            url,
            headers={
                "Origin": "https://www.wildberries.by",
                "Referer": "https://www.wildberries.by/",
            },
            timeout_override=8.0,
        )
        if not payload or payload.get("_http_status") in {403, 404, 429}:
            return {sku: None for sku in requested}
        return {
            sku: parse_wb_detail_payload(
                payload,
                sku=sku,
                currency="BYN",
                rub_to_byn_rate=self._rub_to_byn_rate,
            )
            for sku in requested
        }
