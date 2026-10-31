"""Explicit compatibility exports for parser helpers and shared constants."""

from .admin import AdminMixin
from .app import ParserApp
from .browser import OzonBrowserResolver
from .common import (
    build_ssl_context,
    build_storefront_url,
    clean_html_tags,
    clean_product_title,
    convert_image_bytes_to_jpeg,
    extract_wb_sku,
    get_short_code,
    has_product_sku,
    is_allowed_host,
    is_antibot_redirect_loop,
    is_non_product_url,
    is_ozon_host,
    is_wb_host,
    log,
    normalize_host,
    normalize_image_url,
    strip_antibot_params,
    validate_marketplace_url,
)
from .config import (
    ConfigError,
    Settings,
    env_bool,
    env_float,
    env_int,
)
from .constants import (
    ALLOWED_DOMAINS,
    ANTIBOT_MARKERS,
    AVG_MONTH_LABEL_RE,
    BROWSER_USER_AGENT,
    DEFAULT_HEADERS,
    GENERIC_BRAND_NAMES,
    H1_TAG_RE,
    HEAD_PRODUCT_RE,
    HEAD_READ_LIMIT,
    HTTP_RETRY_ATTEMPTS,
    IMAGE_READ_LIMIT,
    JSON_LD_SCRIPT_RE,
    MAX_FLOOD_RETRIES,
    META_TAG_RE,
    MINIMAL_STEALTH_SCRIPT,
    NON_PRODUCT_PATH_RE,
    NOT_FOUND_MARKERS,
    OUT_OF_STOCK_MARKERS,
    OZON_APP_HEADERS,
    OZON_CHEAPER_BLOCK_RE,
    OZON_DOMAINS,
    OZON_PRODUCT_PATH_RE,
    OZON_SHORT_PATH_RE,
    OZON_SKU_RE,
    PREVIEW_USER_AGENTS,
    PRICE_DROP_BADGE_RE,
    PRICE_VALUE_RE,
    STRICT_PRICE_WITH_CURRENCY_RE,
    TEXT_SKU_RE,
    TITLE_SEO_PREFIX_RE,
    TITLE_SEO_SUFFIX_PATTERNS,
    TITLE_TAG_RE,
    URL_RE,
    WB_BASKET_HOST_IN_URL_RE,
    WB_BASKET_VOL_CACHE,
    WB_COOKIE_DOMAINS,
    WB_DOMAINS,
    WB_SKU_RE,
)
from .entrypoint import (
    configure_logging,
    main,
)
from .market_data import MarketDataMixin
from .marketplace_parsing import (
    _iter_json_ld_objects,
    extract_price_from_post_text,
    get_wb_basket_host,
    get_wb_candidate_basket_hosts,
    get_wb_card_image_url,
    merge_card_data,
    parse_html_product_card,
    parse_json_ld_product,
    parse_ozon_composer_payload,
    parse_ozon_visible_price_text,
    parse_wb_detail_payload,
    parse_wb_price_history_avg,
    parse_wb_price_history_latest,
    parse_webpage_preview_card,
)
from .message_parsing import (
    extract_links,
    extract_product_url_from_text,
    extract_skus_from_text,
    extract_url_from_webpage_obj,
    match_message_preview_url,
    match_message_webpage_obj,
    resolve_ozon_canonical_host,
)
from .models import (
    Job,
    ParserState,
    ProductCardData,
    ProductInfo,
)
from .optional_dependencies import (
    CurlAsyncSession,
    PILImage,
    _ensure_playwright_browser_installed,
    _find_system_chromium,
    _load_async_playwright,
    _load_curl_cffi,
    async_playwright_factory,
)
from .pipeline import PipelineMixin
from .presentation import (
    _format_single_product_block,
    build_publication_text,
    parse_product_info,
    split_text,
)
from .pricing import (
    _collect_clean_leaf_strings,
    _extract_all_strict_prices_byn,
    _find_math_trio_avg_price,
    _is_plausible_avg_price,
    convert_to_byn,
    extract_avg_month_price_from_text,
    extract_ozon_avg_price_from_state,
    extract_ozon_price_modal_urls,
    format_byn_price,
    normalize_ozon_modal_path,
    parse_price_value_and_currency,
)
from .runtime import RuntimeMixin
from .sources import SourcesMixin
from .storage import DuplicateDB

__all__ = [name for name in globals() if not name.startswith("__")]
