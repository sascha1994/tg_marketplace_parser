import aiohttp
import asyncio
from contextlib import suppress
from dataclasses import replace
import io
import ipaddress
import json
import logging
import os
from telethon.tl.functions.messages import (
    GetWebPagePreviewRequest,
    GetWebPageRequest,
)
import time
from typing import (
    Any,
    Iterable,
)
from urllib.parse import (
    quote,
    urljoin,
    urlparse,
)
from .common import (
    build_storefront_url,
    clean_product_title,
    convert_image_bytes_to_jpeg,
    has_product_sku,
    is_antibot_redirect_loop,
    is_non_product_url,
    is_ozon_host,
    log,
    normalize_host,
    normalize_image_url,
    strip_antibot_params,
    validate_marketplace_url,
)
from .constants import (
    BROWSER_USER_AGENT,
    DEFAULT_HEADERS,
    HEAD_READ_LIMIT,
    HTTP_RETRY_ATTEMPTS,
    IMAGE_READ_LIMIT,
    OZON_APP_HEADERS,
    OZON_DOMAINS,
    OZON_SHORT_PATH_RE,
    PREVIEW_USER_AGENTS,
    WB_BASKET_VOL_CACHE,
    WB_COOKIE_DOMAINS,
)
from .marketplace_parsing import (
    extract_price_from_post_text,
    get_wb_candidate_basket_hosts,
    merge_card_data,
    parse_html_product_card,
    parse_ozon_composer_payload,
    parse_wb_detail_payload,
    parse_wb_price_history_avg,
    parse_wb_price_history_latest,
    parse_webpage_preview_card,
)
from .message_parsing import (
    extract_product_url_from_text,
    extract_url_from_webpage_obj,
    match_message_webpage_obj,
    resolve_ozon_canonical_host,
)
from .models import (
    ProductCardData,
    ProductInfo,
)
from .pricing import (
    extract_avg_month_price_from_text,
    extract_ozon_avg_price_from_state,
    extract_ozon_price_modal_urls,
)


class MarketDataMixin:
    async def get_rub_to_byn_rate(self) -> float:
        """Возвращает актуальный курс RUB -> BYN по данным Нацбанка РБ (с кэшированием на 6 часов)."""
        now = time.time()
        if self._rate_updated_at > 0 and (now - self._rate_updated_at) < 21600:
            return self._rub_to_byn_rate

        async with self._rate_lock:
            if self._rate_updated_at > 0 and (time.time() - self._rate_updated_at) < 21600:
                return self._rub_to_byn_rate

            nbrb_url = "https://api.nbrb.by/exrates/rates/RUB?parammode=2"
            if self.http_session is not None:
                with suppress(Exception):
                    async with self.http_session.get(nbrb_url, timeout=aiohttp.ClientTimeout(total=6)) as resp:
                        if resp.status == 200:
                            data = await resp.json(content_type=None)
                            rate = float(data.get("Cur_OfficialRate") or 0)
                            scale = float(data.get("Cur_Scale") or 100)
                            if rate > 0 and scale > 0:
                                self._rub_to_byn_rate = round(rate / scale, 6)
                                self._rate_updated_at = time.time()
                                log(f"Обновлён курс НБРБ: 100 RUB = {rate} BYN (1 RUB = {self._rub_to_byn_rate:.4f} BYN)")
                                return self._rub_to_byn_rate

            self._rate_updated_at = time.time()
            return self._rub_to_byn_rate

    def _sync_cookies_to_http_clients(self, cookies: list[dict]) -> None:
        """Переносит антибот-cookies (Ozon abt_data и Wildberries x_wbaas_token/_wbauid) из браузера в curl_cffi и aiohttp."""
        if not cookies:
            return
        synced = 0
        for cookie in cookies:
            name = cookie.get("name")
            value = cookie.get("value")
            domain = (cookie.get("domain") or "").lstrip(".").lower()
            if not name or value is None:
                continue
            synced += 1
            is_wb_cookie = any(w in domain for w in ("wildberries", "wb.ru")) or name.startswith(
                ("x_wbaas", "_wb", "___wb", "wbx")
            )
            target_domains = WB_COOKIE_DOMAINS if is_wb_cookie else OZON_DOMAINS

            if self.curl_session is not None:
                if domain:
                    with suppress(Exception):
                        self.curl_session.cookies.set(name, value, domain=domain)
                for t_domain in target_domains:
                    with suppress(Exception):
                        self.curl_session.cookies.set(name, value, domain=t_domain)
            if self.http_session is not None:
                for t_domain in target_domains:
                    with suppress(Exception):
                        prefix = "https://" if t_domain.count(".") >= 2 else "https://www."
                        self.http_session.cookie_jar.update_cookies(
                            {name: value},
                            response_url=aiohttp.client.URL(f"{prefix}{t_domain}/"),
                        )
        if synced:
            self.synced_cookie_count = synced

    def _load_cached_cookies_from_disk(self) -> None:
        raw_env_cookies = os.getenv("OZON_COOKIES", "").strip()
        if raw_env_cookies:
            env_list: list[dict] = []
            for part in raw_env_cookies.split(";"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    env_list.append({"name": k.strip(), "value": v.strip(), "domain": "ozon.by"})
            self._sync_cookies_to_http_clients(env_list)

        raw_wb_cookies = os.getenv("WB_COOKIES", "").strip()
        if raw_wb_cookies:
            wb_list: list[dict] = []
            for part in raw_wb_cookies.split(";"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    wb_list.append({"name": k.strip(), "value": v.strip(), "domain": "wildberries.by"})
            self._sync_cookies_to_http_clients(wb_list)

        if os.path.isfile(self.settings.cookies_cache_path):
            with suppress(Exception):
                with open(self.settings.cookies_cache_path, "r", encoding="utf-8") as f:
                    cached = json.load(f)
                if isinstance(cached, list) and cached:
                    self._sync_cookies_to_http_clients(cached)
                    log(f"Загружено {len(cached)} кэшированных антибот-cookies с диска")

        if os.path.isfile(self.settings.wb_baskets_cache_path):
            with suppress(Exception):
                with open(self.settings.wb_baskets_cache_path, "r", encoding="utf-8") as f:
                    baskets_map = json.load(f)
                if isinstance(baskets_map, dict):
                    for k, v in baskets_map.items():
                        with suppress(TypeError, ValueError):
                            if isinstance(v, str) and v.endswith((".wbbasket.ru", ".wbcontent.net")):
                                WB_BASKET_VOL_CACHE[int(k)] = v
                    if WB_BASKET_VOL_CACHE:
                        log(f"Загружено {len(WB_BASKET_VOL_CACHE)} закэшированных шардов корзин WB")

    def _save_wb_baskets_cache_to_disk(self) -> None:
        if not WB_BASKET_VOL_CACHE:
            return
        with suppress(OSError):
            serializable = {str(k): v for k, v in WB_BASKET_VOL_CACHE.items()}
            with open(self.settings.wb_baskets_cache_path, "w", encoding="utf-8") as f:
                json.dump(serializable, f, ensure_ascii=False)

    async def host_has_only_public_ips(self, hostname: str) -> bool:
        """Дополнительная защита от SSRF; allowlist доменов проверяется отдельно."""
        try:
            return ipaddress.ip_address(hostname).is_global
        except ValueError:
            pass
        try:
            loop = asyncio.get_running_loop()
            results = await loop.getaddrinfo(hostname, None, type=0)
            ips = {item[4][0] for item in results}
            return bool(ips) and all(ipaddress.ip_address(ip).is_global for ip in ips)
        except (OSError, ValueError):
            return False

    def _extract_sku_from_html(self, raw_bytes: bytes, url: str) -> str | None:
        if not raw_bytes:
            return None
        text = raw_bytes.decode("utf-8", errors="ignore")
        extracted = extract_product_url_from_text(text, url)
        if extracted:
            log(f"Ссылка на товар извлечена из тела ответа для {url}: {extracted}")
            return extracted
        return None

    async def _fetch_json_any(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        *,
        timeout_override: float | None = None,
    ) -> Any:
        """Выполняет GET-запрос и возвращает распарсенный JSON (dict или list) через curl_cffi или aiohttp.

        Если curl_cffi получил явный ответ 403/429/404 от сервера, не дублирует запрос через aiohttp,
        чтобы не провоцировать каскадный TLS-бан Wildberries/Ozon.
        """
        req_headers = dict(headers or DEFAULT_HEADERS)
        req_headers.setdefault("Accept", "application/json, text/plain, */*")
        req_timeout = timeout_override or self.settings.request_timeout_seconds

        if self.curl_session is not None:
            try:
                async with self.resolve_semaphore:
                    resp = await self.curl_session.get(
                        url,
                        headers=req_headers,
                        allow_redirects=True,
                        timeout=req_timeout,
                    )
                    if resp.status_code in {403, 404, 429}:
                        return {"_http_status": resp.status_code}
                    if 200 <= resp.status_code < 300 and resp.content:
                        return json.loads(resp.content.decode("utf-8", errors="ignore"))
                    return None
            except Exception:
                pass

        if self.http_session is not None:
            with suppress(Exception):
                async with self.resolve_semaphore:
                    client_timeout = aiohttp.ClientTimeout(total=req_timeout)
                    async with self.http_session.get(
                        url,
                        headers=req_headers,
                        allow_redirects=True,
                        timeout=client_timeout,
                    ) as resp:
                        if resp.status in {403, 404, 429}:
                            return {"_http_status": resp.status}
                        if 200 <= resp.status < 300:
                            raw = await resp.content.read(HEAD_READ_LIMIT)
                            return json.loads(raw.decode("utf-8", errors="ignore"))
        return None

    async def _fetch_json_url(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        *,
        timeout_override: float | None = None,
    ) -> dict[str, Any] | None:
        data = await self._fetch_json_any(url, headers=headers, timeout_override=timeout_override)
        return data if isinstance(data, dict) else None

    async def download_product_image(self, image_url: str) -> io.BytesIO | None:
        """Скачивает главное фото из карточки товара и подготавливает его как JPEG для отправки в Telegram."""
        clean_url = normalize_image_url(image_url)
        if not clean_url:
            return None

        candidate_urls = [clean_url]
        if "wbbasket.ru/" in clean_url or "wbcontent.net/" in clean_url:
            if clean_url.endswith("/images/big/1.webp"):
                candidate_urls.append(clean_url[:-5] + ".jpg")
                candidate_urls.append(clean_url.replace("/images/big/1.webp", "/images/c516x688/1.webp"))
                candidate_urls.append(clean_url.replace("/images/big/1.webp", "/images/c516x688/1.jpg"))

        img_headers = {
            "User-Agent": BROWSER_USER_AGENT,
            "Accept": "image/avif,image/webp,image/apng,image/jpeg,image/png,image/*;q=0.8,*/*;q=0.5",
            "Accept-Language": "ru-RU,ru;q=0.9",
        }

        for target_url in candidate_urls:
            if self.curl_session is not None:
                with suppress(Exception):
                    async with self.resolve_semaphore:
                        resp = await self.curl_session.get(
                            target_url,
                            headers=img_headers,
                            allow_redirects=True,
                            timeout=self.settings.request_timeout_seconds,
                        )
                        if 200 <= resp.status_code < 300 and resp.content:
                            converted = await asyncio.to_thread(
                                convert_image_bytes_to_jpeg,
                                resp.content[:IMAGE_READ_LIMIT],
                            )
                            if converted is not None:
                                return converted
                        if resp.status_code == 404:
                            continue

            if self.http_session is not None:
                with suppress(Exception):
                    async with self.resolve_semaphore:
                        async with self.http_session.get(
                            target_url,
                            headers=img_headers,
                            allow_redirects=True,
                        ) as resp:
                            if 200 <= resp.status < 300:
                                raw = await resp.content.read(IMAGE_READ_LIMIT)
                                converted = await asyncio.to_thread(convert_image_bytes_to_jpeg, raw)
                                if converted is not None:
                                    return converted

        return None

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
        if self.user_client.is_connected() and "GetWebPageRequest" in globals() and (
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

    async def fetch_ozon_card_data(
        self,
        sku: str,
        original_url: str = "",
        messages: Iterable = (),
    ) -> ProductCardData | None:
        """Парсит карточку товара Ozon: красивое название, актуальная цена в BYN, средняя цена за месяц, главное фото и наличие."""
        rub_rate = await self.get_rub_to_byn_rate()
        product_path = f"/product/{sku}/"
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
            )
            if fresh_cookies:
                self._sync_cookies_to_http_clients(fresh_cookies)
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
                page_url = f"https://{host}/product/{sku}/" if host == "ozon.by" else f"https://www.{host}/product/{sku}/"
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
                            if html_card is not None:
                                card_data = merge_card_data(card_data, html_card)
                                if card_data.in_stock is False or (
                                    card_data.title and card_data.price_byn is not None and card_data.image_url
                                ):
                                    return card_data

        # 4. Пробуем Telegram GetWebPageRequest для https://ozon.by/product/<sku>/ и превью сообщения
        if self.user_client.is_connected() and "GetWebPageRequest" in globals() and (
            card_data is None or not card_data.title or card_data.price_byn is None
        ):
            for host in ("ozon.by", "www.ozon.ru"):
                default_curr = "BYN" if "ozon.by" in host else "RUB"
                target_url = f"https://{host}/product/{sku}/"
                with suppress(Exception):
                    res = await self.user_client(GetWebPageRequest(url=target_url, hash=0))
                    webpage = getattr(res, "webpage", res)
                    wp_card = parse_webpage_preview_card(
                        webpage,
                        default_currency=default_curr,
                        rub_to_byn_rate=rub_rate,
                    )
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
            return None if self.settings.require_sku else product

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
            )

        if card is not None:
            self.state.cards_parsed += 1

        if card is not None and card.in_stock is False:
            if self.settings.skip_out_of_stock:
                log(f"Товар {product.platform} (арт. {product.sku}) отсутствует в наличии — публикация отменена")
                return None

        if self.settings.require_price and (card is None or card.price_byn is None or card.price_byn <= 0):
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
            rating=card.rating,
            reviews_count=card.reviews_count,
        )

    async def _resolve_via_curl_cffi(self, clean_url: str) -> str | None:
        """Пошагово раскрывает ссылку через curl_cffi с нативным TLS/заголовками его Chrome-профиля."""
        if self.curl_session is None:
            return None

        parsed = urlparse(clean_url)
        orig_domain = resolve_ozon_canonical_host(parsed.hostname)
        storefronts: list[str] = [orig_domain]
        for domain in self.settings.ozon_preferred_storefronts:
            if domain not in storefronts:
                storefronts.append(domain)

        for domain in storefronts:
            for use_www in (False, True):
                current_url = (
                    build_storefront_url(clean_url, domain, use_www=use_www)
                    if is_ozon_host(parsed.hostname)
                    else clean_url
                )
                referer: str | None = None

                for _step in range(1, self.settings.max_redirects + 2):
                    req_headers = {"Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7"}
                    if referer:
                        req_headers["Referer"] = referer

                    try:
                        async with self.resolve_semaphore:
                            resp = await self.curl_session.get(
                                current_url,
                                headers=req_headers,
                                allow_redirects=False,
                                timeout=self.settings.request_timeout_seconds,
                            )
                    except Exception as exc:
                        log(f"curl_cffi ошибка на {current_url}: {type(exc).__name__}: {exc}", logging.DEBUG)
                        break

                    status = resp.status_code
                    location = resp.headers.get("location") or resp.headers.get("Location")

                    if location and 300 <= status < 400:
                        next_url = urljoin(current_url, location)
                        if has_product_sku(next_url):
                            log(f"Ссылка раскрыта через curl_cffi ({domain}): {clean_url} -> {next_url}")
                            return next_url

                        if is_antibot_redirect_loop(current_url, next_url):
                            break

                        referer = current_url
                        current_url = next_url
                        continue

                    content = resp.content[:HEAD_READ_LIMIT] if resp.content else b""
                    upgraded = self._extract_sku_from_html(content, str(resp.url or current_url))
                    if upgraded and has_product_sku(upgraded):
                        log(f"Артикул извлечён через curl_cffi HTML ({domain}): {clean_url} -> {upgraded}")
                        return upgraded
                    break

        return None

    async def _resolve_via_ozon_mobile_api(self, clean_url: str) -> str | None:
        """Пробует раскрыть /p/oXXXXXXX через мобильное API Ozon (composer-api / entrypoint-api) на всех витринах."""
        assert self.http_session is not None
        parsed = urlparse(clean_url)
        if not is_ozon_host(parsed.hostname):
            return None
        path = parsed.path or ""
        if not path or path == "/":
            return None

        encoded_path = quote(path, safe="/")
        orig_domain = resolve_ozon_canonical_host(parsed.hostname)
        storefronts = [orig_domain]
        for domain in self.settings.ozon_preferred_storefronts:
            if domain not in storefronts:
                storefronts.append(domain)

        api_endpoints: list[str] = []
        for host in storefronts:
            api_endpoints.extend(
                (
                    f"https://api.{host}/composer-api.bx/page/json/v2?url={encoded_path}",
                    f"https://www.{host}/api/entrypoint-api.bx/page/json/v2?url={encoded_path}",
                    f"https://www.{host}/api/composer-api.bx/page/json/v2?url={encoded_path}",
                )
            )

        if self.curl_session is not None:
            for api_url in api_endpoints:
                with suppress(Exception):
                    async with self.resolve_semaphore:
                        resp = await self.curl_session.get(
                            api_url,
                            headers=OZON_APP_HEADERS,
                            allow_redirects=True,
                            max_redirects=4,
                        )
                        if has_product_sku(str(resp.url)):
                            return str(resp.url)
                        upgraded = self._extract_sku_from_html(resp.content[:HEAD_READ_LIMIT], clean_url)
                        if upgraded:
                            log(f"Ссылка раскрыта через curl_cffi composer-api: {clean_url} -> {upgraded}")
                            return upgraded

        for api_url in api_endpoints:
            try:
                async with self.resolve_semaphore:
                    async with self.http_session.get(
                        api_url,
                        allow_redirects=True,
                        max_redirects=2,
                        headers=OZON_APP_HEADERS,
                    ) as response:
                        final_url = str(response.url)
                        if has_product_sku(final_url):
                            return final_url
                        raw = await response.content.read(HEAD_READ_LIMIT)
            except (aiohttp.ClientError, asyncio.TimeoutError):
                continue

            upgraded = self._extract_sku_from_html(raw, clean_url)
            if upgraded:
                return upgraded

        return None

    async def _resolve_via_telegram_webpage(self, clean_url: str) -> str | None:
        """Просит серверы Telegram получить превью ссылки (для всех витрин ozon.by / ozon.ru)."""
        if not self.user_client.is_connected():
            return None

        parsed = urlparse(clean_url)
        candidate_urls = [clean_url]
        if is_ozon_host(parsed.hostname):
            for domain in self.settings.ozon_preferred_storefronts:
                mirror = build_storefront_url(clean_url, domain)
                if mirror not in candidate_urls:
                    candidate_urls.append(mirror)

        if "GetWebPageRequest" in globals():
            for target_url in candidate_urls:
                for wait_attempt in range(2):
                    try:
                        res = await self.user_client(GetWebPageRequest(url=target_url, hash=0))
                        webpage = getattr(res, "webpage", res)
                        wp_type = type(webpage).__name__
                        extracted = extract_url_from_webpage_obj(webpage, clean_url)
                        if extracted:
                            log(f"Ссылка раскрыта через Telegram GetWebPageRequest ({target_url}): {clean_url} -> {extracted}")
                            return extracted
                        if "Pending" in wp_type and wait_attempt == 0:
                            await asyncio.sleep(1.2)
                            continue
                        break
                    except Exception as exc:
                        log(f"GetWebPageRequest ошибка для {target_url}: {type(exc).__name__}: {exc}", logging.DEBUG)
                        break

        if "GetWebPagePreviewRequest" in globals():
            for target_url in candidate_urls[:2]:
                with suppress(Exception):
                    media = await self.user_client(GetWebPagePreviewRequest(message=target_url))
                    webpage = getattr(media, "webpage", media)
                    extracted = extract_url_from_webpage_obj(webpage, clean_url)
                    if extracted:
                        log(f"Ссылка раскрыта через Telegram GetWebPagePreviewRequest: {clean_url} -> {extracted}")
                        return extracted

        return None

    async def upgrade_to_product_url(self, url: str, prefetched_body: bytes | None = None) -> str:
        """Если в URL нет артикула (например, ozon.ru/p/oXXXX), ищем его через каскад обхода антибота."""
        if has_product_sku(url):
            return url

        clean_url = strip_antibot_params(url)
        host = normalize_host(urlparse(clean_url).hostname)
        if not is_ozon_host(host):
            return clean_url

        if prefetched_body:
            upgraded = self._extract_sku_from_html(prefetched_body, clean_url)
            if upgraded:
                self.state.http_resolved += 1
                return upgraded

        via_tg = await self._resolve_via_telegram_webpage(clean_url)
        if via_tg and has_product_sku(via_tg):
            self.state.http_resolved += 1
            return via_tg

        if self.browser_resolver.is_available:
            via_browser, fresh_cookies = await self.browser_resolver.resolve_short_url(clean_url)
            if fresh_cookies:
                self._sync_cookies_to_http_clients(fresh_cookies)
            if via_browser and has_product_sku(via_browser):
                self.state.browser_resolved += 1
                return via_browser

        via_curl = await self._resolve_via_curl_cffi(clean_url)
        if via_curl and has_product_sku(via_curl):
            self.state.http_resolved += 1
            return via_curl

        via_api = await self._resolve_via_ozon_mobile_api(clean_url)
        if via_api and has_product_sku(via_api):
            self.state.http_resolved += 1
            return via_api

        log(f"Артикул для короткой ссылки не найден: {clean_url}", logging.WARNING)
        return clean_url

    async def _request_step(
        self,
        url: str,
        referer: str | None = None,
    ) -> tuple[int, str | None, str | None, bytes]:
        """Выполняет один шаг запроса с полными браузерными заголовками и повторами."""
        assert self.http_session is not None
        parsed = urlparse(url)
        is_ozon_short = is_ozon_host(parsed.hostname) and bool(OZON_SHORT_PATH_RE.match(parsed.path or ""))

        last_exc: Exception | None = None
        for attempt in range(1, HTTP_RETRY_ATTEMPTS + 1):
            headers = dict(DEFAULT_HEADERS)
            if attempt > 1:
                headers["User-Agent"] = PREVIEW_USER_AGENTS[(attempt - 1) % len(PREVIEW_USER_AGENTS)]
            if referer:
                headers["Referer"] = referer
                headers["Sec-Fetch-Site"] = "same-origin"

            try:
                async with self.resolve_semaphore:
                    if not is_ozon_short:
                        async with self.http_session.head(
                            url,
                            allow_redirects=False,
                            headers=headers,
                        ) as response:
                            status = response.status
                            location = response.headers.get("Location")
                            antibot = response.headers.get("ozon-antibot")
                            response.release()
                            if status not in {403, 405, 501} and not (antibot == "1" and not location):
                                return status, location, antibot, b""

                    async with self.http_session.get(
                        url,
                        allow_redirects=False,
                        headers=headers,
                    ) as response:
                        status = response.status
                        location = response.headers.get("Location")
                        antibot = response.headers.get("ozon-antibot")
                        body = b""
                        if not location and is_ozon_host(parsed.hostname):
                            with suppress(Exception):
                                body = await response.content.read(HEAD_READ_LIMIT)
                        response.release()

                        if status == 403 and not location and attempt < HTTP_RETRY_ATTEMPTS:
                            await asyncio.sleep(0.35 * attempt)
                            continue
                        return status, location, antibot, body

            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_exc = exc
                if attempt < HTTP_RETRY_ATTEMPTS:
                    await asyncio.sleep(0.6 * attempt)
                    continue

        raise last_exc or RuntimeError(f"Не удалось выполнить запрос к {url}")

    async def resolve_url(self, url: str) -> str | None:
        if not validate_marketplace_url(url):
            return None

        if has_product_sku(url):
            return url

        if is_non_product_url(url):
            log(f"Пропущена нетоварная ссылка: {url}")
            return None

        clean_initial_url = strip_antibot_params(url)
        parsed_initial = urlparse(clean_initial_url)

        if (
            is_ozon_host(parsed_initial.hostname)
            and OZON_SHORT_PATH_RE.match(parsed_initial.path or "")
        ):
            if self.curl_session is not None:
                via_curl = await self._resolve_via_curl_cffi(clean_initial_url)
                if via_curl and has_product_sku(via_curl):
                    self.state.http_resolved += 1
                    return via_curl

        assert self.http_session is not None
        current_url = clean_initial_url
        referer: str | None = None
        max_redirects = self.settings.max_redirects
        last_body = b""

        for step in range(1, max_redirects + 2):
            host = normalize_host(urlparse(current_url).hostname)
            if not validate_marketplace_url(current_url) or not await self.host_has_only_public_ips(host):
                log(f"Отклонён небезопасный URL/редирект: {current_url}", logging.WARNING)
                return None

            if has_product_sku(current_url):
                log(f"Артикул найден в URL на шаге {step}: {current_url}")
                self.state.http_resolved += 1
                return current_url

            if is_non_product_url(current_url):
                log(f"Редирект привёл на нетоварную страницу: {current_url}")
                return None

            try:
                status, location, antibot, body = await self._request_step(current_url, referer=referer)
                if body:
                    last_body = body
            except Exception as exc:
                log(f"Не удалось раскрыть URL {url}: {type(exc).__name__}: {exc}", logging.WARNING)
                break

            if location and 300 <= status < 400:
                next_url = urljoin(current_url, location)
                if has_product_sku(next_url):
                    log(f"Редирект {step}/{max_redirects}: {status} {current_url} -> {next_url}")
                    self.state.http_resolved += 1
                    return next_url

                if is_antibot_redirect_loop(current_url, next_url):
                    log(
                        f"Обнаружен цикл антибота Ozon ({status} {current_url} -> {next_url}); "
                        f"запускаем браузерную эмуляцию для {clean_initial_url}"
                    )
                    break

                if step > max_redirects:
                    log(f"Превышен лимит редиректов для {url}", logging.WARNING)
                    break

                log(f"Редирект {step}/{max_redirects}: {status} {current_url} -> {next_url}")
                referer = current_url
                current_url = next_url
                continue

            log(f"Конец цепочки: HTTP {status}, ozon-antibot={antibot}, {current_url}")
            break

        return await self.upgrade_to_product_url(clean_initial_url, prefetched_body=last_body)
