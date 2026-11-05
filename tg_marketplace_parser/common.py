from contextlib import suppress
import html
import io
import logging
import re
import ssl
from urllib.parse import (
    parse_qs,
    urlparse,
    urlunparse,
)
from .constants import (
    ALLOWED_DOMAINS,
    GENERIC_BRAND_NAMES,
    NON_PRODUCT_PATH_RE,
    OZON_DOMAINS,
    OZON_FULL_SKU_RE,
    OZON_SHORT_PATH_RE,
    OZON_SHORT_PRODUCT_ID_RE,
    OZON_SKU_RE,
    TITLE_SEO_PREFIX_RE,
    TITLE_SEO_SUFFIX_PATTERNS,
    WB_DOMAINS,
    WB_SKU_RE,
)
from .optional_dependencies import PILImage


def log(message: str, level: int = logging.INFO) -> None:
    logging.log(level, message)

def normalize_host(hostname: str | None) -> str:
    return (hostname or "").rstrip(".").lower()

def is_allowed_host(hostname: str | None) -> bool:
    host = normalize_host(hostname)
    return any(host == allowed or host.endswith("." + allowed) for allowed in ALLOWED_DOMAINS)

def is_ozon_host(hostname: str | None) -> bool:
    host = normalize_host(hostname)
    return any(host == domain or host.endswith("." + domain) for domain in OZON_DOMAINS)

def is_wb_host(hostname: str | None) -> bool:
    host = normalize_host(hostname)
    return any(host == domain or host.endswith("." + domain) for domain in WB_DOMAINS)

def validate_marketplace_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return (
            parsed.scheme in {"http", "https"}
            and not parsed.username
            and not parsed.password
            and is_allowed_host(parsed.hostname)
        )
    except ValueError:
        return False

def extract_wb_sku(url: str) -> str | None:
    """Извлекает артикул Wildberries из любого поддерживаемого формата ссылки WB."""
    match = WB_SKU_RE.search(url or "")
    if not match:
        return None
    for group in match.groups():
        if group:
            return group
    return None

def has_product_sku(url: str) -> bool:
    """Проверяет, содержит ли URL уже распознаваемый артикул WB или Ozon."""
    return bool(extract_wb_sku(url) or OZON_SKU_RE.search(url))

def has_expanded_product_url(url: str) -> bool:
    """Проверяет, содержит ли URL полноценный артикул товара, а не короткий ID Ozon."""
    return bool(extract_wb_sku(url) or OZON_FULL_SKU_RE.search(url))

def ozon_product_path(sku: str, canonical_url: str = "") -> str:
    """Возвращает сохранённый путь карточки Ozon, если он соответствует SKU."""
    try:
        parsed = urlparse(canonical_url)
        match = OZON_SKU_RE.search(canonical_url)
        if (
            validate_marketplace_url(canonical_url)
            and is_ozon_host(parsed.hostname)
            and match
            and any(group and group.casefold() == sku.casefold() for group in match.groups())
        ):
            return parsed.path
    except ValueError:
        pass

    if OZON_SHORT_PRODUCT_ID_RE.fullmatch(sku):
        return f"/p/{sku}"
    return f"/product/{sku}/"

def is_non_product_url(url: str) -> bool:
    """Отсекает ссылки на магазины (/s/...), бренды, категории и подборки."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return True
    path = parsed.path or "/"
    if path in {"", "/"}:
        return True
    return bool(NON_PRODUCT_PATH_RE.match(path))

def strip_antibot_params(url: str) -> str:
    """Возвращает чистый URL без параметров антибот-редиректа (?__rr=..., abt_att=...)."""
    try:
        parsed = urlparse(url)
        if not parsed.query:
            return url
        if "__rr=" in parsed.query or "abt_att=" in parsed.query:
            return urlunparse(parsed._replace(query="", fragment=""))
        return url
    except ValueError:
        return url

def is_antibot_redirect_loop(current_url: str, next_url: str) -> bool:
    """Определяет зацикливание антибота Ozon."""
    if has_expanded_product_url(next_url):
        return False
    try:
        cur_parsed = urlparse(current_url)
        nxt_parsed = urlparse(next_url)
    except ValueError:
        return False

    cur_path = (cur_parsed.path or "").rstrip("/")
    nxt_path = (nxt_parsed.path or "").rstrip("/")
    if cur_path != nxt_path:
        return False

    cur_qs = parse_qs(cur_parsed.query)
    nxt_qs = parse_qs(nxt_parsed.query)

    if "__rr" in nxt_qs:
        try:
            rr_val = int(nxt_qs["__rr"][0])
            if rr_val >= 2:
                return True
        except (ValueError, IndexError):
            return True
        if "__rr" in cur_qs and nxt_qs.get("abt_att") == cur_qs.get("abt_att"):
            return True

    return False

def get_short_code(url: str) -> str | None:
    """Извлекает короткий код из ссылок вида ozon.ru/p/o9870017 или ozon.ru/t/mUxty61."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    match = OZON_SHORT_PATH_RE.match(parsed.path or "")
    return match.group(1) if match else None

def build_storefront_url(url: str, storefront_domain: str, use_www: bool = False) -> str:
    """Подменяет домен Ozon на указанную витрину (например ozon.ru -> ozon.by или www.ozon.ru)."""
    parsed = urlparse(url)
    netloc = f"www.{storefront_domain}" if use_www and not storefront_domain.startswith("www.") else storefront_domain
    return urlunparse(parsed._replace(netloc=netloc))

def normalize_image_url(url: str | None) -> str:
    """Нормализует URL изображения товара и улучшает разрешение для CDN Ozon."""
    if not url or not isinstance(url, str):
        return ""
    cleaned = url.strip()
    if cleaned.startswith("//"):
        cleaned = "https:" + cleaned
    if not cleaned.startswith(("http://", "https://")):
        return ""
    # Для CDN Ozon заменяем маленькие превью /wc200/, /wc500/ на /wc1000/
    if "ozone.ru/" in cleaned or "ozonusercontent.com/" in cleaned:
        cleaned = re.sub(r"/wc\d+/", "/wc1000/", cleaned)
    return cleaned

def _is_complete_raw_image(raw_bytes: bytes) -> bool:
    """Проверяет базовые маркеры целостности контейнера для JPEG и PNG."""
    if not raw_bytes or len(raw_bytes) < 32:
        return False
    if raw_bytes.startswith(b"\xff\xd8\xff"):
        return raw_bytes.rstrip(b"\x00\r\n\t ").endswith(b"\xff\xd9")
    if raw_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return raw_bytes.rstrip(b"\x00\r\n\t ").endswith(b"IEND\xaeB`\x82")
    return True

def convert_image_bytes_to_jpeg(raw_bytes: bytes) -> io.BytesIO | None:
    """Готовит изображение в памяти с именем product.jpg, конвертируя WEBP/PNG в JPEG (чтобы Telegram не отправлял его как стикер)."""
    if not _is_complete_raw_image(raw_bytes):
        return None

    if PILImage is not None:
        with suppress(Exception):
            with PILImage.open(io.BytesIO(raw_bytes)) as img:
                img.load()
                if img.width <= 0 or img.height <= 0:
                    return None
                if img.mode not in ("RGB", "L"):
                    bg = PILImage.new("RGB", img.size, (255, 255, 255))
                    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                        rgba = img.convert("RGBA")
                        bg.paste(rgba, mask=rgba.split()[3])
                    else:
                        bg.paste(img.convert("RGB"))
                    out_img = bg
                else:
                    out_img = img.convert("RGB")
                buf = io.BytesIO()
                out_img.save(buf, format="JPEG", quality=92)
                buf.seek(0)
                buf.name = "product.jpg"
                return buf
        return None

    # Если Pillow не установлен, пропускаем только целые JPEG/PNG (WEBP Telegram считает стикером)
    if not raw_bytes.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n")):
        return None

    buf = io.BytesIO(raw_bytes)
    buf.name = "product.jpg"
    return buf

def clean_html_tags(raw: str) -> str:
    """Удаляет HTML-теги и декодирует HTML-сущности."""
    if not raw:
        return ""
    without_tags = re.sub(r"<[^>]+>", " ", raw)
    unescaped = html.unescape(without_tags)
    return re.sub(r"\s+", " ", unescaped).strip()

def clean_product_title(raw_title: str | None, brand: str | None = "") -> str:
    """Форматирует чистое и аккуратное название товара из карточки товара без SEO-мусора."""
    title = clean_html_tags(raw_title or "")
    clean_brand = clean_html_tags(brand or "").strip(" /|-–—•,.;:")
    if clean_brand.casefold() in GENERIC_BRAND_NAMES:
        clean_brand = ""

    if not title:
        return clean_brand

    # Отрезаем префиксы вида «Купить ...»
    title = TITLE_SEO_PREFIX_RE.sub("", title).strip()

    # Отрезаем суффиксы маркетплейсов («— купить в интернет-магазине OZON...», «/ Wildberries» и т.д.)
    for pattern in TITLE_SEO_SUFFIX_PATTERNS:
        title = pattern.sub("", title).strip()

    # Убираем висящие разделители по краям
    title = title.strip(" /|-–—•,.;:")
    title = re.sub(r"\s+", " ", title).strip()

    # Если весь заголовок кричащий КАПСОМ (более 12 букв и все в верхнем регистре), аккуратно нормализуем
    letters = [ch for ch in title if ch.isalpha()]
    if len(letters) > 12 and all(ch.isupper() for ch in letters):
        title = title.capitalize()
    elif title and title[0].islower():
        title = title[0].upper() + title[1:]

    # Если указан бренд и его ещё нет в названии, аккуратно добавляем в начало
    if clean_brand:
        brand_lower = clean_brand.casefold()
        title_lower = title.casefold()
        if brand_lower not in title_lower:
            title = f"{clean_brand} — {title}"
        elif title_lower.startswith(brand_lower + " / "):
            title = f"{clean_brand} — {title[len(clean_brand) + 3:].lstrip()}"

    if len(title) > 240:
        title = title[:237].rstrip(" ,;:-–—") + "..."

    return title

def build_ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    with suppress(ssl.SSLError):
        ctx.set_ciphers(
            "ECDHE+AESGCM:ECDHE+CHACHA20:DHE+AESGCM:DHE+CHACHA20:"
            "ECDH+AESGCM:DH+AESGCM:ECDH+AES:DH+AES:RSA+AESGCM:RSA+AES:!aNULL:!eNULL:!MD5:!DSS"
        )
    return ctx
