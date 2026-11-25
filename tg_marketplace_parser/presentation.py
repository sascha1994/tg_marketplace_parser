import re
from urllib.parse import (
    urlparse,
    urlunparse,
)
from .common import (
    extract_wb_sku,
    has_expanded_product_url,
    is_ozon_host,
    is_wb_host,
    normalize_host,
    validate_marketplace_url,
)
from .constants import (
    OZON_SKU_RE,
    OZON_SHORT_PRODUCT_ID_RE,
    URL_RE,
)
from .marketplace_parsing import get_wb_card_image_url
from .message_parsing import resolve_ozon_canonical_host
from .models import ProductInfo


def parse_product_info(
    original_url: str,
    resolved_url: str,
    canonical_host_override: str = "ozon.by",
) -> ProductInfo:
    wb_sku = extract_wb_sku(resolved_url) or extract_wb_sku(original_url)
    if wb_sku:
        return ProductInfo(
            original_url=original_url,
            resolved_url=resolved_url,
            platform="Wildberries",
            sku=wb_sku,
            canonical_url=f"https://www.wildberries.by/catalog/{wb_sku}/detail.aspx",
            sku_key=f"wildberries:{wb_sku}",
            image_url=get_wb_card_image_url(wb_sku),
        )

    ozon_match = OZON_SKU_RE.search(resolved_url)
    if ozon_match:
        sku = next(group for group in ozon_match.groups() if group)
        orig_host = normalize_host(urlparse(original_url).hostname)
        base_host = orig_host if is_ozon_host(orig_host) else urlparse(resolved_url).hostname
        canonical_host = resolve_ozon_canonical_host(base_host, canonical_host_override)
        is_short_product_id = bool(OZON_SHORT_PRODUCT_ID_RE.fullmatch(sku))
        return ProductInfo(
            original_url=original_url,
            resolved_url=resolved_url,
            platform="Ozon",
            sku=sku,
            canonical_url=(
                f"https://{canonical_host}/p/{sku}"
                if is_short_product_id
                else f"https://{canonical_host}/product/{sku}/"
            ),
            sku_key=f"ozon:{'short:' if is_short_product_id else ''}{sku}",
        )

    parsed = urlparse(resolved_url)
    canonical_url = urlunparse(parsed._replace(query="", fragment=""))
    host = normalize_host(parsed.hostname)
    platform = "Wildberries" if is_wb_host(host) else "Ozon"
    return ProductInfo(
        original_url=original_url,
        resolved_url=resolved_url,
        platform=platform,
        sku=None,
        canonical_url=canonical_url,
        sku_key=f"{platform.lower()}:raw:{canonical_url}",
    )

def _format_single_product_block(product: ProductInfo, index: int | None = None) -> str:
    """Форматирует карточку товара для публикации:
    - Магазин указан в первой строке
    - Без артикула
    - Приоритет средней цены за месяц (при наличии)
    - Пустые строки между каждым блоком для высокой читаемости
    """
    if product.platform == "Wildberries":
        store_badge = "🟪 Wildberries"
    else:
        store_badge = "🔷 Ozon"
    store_line = f"{index}. {store_badge}" if index is not None else store_badge

    paragraphs: list[str] = [
        store_line,
        f"🛍 {product.display_title}",
    ]

    price_str = product.formatted_price_byn
    avg_month_str = product.formatted_avg_month_price_byn
    old_price_str = product.formatted_old_price_byn
    if price_str:
        if avg_month_str:
            paragraphs.append(f"💰 Цена: {price_str} (средняя за месяц: {avg_month_str})")
        elif old_price_str:
            paragraphs.append(f"💰 Цена: {price_str} (было {old_price_str})")
        else:
            paragraphs.append(f"💰 Цена: {price_str}")

    if product.rating and product.rating > 0:
        rating_str = f"{product.rating:.1f}".rstrip("0").rstrip(".")
        if product.reviews_count and product.reviews_count > 0:
            paragraphs.append(f"⭐️ Рейтинг: {rating_str} ({product.reviews_count} отз.)")
        else:
            paragraphs.append(f"⭐️ Рейтинг: {rating_str}")

    paragraphs.append(f"🔗 {product.canonical_url}")

    return "\n\n".join(paragraphs)

def build_publication_text(
    source_text: str,
    products: list[ProductInfo],
    *,
    include_source_notes: bool = False,
) -> str:
    """Формирует читаемый текст поста с разрывами между блоками на основе данных карточки товара."""
    products = [product for product in products if has_expanded_product_url(product.canonical_url)]
    if not products:
        return ""

    blocks: list[str] = []

    if len(products) == 1:
        blocks.append(_format_single_product_block(products[0], index=None))
    else:
        blocks.append(f"🔥 Подборка актуальных товаров ({len(products)} шт.)")
        for idx, product in enumerate(products, start=1):
            blocks.append(_format_single_product_block(product, index=idx))

    if include_source_notes and source_text:
        cleaned_notes = source_text

        def remove_marketplace_url(match: re.Match) -> str:
            raw_url = match.group(0)
            clean_url = raw_url.rstrip(".,;:!?)>\"'")
            if validate_marketplace_url(clean_url):
                return raw_url[len(clean_url):]
            return raw_url

        cleaned_notes = URL_RE.sub(remove_marketplace_url, cleaned_notes)
        cleaned_notes = re.sub(r"[ \t]+\n", "\n", cleaned_notes)
        cleaned_notes = re.sub(r"\n{3,}", "\n\n", cleaned_notes).strip()
        if cleaned_notes:
            blocks.append(f"💬 Описание:\n{cleaned_notes}")

    return "\n\n".join(blocks).strip()

def split_text(text: str, limit: int = 4000) -> list[str]:
    if not text:
        return []
    result: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(line) > limit:
            if current:
                result.append(current.rstrip())
                current = ""
            result.extend(line[i:i + limit] for i in range(0, len(line), limit))
            continue
        if len(current) + len(line) > limit and current:
            result.append(current.rstrip())
            current = line
        else:
            current += line
    if current:
        result.append(current.rstrip())
    return result
