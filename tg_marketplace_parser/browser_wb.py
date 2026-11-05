import asyncio
from contextlib import suppress
import json
import logging
import re
import time
from .common import clean_product_title, log, normalize_image_url
from .constants import OUT_OF_STOCK_MARKERS, WB_BASKET_HOST_IN_URL_RE, WB_BASKET_VOL_CACHE
from .marketplace_parsing import get_wb_card_image_url, merge_card_data, parse_html_product_card, parse_wb_detail_payload, parse_wb_price_history_avg, parse_wb_price_history_latest
from .models import ProductCardData
from .pricing import convert_to_byn, parse_price_value_and_currency


class WildberriesBrowserMixin:
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
