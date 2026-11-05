import asyncio
from contextlib import suppress
from dataclasses import replace
import json
import logging
import time
from urllib.parse import parse_qs, quote, urljoin, urlparse
from .common import build_storefront_url, has_expanded_product_url, is_ozon_host, log, normalize_image_url, ozon_product_path
from .marketplace_parsing import merge_card_data, parse_html_product_card, parse_ozon_composer_payload, parse_ozon_visible_price_text
from .message_parsing import extract_product_url_from_text, resolve_ozon_canonical_host
from .models import ProductCardData
from .pricing import extract_avg_month_price_from_text, extract_ozon_avg_price_from_state, extract_ozon_price_modal_urls


class OzonBrowserMixin:
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

                async def _handle_ozon_response(
                    resp,
                    curr: str = default_currency,
                    base_card: ProductCardData | None = inpage_card,
                ) -> None:
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
                                (base_card.price_byn if base_card else None)
                                or (intercepted_card.price_byn if intercepted_card else None)
                            )
                            ref_old = (
                                (base_card.old_price_byn if base_card else None)
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

                def _on_url_candidate(candidate_url: str, urls: list[str] = captured_urls) -> None:
                    if candidate_url and has_expanded_product_url(candidate_url):
                        urls.append(candidate_url)

                def _handle_request(req) -> None:
                    _on_url_candidate(req.url)

                def _handle_response(resp) -> None:
                    _on_url_candidate(resp.url)
                    loc = resp.headers.get("location") or resp.headers.get("Location")
                    if loc:
                        full_loc = urljoin(resp.url, loc)
                        _on_url_candidate(full_loc)

                def _handle_frame_navigated(frame, page=nav_page) -> None:
                    if frame == page.main_frame:
                        _on_url_candidate(frame.url)

                async def _route_handler(route, urls: list[str] = captured_urls) -> None:
                    nonlocal loop_aborted
                    req_url = route.request.url
                    if has_expanded_product_url(req_url):
                        urls.append(req_url)
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
