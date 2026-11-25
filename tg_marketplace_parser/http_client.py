import aiohttp
import asyncio
from contextlib import suppress
import ipaddress
import json
import logging
import os
from telethon.tl.functions.messages import GetWebPagePreviewRequest, GetWebPageRequest
import time
from typing import Any
from urllib.parse import quote, urljoin, urlparse
from .common import build_storefront_url, has_expanded_product_url, is_antibot_redirect_loop, is_non_product_url, is_ozon_host, log, normalize_host, strip_antibot_params, validate_marketplace_url
from .constants import DEFAULT_HEADERS, HEAD_READ_LIMIT, HTTP_RETRY_ATTEMPTS, OZON_APP_HEADERS, OZON_DOMAINS, OZON_SHORT_PATH_RE, PREVIEW_USER_AGENTS, WB_BASKET_VOL_CACHE, WB_COOKIE_DOMAINS
from .message_parsing import extract_product_url_from_text, extract_url_from_webpage_obj, resolve_ozon_canonical_host


class HttpClientMixin:
    async def get_rub_to_byn_rate(self) -> float:
        """Возвращает актуальный курс RUB -> BYN по данным Нацбанка РБ (с кэшированием на 6 часов)."""
        now = time.time()
        rate_updated_at = getattr(self, "_rate_updated_at", 0.0)
        fallback_rate = getattr(self, "_rub_to_byn_rate", 0.0345)
        if rate_updated_at > 0 and (now - rate_updated_at) < 21600:
            return fallback_rate

        rate_lock = getattr(self, "_rate_lock", None)
        if rate_lock is None:
            rate_lock = asyncio.Lock()
            self._rate_lock = rate_lock

        async with rate_lock:
            rate_updated_at = getattr(self, "_rate_updated_at", 0.0)
            if rate_updated_at > 0 and (time.time() - rate_updated_at) < 21600:
                return getattr(self, "_rub_to_byn_rate", fallback_rate)

            nbrb_url = "https://api.nbrb.by/exrates/rates/RUB?parammode=2"
            http_session = getattr(self, "http_session", None)
            if http_session is not None:
                with suppress(Exception):
                    async with http_session.get(nbrb_url, timeout=aiohttp.ClientTimeout(total=6)) as resp:
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
            return getattr(self, "_rub_to_byn_rate", fallback_rate)
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
                        if has_expanded_product_url(next_url):
                            log(f"Ссылка раскрыта через curl_cffi ({domain}): {clean_url} -> {next_url}")
                            return next_url

                        if is_antibot_redirect_loop(current_url, next_url):
                            break

                        referer = current_url
                        current_url = next_url
                        continue

                    content = resp.content[:HEAD_READ_LIMIT] if resp.content else b""
                    upgraded = self._extract_sku_from_html(content, str(resp.url or current_url))
                    if upgraded and has_expanded_product_url(upgraded):
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
                        if has_expanded_product_url(str(resp.url)):
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
                        if has_expanded_product_url(final_url):
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

        if self.user_client is not None:
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

        if self.user_client is not None:
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
        if has_expanded_product_url(url):
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
        if via_tg and has_expanded_product_url(via_tg):
            self.state.http_resolved += 1
            return via_tg

        if self.browser_resolver.is_available:
            via_browser, fresh_cookies = await self.browser_resolver.resolve_short_url(clean_url)
            if fresh_cookies:
                self._sync_cookies_to_http_clients(fresh_cookies)
            if via_browser and has_expanded_product_url(via_browser):
                self.state.browser_resolved += 1
                return via_browser

        via_curl = await self._resolve_via_curl_cffi(clean_url)
        if via_curl and has_expanded_product_url(via_curl):
            self.state.http_resolved += 1
            return via_curl

        via_api = await self._resolve_via_ozon_mobile_api(clean_url)
        if via_api and has_expanded_product_url(via_api):
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

        if has_expanded_product_url(url):
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
                if via_curl and has_expanded_product_url(via_curl):
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

            if has_expanded_product_url(current_url):
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
                if has_expanded_product_url(next_url):
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

        resolved_url = await self.upgrade_to_product_url(clean_initial_url, prefetched_body=last_body)
        if has_expanded_product_url(resolved_url):
            return resolved_url
        log(
            f"Ссылка Ozon не раскрыта до полного URL товара, публикация пропущена: "
            f"{clean_initial_url}",
            logging.WARNING,
        )
        return None
