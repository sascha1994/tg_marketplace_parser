from contextlib import suppress
import re
from telethon.tl.types import MessageMediaWebPage
from typing import Iterable
from urllib.parse import (
    unquote,
    urlparse,
)
from .common import (
    extract_wb_sku,
    get_short_code,
    has_product_sku,
    is_wb_host,
    normalize_host,
    validate_marketplace_url,
)
from .constants import (
    HEAD_PRODUCT_RE,
    OZON_DOMAINS,
    OZON_PRODUCT_PATH_RE,
    TEXT_SKU_RE,
    URL_RE,
)


def extract_product_url_from_text(raw_text: str, base_url: str) -> str | None:
    """Извлекает ссылку на товар (WB или Ozon) или SKU из HTML, JSON или редиректа."""
    if not raw_text:
        return None

    normalized_variants = [raw_text]
    if "\\/" in raw_text:
        normalized_variants.append(raw_text.replace("\\/", "/"))
    if "%2F" in raw_text or "%2f" in raw_text:
        with suppress(Exception):
            normalized_variants.append(unquote(raw_text))

    base_host = normalize_host(urlparse(base_url).hostname)
    is_wb = is_wb_host(base_host)
    canonical_host = resolve_ozon_canonical_host(base_host)

    for variant in normalized_variants:
        wb_sku = extract_wb_sku(variant)
        if wb_sku:
            return f"https://www.wildberries.by/catalog/{wb_sku}/detail.aspx"

        if is_wb:
            m_wb_id = re.search(r"\"(?:nmId|nm_id|nm)\"\s*:\s*\"?(\d{6,15})\"?", variant, re.IGNORECASE)
            if m_wb_id:
                return f"https://www.wildberries.by/catalog/{m_wb_id.group(1)}/detail.aspx"
            continue

        path_match = OZON_PRODUCT_PATH_RE.search(variant)
        if path_match:
            candidate_path = path_match.group(1).strip("\"'<>\\")
            if candidate_path.startswith(("http://", "https://")):
                if has_product_sku(candidate_path):
                    return candidate_path
            else:
                full = f"https://www.{canonical_host}{candidate_path}"
                if has_product_sku(full):
                    return full

        head_part = variant.split("</head>", 1)[0]
        match = HEAD_PRODUCT_RE.search(head_part) or HEAD_PRODUCT_RE.search(variant)
        if match:
            sku = match.group(1)
            return f"https://www.{canonical_host}/product/{sku}/"

    return None

def extract_links(message) -> list[str]:
    """Возвращает ссылки в порядке появления без дублей."""
    urls = list(URL_RE.findall(message.raw_text or ""))
    for entity in message.entities or []:
        url = getattr(entity, "url", None)
        if url:
            urls.append(url)
    cleaned = (url.rstrip(".,;:!?)>\"'") for url in urls)
    return list(dict.fromkeys(cleaned))

def extract_url_from_webpage_obj(webpage, base_url: str) -> str | None:
    """Пытается достать URL товара или артикул из объекта Telegram WebPage."""
    if not webpage:
        return None

    for attr in ("url", "display_url", "embed_url"):
        candidate = getattr(webpage, attr, None)
        if not candidate or not isinstance(candidate, str):
            continue
        full = candidate if candidate.startswith(("http://", "https://")) else "https://" + candidate.lstrip("/")
        if validate_marketplace_url(full) and has_product_sku(full):
            return full

    for attr in ("url", "display_url", "title", "description"):
        val = getattr(webpage, attr, None)
        if not val or not isinstance(val, str):
            continue
        extracted = extract_product_url_from_text(val, base_url)
        if extracted:
            return extracted
        match = TEXT_SKU_RE.search(val)
        if match:
            sku = match.group(1)
            base_host = normalize_host(urlparse(base_url).hostname)
            if is_wb_host(base_host):
                return f"https://www.wildberries.by/catalog/{sku}/detail.aspx"
            canonical_host = resolve_ozon_canonical_host(base_host)
            return f"https://www.{canonical_host}/product/{sku}/"

    return None

def match_message_preview_url(messages: Iterable, original_url: str, total_links: int) -> str | None:
    """Ищет подходящее WebPage-превью среди сообщений поста."""
    short_code = get_short_code(original_url)
    for message in messages:
        media = getattr(message, "media", None)
        if not isinstance(media, MessageMediaWebPage):
            continue
        webpage = getattr(media, "webpage", None)
        if not webpage:
            continue

        wp_url = getattr(webpage, "url", "") or ""
        if total_links > 1 and short_code and short_code not in wp_url:
            continue

        extracted = extract_url_from_webpage_obj(webpage, original_url)
        if extracted:
            return extracted
    return None

def match_message_webpage_obj(messages: Iterable, original_url: str, sku: str | None, total_links: int):
    """Возвращает объект WebPage из сообщения Telegram, если он относится к данному товару."""
    short_code = get_short_code(original_url)
    for message in messages:
        media = getattr(message, "media", None)
        if not isinstance(media, MessageMediaWebPage):
            continue
        webpage = getattr(media, "webpage", None)
        if not webpage:
            continue
        wp_url = getattr(webpage, "url", "") or ""
        if total_links > 1:
            if short_code and short_code in wp_url:
                return webpage
            if sku and sku in wp_url:
                return webpage
            continue
        return webpage
    return None

def extract_skus_from_text(text: str) -> list[str]:
    """Находит явно указанные артикулы в тексте поста."""
    if not text:
        return []
    return list(dict.fromkeys(TEXT_SKU_RE.findall(text)))

def resolve_ozon_canonical_host(hostname: str | None, preferred_override: str = "") -> str:
    if preferred_override and preferred_override in OZON_DOMAINS:
        return preferred_override
    host = normalize_host(hostname)
    for domain in OZON_DOMAINS:
        if host == domain or host.endswith("." + domain):
            return domain
    return "ozon.by"
