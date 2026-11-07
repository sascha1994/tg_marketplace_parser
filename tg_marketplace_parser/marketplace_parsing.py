from contextlib import suppress
from dataclasses import replace
import html
import json
import re
import time
from typing import (
    Any,
    Iterable,
)
from .common import (
    clean_html_tags,
    clean_product_title,
    normalize_image_url,
)
from .constants import (
    ANTIBOT_MARKERS,
    GENERIC_BRAND_NAMES,
    H1_TAG_RE,
    JSON_LD_SCRIPT_RE,
    META_TAG_RE,
    NOT_FOUND_MARKERS,
    OUT_OF_STOCK_MARKERS,
    TITLE_TAG_RE,
    WB_BASKET_HOST_IN_URL_RE,
    WB_BASKET_VOL_CACHE,
)
from .models import ProductCardData
from .pricing import (
    convert_to_byn,
    extract_avg_month_price_from_text,
    extract_ozon_avg_price_from_state,
    parse_price_value_and_currency,
)


def get_wb_basket_host(vol: int) -> str:
    """Определяет хост корзины Wildberries (basket-01..basket-70.wbbasket.ru) по vol = sku // 100000.

    Сначала проверяет динамический кэш WB_BASKET_VOL_CACHE, затем актуальную таблицу диапазонов
    и формулу шага шардов для новых 9- и 10-значных артикулов.
    """
    cached = WB_BASKET_VOL_CACHE.get(vol)
    if cached:
        return cached

    ranges = (
        (143, "01"),
        (287, "02"),
        (431, "03"),
        (719, "04"),
        (1007, "05"),
        (1061, "06"),
        (1115, "07"),
        (1169, "08"),
        (1313, "09"),
        (1601, "10"),
        (1655, "11"),
        (1919, "12"),
        (2045, "13"),
        (2189, "14"),
        (2405, "15"),
        (2621, "16"),
        (2837, "17"),
        (3053, "18"),
        (3269, "19"),
        (3485, "20"),
        (3701, "21"),
        (3917, "22"),
        (4133, "23"),
        (4349, "24"),
        (4565, "25"),
        (4877, "26"),
        (5189, "27"),
        (5501, "28"),
        (5813, "29"),
        (6125, "30"),
        (6437, "31"),
        (6749, "32"),
        (7061, "33"),
        (7373, "34"),
        (7685, "35"),
        (7997, "36"),
        (8309, "37"),
        (8621, "38"),
        (8933, "39"),
        (9245, "40"),
        (9557, "41"),
        (9869, "42"),
        (10181, "43"),
        (10493, "44"),
        (10805, "45"),
    )
    for max_vol, shard in ranges:
        if vol <= max_vol:
            return f"basket-{shard}.wbbasket.ru"

    # Для более высоких vol (включая 10-значные артикулы) вычисляем ориентировочный шард с шагом 312
    est_shard = min(70, 45 + (vol - 10806) // 312 + 1)
    return f"basket-{est_shard:02d}.wbbasket.ru"

def get_wb_candidate_basket_hosts(vol: int) -> list[str]:
    """Формирует упорядоченный список кандидатов basket-XX.wbbasket.ru для динамического пробинга по vol."""
    primary = get_wb_basket_host(vol)
    m = re.search(r"basket-(\d+)\.", primary)
    center_num = int(m.group(1)) if m else 35

    ordered_nums: list[int] = [center_num]
    for delta in range(1, 70):
        for candidate in (center_num + delta, center_num - delta):
            if 1 <= candidate <= 70 and candidate not in ordered_nums:
                ordered_nums.append(candidate)

    hosts = [f"basket-{num:02d}.wbbasket.ru" for num in ordered_nums]
    if primary not in hosts:
        hosts.insert(0, primary)
    return hosts

def get_wb_card_image_url(sku: str | int, basket_host: str | None = None) -> str:
    """Формирует прямую ссылку на главное фото карточки товара Wildberries на сервере корзины."""
    try:
        sku_int = int(sku)
    except (TypeError, ValueError):
        return ""
    vol = sku_int // 100_000
    part = sku_int // 1_000
    host = basket_host or get_wb_basket_host(vol)
    return f"https://{host}/vol{vol}/part{part}/{sku_int}/images/big/1.webp"

def parse_wb_price_history_avg(
    history_list: Any,
    *,
    rub_to_byn_rate: float = 0.0345,
) -> float | None:
    """Вычисляет среднюю цену за последний месяц по истории цен Wildberries (price-history.json)."""
    if not isinstance(history_list, list) or not history_list:
        return None

    now = int(time.time())
    month_cutoff = now - 35 * 86400

    recent_byn_prices: list[float] = []
    all_byn_prices: list[float] = []

    for entry in history_list:
        if not isinstance(entry, dict):
            continue
        dt = int(entry.get("dt") or 0)
        price_dict = entry.get("price")
        if not isinstance(price_dict, dict):
            continue

        byn_val: float | None = None
        if price_dict.get("BYN"):
            byn_val = round(float(price_dict["BYN"]) / 100.0, 2)
        elif price_dict.get("RUB"):
            rub_val = float(price_dict["RUB"]) / 100.0
            byn_val = convert_to_byn(rub_val, "RUB", rub_to_byn_rate)

        if byn_val is not None and byn_val > 0:
            all_byn_prices.append(byn_val)
            if dt >= month_cutoff:
                recent_byn_prices.append(byn_val)

    target_slice = recent_byn_prices if recent_byn_prices else all_byn_prices[-4:]
    if not target_slice:
        return None

    return round(sum(target_slice) / len(target_slice), 2)

def parse_wb_price_history_latest(
    history_list: Any,
    *,
    rub_to_byn_rate: float = 0.0345,
) -> float | None:
    """Извлекает последнюю известную цену товара в BYN из истории цен Wildberries (price-history.json)."""
    if not isinstance(history_list, list) or not history_list:
        return None

    best_dt = -1
    latest_byn: float | None = None

    for idx, entry in enumerate(history_list):
        if not isinstance(entry, dict):
            continue
        dt = int(entry.get("dt") or idx)
        price_dict = entry.get("price")
        if not isinstance(price_dict, dict):
            continue

        byn_val: float | None = None
        if price_dict.get("BYN"):
            with suppress(TypeError, ValueError):
                byn_val = round(float(price_dict["BYN"]) / 100.0, 2)
        elif price_dict.get("RUB"):
            with suppress(TypeError, ValueError):
                rub_val = float(price_dict["RUB"]) / 100.0
                byn_val = convert_to_byn(rub_val, "RUB", rub_to_byn_rate)

        if byn_val is not None and byn_val > 0 and dt >= best_dt:
            best_dt = dt
            latest_byn = byn_val

    return latest_byn

def extract_price_from_post_text(
    text: str,
    *,
    rub_to_byn_rate: float = 0.0345,
) -> float | None:
    """Извлекает явно указанную цену с валютой (BYN / бел. руб. / ₽ / руб.) из текста исходного поста."""
    if not text:
        return None
    clean = clean_html_tags(text)
    explicit_re = re.compile(
        r"(?:цена|стоимость|всего|за|по\s+цене)?\s*[:\-–—]?\s*"
        r"(\d[\d\s\u00a0\u202f]*(?:[.,]\d{1,2})?)\s*"
        r"(byn|бел\.?\s*руб\.?|бр|₽|рос\.?\s*руб\.?|руб\.?|rub)\b",
        re.IGNORECASE,
    )
    for match in explicit_re.finditer(clean):
        raw_fragment = f"{match.group(1)} {match.group(2)}"
        val, curr = parse_price_value_and_currency(raw_fragment, default_currency="BYN", require_currency=True)
        byn_val = convert_to_byn(val, curr, rub_to_byn_rate)
        if byn_val is not None and 0.2 <= byn_val <= 50000:
            return byn_val
    return None

def parse_wb_detail_payload(
    payload: dict[str, Any],
    *,
    sku: str | int | None = None,
    currency: str = "BYN",
    rub_to_byn_rate: float = 0.0345,
) -> ProductCardData | None:
    """Извлекает название, цену в BYN, ссылку на фото и наличие из ответа card.wb.ru / u-card.wb.ru / search.wb.ru."""
    if not isinstance(payload, dict):
        return None

    data_block = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    products = data_block.get("products")
    item: dict[str, Any] | None = None

    if isinstance(products, list):
        if not products:
            return ProductCardData(in_stock=False)
        if sku is not None:
            sku_str = str(sku).strip()
            for candidate in products:
                if isinstance(candidate, dict) and str(candidate.get("id") or "").strip() == sku_str:
                    item = candidate
                    break
            if item is None:
                # В поисковой выдаче могли прийти другие товары — не путаем их с искомым SKU
                if len(products) == 1 and isinstance(products[0], dict) and not products[0].get("id"):
                    item = products[0]
                else:
                    return None
        else:
            item = products[0] if isinstance(products[0], dict) else None
    elif isinstance(data_block.get("id"), int) and isinstance(data_block.get("sizes"), list):
        item = data_block

    if not isinstance(item, dict):
        return None

    sku_id = item.get("id") or sku
    image_url = get_wb_card_image_url(sku_id) if sku_id else ""

    raw_name = str(item.get("name") or item.get("imt_name") or "").strip()
    raw_brand = str(item.get("brand") or "").strip()
    rating = None
    with suppress(TypeError, ValueError):
        rating_val = item.get("reviewRating") or item.get("nmReviewRating") or item.get("rating")
        if rating_val is not None:
            rating = float(rating_val)

    reviews_count = None
    with suppress(TypeError, ValueError):
        fb_val = item.get("feedbacks") or item.get("nmFeedbacks")
        if fb_val is not None:
            reviews_count = int(fb_val)

    raw_total_quantity = item.get("totalQuantity")
    total_quantity: int | None = None
    if raw_total_quantity is not None and not isinstance(raw_total_quantity, bool):
        with suppress(TypeError, ValueError):
            total_quantity = int(raw_total_quantity)
    sizes = item.get("sizes") if isinstance(item.get("sizes"), list) else []

    total_stocks_qty = 0
    has_nonempty_stocks_entries = False
    has_size_wh_or_payload = False
    candidate_product_prices: list[float] = []
    candidate_basic_prices: list[float] = []
    fallback_product_prices: list[float] = []
    fallback_basic_prices: list[float] = []

    for size in sizes:
        if not isinstance(size, dict):
            continue
        if size.get("wh") or size.get("payload") or size.get("Sign"):
            has_size_wh_or_payload = True

        stocks = size.get("stocks")
        size_qty = 0
        if isinstance(stocks, list) and stocks:
            for st in stocks:
                if isinstance(st, dict):
                    if "qty" in st and st.get("qty") is not None:
                        has_nonempty_stocks_entries = True
                        with suppress(TypeError, ValueError):
                            size_qty += int(st.get("qty") or 0)
                    elif st.get("wh"):
                        has_size_wh_or_payload = True
            total_stocks_qty += size_qty

        price_obj = size.get("price")
        prod_kop = None
        basic_kop = None
        if isinstance(price_obj, dict):
            prod_kop = (
                price_obj.get("product")
                or price_obj.get("total")
                or price_obj.get("sale")
                or price_obj.get("client")
            )
            basic_kop = price_obj.get("basic")
        if not prod_kop:
            prod_kop = size.get("salePriceU") or size.get("priceU")
        if not basic_kop:
            basic_kop = size.get("priceU")

        if prod_kop:
            with suppress(TypeError, ValueError):
                if int(prod_kop) > 0:
                    val = int(prod_kop) / 100.0
                    fallback_product_prices.append(val)
                    if size_qty > 0 or not has_nonempty_stocks_entries or size.get("wh") or size.get("payload"):
                        candidate_product_prices.append(val)
        if basic_kop:
            with suppress(TypeError, ValueError):
                if int(basic_kop) > 0:
                    b_val = int(basic_kop) / 100.0
                    fallback_basic_prices.append(b_val)
                    if size_qty > 0 or not has_nonempty_stocks_entries or size.get("wh") or size.get("payload"):
                        candidate_basic_prices.append(b_val)

    if not candidate_product_prices and not fallback_product_prices:
        extended = item.get("extended") if isinstance(item.get("extended"), dict) else {}
        sale_u = (
            item.get("salePriceU")
            or extended.get("clientPriceU")
            or extended.get("basicPriceU")
            or item.get("priceU")
        )
        basic_u = item.get("priceU") or extended.get("basicPriceU")
        if sale_u:
            with suppress(TypeError, ValueError):
                if int(sale_u) > 0:
                    fallback_product_prices.append(int(sale_u) / 100.0)
        if basic_u:
            with suppress(TypeError, ValueError):
                if int(basic_u) > 0:
                    fallback_basic_prices.append(int(basic_u) / 100.0)

    active_prices = candidate_product_prices or fallback_product_prices
    active_basic = candidate_basic_prices or fallback_basic_prices

    # Логика наличия WB (v2/v4/search):
    # В актуальном API WB у размеров в наличии всегда присутствует блок price (product/total > 0),
    # при этом массив stocks может быть пустым [] (остатки скрыты) или не содержать поля qty.
    # У закончившихся товаров блок price в sizes отсутствует, а totalQuantity == 0.
    if active_prices:
        if (
            isinstance(total_quantity, int)
            and total_quantity <= 0
            and has_nonempty_stocks_entries
            and total_stocks_qty <= 0
            and not has_size_wh_or_payload
        ):
            in_stock = False
        else:
            in_stock = True
    else:
        if isinstance(total_quantity, int) and total_quantity <= 0:
            in_stock = False
        elif has_nonempty_stocks_entries and total_stocks_qty <= 0:
            in_stock = False
        elif sizes and not has_size_wh_or_payload:
            in_stock = False
        else:
            in_stock = None

    raw_price = min(active_prices) if (active_prices and in_stock is not False) else None
    raw_old_price = max(active_basic) if (active_basic and in_stock is not False) else None

    price_byn = convert_to_byn(raw_price, currency, rub_to_byn_rate) if in_stock is not False else None
    old_price_byn = convert_to_byn(raw_old_price, currency, rub_to_byn_rate) if in_stock is not False else None

    reported_quantity = total_quantity
    if reported_quantity is None and has_nonempty_stocks_entries:
        reported_quantity = total_stocks_qty

    return ProductCardData(
        title=clean_product_title(raw_name, raw_brand),
        brand=raw_brand,
        price_byn=price_byn,
        old_price_byn=old_price_byn,
        image_url=image_url,
        in_stock=in_stock,
        total_quantity=reported_quantity,
        rating=rating,
        reviews_count=reviews_count,
        raw_currency=currency.upper(),
    )

def _iter_json_ld_objects(raw_json_ld: Any) -> Iterable[dict[str, Any]]:
    if isinstance(raw_json_ld, dict):
        yield raw_json_ld
        graph = raw_json_ld.get("@graph")
        if isinstance(graph, list):
            for node in graph:
                if isinstance(node, dict):
                    yield node
    elif isinstance(raw_json_ld, list):
        for item in raw_json_ld:
            yield from _iter_json_ld_objects(item)

def parse_json_ld_product(
    raw_script_text: str,
    *,
    default_currency: str = "BYN",
    rub_to_byn_rate: float = 0.0345,
) -> ProductCardData | None:
    """Извлекает данные карточки товара (включая фото) из JSON-LD (Schema.org Product)."""
    if not raw_script_text:
        return None
    try:
        parsed = json.loads(raw_script_text.strip())
    except Exception:
        return None

    for obj in _iter_json_ld_objects(parsed):
        obj_type = str(obj.get("@type") or "").lower()
        if "product" not in obj_type and "offers" not in obj:
            continue

        name = str(obj.get("name") or "").strip()
        brand_field = obj.get("brand")
        if isinstance(brand_field, dict):
            brand = str(brand_field.get("name") or "").strip()
        elif isinstance(brand_field, str):
            brand = brand_field.strip()
        else:
            brand = ""

        # Извлекаем главное фото из поля image
        image_url = ""
        img_field = obj.get("image")
        if isinstance(img_field, str):
            image_url = normalize_image_url(img_field)
        elif isinstance(img_field, list) and img_field:
            first_img = img_field[0]
            if isinstance(first_img, str):
                image_url = normalize_image_url(first_img)
            elif isinstance(first_img, dict):
                image_url = normalize_image_url(first_img.get("url") or first_img.get("contentUrl"))
        elif isinstance(img_field, dict):
            image_url = normalize_image_url(img_field.get("url") or img_field.get("contentUrl"))

        rating = None
        reviews_count = None
        agg_rating = obj.get("aggregateRating")
        if isinstance(agg_rating, dict):
            with suppress(TypeError, ValueError):
                if agg_rating.get("ratingValue") is not None:
                    rating = float(str(agg_rating["ratingValue"]).replace(",", "."))
            with suppress(TypeError, ValueError):
                rc = agg_rating.get("reviewCount") or agg_rating.get("ratingCount")
                if rc is not None:
                    reviews_count = int(rc)

        offers = obj.get("offers")
        offer_obj = None
        if isinstance(offers, dict):
            offer_obj = offers
        elif isinstance(offers, list) and offers:
            offer_obj = offers[0] if isinstance(offers[0], dict) else None

        in_stock: bool | None = None
        price_byn: float | None = None
        currency = default_currency

        if isinstance(offer_obj, dict):
            avail = str(offer_obj.get("availability") or "").lower()
            if avail:
                if any(k in avail for k in ("outofstock", "soldout", "discontinued")):
                    in_stock = False
                elif "instock" in avail or "limitedavailability" in avail or "preorder" in avail:
                    in_stock = True

            raw_curr = str(offer_obj.get("priceCurrency") or default_currency).strip().upper()
            if raw_curr:
                currency = raw_curr
            raw_price = offer_obj.get("price") or offer_obj.get("lowPrice")
            val, detected_curr = parse_price_value_and_currency(raw_price, default_currency=currency)
            price_byn = convert_to_byn(val, detected_curr, rub_to_byn_rate)
            if price_byn is not None and in_stock is None:
                in_stock = True

        if name or price_byn is not None or in_stock is not None or image_url:
            return ProductCardData(
                title=clean_product_title(name, brand),
                brand=brand,
                price_byn=price_byn if in_stock is not False else None,
                image_url=image_url,
                in_stock=in_stock,
                rating=rating,
                reviews_count=reviews_count,
                raw_currency=currency,
            )

    return None

def parse_ozon_composer_payload(
    payload: dict[str, Any],
    *,
    default_currency: str = "BYN",
    rub_to_byn_rate: float = 0.0345,
) -> ProductCardData | None:
    """Извлекает название, актуальную цену в BYN, среднюю цену за месяц, главное фото и наличие из ответа Ozon."""
    if not isinstance(payload, dict):
        return None

    title = ""
    brand = ""
    price_byn: float | None = None
    avg_month_price_byn: float | None = None
    old_price_byn: float | None = None
    image_url = ""
    in_stock: bool | None = None
    explicit_out_of_stock = False
    rating: float | None = None
    reviews_count: int | None = None

    # 1. Проверяем встроенный JSON-LD внутри seo.script
    seo = payload.get("seo")
    if isinstance(seo, dict):
        seo_title = str(seo.get("title") or "").strip()
        scripts = seo.get("script")
        if isinstance(scripts, list):
            for scr in scripts:
                if isinstance(scr, dict) and scr.get("innerHTML"):
                    ld_data = parse_json_ld_product(
                        str(scr["innerHTML"]),
                        default_currency=default_currency,
                        rub_to_byn_rate=rub_to_byn_rate,
                    )
                    if ld_data:
                        title = ld_data.title or title
                        brand = ld_data.brand or brand
                        price_byn = ld_data.price_byn if ld_data.price_byn is not None else price_byn
                        image_url = ld_data.image_url or image_url
                        in_stock = ld_data.in_stock if ld_data.in_stock is not None else in_stock
                        rating = ld_data.rating if ld_data.rating is not None else rating
                        reviews_count = ld_data.reviews_count if ld_data.reviews_count is not None else reviews_count
        if not title and seo_title:
            title = clean_product_title(seo_title)

    # 2. Анализируем widgetStates
    widget_states = payload.get("widgetStates")
    parsed_states: list[tuple[str, dict[str, Any], str]] = []
    if isinstance(widget_states, dict):
        for widget_key, raw_state in widget_states.items():
            if not isinstance(raw_state, str) or not raw_state.strip():
                continue
            try:
                state = json.loads(raw_state)
            except Exception:
                continue
            if isinstance(state, dict):
                parsed_states.append((widget_key.lower(), state, raw_state))

    for key_lower, state, _raw_state in parsed_states:
        # Заголовок товара
        if "webproductheading" in key_lower:
            heading_title = str(state.get("title") or "").strip()
            if heading_title:
                title = clean_product_title(heading_title, brand)

        # Бренд товара
        elif "webbrand" in key_lower:
            b_name = str(state.get("name") or state.get("title") or "").strip()
            if b_name and b_name.casefold() not in GENERIC_BRAND_NAMES:
                brand = b_name
                if title:
                    title = clean_product_title(title, brand)

        # Галерея фото товара (webGallery / webMobGallery)
        elif "webgallery" in key_lower or "gallery" in key_lower:
            cover = state.get("coverImage")
            if isinstance(cover, str) and cover:
                image_url = normalize_image_url(cover) or image_url
            images = state.get("images")
            if not image_url and isinstance(images, list) and images:
                first = images[0]
                if isinstance(first, dict):
                    image_url = normalize_image_url(first.get("src") or first.get("url"))
                elif isinstance(first, str):
                    image_url = normalize_image_url(first)

        # Виджет цены (webPrice)
        elif "webprice" in key_lower:
            if state.get("isAvailable") is False:
                in_stock = False
            elif state.get("isAvailable") is True and in_stock is None:
                in_stock = True

            raw_actual = state.get("cardPrice") or state.get("price") or state.get("finalPrice")
            raw_orig = state.get("originalPrice") or state.get("price")
            if raw_actual:
                val, curr = parse_price_value_and_currency(raw_actual, default_currency=default_currency)
                converted = convert_to_byn(val, curr, rub_to_byn_rate)
                if converted is not None:
                    price_byn = converted
            if raw_orig and raw_orig != raw_actual:
                val_old, curr_old = parse_price_value_and_currency(raw_orig, default_currency=default_currency)
                converted_old = convert_to_byn(val_old, curr_old, rub_to_byn_rate)
                if converted_old is not None:
                    old_price_byn = converted_old

        # Виджет отсутствия товара в наличии (webOutOfStock / webSoldOut)
        elif "outofstock" in key_lower or "soldout" in key_lower:
            if state:
                in_stock = False
                explicit_out_of_stock = True

        # Виджет кнопки покупки / корзины (webSale / webAddToCart)
        elif "websale" in key_lower or "addtocart" in key_lower:
            if state.get("isAvailable") is False or state.get("outOfStock") is True:
                in_stock = False
                explicit_out_of_stock = True
            state_dump = json.dumps(state, ensure_ascii=False).casefold()
            if any(m in state_dump for m in OUT_OF_STOCK_MARKERS):
                in_stock = False
                explicit_out_of_stock = True

        # Виджет рейтинга и отзывов
        elif "reviewproductscore" in key_lower or "singleproductscore" in key_lower:
            with suppress(TypeError, ValueError):
                if state.get("score") is not None:
                    rating = float(state["score"])
            with suppress(TypeError, ValueError):
                if state.get("count") is not None:
                    reviews_count = int(state["count"])

    # Второй проход по widgetStates: ищем среднюю цену за месяц («Стало дешевле» / «Средняя за ...»)
    for key_lower, state, raw_state in parsed_states:
        raw_lower = raw_state.lower()
        if (
            "webprice" in key_lower
            or "pricedecrease" in key_lower
            or "pricehistory" in key_lower
            or "modal" in key_lower
            or "средн" in raw_lower
            or "стало дешевле" in raw_lower
            or "цена сейчас" in raw_lower
        ):
            found_avg = extract_ozon_avg_price_from_state(
                state,
                actual_price_byn=price_byn,
                old_price_byn=old_price_byn,
                default_currency=default_currency,
                rub_to_byn_rate=rub_to_byn_rate,
            )
            if found_avg is None:
                decoded_state_str = json.dumps(state, ensure_ascii=False)
                found_avg = extract_avg_month_price_from_text(
                    decoded_state_str,
                    actual_price_byn=price_byn,
                    old_price_byn=old_price_byn,
                    default_currency=default_currency,
                    rub_to_byn_rate=rub_to_byn_rate,
                )
            if found_avg is not None:
                avg_month_price_byn = found_avg
                break

    # Проверяем общий дерево/текст ответа на среднюю цену за месяц, если в отдельных виджетах не нашли
    if avg_month_price_byn is None:
        avg_month_price_byn = extract_ozon_avg_price_from_state(
            payload,
            actual_price_byn=price_byn,
            old_price_byn=old_price_byn,
            default_currency=default_currency,
            rub_to_byn_rate=rub_to_byn_rate,
        )
    if avg_month_price_byn is None:
        full_dump = json.dumps(payload, ensure_ascii=False)
        avg_month_price_byn = extract_avg_month_price_from_text(
            full_dump,
            actual_price_byn=price_byn,
            old_price_byn=old_price_byn,
            default_currency=default_currency,
            rub_to_byn_rate=rub_to_byn_rate,
        )

    if explicit_out_of_stock:
        in_stock = False
    elif price_byn is not None and price_byn > 0:
        in_stock = True
    elif in_stock is None:
        raw_dump = json.dumps(payload, ensure_ascii=False).casefold()
        if any(marker in raw_dump for marker in NOT_FOUND_MARKERS):
            in_stock = False
        elif any(marker in raw_dump for marker in OUT_OF_STOCK_MARKERS) and price_byn is None:
            in_stock = False
        elif price_byn is not None and price_byn > 0:
            in_stock = True

    if in_stock is False:
        price_byn = None
        avg_month_price_byn = None

    if not title and price_byn is None and in_stock is None:
        return None

    return ProductCardData(
        title=clean_product_title(title, brand),
        brand=brand,
        price_byn=price_byn,
        avg_month_price_byn=avg_month_price_byn,
        old_price_byn=old_price_byn,
        image_url=image_url,
        in_stock=in_stock,
        rating=rating,
        reviews_count=reviews_count,
        raw_currency=default_currency,
    )

def parse_ozon_visible_price_text(
    text: str,
    *,
    default_currency: str = "BYN",
    rub_to_byn_rate: float = 0.0345,
) -> ProductCardData | None:
    """Extracts the current offer from the visible Ozon price widget text."""
    if not text:
        return None

    value, currency = parse_price_value_and_currency(text, default_currency=default_currency)
    price_byn = convert_to_byn(value, currency, rub_to_byn_rate)
    if price_byn is None or price_byn <= 0:
        return None

    return ProductCardData(
        price_byn=price_byn,
        in_stock=True,
        raw_currency=currency,
    )

def parse_html_product_card(
    html_text: str,
    *,
    default_currency: str = "BYN",
    rub_to_byn_rate: float = 0.0345,
) -> ProductCardData | None:
    """Извлекает данные карточки товара (название, цену в BYN, среднюю цену за месяц, фото, наличие) из HTML."""
    if not html_text:
        return None

    lower_html = html_text.casefold()
    if any(marker in lower_html for marker in ANTIBOT_MARKERS) and "application/ld+json" not in lower_html:
        return None

    meta_map: dict[str, str] = {}
    for m in META_TAG_RE.finditer(html_text):
        key = (m.group(1) or m.group(4) or "").strip().lower()
        val = html.unescape((m.group(2) or m.group(3) or "").strip())
        if key and val and key not in meta_map:
            meta_map[key] = val

    og_image = normalize_image_url(meta_map.get("og:image") or meta_map.get("twitter:image"))

    # 1. Пробуем блоки JSON-LD (Schema.org Product)
    for match in JSON_LD_SCRIPT_RE.finditer(html_text):
        ld_card = parse_json_ld_product(
            match.group(1),
            default_currency=default_currency,
            rub_to_byn_rate=rub_to_byn_rate,
        )
        if ld_card and (ld_card.title or ld_card.price_byn is not None or ld_card.in_stock is False):
            avg_price = extract_avg_month_price_from_text(
                html_text,
                actual_price_byn=ld_card.price_byn,
                default_currency=default_currency,
                rub_to_byn_rate=rub_to_byn_rate,
            )
            ld_card = replace(
                ld_card,
                avg_month_price_byn=avg_price or ld_card.avg_month_price_byn,
                image_url=ld_card.image_url or og_image,
            )
            if ld_card.in_stock is not False and any(m in lower_html for m in OUT_OF_STOCK_MARKERS):
                if "weboutofstock" in lower_html or ld_card.price_byn is None:
                    return replace(ld_card, in_stock=False, price_byn=None, avg_month_price_byn=None)
            return ld_card

    # 2. Извлекаем OpenGraph / meta-теги и <h1> / <title>
    raw_title = ""
    h1_match = H1_TAG_RE.search(html_text)
    if h1_match:
        raw_title = clean_html_tags(h1_match.group(1))
    if not raw_title:
        raw_title = meta_map.get("og:title") or meta_map.get("twitter:title") or ""
    if not raw_title:
        t_match = TITLE_TAG_RE.search(html_text)
        if t_match:
            raw_title = clean_html_tags(t_match.group(1))

    if raw_title and any(marker in raw_title.casefold() for marker in ANTIBOT_MARKERS):
        return None
    if raw_title and any(marker in raw_title.casefold() for marker in NOT_FOUND_MARKERS):
        return ProductCardData(in_stock=False)

    brand = meta_map.get("product:brand") or meta_map.get("brand") or ""
    currency = (
        meta_map.get("product:price:currency")
        or meta_map.get("og:price:currency")
        or meta_map.get("pricecurrency")
        or default_currency
    ).upper()

    raw_price_meta = (
        meta_map.get("product:price:amount")
        or meta_map.get("og:price:amount")
        or meta_map.get("price")
    )
    price_byn: float | None = None
    if raw_price_meta:
        val, det_curr = parse_price_value_and_currency(raw_price_meta, default_currency=currency)
        price_byn = convert_to_byn(val, det_curr, rub_to_byn_rate)

    desc = meta_map.get("og:description") or meta_map.get("description") or ""
    if price_byn is None and desc:
        val, det_curr = parse_price_value_and_currency(desc, default_currency=currency)
        if val is not None:
            price_byn = convert_to_byn(val, det_curr, rub_to_byn_rate)

    avg_month_price_byn = extract_avg_month_price_from_text(
        html_text,
        actual_price_byn=price_byn,
        default_currency=currency,
        rub_to_byn_rate=rub_to_byn_rate,
    )

    in_stock: bool | None = None
    avail_meta = (meta_map.get("product:availability") or meta_map.get("availability") or "").lower()
    if avail_meta:
        if any(k in avail_meta for k in ("out of stock", "outofstock", "oos", "soldout", "discontinued")):
            in_stock = False
        elif "in stock" in avail_meta or "instock" in avail_meta:
            in_stock = True

    if in_stock is None:
        if "weboutofstock" in lower_html or any(m in lower_html for m in OUT_OF_STOCK_MARKERS):
            if price_byn is None or "этот товар закончился" in lower_html or "товара нет в наличии" in lower_html:
                in_stock = False
        elif price_byn is not None and price_byn > 0:
            in_stock = True

    if in_stock is False:
        price_byn = None
        avg_month_price_byn = None

    cleaned_title = clean_product_title(raw_title, brand)
    if not cleaned_title and price_byn is None and in_stock is None:
        return None

    return ProductCardData(
        title=cleaned_title,
        brand=brand,
        price_byn=price_byn,
        avg_month_price_byn=avg_month_price_byn,
        image_url=og_image,
        in_stock=in_stock,
        raw_currency=currency,
    )

def parse_webpage_preview_card(
    webpage,
    *,
    default_currency: str = "BYN",
    rub_to_byn_rate: float = 0.0345,
) -> ProductCardData | None:
    """Извлекает название товара, цену и наличие из объекта Telegram WebPage."""
    if not webpage:
        return None

    raw_title = getattr(webpage, "title", None) or ""
    raw_desc = getattr(webpage, "description", None) or ""
    combined = f"{raw_title}\n{raw_desc}".strip()
    if not combined:
        return None

    combined_lower = combined.casefold()
    if any(m in combined_lower for m in ANTIBOT_MARKERS):
        return None
    if any(m in combined_lower for m in NOT_FOUND_MARKERS):
        return ProductCardData(in_stock=False)

    in_stock: bool | None = None
    if any(m in combined_lower for m in OUT_OF_STOCK_MARKERS):
        in_stock = False

    price_byn: float | None = None
    for text_part in (raw_desc, raw_title):
        if not text_part:
            continue
        explicit_price_match = re.search(
            r"(\d[\d\s\u00a0\u202f]*(?:[.,]\d{1,2})?)\s*(byn|бел\.?\s*руб\.?|бр|₽|руб\.?)",
            text_part,
            re.IGNORECASE,
        )
        if explicit_price_match:
            val, curr = parse_price_value_and_currency(
                explicit_price_match.group(0),
                default_currency=default_currency,
            )
            price_byn = convert_to_byn(val, curr, rub_to_byn_rate)
            if price_byn is not None:
                break

    if price_byn is not None and in_stock is None:
        in_stock = True
    if in_stock is False:
        price_byn = None

    title = clean_product_title(raw_title)
    if not title and price_byn is None and in_stock is None:
        return None

    return ProductCardData(
        title=title,
        price_byn=price_byn,
        in_stock=in_stock,
        raw_currency=default_currency,
    )

def merge_card_data(primary: ProductCardData | None, secondary: ProductCardData | None) -> ProductCardData | None:
    """Объединяет данные карточки из двух источников, сохраняя наиболее полные поля.

    Положительное наличие с актуальной ценой (in_stock=True + price_byn > 0) на одном из регионов/эндпоинтов
    имеет приоритет над региональным отказом (in_stock=False) другого региона.
    """
    if primary is None:
        return secondary
    if secondary is None:
        return primary

    primary_has_live_offer = primary.in_stock is True and primary.price_byn is not None and primary.price_byn > 0
    secondary_has_live_offer = secondary.in_stock is True and secondary.price_byn is not None and secondary.price_byn > 0

    if primary_has_live_offer or secondary_has_live_offer:
        in_stock: bool | None = True
    elif primary.in_stock is False or secondary.in_stock is False:
        in_stock = False
    elif primary.in_stock is True or secondary.in_stock is True:
        in_stock = True
    else:
        in_stock = None

    brand = primary.brand or secondary.brand
    if primary.title and secondary.title:
        title = primary.title if len(primary.title) >= len(secondary.title) else secondary.title
    else:
        title = primary.title or secondary.title
    title = clean_product_title(title, brand)

    price_byn = (primary.price_byn if primary.price_byn is not None else secondary.price_byn) if in_stock is not False else None
    avg_month_price_byn = (
        primary.avg_month_price_byn if primary.avg_month_price_byn is not None else secondary.avg_month_price_byn
    ) if in_stock is not False else None
    old_price_byn = (primary.old_price_byn if primary.old_price_byn is not None else secondary.old_price_byn) if in_stock is not False else None

    # Если во втором источнике ссылка на фото указывает на проверенный в кэше баскет WB, отдаём ей приоритет
    image_url = primary.image_url or secondary.image_url
    if primary.image_url and secondary.image_url and primary.image_url != secondary.image_url:
        m_sec = WB_BASKET_HOST_IN_URL_RE.search(secondary.image_url)
        if m_sec:
            sec_host, sec_vol = m_sec.group(1).lower(), int(m_sec.group(2))
            if WB_BASKET_VOL_CACHE.get(sec_vol) == sec_host:
                image_url = secondary.image_url

    quantities = [
        quantity
        for quantity in (primary.total_quantity, secondary.total_quantity)
        if quantity is not None
    ]
    if primary.price_byn is not None:
        price_source = primary.price_source
    elif secondary.price_byn is not None:
        price_source = secondary.price_source
    else:
        price_source = primary.price_source or secondary.price_source or "live"
    return ProductCardData(
        title=title,
        brand=brand,
        price_byn=price_byn,
        avg_month_price_byn=avg_month_price_byn,
        old_price_byn=old_price_byn,
        image_url=image_url,
        in_stock=in_stock,
        total_quantity=max(quantities) if quantities else None,
        rating=primary.rating if primary.rating is not None else secondary.rating,
        reviews_count=primary.reviews_count if primary.reviews_count is not None else secondary.reviews_count,
        raw_currency=primary.raw_currency or secondary.raw_currency,
        price_source=price_source,
    )
