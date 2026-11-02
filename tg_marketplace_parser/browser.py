import asyncio
from contextlib import suppress
from dataclasses import replace
import json
import logging
import os
import random
import re
import time
from urllib.parse import (
    parse_qs,
    quote,
    urljoin,
    urlparse,
)
from .common import (
    build_storefront_url,
    clean_product_title,
    has_expanded_product_url,
    is_ozon_host,
    log,
    normalize_image_url,
    ozon_product_path,
)
from .config import Settings
from .constants import (
    MINIMAL_STEALTH_SCRIPT,
    OUT_OF_STOCK_MARKERS,
    WB_BASKET_HOST_IN_URL_RE,
    WB_BASKET_VOL_CACHE,
)
from .marketplace_parsing import (
    get_wb_card_image_url,
    merge_card_data,
    parse_html_product_card,
    parse_ozon_composer_payload,
    parse_ozon_visible_price_text,
    parse_wb_detail_payload,
    parse_wb_price_history_avg,
    parse_wb_price_history_latest,
)
from .message_parsing import (
    extract_product_url_from_text,
    resolve_ozon_canonical_host,
)
from .models import ProductCardData
from .optional_dependencies import (
    _ensure_playwright_browser_installed,
    _find_system_chromium,
    async_playwright_factory,
)
from .pricing import (
    convert_to_byn,
    extract_avg_month_price_from_text,
    extract_ozon_avg_price_from_state,
    extract_ozon_price_modal_urls,
    parse_price_value_and_currency,
)


class OzonBrowserResolver:
    """Браузерный резолвер коротких ссылок и парсер карточек Ozon с обходом Variti antibot."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._playwright = None
        self._context = None
        self._warm_pages: dict[str, object] = {}
        self._lock = asyncio.Lock()
        self._last_used: float = 0.0
        self._idle_task: asyncio.Task | None = None
        self._available: bool = bool(settings.use_browser_resolver and async_playwright_factory is not None)
        self._cdp_ua_metadata: dict | None = None
        self.last_cookies: list[dict] = []

    @property
    def is_available(self) -> bool:
        return self._available

    @property
    def is_running(self) -> bool:
        return self._context is not None

    async def start_idle_monitor(self) -> None:
        if self._idle_task is None and self._available:
            self._idle_task = asyncio.create_task(self._idle_watchdog(), name="ozon-browser-watchdog")

    async def _idle_watchdog(self) -> None:
        while True:
            await asyncio.sleep(30)
            if self._context is not None and self._last_used > 0:
                idle_for = time.time() - self._last_used
                if idle_for >= self.settings.browser_idle_timeout:
                    async with self._lock:
                        if self._context is not None and (time.time() - self._last_used) >= self.settings.browser_idle_timeout:
                            log("Закрытие браузера Ozon после простоя для освобождения памяти")
                            await self._close_unlocked()

    async def _apply_cdp_stealth(self, page) -> None:
        """Применяет нативный C++ UserAgentOverride через CDP без расхождения ОС и без HeadlessChrome."""
        with suppress(Exception):
            if self._cdp_ua_metadata is None:
                raw_ua, raw_platform = await page.evaluate("[navigator.userAgent, navigator.platform]")
                clean_ua = raw_ua.replace("HeadlessChrome/", "Chrome/")
                ver_match = re.search(r"Chrome/(\d+)\.(\d+\.\d+\.\d+)", clean_ua)
                major = ver_match.group(1) if ver_match else "131"
                full_ver = f"{major}.{ver_match.group(2)}" if ver_match else "131.0.0.0"

                if "Linux" in raw_platform or "X11" in clean_ua:
                    platform_name = "Linux"
                    platform_ver = "6.5.0"
                elif "Mac" in raw_platform:
                    platform_name = "macOS"
                    platform_ver = "14.5.0"
                else:
                    platform_name = "Windows"
                    platform_ver = "10.0.0"

                self._cdp_ua_metadata = {
                    "userAgent": clean_ua,
                    "acceptLanguage": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
                    "platform": raw_platform or "Linux x86_64",
                    "userAgentMetadata": {
                        "brands": [
                            {"brand": "Not_A Brand", "version": "24"},
                            {"brand": "Chromium", "version": major},
                            {"brand": "Google Chrome", "version": major},
                        ],
                        "fullVersionList": [
                            {"brand": "Not_A Brand", "version": "24.0.0.0"},
                            {"brand": "Chromium", "version": full_ver},
                            {"brand": "Google Chrome", "version": full_ver},
                        ],
                        "fullVersion": full_ver,
                        "platform": platform_name,
                        "platformVersion": platform_ver,
                        "architecture": "x86",
                        "model": "",
                        "mobile": False,
                        "bitness": "64",
                        "wow64": False,
                    },
                }

            cdp = await page.context.new_cdp_session(page)
            await cdp.send("Emulation.setUserAgentOverride", self._cdp_ua_metadata)

    async def _ensure_context(self) -> bool:
        if not self._available:
            return False
        if self._context is not None:
            self._last_used = time.time()
            return True

        os.makedirs(self.settings.browser_profile_dir, exist_ok=True)
        for lock_file in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            lock_path = os.path.join(self.settings.browser_profile_dir, lock_file)
            with suppress(OSError):
                if os.path.lexists(lock_path):
                    os.unlink(lock_path)

        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, _ensure_playwright_browser_installed)

            self._playwright = await async_playwright_factory().start()
            cdp_url = os.getenv("OZON_BROWSER_CDP_URL", "").strip()
            if cdp_url:
                browser = await self._playwright.chromium.connect_over_cdp(cdp_url)
                contexts = browser.contexts
                self._context = contexts[0] if contexts else await browser.new_context()
                await self._context.add_init_script(MINIMAL_STEALTH_SCRIPT)
                log(f"Подключён внешний браузер по CDP: {cdp_url}")
            else:
                chrome_args = [
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-infobars",
                    "--window-size=1920,1080",
                    "--lang=ru-RU,ru",
                ]
                if self.settings.browser_headless:
                    chrome_args.append("--headless=new")

                launch_kwargs: dict = {
                    "user_data_dir": self.settings.browser_profile_dir,
                    "headless": self.settings.browser_headless,
                    "viewport": {"width": 1920, "height": 1080},
                    "locale": "ru-RU",
                    "timezone_id": "Europe/Minsk",
                    "java_script_enabled": True,
                    "ignore_https_errors": True,
                    "args": chrome_args,
                }
                if self.settings.browser_headless:
                    launch_kwargs["ignore_default_args"] = ["--headless"]

                exec_path = _find_system_chromium()
                if exec_path:
                    launch_kwargs["executable_path"] = exec_path
                if self.settings.ozon_proxy:
                    launch_kwargs["proxy"] = {"server": self.settings.ozon_proxy}

                try:
                    self._context = await self._playwright.chromium.launch_persistent_context(**launch_kwargs)
                except Exception:
                    launch_kwargs.pop("ignore_default_args", None)
                    self._context = await self._playwright.chromium.launch_persistent_context(**launch_kwargs)

                await self._context.add_init_script(MINIMAL_STEALTH_SCRIPT)
                init_page = self._context.pages[0] if self._context.pages else await self._context.new_page()
                await self._apply_cdp_stealth(init_page)
                log("Запущен Chromium (--headless=new + CDP native UA) для обхода антибота Ozon")

            self._last_used = time.time()
            return True
        except Exception as exc:
            log(f"Не удалось запустить браузер Playwright для Ozon: {type(exc).__name__}: {exc}", logging.WARNING)
            await self._close_unlocked()
            return False

    async def _simulate_human_interaction(self, page) -> None:
        with suppress(Exception):
            for _ in range(3):
                x = random.randint(180, 1100)
                y = random.randint(140, 720)
                await page.mouse.move(x, y, steps=random.randint(4, 10))
                await asyncio.sleep(random.uniform(0.1, 0.25))

    async def _has_abt_cookie(self) -> bool:
        if self._context is None:
            return False
        with suppress(Exception):
            cookies = await self._context.cookies()
            return any(c.get("name") == "abt_data" and c.get("value") for c in cookies)
        return False

    async def _warm_up_storefront(self, domain: str):
        """Открывает и прогревает главную страницу витрины (например https://ozon.by/ или https://www.ozon.ru/)."""
        assert self._context is not None
        existing = self._warm_pages.get(domain)
        if existing is not None:
            with suppress(Exception):
                if not existing.is_closed() and is_ozon_host(urlparse(existing.url).hostname):
                    return existing
            self._warm_pages.pop(domain, None)

        page = await self._context.new_page()
        await self._apply_cdp_stealth(page)
        home_url = f"https://www.{domain}/" if domain == "ozon.ru" else f"https://{domain}/"

        try:
            resp = await page.goto(home_url, wait_until="domcontentloaded", timeout=12_000)
            status = resp.status if resp else 0
            title = ""
            with suppress(Exception):
                title = (await page.title()).strip()

            deadline = time.monotonic() + self.settings.browser_challenge_timeout
            while time.monotonic() < deadline:
                if await self._has_abt_cookie() and "доступ ограничен" not in title.lower():
                    break

                await self._simulate_human_interaction(page)

                with suppress(Exception):
                    reload_btn = await page.query_selector(
                        "button#reload-button, button[type='submit'], a.reload-button, button"
                    )
                    if reload_btn:
                        btn_text = ((await reload_btn.inner_text()) or "").strip().lower()
                        if any(w in btn_text for w in ("обновить", "продолжить", "reload", "повторить")):
                            await reload_btn.click()

                await asyncio.sleep(0.8)
                with suppress(Exception):
                    title = (await page.title()).strip()
                    if title and "доступ ограничен" not in title.lower() and "почти готово" not in title.lower():
                        break

            has_abt = await self._has_abt_cookie()
            log(
                f"Прогрев витрины {domain}: HTTP {status}, title={title!r}, "
                f"abt_data={'да' if has_abt else 'нет'}, url={page.url}"
            )
            self._warm_pages[domain] = page
            return page
        except Exception as exc:
            log(f"Ошибка прогрева витрины {domain}: {type(exc).__name__}: {exc}", logging.DEBUG)
            with suppress(Exception):
                await page.close()
            return None

    async def _fetch_via_inpage_api(self, page, short_path: str, base_url: str) -> str | None:
        """Выполняет запросы к entrypoint-api / composer-api и самой короткой ссылке прямо из контекста прогретой страницы."""
        if not short_path or not short_path.startswith("/"):
            return None

        encoded_path = quote(short_path, safe="/")
        with suppress(Exception):
            payload = await page.evaluate(
                """async ({ shortPath, encodedPath }) => {
                    const results = [];
                    const endpoints = [
                        `/api/entrypoint-api.bx/page/json/v2?url=${encodedPath}`,
                        `/api/composer-api.bx/page/json/v2?url=${encodedPath}`,
                        shortPath
                    ];
                    for (const ep of endpoints) {
                        try {
                            const resp = await fetch(ep, {
                                method: 'GET',
                                credentials: 'include',
                                redirect: 'follow',
                                headers: {
                                    'Accept': ep.startsWith('/api/') ? 'application/json' : 'text/html,application/xhtml+xml,*/*;q=0.8'
                                }
                            });
                            if (resp.url && resp.url.includes('/product/')) {
                                return { url: resp.url, body: '' };
                            }
                            const text = await resp.text();
                            if (text && (text.includes('/product/') || text.includes('"sku"') || text.includes('skuId'))) {
                                return { url: resp.url || '', body: text.slice(0, 250000) };
                            }
                            results.push(`${ep}:${resp.status}`);
                        } catch (e) {
                            results.push(`${ep}:err`);
                        }
                    }
                    return { url: '', body: '', debug: results.join(',') };
                }""",
                {"shortPath": short_path, "encodedPath": encoded_path},
            )
            if isinstance(payload, dict):
                direct_url = payload.get("url") or ""
                if direct_url and has_expanded_product_url(direct_url):
                    return direct_url
                body = payload.get("body") or ""
                if body:
                    extracted = extract_product_url_from_text(body, page.url or base_url)
                    if extracted and has_expanded_product_url(extracted):
                        return extracted
                debug_info = payload.get("debug")
                if debug_info:
                    log(f"In-page fetch ({page.url}): {debug_info}", logging.DEBUG)
        return None

    async def fetch_product_card(
        self,
        sku: str,
        *,
        rub_to_byn_rate: float = 0.0345,
        canonical_url: str = "",
    ) -> tuple[ProductCardData | None, list[dict]]:
        """Получает данные карточки товара Ozon (название, цена в BYN, средняя цена за месяц, главное фото, наличие) через браузер.

        Оптимизированный многоуровневый алгоритм для «Стало дешевле»:
        1. In-page API через прогретую вкладку (warm_page): за ~100 мс запрашивает JSON карточки товара,
           а при наличии ссылок на модальное окно «Стало дешевле» в widgetStates — сразу же подгружает JSON модалки
           в том же вызове без открытия новой вкладки.
        2. Если средняя цена не пришла в in-page API (требуется интерактивное открытие попапа):
           открывает лёгкую вкладку с блокировкой тяжёлых медиа/картинок/шрифтов, перехватывает все XHR-ответы
           модальных окон Ozon, кликает по «Стало дешевле» (Playwright + JS fallback) и ждёт появления
           «Средняя за ...» / «Цена сейчас» в портале модального окна или в сетевом ответе.
        """
        if not self._available or not sku:
            return None, []

        async with self._lock:
            if not await self._ensure_context():
                return None, []

            self._last_used = time.time()
            storefronts: list[str] = ["ozon.by"]
            for domain in self.settings.ozon_preferred_storefronts:
                if domain not in storefronts:
                    storefronts.append(domain)

            product_path = ozon_product_path(sku, canonical_url)
            encoded_path = quote(product_path, safe="/")

            for domain in storefronts:
                default_currency = "BYN" if domain == "ozon.by" else "RUB"
                warm_page = await self._warm_up_storefront(domain)
                inpage_card: ProductCardData | None = None

                if warm_page is not None:
                    with suppress(Exception):
                        inpage_bundle_str = await warm_page.evaluate(
                            """async ({ encodedPath }) => {
                                const endpoints = [
                                    `/api/entrypoint-api.bx/page/json/v2?url=${encodedPath}`,
                                    `/api/composer-api.bx/page/json/v2?url=${encodedPath}`
                                ];
                                let mainText = '';
                                for (const ep of endpoints) {
                                    try {
                                        const resp = await fetch(ep, {
                                            method: 'GET',
                                            credentials: 'include',
                                            redirect: 'follow',
                                            headers: { 'Accept': 'application/json' }
                                        });
                                        if (resp.status === 404) {
                                            return JSON.stringify({ _not_found: true });
                                        }
                                        const text = await resp.text();
                                        if (text && (text.includes('widgetStates') || text.includes('"seo"'))) {
                                            mainText = text;
                                            break;
                                        }
                                    } catch (e) {}
                                }
                                if (!mainText) return '';

                                const modalTexts = [];
                                try {
                                    const lowerMain = mainText.toLowerCase();
                                    if (lowerMain.includes('стало дешевле') || lowerMain.includes('pricedecrease') || lowerMain.includes('/modal/')) {
                                        const parsed = JSON.parse(mainText);
                                        const ws = (parsed && parsed.widgetStates) || {};
                                        const candidateModalUrls = new Set();
                                        const collectLinks = (obj, inCheaper) => {
                                            if (!obj || typeof obj !== 'object') return;
                                            const dump = JSON.stringify(obj).toLowerCase();
                                            const hasCheaper = inCheaper || dump.includes('стало дешевле') || dump.includes('pricedecrease');
                                            const isTight = dump.includes('стало дешевле') && dump.length < 1200;
                                            for (const [k, v] of Object.entries(obj)) {
                                                const kl = k.toLowerCase();
                                                if (typeof v === 'string') {
                                                    let s = v.trim().replace(/\\\\\\//g, '/');
                                                    if (s.startsWith('ozon://')) s = '/' + s.slice(7).replace(/^\\/+/, '');
                                                    const sl = s.toLowerCase();
                                                    const isModalHint = (
                                                        sl.includes('/modal/') ||
                                                        sl.includes('modal=') ||
                                                        sl.includes('pricedecrease') ||
                                                        sl.includes('price_decrease') ||
                                                        sl.includes('price-decrease') ||
                                                        sl.includes('cheaper') ||
                                                        sl.includes('stat=price')
                                                    );
                                                    const isLinkKey = ['link', 'deeplink', 'url', 'modalurl', 'uri', 'href', 'action', 'path', 'targeturl'].includes(kl);
                                                    if (s.startsWith('/') && (isModalHint || (isTight && isLinkKey && !sl.startsWith('/product/')))) {
                                                        candidateModalUrls.add(s);
                                                    }
                                                } else if (v && typeof v === 'object') {
                                                    collectLinks(v, hasCheaper);
                                                }
                                            }
                                        };
                                        for (const [wKey, rawState] of Object.entries(ws)) {
                                            if (typeof rawState !== 'string') continue;
                                            const wl = wKey.toLowerCase();
                                            const rl = rawState.toLowerCase();
                                            if (wl.includes('webprice') || wl.includes('pricedecrease') || rl.includes('стало дешевле') || rl.includes('modal')) {
                                                try {
                                                    collectLinks(JSON.parse(rawState), rl.includes('стало дешевле'));
                                                } catch (e) {}
                                            }
                                        }
                                        for (const mPath of Array.from(candidateModalUrls).slice(0, 3)) {
                                            try {
                                                const encM = encodeURIComponent(mPath);
                                                const mResp = await fetch(`/api/entrypoint-api.bx/page/json/v2?url=${encM}`, {
                                                    method: 'GET',
                                                    credentials: 'include',
                                                    headers: { 'Accept': 'application/json' }
                                                });
                                                if (mResp.ok) {
                                                    const mTxt = await mResp.text();
                                                    if (mTxt) modalTexts.push(mTxt);
                                                }
                                            } catch (e) {}
                                        }
                                    }
                                } catch (e) {}

                                return JSON.stringify({ main: mainText, modals: modalTexts });
                            }""",
                            {"encodedPath": encoded_path},
                        )
                        if inpage_bundle_str:
                            bundle = json.loads(inpage_bundle_str)
                            if isinstance(bundle, dict):
                                if bundle.get("_not_found"):
                                    cookies = await self._export_cookies()
                                    return ProductCardData(in_stock=False), cookies
                                raw_main = bundle.get("main") or ""
                                modal_list = bundle.get("modals") or []
                                data = json.loads(raw_main) if raw_main else None
                                if isinstance(data, dict):
                                    inpage_card = parse_ozon_composer_payload(
                                        data,
                                        default_currency=default_currency,
                                        rub_to_byn_rate=rub_to_byn_rate,
                                    )
                                    if inpage_card and inpage_card.in_stock is False:
                                        cookies = await self._export_cookies()
                                        return inpage_card, cookies

                                    # Если средняя цена не нашлась в основном ответе, проверяем подгруженные JSON модальных окон
                                    if inpage_card and inpage_card.avg_month_price_byn is None:
                                        extra_modal_urls = extract_ozon_price_modal_urls(data)
                                        for m_raw in modal_list:
                                            with suppress(Exception):
                                                m_data = json.loads(m_raw)
                                                m_avg = extract_ozon_avg_price_from_state(
                                                    m_data,
                                                    actual_price_byn=inpage_card.price_byn,
                                                    old_price_byn=inpage_card.old_price_byn,
                                                    default_currency=default_currency,
                                                    rub_to_byn_rate=rub_to_byn_rate,
                                                ) or extract_avg_month_price_from_text(
                                                    m_raw,
                                                    actual_price_byn=inpage_card.price_byn,
                                                    old_price_byn=inpage_card.old_price_byn,
                                                    default_currency=default_currency,
                                                    rub_to_byn_rate=rub_to_byn_rate,
                                                )
                                                if m_avg is not None:
                                                    inpage_card = replace(inpage_card, avg_month_price_byn=m_avg)
                                                    break

                                        # Если в Python нашлись дополнительные пути модалок, догружаем их через warm_page
                                        if inpage_card.avg_month_price_byn is None and extra_modal_urls and not modal_list:
                                            for m_path in extra_modal_urls[:2]:
                                                with suppress(Exception):
                                                    enc_m = quote(m_path, safe="/?=&")
                                                    m_str = await warm_page.evaluate(
                                                        """async ({ encM }) => {
                                                            try {
                                                                const r = await fetch(`/api/entrypoint-api.bx/page/json/v2?url=${encM}`, {
                                                                    credentials: 'include',
                                                                    headers: { 'Accept': 'application/json' }
                                                                });
                                                                return r.ok ? await r.text() : '';
                                                            } catch (e) { return ''; }
                                                        }""",
                                                        {"encM": enc_m},
                                                    )
                                                    if m_str:
                                                        m_obj = json.loads(m_str)
                                                        m_avg = extract_ozon_avg_price_from_state(
                                                            m_obj,
                                                            actual_price_byn=inpage_card.price_byn,
                                                            old_price_byn=inpage_card.old_price_byn,
                                                            default_currency=default_currency,
                                                            rub_to_byn_rate=rub_to_byn_rate,
                                                        ) or extract_avg_month_price_from_text(
                                                            m_str,
                                                            actual_price_byn=inpage_card.price_byn,
                                                            old_price_byn=inpage_card.old_price_byn,
                                                            default_currency=default_currency,
                                                            rub_to_byn_rate=rub_to_byn_rate,
                                                        )
                                                        if m_avg is not None:
                                                            inpage_card = replace(inpage_card, avg_month_price_byn=m_avg)
                                                            break

                                    if (
                                        inpage_card
                                        and inpage_card.title
                                        and inpage_card.price_byn is not None
                                        and inpage_card.image_url
                                        and inpage_card.avg_month_price_byn is not None
                                    ):
                                        cookies = await self._export_cookies()
                                        log(
                                            f"Карточка Ozon {sku} полностью получена через in-page API ({domain}): "
                                            f"price={inpage_card.price_byn} BYN, avg={inpage_card.avg_month_price_byn} BYN"
                                        )
                                        return inpage_card, cookies

                # Открываем страницу товара во вкладке для извлечения DOM / попапа «Стало дешевле» / перехвата XHR модалки
                assert self._context is not None
                nav_page = await self._context.new_page()
                await self._apply_cdp_stealth(nav_page)

                intercepted_avg_byn: float | None = None
                intercepted_card: ProductCardData | None = None

                # Блокируем загрузку тяжёлых картинок, видео и шрифтов на вкладке-доноре, чтобы страница и Vue-гидратация загружались в 3 раза быстрее
                async def _fast_route_handler(route) -> None:
                    with suppress(Exception):
                        if route.request.resource_type in {"image", "media", "font"}:
                            await route.abort()
                            return
                        await route.continue_()

                with suppress(Exception):
                    await nav_page.route("**/*", _fast_route_handler)

                async def _handle_ozon_response(resp, curr: str = default_currency) -> None:
                    nonlocal intercepted_avg_byn, intercepted_card
                    with suppress(Exception):
                        r_url = (resp.url or "").lower()
                        if resp.status < 200 or resp.status >= 300:
                            return
                        if (
                            "/entrypoint-api.bx/" in r_url
                            or "/composer-api.bx/" in r_url
                            or "/modal/" in r_url
                            or "pricedecrease" in r_url
                        ):
                            raw_bytes = await resp.body()
                            if not raw_bytes:
                                return
                            txt = raw_bytes.decode("utf-8", errors="ignore")
                            if not txt:
                                return
                            ref_price = (
                                (inpage_card.price_byn if inpage_card else None)
                                or (intercepted_card.price_byn if intercepted_card else None)
                            )
                            ref_old = (
                                (inpage_card.old_price_byn if inpage_card else None)
                                or (intercepted_card.old_price_byn if intercepted_card else None)
                            )
                            payload_obj = json.loads(txt)
                            if isinstance(payload_obj, dict):
                                if "widgetStates" in payload_obj or "seo" in payload_obj:
                                    parsed_resp = parse_ozon_composer_payload(
                                        payload_obj,
                                        default_currency=curr,
                                        rub_to_byn_rate=rub_to_byn_rate,
                                    )
                                    if parsed_resp is not None:
                                        intercepted_card = merge_card_data(intercepted_card, parsed_resp)
                                        if parsed_resp.avg_month_price_byn is not None:
                                            intercepted_avg_byn = parsed_resp.avg_month_price_byn
                                            return
                                found_avg = extract_ozon_avg_price_from_state(
                                    payload_obj,
                                    actual_price_byn=ref_price,
                                    old_price_byn=ref_old,
                                    default_currency=curr,
                                    rub_to_byn_rate=rub_to_byn_rate,
                                )
                                if found_avg is not None:
                                    intercepted_avg_byn = found_avg
                                    return
                            found_txt_avg = extract_avg_month_price_from_text(
                                txt,
                                actual_price_byn=ref_price,
                                old_price_byn=ref_old,
                                default_currency=curr,
                                rub_to_byn_rate=rub_to_byn_rate,
                            )
                            if found_txt_avg is not None:
                                intercepted_avg_byn = found_txt_avg

                nav_page.on("response", lambda r: asyncio.create_task(_handle_ozon_response(r)))

                target_url = (
                    f"https://{domain}{product_path}"
                    if domain == "ozon.by"
                    else f"https://www.{domain}{product_path}"
                )
                try:
                    resp = await nav_page.goto(target_url, wait_until="domcontentloaded", timeout=11_000)
                    if resp and resp.status == 404:
                        cookies = await self._export_cookies()
                        return ProductCardData(in_stock=False), cookies

                    # Ожидаем появления кнопки «Стало дешевле» (или уже отрисованной «Средняя за ...») до 3.2 сек
                    wait_deadline = time.monotonic() + 3.2
                    cheaper_found = False
                    while time.monotonic() < wait_deadline:
                        if intercepted_avg_byn is not None:
                            break
                        with suppress(Exception):
                            state_check = await nav_page.evaluate(
                                """() => {
                                    const txt = (document.body ? document.body.innerText : '').toLowerCase();
                                    if (txt.includes('средняя за') || txt.includes('цена сейчас')) return 'modal_ready';
                                    if (txt.includes('стало дешевле')) return 'button_ready';
                                    return '';
                                }"""
                            )
                            if state_check == "modal_ready":
                                break
                            if state_check == "button_ready":
                                cheaper_found = True
                                break
                        await asyncio.sleep(0.22)

                    # Если кнопка «Стало дешевле» найдена, кликаем по ней (Playwright + JS) и ждём открытия попапа / XHR-ответа
                    if cheaper_found and intercepted_avg_byn is None:
                        for click_attempt in range(2):
                            with suppress(Exception):
                                for sel in (
                                    '[data-widget="webPrice"] >> text=Стало дешевле',
                                    'text=Стало дешевле',
                                ):
                                    cheaper_el = await nav_page.query_selector(sel)
                                    if cheaper_el:
                                        await cheaper_el.click(timeout=1200, force=True)
                                        break
                            with suppress(Exception):
                                await nav_page.evaluate(
                                    """() => {
                                        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                                        let node;
                                        while ((node = walker.nextNode())) {
                                            if (node.nodeValue && node.nodeValue.toLowerCase().includes('стало дешевле')) {
                                                const el = node.parentElement;
                                                if (!el) continue;
                                                const clickable = el.closest('button, a, [role="button"], [tabindex]') || el;
                                                for (const target of [clickable, el, el.parentElement]) {
                                                    if (!target) continue;
                                                    try { target.click(); } catch (e) {}
                                                    try {
                                                        target.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
                                                        target.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true }));
                                                        target.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
                                                    } catch (e) {}
                                                }
                                                break;
                                            }
                                        }
                                    }"""
                                )

                            # Ждём появления текста модального окна («Средняя за ...» / «Цена сейчас») или перехвата XHR
                            popup_deadline = time.monotonic() + (1.1 if click_attempt == 0 else 1.4)
                            modal_appeared = False
                            while time.monotonic() < popup_deadline:
                                if intercepted_avg_byn is not None:
                                    modal_appeared = True
                                    break
                                with suppress(Exception):
                                    has_modal_txt = await nav_page.evaluate(
                                        """() => {
                                            const txt = (document.body ? document.body.innerText : '').toLowerCase();
                                            return txt.includes('средняя за') || txt.includes('цена сейчас');
                                        }"""
                                    )
                                    if has_modal_txt:
                                        modal_appeared = True
                                        break
                                await asyncio.sleep(0.14)
                            if modal_appeared:
                                break

                    html_content = await nav_page.content()
                    dom_card = parse_html_product_card(
                        html_content,
                        default_currency=default_currency,
                        rub_to_byn_rate=rub_to_byn_rate,
                    )
                    visible_price_card: ProductCardData | None = None

                    # Дополнительно проверяем текст открытого модального окна, весь видимый текст страницы и главное фото через JS
                    with suppress(Exception):
                        dom_extra = await nav_page.evaluate(
                            """() => {
                                const modalSelectors = [
                                    '[role="dialog"]',
                                    '[aria-modal="true"]',
                                    '.vue-portal-target',
                                    '[data-widget*="Modal"]',
                                    '[data-widget*="Price"]',
                                    '[class*="modal"]',
                                    '[class*="popover"]'
                                ];
                                const modalParts = [];
                                for (const sel of modalSelectors) {
                                    for (const el of document.querySelectorAll(sel)) {
                                        const t = (el.innerText || el.textContent || '').trim();
                                        if (t && (t.toLowerCase().includes('средн') || t.toLowerCase().includes('стало дешевле') || t.toLowerCase().includes('цена сейчас'))) {
                                            modalParts.push(t);
                                        }
                                    }
                                }
                                const bodyText = document.body ? document.body.innerText : '';
                                const priceWidget = document.querySelector('[data-widget="webPrice"]');
                                const galleryImg = document.querySelector('[data-widget="webGallery"] img, [data-widget="webMobGallery"] img');
                                return {
                                    modalText: modalParts.join(' \\n '),
                                    bodyText: bodyText.length > 120000 ? (bodyText.slice(0, 60000) + ' \\n ' + bodyText.slice(-60000)) : bodyText,
                                    priceText: priceWidget ? (priceWidget.innerText || priceWidget.textContent || '') : '',
                                    imgSrc: galleryImg ? (galleryImg.currentSrc || galleryImg.src || '') : ''
                                };
                            }"""
                        )
                        if isinstance(dom_extra, dict):
                            extra_img = normalize_image_url(dom_extra.get("imgSrc"))
                            modal_text = str(dom_extra.get("modalText") or "")
                            body_text = str(dom_extra.get("bodyText") or "")
                            visible_price_card = parse_ozon_visible_price_text(
                                str(dom_extra.get("priceText") or ""),
                                default_currency=default_currency,
                                rub_to_byn_rate=rub_to_byn_rate,
                            )
                            ref_price = (
                                (inpage_card.price_byn if inpage_card else None)
                                or (intercepted_card.price_byn if intercepted_card else None)
                                or (dom_card.price_byn if dom_card else None)
                            )
                            ref_old = (
                                (inpage_card.old_price_byn if inpage_card else None)
                                or (intercepted_card.old_price_byn if intercepted_card else None)
                                or (dom_card.old_price_byn if dom_card else None)
                            )
                            extra_avg = (
                                intercepted_avg_byn
                                or extract_avg_month_price_from_text(
                                    modal_text,
                                    actual_price_byn=ref_price,
                                    old_price_byn=ref_old,
                                    default_currency=default_currency,
                                    rub_to_byn_rate=rub_to_byn_rate,
                                )
                                or extract_avg_month_price_from_text(
                                    body_text,
                                    actual_price_byn=ref_price,
                                    old_price_byn=ref_old,
                                    default_currency=default_currency,
                                    rub_to_byn_rate=rub_to_byn_rate,
                                )
                            )
                            if dom_card:
                                dom_card = replace(
                                    dom_card,
                                    avg_month_price_byn=extra_avg or dom_card.avg_month_price_byn,
                                    image_url=dom_card.image_url or extra_img,
                                )
                            elif extra_avg or extra_img:
                                dom_card = ProductCardData(
                                    avg_month_price_byn=extra_avg,
                                    image_url=extra_img,
                                    raw_currency=default_currency,
                                )

                    merged = merge_card_data(inpage_card, intercepted_card)
                    merged = merge_card_data(merged, dom_card)
                    merged = merge_card_data(merged, visible_price_card)
                    if intercepted_avg_byn is not None and merged and merged.avg_month_price_byn is None:
                        merged = replace(merged, avg_month_price_byn=intercepted_avg_byn)

                    if merged and (merged.in_stock is False or (merged.title and merged.price_byn is not None)):
                        cookies = await self._export_cookies()
                        log(
                            f"Карточка Ozon {sku} получена через браузер ({domain}): "
                            f"in_stock={merged.in_stock}, price={merged.price_byn} BYN, "
                            f"avg={merged.avg_month_price_byn} BYN, img={bool(merged.image_url)}"
                        )
                        return merged, cookies
                except Exception as exc:
                    log(f"Ошибка чтения карточки Ozon {sku} в браузере ({domain}): {exc}", logging.DEBUG)
                finally:
                    with suppress(Exception):
                        await nav_page.close()

                if inpage_card and (inpage_card.title and inpage_card.price_byn is not None):
                    cookies = await self._export_cookies()
                    return inpage_card, cookies

            cookies = await self._export_cookies()
            return None, cookies

    async def fetch_wb_product_card(
        self,
        sku: str,
        *,
        rub_to_byn_rate: float = 0.0345,
    ) -> tuple[ProductCardData | None, list[dict]]:
        """Получает карточку товара Wildberries через браузер Playwright (перехват XHR + чтение DOM).

        Используется как надёжный обход, когда публичные API card.wb.ru / search.wb.ru закрыты антиботом (HTTP 403).
        """
        if not self._available or not sku:
            return None, []

        async with self._lock:
            if not await self._ensure_context():
                return None, []

            self._last_used = time.time()
            assert self._context is not None

            intercepted_card: ProductCardData | None = None
            intercepted_basket_title = ""
            intercepted_basket_brand = ""
            intercepted_avg_byn: float | None = None
            intercepted_latest_byn: float | None = None
            intercepted_img_url = ""

            storefronts = (
                ("www.wildberries.by", "BYN"),
                ("www.wildberries.ru", "RUB"),
            )

            for host, default_currency in storefronts:
                nav_page = await self._context.new_page()
                await self._apply_cdp_stealth(nav_page)

                async def _handle_wb_response(resp, curr: str = default_currency) -> None:
                    nonlocal intercepted_card, intercepted_basket_title, intercepted_basket_brand
                    nonlocal intercepted_avg_byn, intercepted_latest_byn, intercepted_img_url
                    with suppress(Exception):
                        r_url = resp.url or ""
                        m_b = WB_BASKET_HOST_IN_URL_RE.search(r_url)
                        if m_b:
                            b_host = m_b.group(1).lower()
                            b_vol = int(m_b.group(2))
                            WB_BASKET_VOL_CACHE[b_vol] = b_host
                            if not intercepted_img_url:
                                intercepted_img_url = get_wb_card_image_url(sku, basket_host=b_host)

                        if resp.status < 200 or resp.status >= 300:
                            return

                        if (
                            ("/cards/" in r_url and "detail" in r_url)
                            or "/__internal/card/" in r_url
                            or ("/exactmatch/" in r_url and "search" in r_url)
                        ):
                            raw_bytes = await resp.body()
                            if raw_bytes:
                                payload = json.loads(raw_bytes.decode("utf-8", errors="ignore"))
                                api_curr = "BYN" if "curr=byn" in r_url.lower() else ("RUB" if "curr=rub" in r_url.lower() else curr)
                                parsed = parse_wb_detail_payload(
                                    payload,
                                    sku=sku,
                                    currency=api_curr,
                                    rub_to_byn_rate=rub_to_byn_rate,
                                )
                                if parsed is not None:
                                    intercepted_card = merge_card_data(intercepted_card, parsed)
                        elif r_url.endswith("/card.json"):
                            raw_bytes = await resp.body()
                            if raw_bytes:
                                c_json = json.loads(raw_bytes.decode("utf-8", errors="ignore"))
                                if isinstance(c_json, dict):
                                    imt_n = str(c_json.get("imt_name") or c_json.get("subj_name") or "").strip()
                                    selling = c_json.get("selling")
                                    b_n = str(selling.get("brand_name") or "").strip() if isinstance(selling, dict) else ""
                                    if not b_n:
                                        b_n = str(c_json.get("brand_name") or "").strip()
                                    if imt_n:
                                        intercepted_basket_title = clean_product_title(imt_n, b_n)
                                    if b_n:
                                        intercepted_basket_brand = b_n
                        elif r_url.endswith("/price-history.json"):
                            raw_bytes = await resp.body()
                            if raw_bytes:
                                h_json = json.loads(raw_bytes.decode("utf-8", errors="ignore"))
                                if isinstance(h_json, list):
                                    intercepted_avg_byn = parse_wb_price_history_avg(
                                        h_json,
                                        rub_to_byn_rate=rub_to_byn_rate,
                                    ) or intercepted_avg_byn
                                    intercepted_latest_byn = parse_wb_price_history_latest(
                                        h_json,
                                        rub_to_byn_rate=rub_to_byn_rate,
                                    ) or intercepted_latest_byn

                nav_page.on("response", lambda r: asyncio.create_task(_handle_wb_response(r)))

                target_url = f"https://{host}/catalog/{sku}/detail.aspx"
                try:
                    resp = await nav_page.goto(target_url, wait_until="domcontentloaded", timeout=14_000)
                    if resp and resp.status == 404:
                        cookies = await self._export_cookies()
                        return ProductCardData(in_stock=False), cookies

                    # Ожидаем отрисовки SPA-карточки WB или прохождения антибот-экрана
                    deadline = time.monotonic() + min(9.0, float(self.settings.browser_challenge_timeout))
                    while time.monotonic() < deadline:
                        if intercepted_card and intercepted_card.price_byn is not None and intercepted_card.title:
                            break
                        ready = False
                        with suppress(Exception):
                            ready = bool(
                                await nav_page.evaluate(
                                    """() => {
                                        const h1 = document.querySelector('h1.product-page__title, [class*="productPage"] h1, h1');
                                        const price = document.querySelector(
                                            '.price-block__final-price, .price-block__wallet-price, [class*="priceBlockFinalPrice"], [class*="priceBlockWalletPrice"]'
                                        );
                                        const soldOut = document.querySelector(
                                            '.sold-out-product, .price-block__sold-out, [class*="soldOut"]'
                                        );
                                        const bodyTxt = (document.body ? document.body.innerText : '').toLowerCase();
                                        const notFound = bodyTxt.includes('по вашему запросу ничего не найдено') || bodyTxt.includes('страница не найдена');
                                        return Boolean((h1 && (price || soldOut)) || notFound);
                                    }"""
                                )
                            )
                        if ready:
                            break
                        await self._simulate_human_interaction(nav_page)
                        await asyncio.sleep(0.55)

                    # Пробуем также in-page запрос к внутреннему эндпоинту WB из контекста вкладки, если цена ещё не перехвачена
                    if intercepted_card is None or intercepted_card.price_byn is None:
                        with suppress(Exception):
                            inpage_raw = await nav_page.evaluate(
                                """async ({ sku, dest, spp }) => {
                                    const urls = [
                                        `/__internal/card/cards/v4/detail?appType=1&curr=byn&dest=${dest}&spp=${spp}&nm=${sku}`,
                                        `/__internal/card/cards/v4/detail?appType=1&curr=rub&dest=-1257786&spp=${spp}&nm=${sku}`,
                                        `https://card.wb.ru/cards/v4/detail?appType=1&curr=byn&dest=${dest}&spp=${spp}&nm=${sku}`,
                                        `https://card.wb.ru/cards/v2/detail?appType=1&curr=byn&dest=${dest}&spp=${spp}&nm=${sku}`
                                    ];
                                    for (const u of urls) {
                                        try {
                                            const r = await fetch(u, { credentials: 'include', headers: { 'Accept': 'application/json' } });
                                            if (r.ok) {
                                                const txt = await r.text();
                                                if (txt && txt.includes('"products"')) {
                                                    return JSON.stringify({ url: u, body: txt });
                                                }
                                            }
                                        } catch (e) {}
                                    }
                                    return '';
                                }""",
                                {"sku": str(sku), "dest": self.settings.wb_dest_byn, "spp": self.settings.wb_spp},
                            )
                            if inpage_raw:
                                wrapper = json.loads(inpage_raw)
                                u_str = str(wrapper.get("url") or "")
                                b_obj = json.loads(wrapper.get("body") or "{}")
                                api_curr = "BYN" if "curr=byn" in u_str.lower() else "RUB"
                                parsed_inpage = parse_wb_detail_payload(
                                    b_obj,
                                    sku=sku,
                                    currency=api_curr,
                                    rub_to_byn_rate=rub_to_byn_rate,
                                )
                                if parsed_inpage is not None:
                                    intercepted_card = merge_card_data(intercepted_card, parsed_inpage)

                    # Извлекаем данные напрямую из отрендеренного DOM главной карточки товара (исключая карусели рекомендаций)
                    dom_info = None
                    with suppress(Exception):
                        dom_info = await nav_page.evaluate(
                            """() => {
                                const isInsideCarousel = (el) => {
                                    if (!el) return true;
                                    return Boolean(
                                        el.closest(
                                            '.product-card, .j-card-item, .goods-card, .custom-slider, .swiper-wrapper .product-card, [class*="recommend"], [class*="similar"]'
                                        )
                                    );
                                };
                                const firstText = (selectors) => {
                                    for (const sel of selectors) {
                                        const nodes = document.querySelectorAll(sel);
                                        for (const n of nodes) {
                                            if (!isInsideCarousel(n)) {
                                                const t = (n.innerText || n.textContent || '').trim();
                                                if (t) return t;
                                            }
                                        }
                                    }
                                    return '';
                                };

                                const title = firstText([
                                    'h1.product-page__title',
                                    '[class*="productPage"] h1',
                                    '[data-link*="imt_name"]',
                                    'h1'
                                ]);
                                const brand = firstText([
                                    '.product-page__header-brand',
                                    'a.product-page__header-brand',
                                    '[class*="productHeaderBrand"]',
                                    '[class*="brandName"]'
                                ]);
                                const walletPrice = firstText([
                                    '.product-page .price-block__wallet-price',
                                    '.price-block__wallet-price',
                                    '[class*="priceBlockWalletPrice"]',
                                    '[class*="walletPrice"]'
                                ]);
                                const finalPrice = firstText([
                                    '.product-page ins.price-block__final-price',
                                    '.product-page .price-block__final-price',
                                    'ins.price-block__final-price',
                                    '.price-block__final-price',
                                    '[class*="priceBlockFinalPrice"]'
                                ]);
                                const oldPrice = firstText([
                                    '.product-page del.price-block__old-price',
                                    'del.price-block__old-price',
                                    '.price-block__old-price',
                                    '[class*="priceBlockOldPrice"]'
                                ]);
                                const soldOutText = firstText([
                                    '.sold-out-product',
                                    '.price-block__sold-out',
                                    '[class*="soldOutProduct"]',
                                    '[class*="soldOut"]'
                                ]);

                                let hasAddToCart = false;
                                const btnSelectors = [
                                    '.product-page .order__button',
                                    'button.order__button',
                                    '[class*="orderButton"]',
                                    'button[aria-label*="корзин"]',
                                    'button'
                                ];
                                for (const sel of btnSelectors) {
                                    for (const b of document.querySelectorAll(sel)) {
                                        if (isInsideCarousel(b)) continue;
                                        const bt = ((b.innerText || b.textContent || '') + ' ' + (b.getAttribute('aria-label') || '')).toLowerCase();
                                        if (bt.includes('в корзину') || bt.includes('добавить в корзину') || bt.includes('купить сейчас')) {
                                            hasAddToCart = true;
                                            break;
                                        }
                                    }
                                    if (hasAddToCart) break;
                                }

                                let imgSrc = '';
                                const imgNodes = document.querySelectorAll(
                                    '.photo-zoom__preview, .slide__content img, img[src*="/images/big/"], img[src*="wbbasket.ru"]'
                                );
                                for (const img of imgNodes) {
                                    if (isInsideCarousel(img)) continue;
                                    const s = img.currentSrc || img.src || '';
                                    if (s && s.startsWith('http')) {
                                        imgSrc = s;
                                        break;
                                    }
                                }

                                const ratingText = firstText([
                                    '.product-review__rating',
                                    '[class*="productReviewRating"]',
                                    '[class*="ratingValue"]'
                                ]);
                                const reviewsText = firstText([
                                    '.product-review__count-review',
                                    '[class*="productReviewCount"]',
                                    '[class*="reviewsCount"]'
                                ]);

                                const mainArea = document.querySelector('.product-page, main, body');
                                const mainTxt = (mainArea ? mainArea.innerText : '').slice(0, 15000).toLowerCase();
                                const isNotFound = (
                                    mainTxt.includes('по вашему запросу ничего не найдено') ||
                                    mainTxt.includes('страница не найдена') ||
                                    mainTxt.includes('ошибка 404')
                                );

                                return {
                                    title,
                                    brand,
                                    priceText: walletPrice || finalPrice,
                                    finalPriceText: finalPrice,
                                    oldPriceText: oldPrice,
                                    soldOutText,
                                    hasAddToCart,
                                    imgSrc,
                                    ratingText,
                                    reviewsText,
                                    isNotFound
                                };
                            }"""
                        )

                    dom_card: ProductCardData | None = None
                    if isinstance(dom_info, dict):
                        if dom_info.get("isNotFound"):
                            cookies = await self._export_cookies()
                            return ProductCardData(in_stock=False), cookies

                        d_title = str(dom_info.get("title") or "").strip()
                        d_brand = str(dom_info.get("brand") or "").strip()
                        d_price_raw = str(dom_info.get("priceText") or dom_info.get("finalPriceText") or "").strip()
                        d_old_raw = str(dom_info.get("oldPriceText") or "").strip()
                        d_sold_out = str(dom_info.get("soldOutText") or "").strip().lower()
                        has_cart_btn = bool(dom_info.get("hasAddToCart"))
                        d_img = normalize_image_url(dom_info.get("imgSrc"))

                        if d_img:
                            m_b = WB_BASKET_HOST_IN_URL_RE.search(d_img)
                            if m_b:
                                WB_BASKET_VOL_CACHE[int(m_b.group(2))] = m_b.group(1).lower()

                        p_val, p_curr = parse_price_value_and_currency(d_price_raw, default_currency=default_currency)
                        p_byn = convert_to_byn(p_val, p_curr, rub_to_byn_rate)

                        old_val, old_curr = parse_price_value_and_currency(d_old_raw, default_currency=default_currency)
                        old_byn = convert_to_byn(old_val, old_curr, rub_to_byn_rate)

                        d_rating = None
                        with suppress(TypeError, ValueError):
                            r_m = re.search(r"(\d(?:[.,]\d)?)", str(dom_info.get("ratingText") or ""))
                            if r_m:
                                d_rating = float(r_m.group(1).replace(",", "."))

                        d_reviews = None
                        with suppress(TypeError, ValueError):
                            rv_digits = re.sub(r"\D+", "", str(dom_info.get("reviewsText") or ""))
                            if rv_digits:
                                d_reviews = int(rv_digits)

                        if p_byn is not None and p_byn > 0:
                            d_in_stock: bool | None = True
                        elif has_cart_btn:
                            d_in_stock = True
                        elif d_sold_out and any(m in d_sold_out for m in OUT_OF_STOCK_MARKERS):
                            d_in_stock = False
                        else:
                            d_in_stock = None

                        if d_title or p_byn is not None or d_in_stock is not None or d_img:
                            dom_card = ProductCardData(
                                title=clean_product_title(d_title, d_brand),
                                brand=d_brand,
                                price_byn=p_byn if d_in_stock is not False else None,
                                old_price_byn=old_byn if d_in_stock is not False else None,
                                image_url=d_img,
                                in_stock=d_in_stock,
                                rating=d_rating,
                                reviews_count=d_reviews,
                                raw_currency=default_currency,
                            )

                    html_content = await nav_page.content()
                    html_card = parse_html_product_card(
                        html_content,
                        default_currency=default_currency,
                        rub_to_byn_rate=rub_to_byn_rate,
                    )

                    extras_card = ProductCardData(
                        title=intercepted_basket_title,
                        brand=intercepted_basket_brand,
                        price_byn=intercepted_latest_byn if (intercepted_card is None or intercepted_card.price_byn is None) else None,
                        avg_month_price_byn=intercepted_avg_byn,
                        image_url=intercepted_img_url,
                    )

                    combined = merge_card_data(intercepted_card, dom_card)
                    combined = merge_card_data(combined, html_card)
                    combined = merge_card_data(combined, extras_card)

                    if combined and (
                        combined.in_stock is False
                        or (combined.title and combined.price_byn is not None)
                    ):
                        cookies = await self._export_cookies()
                        log(
                            f"Карточка Wildberries {sku} получена через браузер ({host}): "
                            f"in_stock={combined.in_stock}, price={combined.price_byn} BYN, "
                            f"avg={combined.avg_month_price_byn} BYN"
                        )
                        return combined, cookies
                except Exception as exc:
                    log(f"Ошибка чтения карточки Wildberries {sku} в браузере ({host}): {exc}", logging.DEBUG)
                finally:
                    with suppress(Exception):
                        await nav_page.close()

            cookies = await self._export_cookies()
            return intercepted_card, cookies

    async def _export_cookies(self) -> list[dict]:
        if self._context is None:
            return []
        with suppress(Exception):
            cookies = await self._context.cookies()
            if cookies:
                self.last_cookies = cookies
                with suppress(OSError):
                    with open(self.settings.cookies_cache_path, "w", encoding="utf-8") as f:
                        json.dump(cookies, f, ensure_ascii=False)
                return cookies
        return []

    async def resolve_short_url(self, clean_url: str) -> tuple[str | None, list[dict]]:
        """Раскрывает короткую ссылку Ozon через прогретый Chromium и возвращает (resolved_url, cookies)."""
        if not self._available:
            return None, []

        async with self._lock:
            if not await self._ensure_context():
                return None, []

            self._last_used = time.time()
            parsed = urlparse(clean_url)
            short_path = parsed.path or ""
            orig_domain = resolve_ozon_canonical_host(parsed.hostname)

            storefronts: list[str] = []
            for domain in (*self.settings.ozon_preferred_storefronts, orig_domain):
                if domain not in storefronts:
                    storefronts.append(domain)

            assert self._context is not None

            for domain in storefronts:
                warm_page = await self._warm_up_storefront(domain)
                if warm_page is not None:
                    via_inpage = await self._fetch_via_inpage_api(warm_page, short_path, clean_url)
                    if via_inpage and has_expanded_product_url(via_inpage):
                        cookies = await self._export_cookies()
                        log(f"Ссылка раскрыта через прогретый браузер ({domain} in-page): {clean_url} -> {via_inpage}")
                        return via_inpage, cookies

                nav_page = await self._context.new_page()
                await self._apply_cdp_stealth(nav_page)
                captured_urls: list[str] = []
                loop_aborted = False

                def _on_url_candidate(candidate_url: str) -> None:
                    if candidate_url and has_expanded_product_url(candidate_url):
                        captured_urls.append(candidate_url)

                def _handle_request(req) -> None:
                    _on_url_candidate(req.url)

                def _handle_response(resp) -> None:
                    _on_url_candidate(resp.url)
                    loc = resp.headers.get("location") or resp.headers.get("Location")
                    if loc:
                        full_loc = urljoin(resp.url, loc)
                        _on_url_candidate(full_loc)

                def _handle_frame_navigated(frame) -> None:
                    if frame == nav_page.main_frame:
                        _on_url_candidate(frame.url)

                async def _route_handler(route) -> None:
                    nonlocal loop_aborted
                    req_url = route.request.url
                    if has_expanded_product_url(req_url):
                        captured_urls.append(req_url)
                    if "__rr=" in req_url and not has_expanded_product_url(req_url):
                        qs = parse_qs(urlparse(req_url).query)
                        try:
                            rr_val = int(qs.get("__rr", ["0"])[0])
                        except (ValueError, IndexError):
                            rr_val = 2
                        if rr_val >= 2:
                            loop_aborted = True
                            await route.abort()
                            return
                    await route.continue_()

                nav_page.on("request", _handle_request)
                nav_page.on("response", _handle_response)
                nav_page.on("framenavigated", _handle_frame_navigated)
                with suppress(Exception):
                    await nav_page.route("**/*", _route_handler)

                try:
                    for use_www in (False, True):
                        if captured_urls:
                            break
                        loop_aborted = False
                        target_url = build_storefront_url(clean_url, domain, use_www=use_www)
                        referer_url = f"https://www.{domain}/" if use_www else f"https://{domain}/"
                        log(f"Браузер открывает короткую ссылку Ozon: {target_url}")

                        nav_status = 0
                        try:
                            resp = await nav_page.goto(
                                target_url,
                                referer=referer_url,
                                wait_until="domcontentloaded",
                                timeout=10_000,
                            )
                            if resp:
                                nav_status = resp.status
                        except Exception as exc:
                            log(f"Навигация {target_url}: {type(exc).__name__}: {str(exc)[:120]}", logging.DEBUG)

                        for candidate in reversed(captured_urls):
                            if has_expanded_product_url(candidate):
                                cookies = await self._export_cookies()
                                log(f"Ссылка раскрыта через браузер ({domain}): {clean_url} -> {candidate}")
                                return candidate, cookies

                        if has_expanded_product_url(nav_page.url):
                            cookies = await self._export_cookies()
                            log(f"Ссылка раскрыта через браузер ({domain}): {clean_url} -> {nav_page.url}")
                            return nav_page.url, cookies

                        cur_url = nav_page.url or ""
                        if loop_aborted or cur_url.startswith(("chrome-error://", "about:blank")):
                            log(
                                f"Витрина {target_url} вернула цикл редиректа (cur_url={cur_url}); "
                                f"переходим к следующему варианту",
                                logging.DEBUG,
                            )
                            continue

                        deadline = time.monotonic() + min(8, self.settings.browser_challenge_timeout)
                        while time.monotonic() < deadline:
                            for candidate in reversed(captured_urls):
                                if has_expanded_product_url(candidate):
                                    cookies = await self._export_cookies()
                                    log(f"Ссылка раскрыта через браузер ({domain}): {clean_url} -> {candidate}")
                                    return candidate, cookies
                            if has_expanded_product_url(nav_page.url):
                                cookies = await self._export_cookies()
                                log(f"Ссылка раскрыта через браузер ({domain}): {clean_url} -> {nav_page.url}")
                                return nav_page.url, cookies

                            await self._simulate_human_interaction(nav_page)
                            await asyncio.sleep(0.5)

                        with suppress(Exception):
                            title = (await nav_page.title()).strip()
                            html_body = await nav_page.content()
                            extracted = extract_product_url_from_text(html_body, target_url)
                            if extracted and has_expanded_product_url(extracted):
                                cookies = await self._export_cookies()
                                log(f"Ссылка извлечена из DOM браузера ({domain}): {clean_url} -> {extracted}")
                                return extracted, cookies
                            log(
                                f"Статус вкладки {target_url}: HTTP {nav_status}, title={title!r}, url={nav_page.url}",
                                logging.INFO,
                            )
                finally:
                    with suppress(Exception):
                        await nav_page.close()

            cookies = await self._export_cookies()
            return None, cookies

    async def _close_unlocked(self) -> None:
        for page in list(self._warm_pages.values()):
            with suppress(Exception):
                await page.close()
        self._warm_pages.clear()
        if self._context is not None:
            with suppress(Exception):
                await self._context.close()
            self._context = None
        if self._playwright is not None:
            with suppress(Exception):
                await self._playwright.stop()
            self._playwright = None

    async def close(self) -> None:
        if self._idle_task is not None:
            self._idle_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._idle_task
            self._idle_task = None
        async with self._lock:
            await self._close_unlocked()
