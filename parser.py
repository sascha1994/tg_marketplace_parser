import asyncio
import html
import io
import ipaddress
import json
import logging
import os
import random
import re
import shutil
import sqlite3
import ssl
import subprocess
import sys
import time
from contextlib import closing, suppress
from dataclasses import dataclass, field, replace
from typing import Any, Iterable
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse, urlunparse

import aiohttp
from dotenv import load_dotenv
from telethon import Button, TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.tl.functions.messages import GetDialogFiltersRequest
from telethon.tl.types import MessageMediaWebPage
from telethon.utils import get_peer_id

with suppress(ImportError):
    from telethon.tl.functions.messages import GetWebPagePreviewRequest, GetWebPageRequest

with suppress(ImportError):
    from PIL import Image as PILImage

load_dotenv()


def _load_curl_cffi():
    try:
        from curl_cffi.requests import AsyncSession as CurlAsyncSession
        return CurlAsyncSession
    except ImportError:
        pass

    if os.getenv("AUTO_INSTALL_CURL_CFFI", "true").strip().lower() not in {"1", "true", "yes", "on"}:
        return None

    for extra_args in ([], ["--break-system-packages"], ["--user"]):
        with suppress(Exception):
            res = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "-q",
                    "--disable-pip-version-check",
                    *extra_args,
                    "curl_cffi",
                ],
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )
            if res.returncode == 0:
                from curl_cffi.requests import AsyncSession as CurlAsyncSession
                return CurlAsyncSession
    return None


def _load_async_playwright():
    # Сначала пробуем patchright (undetected-форк Playwright), если он установлен
    with suppress(ImportError):
        from patchright.async_api import async_playwright
        return async_playwright

    with suppress(ImportError):
        from playwright.async_api import async_playwright
        return async_playwright

    if os.getenv("AUTO_INSTALL_PLAYWRIGHT", "true").strip().lower() not in {"1", "true", "yes", "on"}:
        return None

    for extra_args in ([], ["--break-system-packages"], ["--user"]):
        with suppress(Exception):
            res = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "-q",
                    "--disable-pip-version-check",
                    *extra_args,
                    "playwright",
                ],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            if res.returncode == 0:
                break

    with suppress(Exception):
        from playwright.async_api import async_playwright
        return async_playwright

    return None


def _find_system_chromium() -> str | None:
    custom_exec = os.getenv("OZON_CHROME_EXECUTABLE_PATH", "").strip()
    if custom_exec and os.path.isfile(custom_exec):
        return custom_exec

    candidates = (
        "google-chrome-stable",
        "google-chrome",
        "chromium",
        "chromium-browser",
        "chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    )
    for candidate in candidates:
        path = candidate if os.path.isabs(candidate) else shutil.which(candidate)
        if path and os.path.isfile(path):
            return path
    return None


def _ensure_playwright_browser_installed() -> None:
    if _find_system_chromium():
        return
    if os.getenv("AUTO_INSTALL_PLAYWRIGHT", "true").strip().lower() not in {"1", "true", "yes", "on"}:
        return
    with suppress(Exception):
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True,
            text=True,
            timeout=240,
            check=False,
        )


CurlAsyncSession = _load_curl_cffi()
async_playwright_factory = _load_async_playwright()

ALLOWED_DOMAINS = frozenset(
    {
        "wildberries.ru",
        "wildberries.by",
        "wb.ru",
        "ozon.ru",
        "ozon.by",
        "ozon.kz",
    }
)
OZON_DOMAINS = ("ozon.ru", "ozon.by", "ozon.kz")
WB_DOMAINS = ("wildberries.ru", "wildberries.by", "wb.ru")

WB_SEARCH_SHARDS = (
    "common",
    "preset",
    "msk",
    "blackhole",
    "male",
    "female",
    "sport",
    "beauty",
    "electronic",
    "household",
)

# Кэш найденных корзин Wildberries: vol -> хост (например "basket-28.wbbasket.ru")
WB_BASKET_CACHE: dict[int, str] = {}

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

PREVIEW_USER_AGENTS = (
    "TelegramBot (like TwitterBot)",
    "vkShare; (+http://vk.com/dev/Share)",
    "WhatsApp/2.24.1.6 i",
    BROWSER_USER_AGENT,
)

OZON_APP_HEADERS = {
    "User-Agent": "ozonapp_android/17.48.0+2540",
    "x-o3-app-name": "ozonapp_android",
    "x-o3-app-version": "17.48.0(2540)",
    "x-o3-device-type": "mobile",
    "x-o3-sample-trace": "false",
    "Accept": "application/json; charset=utf-8",
    "Accept-Language": "ru-RU,ru;q=0.9",
}

OZON_WEB_HEADERS = {
    "User-Agent": BROWSER_USER_AGENT,
    "x-o3-app-name": "dweb_client",
    "x-o3-app-version": "release_2026",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Referer": "https://ozon.by/",
}

DEFAULT_HEADERS = {
    "User-Agent": BROWSER_USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "max-age=0",
    "Sec-CH-UA": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-CH-UA-Mobile": "?0",
    "Sec-CH-UA-Platform": '"Windows"',
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

# Минимальный, чистый init-скрипт без переопределения own-свойств на объекте navigator
MINIMAL_STEALTH_SCRIPT = """
(() => {
    if (!window.chrome) {
        const chromeObj = {
            app: {
                isInstalled: false,
                InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' },
                RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' }
            },
            runtime: {
                OnInstalledReason: { CHROME_UPDATE: 'chrome_update', INSTALL: 'install', SHARED_MODULE_UPDATE: 'shared_module_update', UPDATE: 'update' },
                OnRestartRequiredReason: { APP_UPDATE: 'app_update', OS_UPDATE: 'os_update', PERIODIC: 'periodic' },
                PlatformArch: { ARM: 'arm', ARM64: 'arm64', MIPS: 'mips', MIPS64: 'mips64', X86_32: 'x86-32', X86_64: 'x86-64' },
                PlatformNaclArch: { ARM: 'arm', MIPS: 'mips', MIPS64: 'mips64', X86_32: 'x86-32', X86_64: 'x86-64' },
                PlatformOs: { ANDROID: 'android', CROS: 'cros', LINUX: 'linux', MAC: 'mac', OPENBSD: 'openbsd', WIN: 'win' },
                RequestUpdateCheckStatus: { NO_UPDATE: 'no_update', THROTTLED: 'throttled', UPDATE_AVAILABLE: 'update_available' }
            },
            loadTimes: function() { return {}; },
            csi: function() { return {}; }
        };
        Object.defineProperty(window, 'chrome', {
            value: chromeObj,
            writable: true,
            enumerable: true,
            configurable: false
        });
    }
})();
"""

MAX_FLOOD_RETRIES = 3
HTTP_RETRY_ATTEMPTS = 3
HEAD_READ_LIMIT = 450_000
IMAGE_READ_LIMIT = 8_000_000

URL_RE = re.compile(r"https?://[^\s<>\[\]\(\)\"']+", re.IGNORECASE)

# Поддержка любых поддоменов wildberries.(ru|by) и короткого домена wb.ru
WB_SKU_RE = re.compile(
    r"(?:[a-z0-9-]+\.)?(?:wildberries\.(?:ru|by)|wb\.ru)/catalog/(\d+)",
    re.IGNORECASE,
)

# Поддержка любых поддоменов ozon.(ru|by|kz), путей /product/...-<sku>, /products/<sku>, /context/detail/id/<sku> и query ?sku=<sku>
OZON_SKU_RE = re.compile(
    r"(?:[a-z0-9-]+\.)?ozon\.(?:ru|by|kz)/(?:"
    r"(?:product|products)/(?:[^/?#\"'\s<>\\]*-)?(\d{5,15})(?:[/?#\"'\s<>\\]|$)|"
    r"context/detail/id/(\d{5,15})(?:[/?#\"'\s<>\\]|$)|"
    r"[^\s\"'<>]*?[?&](?:sku|skuId|product_id|item_id)=(\d{5,15})(?:[&#\"'\s<>\\]|$)"
    r")",
    re.IGNORECASE,
)

# Поиск полного пути /product/<slug>-<sku>/ в теле ответа/JSON
OZON_PRODUCT_PATH_RE = re.compile(
    r"((?:https?://(?:[a-z0-9-]+\.)?ozon\.(?:ru|by|kz))?/(?:(?:product|products)/(?:[^/?#\"'\s<>\\]+-)?|context/detail/id/)(\d{5,15})(?:/|\b)[^\s\"'<>\\]*)",
    re.IGNORECASE,
)

# Поиск артикула в HTML/JSON (<head>, body, composer-api, deeplink, относительные ссылки /product/...-<sku>)
HEAD_PRODUCT_RE = re.compile(
    r"(?:"
    r"(?:ozon\.(?:ru|by|kz))?/(?:(?:product|products)/(?:[^/\"'?#\s<>\\]*-)?|context/detail/id/)|"
    r"ozon://(?:products|product)/|"
    r"[?&](?:sku|skuId|product_id|item_id)=|"
    r"\"(?:sku|skuId|itemId|product_id)\"\s*:\s*\"?"
    r")"
    r"(\d{5,15})(?:[/?#\"'\\&\s<>,}]|$)",
    re.IGNORECASE,
)

# Извлечение артикула из текста поста или скобок заголовка («Артикул: 12345678», «(123456789)»)
TEXT_SKU_RE = re.compile(
    r"(?:(?:артикул|арт\.?|код\s+товара|sku)\s*[:#№\-–—]?\s*(\d{6,15})\b|\(\s*(\d{7,15})\s*\))",
    re.IGNORECASE,
)

# Короткие/реферальные пути Ozon (/t/XXXX или /p/oXXXXXXX)
OZON_SHORT_PATH_RE = re.compile(r"^/(?:t|p)/([A-Za-z0-9_-]+)", re.IGNORECASE)

# Заведомо нетоварные разделы (магазины, продавцы, бренды, категории, подборки)
NON_PRODUCT_PATH_RE = re.compile(
    r"^/(?:s|seller|brand|Brands|category|highlight|search|actions|special)/",
    re.IGNORECASE,
)

ANTIBOT_MARKERS = (
    "доступ ограничен",
    "похоже, нет соединения",
    "выключите vpn",
    "почти готово",
    "проверяем браузер",
    "один момент",
    "incidentid",
    "fab_chlg",
    "fab_chig",
    "ozon-antibot",
    "variti",
)

OUT_OF_STOCK_MARKERS = (
    "этот товар закончился",
    "товара нет в наличии",
    "нет в наличии",
    "товар закончился",
    "раскупили",
    "продано",
    "нет в продаже",
    "недоступен для заказа",
    "товар недоступен",
    "снят с продажи",
    "sold out",
    "out of stock",
    "outofstock",
)

NOT_FOUND_MARKERS = (
    "страница не найдена",
    "товар не найден",
    "такой страницы не существует",
    "по вашему запросу ничего не найдено",
    "ошибка 404",
)

GENERIC_BRAND_NAMES = frozenset(
    {
        "",
        "нет бренда",
        "без бренда",
        "noname",
        "no name",
        "no brand",
        "generic",
        "oem",
        "wildberries",
        "ozon",
    }
)

# Регулярные выражения для очистки заголовка карточки товара от SEO-мусора маркетплейсов
TITLE_SEO_PREFIX_RE = re.compile(
    r"^\s*(?:купить|заказать|покупайте)\s+",
    re.IGNORECASE,
)

TITLE_SEO_SUFFIX_PATTERNS = (
    re.compile(
        r"\s*(?:[—–\-|•/]\s*)?(?:купить|заказать)\s+(?:в\s+интернет[\s-]*магазине|на\s+(?:ozon|озон|wildberries|вайлдберриз|вб)|по\s+(?:выгодной|низкой)\s+цене).*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"\s+в\s+интернет[\s-]*магазине\s+(?:ozon|озон|wildberries|вайлдберриз).*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"\s+по\s+(?:выгодной|низкой|доступной)\s+цене.*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"\s+с\s+(?:быстрой\s+)?доставкой\s+(?:по\s+беларуси|в\s+минске|на\s+ozon|на\s+wildberries).*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"\s*(?:[—–\-|•/]\s*)(?:ozon|озон|wildberries|вайлдберриз|ozon\.by|ozon\.ru|wildberries\.ru|wildberries\.by)\s*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"\s*\(\s*(?:арт(?:икул)?\.?\s*[:#№]?\s*)?\d{5,15}\s*\)\s*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"\s+(?:артикул|арт\.?|код\s+товара)\s*[:#№\-–—]?\s*\d{5,15}.*$",
        re.IGNORECASE,
    ),
)

PRICE_VALUE_RE = re.compile(
    r"(\d[\d\s\u00a0\u202f\u2009]*(?:[.,]\d{1,2})?)\s*"
    r"(byn|бел\.?\s*руб\.?|бр|br|б(?![а-яёa-z])|р\.|руб\.?|₽|rub|rur)?",
    re.IGNORECASE,
)

# Поиск средней цены за месяц в Ozon («8,79 BYN ... Средняя за сентябрь» или «Средняя за месяц: 8,79 BYN»)
# Важно: не захватываем справочную фразу «...со средней за прошлый месяц» внизу попапа «Стало дешевле»
OZON_MONTH_NAMES_PATTERN = (
    r"(?:прошлый\s+месяц|месяц|январ[ья]|феврал[ья]|март[а]?|апрел[ья]|ма[йя]|июн[ья]|"
    r"июл[ья]|август[а]?|сентябр[ья]|октябр[ья]|ноябр[ья]|декабр[ья])"
)

AVG_MONTH_LABEL_RE = re.compile(
    r"(?<!со\s)средн(?:яя|юю|ей)\s+(?:цен[ауы]\s+)?за\s+" + OZON_MONTH_NAMES_PATTERN,
    re.IGNORECASE,
)

AVG_PRICE_BEFORE_LABEL_RE = re.compile(
    r"(\d[\d\s\u00a0\u202f\u2009]*(?:[.,]\d{1,2})?)\s*"
    r"(byn|бел\.?\s*руб\.?|бр|br|р\.|руб\.?|₽|rub|rur)"
    r"(?!\s*(?:/|за)\s*\d*\s*шт)"
    r"[^0-9]{0,120}?"
    r"(?<!со\s)средн(?:яя|юю|ей)\s+(?:цен[ауы]\s+)?за\s+" + OZON_MONTH_NAMES_PATTERN,
    re.IGNORECASE,
)

AVG_PRICE_AFTER_LABEL_RE = re.compile(
    r"(?<!со\s)средн(?:яя|юю|ей)\s+(?:цен[ауы]\s+)?за\s+" + OZON_MONTH_NAMES_PATTERN
    + r"[^0-9]{0,120}?"
    r"(\d[\d\s\u00a0\u202f\u2009]*(?:[.,]\d{1,2})?)\s*"
    r"(byn|бел\.?\s*руб\.?|бр|br|р\.|руб\.?|₽|rub|rur)"
    r"(?!\s*(?:/|за)\s*\d*\s*шт)",
    re.IGNORECASE,
)

OZON_MODAL_LINK_RE = re.compile(
    r"((?:/api/(?:entrypoint-api|composer-api)\.bx/page/json/v2\?url=)?/modal/[A-Za-z0-9_\-/?=&%.]+)",
    re.IGNORECASE,
)

# Сумма скидки на зелёном бейдже в попапе «Стало дешевле» («↓ 1,11 BYN»)
PRICE_DROP_BADGE_RE = re.compile(
    r"(?:[↓⬇]|\\u2193|\\u2b07|&darr;)\s*(\d[\d\s\u00a0\u202f\u2009]*(?:[.,]\d{1,2})?)\s*"
    r"(byn|бел\.?\s*руб\.?|бр|br|р\.|руб\.?|₽|rub|rur)",
    re.IGNORECASE,
)

JSON_LD_SCRIPT_RE = re.compile(
    r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)

META_TAG_RE = re.compile(
    r"<meta\s+[^>]*?(?:property|name|itemprop)=[\"']([^\"']+)[\"'][^>]*?content=[\"']([^\"']*)[\"'][^>]*?>|"
    r"<meta\s+[^>]*?content=[\"']([^\"']*)[\"'][^>]*?(?:property|name|itemprop)=[\"']([^\"']+)[\"'][^>]*?>",
    re.IGNORECASE | re.DOTALL,
)

H1_TAG_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)
TITLE_TAG_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


class ConfigError(RuntimeError):
    pass


def env_int(name: str, *, default: int | None = None, minimum: int | None = None) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        if default is not None:
            return default
        raise ConfigError(f"В .env не задана обязательная переменная {name}")
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} должен быть целым числом, получено: {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} должен быть не меньше {minimum}")
    return value


def env_float(name: str, *, default: float, minimum: float | None = None) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw.strip().replace(",", "."))
    except ValueError as exc:
        raise ConfigError(f"{name} должен быть числом, получено: {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} должен быть не меньше {minimum}")
    return value


def env_bool(name: str, *, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    api_id: int
    api_hash: str
    bot_token: str
    admin_id: int
    target_channel: str
    folder_name: str
    cooldown_hours: int
    pending_ttl_minutes: int
    worker_count: int
    max_links_per_post: int
    request_timeout_seconds: int
    max_redirects: int
    dry_run: bool
    require_sku: bool = True
    use_browser_resolver: bool = True
    browser_headless: bool = True
    browser_challenge_timeout: int = 12
    browser_idle_timeout: int = 600
    ozon_proxy: str = ""
    ozon_canonical_host: str = "ozon.by"
    ozon_preferred_storefronts: tuple[str, ...] = ("ozon.by", "ozon.ru", "ozon.kz")
    wb_dest_byn: int = -59208
    wb_spp: int = 30
    rub_to_byn_fallback_rate: float = 0.0345
    skip_out_of_stock: bool = True
    require_price: bool = True
    prefer_card_image: bool = True
    data_dir: str = "data"

    @property
    def user_session_path(self) -> str:
        return os.path.join(self.data_dir, "session")

    @property
    def bot_session_path(self) -> str:
        return os.path.join(self.data_dir, "admin_bot_session")

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "seen_products.db")

    @property
    def browser_profile_dir(self) -> str:
        return os.path.join(self.data_dir, "ozon_browser_profile")

    @property
    def cookies_cache_path(self) -> str:
        return os.path.join(self.data_dir, "ozon_cookies.json")

    @classmethod
    def from_env(cls) -> "Settings":
        api_hash = os.getenv("TG_API_HASH", "").strip()
        bot_token = os.getenv("BOT_TOKEN", "").strip()
        target_channel = os.getenv("TG_TARGET_CHANNEL", "").strip()
        if not api_hash:
            raise ConfigError("В .env не задан TG_API_HASH")
        if not bot_token:
            raise ConfigError("В .env не задан BOT_TOKEN")
        if not target_channel:
            raise ConfigError("В .env не задан TG_TARGET_CHANNEL")

        folder_name = os.getenv("TG_FOLDER_NAME", "Скидки").strip().strip("\"'")
        if not folder_name:
            raise ConfigError("TG_FOLDER_NAME не может быть пустым")

        raw_storefronts = os.getenv("OZON_STOREFRONTS", "ozon.by,ozon.ru,ozon.kz")
        storefronts = tuple(
            s.strip().lower()
            for s in raw_storefronts.split(",")
            if s.strip().lower() in OZON_DOMAINS
        ) or ("ozon.by", "ozon.ru", "ozon.kz")

        canonical_host = os.getenv("OZON_CANONICAL_HOST", "ozon.by").strip().lower()
        if canonical_host not in OZON_DOMAINS:
            canonical_host = "ozon.by"

        return cls(
            api_id=env_int("TG_API_ID", minimum=1),
            api_hash=api_hash,
            bot_token=bot_token,
            admin_id=env_int("ADMIN_ID", minimum=1),
            target_channel=target_channel,
            folder_name=folder_name,
            cooldown_hours=env_int("COOLDOWN_HOURS", default=48, minimum=1),
            pending_ttl_minutes=env_int("PENDING_TTL_MINUTES", default=20, minimum=1),
            worker_count=env_int("WORKER_COUNT", default=2, minimum=1),
            max_links_per_post=env_int("MAX_LINKS_PER_POST", default=5, minimum=1),
            request_timeout_seconds=env_int("REQUEST_TIMEOUT_SECONDS", default=12, minimum=1),
            max_redirects=env_int("MAX_REDIRECTS", default=6, minimum=1),
            dry_run=env_bool("DRY_RUN", default=False),
            require_sku=env_bool("REQUIRE_SKU", default=True),
            use_browser_resolver=env_bool("USE_BROWSER_RESOLVER", default=True),
            browser_headless=env_bool("OZON_BROWSER_HEADLESS", default=True),
            browser_challenge_timeout=env_int("OZON_CHALLENGE_TIMEOUT", default=12, minimum=5),
            browser_idle_timeout=env_int("OZON_BROWSER_IDLE_SECONDS", default=600, minimum=60),
            ozon_proxy=os.getenv("OZON_PROXY", "").strip(),
            ozon_canonical_host=canonical_host,
            ozon_preferred_storefronts=storefronts,
            wb_dest_byn=env_int("WB_DEST_BYN", default=-59208),
            wb_spp=env_int("WB_SPP", default=30, minimum=0),
            rub_to_byn_fallback_rate=env_float("RUB_TO_BYN_RATE", default=0.0345, minimum=0.001),
            skip_out_of_stock=env_bool("SKIP_OUT_OF_STOCK", default=True),
            require_price=env_bool("REQUIRE_PRICE", default=True),
            prefer_card_image=env_bool("PREFER_CARD_IMAGE", default=True),
        )


@dataclass(frozen=True)
class ProductCardData:
    title: str = ""
    brand: str = ""
    price_byn: float | None = None
    avg_month_price_byn: float | None = None
    image_url: str = ""
    in_stock: bool | None = None
    rating: float | None = None
    reviews_count: int | None = None


@dataclass(frozen=True)
class ProductInfo:
    original_url: str
    platform: str
    sku: str | None
    canonical_url: str
    sku_key: str
    title: str = ""
    brand: str = ""
    price_byn: float | None = None
    avg_month_price_byn: float | None = None
    image_url: str = ""
    rating: float | None = None
    reviews_count: int | None = None

    @property
    def display_title(self) -> str:
        cleaned = clean_product_title(self.title, self.brand)
        if cleaned:
            return cleaned
        return f"Товар {self.platform}"

    @property
    def formatted_price_byn(self) -> str | None:
        return format_byn_price(self.price_byn)

    @property
    def formatted_avg_month_price_byn(self) -> str | None:
        if self.platform == "Wildberries":
            return None
        if (
            self.avg_month_price_byn is not None
            and self.price_byn is not None
            and self.avg_month_price_byn > self.price_byn * 1.01
        ):
            return format_byn_price(self.avg_month_price_byn)
        return None


@dataclass
class Job:
    messages: list
    source_chat_id: int
    attempts: int = 0


@dataclass
class ParserState:
    is_paused: bool = False
    start_time: float = field(default_factory=time.time)
    incoming: int = 0
    published: int = 0
    duplicates: int = 0
    out_of_stock: int = 0
    rejected: int = 0
    errors: int = 0
    queue_dropped: int = 0
    browser_resolved: int = 0
    http_resolved: int = 0
    cards_parsed: int = 0


class DuplicateDB:
    """SQLite-дедупликация с краткоживущими резервированиями pending."""

    def __init__(self, db_path: str, cooldown_hours: int, pending_ttl_minutes: int):
        self.db_path = db_path
        self.cooldown_seconds = cooldown_hours * 3600
        self.pending_ttl_seconds = pending_ttl_minutes * 60
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _init_db(self) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS seen_items (
                    sku_key TEXT PRIMARY KEY,
                    platform TEXT NOT NULL,
                    sku TEXT,
                    canonical_url TEXT NOT NULL,
                    source_chat_id INTEGER,
                    source_message_id INTEGER,
                    status TEXT NOT NULL CHECK(status IN ('pending', 'published')),
                    created_at INTEGER NOT NULL,
                    published_at INTEGER
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_seen_items_status_time "
                "ON seen_items(status, published_at, created_at)"
            )

    def reserve_fresh(
        self,
        products: Iterable[ProductInfo],
        source_chat_id: int,
        source_message_id: int,
    ) -> set[str]:
        products_by_key = {product.sku_key: product for product in products}
        if not products_by_key:
            return set()

        now = int(time.time())
        published_cutoff = now - self.cooldown_seconds
        pending_cutoff = now - self.pending_ttl_seconds

        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "DELETE FROM seen_items WHERE status = 'published' AND published_at < ?",
                    (published_cutoff,),
                )
                conn.execute(
                    "DELETE FROM seen_items WHERE status = 'pending' AND created_at < ?",
                    (pending_cutoff,),
                )
                placeholders = ",".join("?" for _ in products_by_key)
                rows = conn.execute(
                    f"SELECT sku_key FROM seen_items WHERE sku_key IN ({placeholders})",
                    tuple(products_by_key),
                ).fetchall()
                occupied = {row[0] for row in rows}
                fresh_keys = set(products_by_key) - occupied
                conn.executemany(
                    """
                    INSERT INTO seen_items (
                        sku_key, platform, sku, canonical_url, source_chat_id,
                        source_message_id, status, created_at, published_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, NULL)
                    """,
                    [
                        (
                            product.sku_key,
                            product.platform,
                            product.sku,
                            product.canonical_url,
                            source_chat_id,
                            source_message_id,
                            now,
                        )
                        for key, product in products_by_key.items()
                        if key in fresh_keys
                    ],
                )
                conn.execute("COMMIT")
                return fresh_keys
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def mark_published(self, sku_keys: Iterable[str]) -> None:
        keys = tuple(set(sku_keys))
        if not keys:
            return
        now = int(time.time())
        placeholders = ",".join("?" for _ in keys)
        with closing(self._connect()) as conn:
            conn.execute(
                f"UPDATE seen_items SET status = 'published', published_at = ? "
                f"WHERE status = 'pending' AND sku_key IN ({placeholders})",
                (now, *keys),
            )

    def release_reservations(self, sku_keys: Iterable[str]) -> None:
        keys = tuple(set(sku_keys))
        if not keys:
            return
        placeholders = ",".join("?" for _ in keys)
        with closing(self._connect()) as conn:
            conn.execute(
                f"DELETE FROM seen_items WHERE status = 'pending' AND sku_key IN ({placeholders})",
                keys,
            )

    def clear_published(self) -> None:
        with closing(self._connect()) as conn:
            conn.execute("DELETE FROM seen_items")

    def count_by_status(self) -> tuple[int, int]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) FROM seen_items GROUP BY status"
            ).fetchall()
        counts = {status: count for status, count in rows}
        return counts.get("published", 0), counts.get("pending", 0)


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


def has_product_sku(url: str) -> bool:
    """Проверяет, содержит ли URL уже распознаваемый артикул WB или Ozon."""
    return bool(WB_SKU_RE.search(url) or OZON_SKU_RE.search(url))


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
    """Возвращает чистый URL без параметров антибот-редиректа (?__rr=..., abt_att=...), сохраняя полезные параметры."""
    try:
        parsed = urlparse(url)
        if not parsed.query:
            return url
        if "__rr=" not in parsed.query and "abt_att=" not in parsed.query:
            return url
        kept_pairs = [
            part
            for part in parsed.query.split("&")
            if part and not part.startswith(("__rr=", "abt_att=", "origin_referer="))
        ]
        return urlunparse(parsed._replace(query="&".join(kept_pairs), fragment=""))
    except ValueError:
        return url


def is_antibot_redirect_loop(current_url: str, next_url: str) -> bool:
    """Определяет зацикливание антибота Ozon."""
    if has_product_sku(next_url):
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
            if rr_val >= 3:
                return True
        except (ValueError, IndexError):
            return True
        if "__rr" in cur_qs and nxt_qs.get("__rr") == cur_qs.get("__rr") and nxt_qs.get("abt_att") == cur_qs.get("abt_att"):
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
    if "ozone.ru/" in cleaned or "ozonusercontent.com/" in cleaned:
        cleaned = re.sub(r"/wc\d+/", "/wc1000/", cleaned)
    return cleaned


def convert_image_bytes_to_jpeg(raw_bytes: bytes) -> io.BytesIO | None:
    """Готовит изображение в памяти с именем product.jpg, конвертируя WEBP/PNG в JPEG (чтобы Telegram не отправлял его как стикер)."""
    if not raw_bytes or len(raw_bytes) < 32:
        return None

    if "PILImage" in globals():
        with suppress(Exception):
            with PILImage.open(io.BytesIO(raw_bytes)) as img:
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

    if raw_bytes[:4] == b"RIFF" and raw_bytes[8:12] == b"WEBP":
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

    title = TITLE_SEO_PREFIX_RE.sub("", title).strip()

    for pattern in TITLE_SEO_SUFFIX_PATTERNS:
        title = pattern.sub("", title).strip()

    title = title.strip(" /|-–—•,.;:")
    title = re.sub(r"\s+", " ", title).strip()

    letters = [ch for ch in title if ch.isalpha()]
    if len(letters) > 12 and all(ch.isupper() for ch in letters):
        title = title.capitalize()
    elif title and title[0].islower():
        title = title[0].upper() + title[1:]

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


def format_byn_price(price_byn: float | None) -> str | None:
    """Форматирует цену в белорусских рублях (например: 24,50 бел. руб. или 120 бел. руб.)."""
    if price_byn is None or price_byn <= 0:
        return None
    rounded = round(float(price_byn), 2)
    int_part = int(rounded)
    frac_part = int(round((rounded - int_part) * 100))
    int_str = f"{int_part:,}".replace(",", " ")
    if frac_part == 0:
        return f"{int_str} бел. руб."
    return f"{int_str},{frac_part:02d} бел. руб."


def parse_price_value_and_currency(
    raw_value: Any,
    default_currency: str = "BYN",
) -> tuple[float | None, str]:
    """Извлекает числовое значение цены и код валюты ('BYN' или 'RUB') из строки или числа."""
    if raw_value is None:
        return None, default_currency

    if isinstance(raw_value, (int, float)):
        val = float(raw_value)
        return (val if val > 0 else None), default_currency.upper()

    text = clean_html_tags(str(raw_value))
    if not text:
        return None, default_currency.upper()

    text_lower = text.casefold()
    detected_currency = default_currency.upper()
    if (
        any(marker in text_lower for marker in ("byn", "бел", "бр", "br"))
        or re.search(r"(?:^|[\s\u00a0\u202f\u2009\d])б(?![а-яёa-z])", text_lower)
    ):
        detected_currency = "BYN"
    elif any(marker in text_lower for marker in ("₽", "rub", "rur", "рос")):
        detected_currency = "RUB"

    match = PRICE_VALUE_RE.search(text)
    if not match:
        return None, detected_currency

    num_str = re.sub(r"[\s\u00a0\u202f\u2009]+", "", match.group(1)).replace(",", ".")
    try:
        val = float(num_str)
    except ValueError:
        return None, detected_currency

    if val <= 0:
        return None, detected_currency

    unit = (match.group(2) or "").casefold().strip()
    if unit:
        if any(m in unit for m in ("byn", "бел", "бр", "br")) or unit == "б":
            detected_currency = "BYN"
        elif any(m in unit for m in ("₽", "rub", "rur")):
            detected_currency = "RUB"

    return val, detected_currency


def convert_to_byn(
    amount: float | None,
    currency: str,
    rub_to_byn_rate: float,
) -> float | None:
    """Конвертирует сумму в белорусские рубли (BYN), если исходная валюта — RUB."""
    if amount is None or amount <= 0:
        return None
    curr = (currency or "BYN").upper()
    if curr == "BYN":
        return round(amount, 2)
    if curr in {"RUB", "RUR"}:
        return round(amount * rub_to_byn_rate, 2)
    return round(amount, 2)


def _is_valid_avg_month_price(
    candidate_byn: float | None,
    actual_price_byn: float | None,
) -> bool:
    """Проверяет, что найденная средняя цена за месяц валидна и выше текущей цены."""
    if candidate_byn is None or candidate_byn <= 0:
        return False
    if actual_price_byn is not None and actual_price_byn > 0:
        if candidate_byn <= actual_price_byn * 1.005:
            return False
    return True


def extract_avg_month_price_from_text(
    text: str,
    *,
    actual_price_byn: float | None = None,
    default_currency: str = "BYN",
    rub_to_byn_rate: float = 0.0345,
) -> float | None:
    """Ищет среднюю цену за месяц в блоке/попапе «Стало дешевле» (например: «8,79 BYN Средняя за сентябрь» или через бейдж «↓ 1,11 BYN»)."""
    if not text:
        return None

    clean_text = clean_html_tags(text)

    for m_before in AVG_PRICE_BEFORE_LABEL_RE.finditer(clean_text):
        raw_p = f"{m_before.group(1)} {m_before.group(2)}"
        val, curr = parse_price_value_and_currency(raw_p, default_currency=default_currency)
        byn_val = convert_to_byn(val, curr, rub_to_byn_rate)
        if _is_valid_avg_month_price(byn_val, actual_price_byn):
            return byn_val

    for m_after in AVG_PRICE_AFTER_LABEL_RE.finditer(clean_text):
        raw_p = f"{m_after.group(1)} {m_after.group(2) or default_currency}"
        val, curr = parse_price_value_and_currency(raw_p, default_currency=default_currency)
        byn_val = convert_to_byn(val, curr, rub_to_byn_rate)
        if _is_valid_avg_month_price(byn_val, actual_price_byn):
            return byn_val

    if actual_price_byn is not None and actual_price_byn > 0:
        m_drop = PRICE_DROP_BADGE_RE.search(clean_text) or PRICE_DROP_BADGE_RE.search(text)
        if m_drop:
            raw_drop = f"{m_drop.group(1)} {m_drop.group(2)}"
            drop_val, drop_curr = parse_price_value_and_currency(raw_drop, default_currency=default_currency)
            drop_byn = convert_to_byn(drop_val, drop_curr, rub_to_byn_rate)
            if drop_byn is not None and drop_byn > 0:
                return round(actual_price_byn + drop_byn, 2)

    return None


def extract_ozon_avg_price_from_state(
    state: Any,
    *,
    actual_price_byn: float | None = None,
    default_currency: str = "BYN",
    rub_to_byn_rate: float = 0.0345,
) -> float | None:
    """Рекурсивно ищет среднюю цену за месяц в JSON-состоянии виджетов Ozon (webPrice, модальное окно «Стало дешевле»)."""
    if isinstance(state, dict):
        for key in (
            "averagePrice",
            "avgPrice",
            "monthAvgPrice",
            "averageMonthPrice",
            "prevMonthAvgPrice",
            "avgMonthPrice",
        ):
            if state.get(key):
                val, curr = parse_price_value_and_currency(state[key], default_currency=default_currency)
                byn_val = convert_to_byn(val, curr, rub_to_byn_rate)
                if _is_valid_avg_month_price(byn_val, actual_price_byn):
                    return byn_val

        dict_strings = [
            str(v).strip()
            for v in state.values()
            if isinstance(v, (str, int, float)) and str(v).strip()
        ]
        if dict_strings:
            for combined_dict_str in (
                " | ".join(dict_strings),
                " | ".join(reversed(dict_strings)),
            ):
                if AVG_MONTH_LABEL_RE.search(combined_dict_str) or PRICE_DROP_BADGE_RE.search(combined_dict_str):
                    inline_avg = extract_avg_month_price_from_text(
                        combined_dict_str,
                        actual_price_byn=actual_price_byn,
                        default_currency=default_currency,
                        rub_to_byn_rate=rub_to_byn_rate,
                    )
                    if _is_valid_avg_month_price(inline_avg, actual_price_byn):
                        return inline_avg

            if any(AVG_MONTH_LABEL_RE.search(s) for s in dict_strings):
                for s in dict_strings:
                    if AVG_MONTH_LABEL_RE.search(s) or "/шт" in s.lower():
                        continue
                    if not re.search(r"(byn|бел|бр|br|₽|rub|rur|руб)", s, re.IGNORECASE):
                        continue
                    val, curr = parse_price_value_and_currency(s, default_currency=default_currency)
                    byn_val = convert_to_byn(val, curr, rub_to_byn_rate)
                    if _is_valid_avg_month_price(byn_val, actual_price_byn):
                        return byn_val

        for v in state.values():
            found = extract_ozon_avg_price_from_state(
                v,
                actual_price_byn=actual_price_byn,
                default_currency=default_currency,
                rub_to_byn_rate=rub_to_byn_rate,
            )
            if _is_valid_avg_month_price(found, actual_price_byn):
                return found

    elif isinstance(state, list):
        for idx, item in enumerate(state):
            found = extract_ozon_avg_price_from_state(
                item,
                actual_price_byn=actual_price_byn,
                default_currency=default_currency,
                rub_to_byn_rate=rub_to_byn_rate,
            )
            if _is_valid_avg_month_price(found, actual_price_byn):
                return found
            if isinstance(item, (str, dict)):
                item_dump = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
                if AVG_MONTH_LABEL_RE.search(item_dump):
                    for neighbor_idx in (idx - 1, idx + 1):
                        if 0 <= neighbor_idx < len(state):
                            neighbor = state[neighbor_idx]
                            neighbor_dump = (
                                neighbor if isinstance(neighbor, str) else json.dumps(neighbor, ensure_ascii=False)
                            )
                            if "/шт" in neighbor_dump.lower():
                                continue
                            if not re.search(r"(byn|бел|бр|br|₽|rub|rur|руб)", neighbor_dump, re.IGNORECASE):
                                continue
                            val, curr = parse_price_value_and_currency(neighbor_dump, default_currency=default_currency)
                            byn_val = convert_to_byn(val, curr, rub_to_byn_rate)
                            if _is_valid_avg_month_price(byn_val, actual_price_byn):
                                return byn_val

    return None


def extract_ozon_modal_links(payload: Any) -> list[str]:
    """Ищет ссылки на модальное окно «Стало дешевле» (/modal/...) в JSON-ответе карточки Ozon."""
    raw_dump = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    if "\\/" in raw_dump:
        raw_dump = raw_dump.replace("\\/", "/")
    if "\\u002F" in raw_dump or "\\u002f" in raw_dump:
        raw_dump = re.sub(r"\\u002[fF]", "/", raw_dump)

    links: list[str] = []
    for match in OZON_MODAL_LINK_RE.finditer(raw_dump):
        cand = match.group(1).strip("\"'<>\\ ")
        if cand.startswith("/modal/"):
            ep = f"/api/entrypoint-api.bx/page/json/v2?url={quote(cand, safe='/?=&')}"
        else:
            ep = cand
        if ep not in links:
            links.append(ep)
    return links


def get_wb_basket_shard_number(vol: int) -> int:
    """Определяет расчётный номер корзины Wildberries (1..45) по vol = sku // 100000."""
    ranges = (
        (143, 1),
        (287, 2),
        (431, 3),
        (719, 4),
        (1007, 5),
        (1061, 6),
        (1115, 7),
        (1169, 8),
        (1313, 9),
        (1601, 10),
        (1655, 11),
        (1919, 12),
        (2045, 13),
        (2189, 14),
        (2405, 15),
        (2621, 16),
        (2837, 17),
        (3053, 18),
        (3269, 19),
        (3485, 20),
        (3701, 21),
        (3917, 22),
        (4133, 23),
        (4349, 24),
        (4565, 25),
        (4877, 26),
        (5189, 27),
        (5501, 28),
        (5813, 29),
        (6125, 30),
        (6437, 31),
        (6749, 32),
        (7061, 33),
        (7373, 34),
        (7685, 35),
        (7997, 36),
        (8309, 37),
        (8621, 38),
        (8933, 39),
        (9245, 40),
        (9557, 41),
        (9869, 42),
        (10181, 43),
        (10493, 44),
    )
    for max_vol, shard_num in ranges:
        if vol <= max_vol:
            return shard_num
    return 45


def get_wb_basket_host(vol: int) -> str:
    """Возвращает хост корзины Wildberries (с учётом кэша динамического обнаружения)."""
    if vol in WB_BASKET_CACHE:
        return WB_BASKET_CACHE[vol]
    shard_num = get_wb_basket_shard_number(vol)
    return f"basket-{shard_num:02d}.wbbasket.ru"


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


def parse_wb_latest_history_price(
    history_list: Any,
    *,
    rub_to_byn_rate: float = 0.0345,
) -> float | None:
    """Возвращает последнюю цену в BYN по истории цен Wildberries (price-history.json)."""
    if not isinstance(history_list, list) or not history_list:
        return None

    for entry in reversed(history_list):
        if not isinstance(entry, dict):
            continue
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
            return byn_val

    return None


def parse_wb_detail_payload(
    payload: dict[str, Any],
    *,
    target_sku: str | int | None = None,
    currency: str = "BYN",
    rub_to_byn_rate: float = 0.0345,
) -> ProductCardData | None:
    """Извлекает название, цену в BYN, ссылку на фото и наличие из ответа Wildberries (card.wb.ru или u-search.wb.ru)."""
    if not isinstance(payload, dict):
        return None

    data_block = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    products = data_block.get("products")
    if not isinstance(products, list) or not products:
        # Если в данном ответе список products пуст (например, для конкретного региона dest или устаревшей версии API),
        # возвращаем None, чтобы парсер проверил следующий dest / эндпоинт, а не браковал товар раньше времени.
        return None

    item = None
    if target_sku is not None:
        target_str = str(target_sku).strip()
        for candidate in products:
            if isinstance(candidate, dict) and str(candidate.get("id")) == target_str:
                item = candidate
                break
        if item is None and len(products) == 1 and isinstance(products[0], dict):
            item = products[0]
    else:
        item = products[0] if isinstance(products[0], dict) else None

    if not isinstance(item, dict):
        return None

    sku_id = item.get("id") or target_sku
    image_url = get_wb_card_image_url(sku_id) if sku_id else ""

    raw_name = str(item.get("name") or "").strip()
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

    total_quantity = item.get("totalQuantity")
    sizes = item.get("sizes") if isinstance(item.get("sizes"), list) else []

    total_stocks_qty = 0
    candidate_product_prices: list[float] = []
    fallback_product_prices: list[float] = []

    for size in sizes:
        if not isinstance(size, dict):
            continue
        stocks = size.get("stocks")
        size_qty = 0
        if isinstance(stocks, list):
            for st in stocks:
                if isinstance(st, dict):
                    with suppress(TypeError, ValueError):
                        size_qty += int(st.get("qty") or 0)
            total_stocks_qty += size_qty

        price_obj = size.get("price")
        if isinstance(price_obj, dict):
            # В современном API Wildberries (v4/detail, v18/search) итоговая цена покупателя на карточке
            # складывается из поля "product" + "logistics" (либо передаётся в "total").
            # Использование только "product" без "logistics" занижает цену относительно витрины РБ.
            tot_kop = int(price_obj.get("total") or 0)
            prod_kop = int(price_obj.get("product") or 0)
            log_kop = int(price_obj.get("logistics") or 0)
            basic_kop = int(price_obj.get("basic") or 0)

            if tot_kop > 0 or prod_kop > 0:
                final_kop = max(tot_kop, prod_kop + max(0, log_kop))
            else:
                final_kop = basic_kop

            if final_kop > 0:
                val = final_kop / 100.0
                fallback_product_prices.append(val)
                if size_qty > 0 or not isinstance(stocks, list):
                    candidate_product_prices.append(val)

    # Фолбэк на поля верхнего уровня salePriceU / priceU / clientPriceU (в копейках)
    for top_key in ("salePriceU", "priceU"):
        top_val = item.get(top_key)
        if top_val and int(top_val) > 0:
            fallback_product_prices.append(int(top_val) / 100.0)
    ext = item.get("extended")
    if isinstance(ext, dict):
        for ext_key in ("clientPriceU", "basicPriceU"):
            ext_val = ext.get(ext_key)
            if ext_val and int(ext_val) > 0:
                fallback_product_prices.append(int(ext_val) / 100.0)

    active_prices = candidate_product_prices or fallback_product_prices

    # Логика определения наличия на Wildberries:
    # В современных эндпоинтах WB (v4/detail, v2/detail, v18/search) массив stocks может быть пустым ([]),
    # но если товар есть в продаже, у него присутствует объект price (active_prices не пуст) и totalQuantity != 0.
    # Если же товар реально раскуплен, то totalQuantity == 0 либо ни у одного размера нет объекта price.
    if isinstance(total_quantity, int) and total_quantity <= 0 and total_stocks_qty <= 0:
        in_stock = False
    elif active_prices or total_stocks_qty > 0 or (isinstance(total_quantity, int) and total_quantity > 0):
        in_stock = True
    else:
        in_stock = False

    raw_price = min(active_prices) if active_prices else None
    price_byn = convert_to_byn(raw_price, currency, rub_to_byn_rate) if in_stock else None

    return ProductCardData(
        title=clean_product_title(raw_name, raw_brand),
        brand=raw_brand,
        price_byn=price_byn,
        image_url=image_url,
        in_stock=in_stock,
        rating=rating,
        reviews_count=reviews_count,
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
    image_url = ""
    in_stock: bool | None = None
    rating: float | None = None
    reviews_count: int | None = None

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
        if "webproductheading" in key_lower:
            heading_title = str(state.get("title") or "").strip()
            if heading_title:
                title = clean_product_title(heading_title, brand)

        elif "webbrand" in key_lower:
            b_name = str(state.get("name") or state.get("title") or "").strip()
            if b_name and b_name.casefold() not in GENERIC_BRAND_NAMES:
                brand = b_name
                if title:
                    title = clean_product_title(title, brand)

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

        elif "webprice" in key_lower:
            if state.get("isAvailable") is False:
                in_stock = False
            elif state.get("isAvailable") is True and in_stock is None:
                in_stock = True

            candidate_actual_byn: list[float] = []
            for price_key in ("cardPrice", "ozonCardPrice", "accentPrice", "finalPrice", "price", " salePrice"):
                raw_cand = state.get(price_key.strip())
                if raw_cand:
                    val, curr = parse_price_value_and_currency(raw_cand, default_currency=default_currency)
                    converted = convert_to_byn(val, curr, rub_to_byn_rate)
                    if converted is not None and converted > 0:
                        candidate_actual_byn.append(converted)

            if candidate_actual_byn:
                best_actual = min(candidate_actual_byn)
                price_byn = min(price_byn, best_actual) if price_byn is not None else best_actual

        elif "outofstock" in key_lower or "soldout" in key_lower:
            if state:
                in_stock = False

        elif "websale" in key_lower or "addtocart" in key_lower:
            if state.get("isAvailable") is False or state.get("outOfStock") is True:
                in_stock = False
            state_dump = json.dumps(state, ensure_ascii=False).casefold()
            if any(m in state_dump for m in OUT_OF_STOCK_MARKERS):
                in_stock = False

        elif "reviewproductscore" in key_lower or "singleproductscore" in key_lower:
            with suppress(TypeError, ValueError):
                if state.get("score") is not None:
                    rating = float(state["score"])
            with suppress(TypeError, ValueError):
                if state.get("count") is not None:
                    reviews_count = int(state["count"])

    for key_lower, state, raw_state in parsed_states:
        if "webprice" in key_lower or "pricedecrease" in key_lower or "pricehistory" in key_lower or "средняя за" in raw_state.lower() or "стало дешевле" in raw_state.lower():
            found_avg = extract_ozon_avg_price_from_state(
                state,
                actual_price_byn=price_byn,
                default_currency=default_currency,
                rub_to_byn_rate=rub_to_byn_rate,
            )
            if found_avg is None:
                found_avg = extract_avg_month_price_from_text(
                    raw_state,
                    actual_price_byn=price_byn,
                    default_currency=default_currency,
                    rub_to_byn_rate=rub_to_byn_rate,
                )
            if found_avg is not None:
                avg_month_price_byn = found_avg
                break

    if avg_month_price_byn is None:
        full_dump = json.dumps(payload, ensure_ascii=False)
        avg_month_price_byn = extract_avg_month_price_from_text(
            full_dump,
            actual_price_byn=price_byn,
            default_currency=default_currency,
            rub_to_byn_rate=rub_to_byn_rate,
        )

    if in_stock is None:
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
        image_url=image_url,
        in_stock=in_stock,
        rating=rating,
        reviews_count=reviews_count,
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
    )


def merge_card_data(primary: ProductCardData | None, secondary: ProductCardData | None) -> ProductCardData | None:
    """Объединяет данные карточки из двух источников, сохраняя наиболее полные поля.

    Если один из источников нашёл живую цену и наличие (in_stock=True, price_byn > 0),
    он имеет приоритет над региональным ответом без наличия.
    """
    if primary is None:
        return secondary
    if secondary is None:
        return primary

    primary_has_price = primary.in_stock is True and primary.price_byn is not None and primary.price_byn > 0
    secondary_has_price = secondary.in_stock is True and secondary.price_byn is not None and secondary.price_byn > 0

    if primary_has_price or secondary_has_price:
        in_stock: bool | None = True
    elif primary.in_stock is True or secondary.in_stock is True:
        in_stock = True
    elif primary.in_stock is False or secondary.in_stock is False:
        in_stock = False
    else:
        in_stock = None

    brand = primary.brand or secondary.brand
    if primary.title and secondary.title:
        title = primary.title if len(primary.title) >= len(secondary.title) else secondary.title
    else:
        title = primary.title or secondary.title
    title = clean_product_title(title, brand)

    if in_stock is False:
        price_byn = None
    elif primary.price_byn is not None and secondary.price_byn is not None:
        price_byn = min(primary.price_byn, secondary.price_byn)
    else:
        price_byn = primary.price_byn if primary.price_byn is not None else secondary.price_byn
    avg_month_price_byn = (
        primary.avg_month_price_byn if primary.avg_month_price_byn is not None else secondary.avg_month_price_byn
    ) if in_stock is not False else None
    image_url = primary.image_url or secondary.image_url

    return ProductCardData(
        title=title,
        brand=brand,
        price_byn=price_byn,
        avg_month_price_byn=avg_month_price_byn,
        image_url=image_url,
        in_stock=in_stock,
        rating=primary.rating if primary.rating is not None else secondary.rating,
        reviews_count=primary.reviews_count if primary.reviews_count is not None else secondary.reviews_count,
    )


def extract_product_url_from_text(raw_text: str, base_url: str) -> str | None:
    """Извлекает ссылку на товар или SKU из HTML, JSON (composer-api / entrypoint-api) или редиректа."""
    if not raw_text:
        return None

    canonical_host = resolve_ozon_canonical_host(urlparse(base_url).hostname)
    stripped = raw_text.strip()

    # 1. Если ответ — JSON от entrypoint-api / composer-api Ozon, проверяем поля redirect / pageInfo / seo
    if stripped.startswith("{"):
        with suppress(Exception):
            data = json.loads(stripped)
            if isinstance(data, dict):
                candidates: list[str] = []
                for top_key in ("redirect", "pageInfo", "seo", "shared"):
                    block = data.get(top_key)
                    if isinstance(block, dict):
                        for k in ("url", "redirectUrl", "location", "pageUrl", "canonicalUrl", "cleanPath", "path"):
                            val = block.get(k)
                            if isinstance(val, str) and val.strip():
                                candidates.append(val.strip())
                    elif isinstance(block, str) and block.strip():
                        candidates.append(block.strip())
                for k in ("redirectUrl", "url", "location"):
                    val = data.get(k)
                    if isinstance(val, str) and val.strip():
                        candidates.append(val.strip())
                for cand in candidates:
                    full_cand = (
                        cand
                        if cand.startswith(("http://", "https://"))
                        else f"https://www.{canonical_host}/{cand.lstrip('/')}"
                    )
                    if has_product_sku(full_cand):
                        return full_cand

    normalized_variants = [raw_text]
    if "\\/" in raw_text:
        normalized_variants.append(raw_text.replace("\\/", "/"))
    if "\\u002F" in raw_text or "\\u002f" in raw_text:
        normalized_variants.append(re.sub(r"\\u002[fF]", "/", raw_text))
    if "%2F" in raw_text or "%2f" in raw_text or "%3F" in raw_text or "%3f" in raw_text:
        with suppress(Exception):
            normalized_variants.append(unquote(raw_text))

    for variant in normalized_variants:
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
    """Пытается достать URL товара или артикул из объекта Telegram WebPage (включая cached_page)."""
    if not webpage:
        return None

    for attr in ("url", "display_url", "embed_url"):
        candidate = getattr(webpage, attr, None)
        if not candidate or not isinstance(candidate, str):
            continue
        full = candidate if candidate.startswith(("http://", "https://")) else "https://" + candidate.lstrip("/")
        if validate_marketplace_url(full) and has_product_sku(full):
            return full

    for attr in ("url", "display_url", "title", "description", "site_name"):
        val = getattr(webpage, attr, None)
        if not val or not isinstance(val, str):
            continue
        extracted = extract_product_url_from_text(val, base_url)
        if extracted:
            return extracted
        match = TEXT_SKU_RE.search(val)
        if match:
            sku = next((g for g in match.groups() if g), None)
            if sku:
                canonical_host = resolve_ozon_canonical_host(urlparse(base_url).hostname)
                return f"https://www.{canonical_host}/product/{sku}/"

    # Проверяем блок Instant View (cached_page), если он есть у превью Telegram
    cached_page = getattr(webpage, "cached_page", None)
    if cached_page is not None:
        with suppress(Exception):
            cp_str = str(cached_page)
            extracted = extract_product_url_from_text(cp_str, base_url)
            if extracted and has_product_sku(extracted):
                return extracted
            match = TEXT_SKU_RE.search(cp_str)
            if match:
                sku = next((g for g in match.groups() if g), None)
                if sku:
                    canonical_host = resolve_ozon_canonical_host(urlparse(base_url).hostname)
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


def extract_ozon_sku(url: str) -> str | None:
    """Извлекает артикул (SKU) Ozon из любого формата товарной ссылки Ozon."""
    match = OZON_SKU_RE.search(url or "")
    if not match:
        return None
    return next((g for g in match.groups() if g), None)


def extract_skus_from_text(text: str) -> list[str]:
    """Находит явно указанные артикулы в тексте поста."""
    if not text:
        return []
    found: list[str] = []
    for m in TEXT_SKU_RE.finditer(text):
        sku = next((g for g in m.groups() if g), None)
        if sku and sku not in found:
            found.append(sku)
    return found


def resolve_ozon_canonical_host(hostname: str | None, preferred_override: str = "") -> str:
    if preferred_override and preferred_override in OZON_DOMAINS:
        return preferred_override
    host = normalize_host(hostname)
    for domain in OZON_DOMAINS:
        if host == domain or host.endswith("." + domain):
            return domain
    return "ozon.by"


def parse_product_info(
    original_url: str,
    resolved_url: str,
    canonical_host_override: str = "ozon.by",
) -> ProductInfo:
    wb_match = WB_SKU_RE.search(resolved_url)
    if wb_match:
        sku = wb_match.group(1)
        return ProductInfo(
            original_url=original_url,
            platform="Wildberries",
            sku=sku,
            canonical_url=f"https://www.wildberries.by/catalog/{sku}/detail.aspx",
            sku_key=f"wildberries:{sku}",
            image_url=get_wb_card_image_url(sku),
        )

    ozon_sku = extract_ozon_sku(resolved_url)
    if ozon_sku:
        sku = ozon_sku
        orig_host = normalize_host(urlparse(original_url).hostname)
        base_host = orig_host if is_ozon_host(orig_host) else urlparse(resolved_url).hostname
        canonical_host = resolve_ozon_canonical_host(base_host, canonical_host_override)
        return ProductInfo(
            original_url=original_url,
            platform="Ozon",
            sku=sku,
            canonical_url=f"https://{canonical_host}/product/{sku}/",
            sku_key=f"ozon:{sku}",
        )

    parsed = urlparse(resolved_url)
    canonical_url = urlunparse(parsed._replace(query="", fragment=""))
    host = normalize_host(parsed.hostname)
    platform = "Wildberries" if is_wb_host(host) else "Ozon"
    return ProductInfo(
        original_url=original_url,
        platform=platform,
        sku=None,
        canonical_url=canonical_url,
        sku_key=f"{platform.lower()}:raw:{canonical_url}",
    )


def _format_single_product_block(product: ProductInfo, index: int | None = None) -> str:
    """Форматирует карточку товара для публикации:
    - Название магазина в самом начале
    - Без артикула
    - Приоритет средней цены за месяц (при наличии)
    - Пустые строки между каждым блоком для высокой читаемости
    """
    icon = "🟣" if product.platform == "Wildberries" else "🔵"
    store_line = f"{index}. {icon} {product.platform}" if index is not None else f"{icon} {product.platform}"

    paragraphs: list[str] = [
        store_line,
        f"🛍 {product.display_title}",
    ]

    price_str = product.formatted_price_byn
    avg_month_str = product.formatted_avg_month_price_byn
    if price_str:
        if avg_month_str:
            paragraphs.append(f"💰 Цена: {price_str} (средняя за месяц: {avg_month_str})")
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


def build_publication_text(products: list[ProductInfo]) -> str:
    """Формирует читаемый текст поста с разрывами между блоками на основе данных карточки товара."""
    if not products:
        return ""

    blocks: list[str] = []

    if len(products) == 1:
        blocks.append(_format_single_product_block(products[0], index=None))
    else:
        blocks.append(f"🔥 Подборка актуальных товаров ({len(products)} шт.)")
        for idx, product in enumerate(products, start=1):
            blocks.append(_format_single_product_block(product, index=idx))

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


def build_ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    with suppress(ssl.SSLError):
        ctx.set_ciphers(
            "ECDHE+AESGCM:ECDHE+CHACHA20:DHE+AESGCM:DHE+CHACHA20:"
            "ECDH+AESGCM:DH+AESGCM:ECDH+AES:DH+AES:RSA+AESGCM:RSA+AES:!aNULL:!eNULL:!MD5:!DSS"
        )
    return ctx


def parse_ozon_cookies_input(raw_input: str) -> list[dict[str, Any]]:
    """Универсальный парсер cookies Ozon из любого формата:
    1) JSON-массив из расширений Cookie-Editor / EditThisCookie / Playwright ([{"name": ..., "value": ...}])
    2) JSON-объект ({"cookies": [...]} или {"__Secure-access-token": "...", ...})
    3) Формат Netscape cookies.txt (табличный с табуляцией)
    4) Строка заголовка Cookie: key1=val1; key2=val2
    Автоматически дублирует ключевые сессионные cookies на все домены Ozon (.ozon.by, .ozon.ru).
    """
    if not raw_input or not raw_input.strip():
        return []

    text = raw_input.strip()
    if text.lower().startswith("cookie:"):
        text = text.split(":", 1)[1].strip()

    raw_items: list[dict[str, Any]] = []

    if text.startswith(("[", "{")):
        with suppress(Exception):
            parsed_json = json.loads(text)
            if isinstance(parsed_json, list):
                for item in parsed_json:
                    if isinstance(item, dict) and item.get("name") and item.get("value") is not None:
                        raw_items.append(item)
            elif isinstance(parsed_json, dict):
                if isinstance(parsed_json.get("cookies"), list):
                    for item in parsed_json["cookies"]:
                        if isinstance(item, dict) and item.get("name") and item.get("value") is not None:
                            raw_items.append(item)
                else:
                    for k, v in parsed_json.items():
                        if isinstance(v, (str, int, float)):
                            raw_items.append({"name": str(k).strip(), "value": str(v).strip(), "domain": ".ozon.by"})

    if not raw_items:
        # Проверяем Netscape-формат (7 колонок через табуляцию) или обычный key=value; key2=value2
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "\t" in line:
                parts = line.split("\t")
                if len(parts) >= 7:
                    raw_items.append(
                        {
                            "name": parts[5].strip(),
                            "value": parts[6].strip(),
                            "domain": parts[0].strip() or ".ozon.by",
                            "path": parts[2].strip() or "/",
                            "secure": parts[3].strip().upper() == "TRUE",
                        }
                    )
                    continue
            for chunk in line.split(";"):
                chunk = chunk.strip()
                if "=" not in chunk:
                    continue
                k, v = chunk.split("=", 1)
                k = k.strip()
                v = v.strip()
                if not k or k.lower() in {"path", "domain", "expires", "max-age", "samesite", "secure", "httponly"}:
                    continue
                raw_items.append({"name": k, "value": v, "domain": ".ozon.by"})

    normalized: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str]] = set()

    for item in raw_items:
        name = str(item.get("name") or "").strip()
        value = str(item.get("value") or "").strip()
        if not name or value == "":
            continue
        is_secure = name.startswith("__Secure-") or bool(item.get("secure", True))
        for target_domain in (".ozon.by", ".ozon.ru", ".ozon.kz"):
            key_pair = (name, target_domain)
            if key_pair in seen_pairs:
                continue
            seen_pairs.add(key_pair)
            cookie_dict: dict[str, Any] = {
                "name": name,
                "value": value,
                "domain": target_domain,
                "path": str(item.get("path") or "/"),
                "secure": is_secure,
            }
            if item.get("httpOnly") is not None:
                cookie_dict["httpOnly"] = bool(item["httpOnly"])
            normalized.append(cookie_dict)

    return normalized


def extract_ozon_auth_info(cookies: Iterable[dict[str, Any]]) -> tuple[bool, str]:
    """Проверяет, содержат ли cookies авторизованную сессию аккаунта Ozon.
    Возвращает (is_authenticated, user_id_or_details).
    """
    has_token = False
    user_id = ""
    for c in cookies:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name") or "").strip()
        val = str(c.get("value") or "").strip()
        if not val or val in {"0", "null", "undefined", "deleted"}:
            continue
        if name in {"__Secure-access-token", "__Secure-refresh-token", "access_token", "refresh_token"} and len(val) > 16:
            has_token = True
        elif name == "__Secure-user-id" and val != "0":
            user_id = val

    if has_token or user_id:
        label = f"ID {user_id}" if user_id else "токен активен"
        return True, label
    return False, ""


class OzonBrowserResolver:
    """Браузерный резолвер ссылок и парсер карточек Ozon и Wildberries."""

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
                            log("Закрытие браузера после простоя для освобождения памяти")
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
                log("Запущен Chromium (--headless=new + CDP native UA) для парсинга маркетплейсов")

            await self._inject_cached_cookies_unlocked()
            self._last_used = time.time()
            return True
        except Exception as exc:
            log(f"Не удалось запустить браузер Playwright: {type(exc).__name__}: {exc}", logging.WARNING)
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

    @staticmethod
    def _is_challenge_title(title: str) -> bool:
        t = (title or "").strip().lower()
        return any(
            m in t
            for m in (
                "доступ ограничен",
                "похоже, нет соединения",
                "почти готово",
                "один момент",
                "проверяем браузер",
            )
        )

    async def _warm_up_storefront(self, domain: str):
        """Открывает и прогревает главную страницу витрины (например https://ozon.by/ или https://www.ozon.ru/)."""
        assert self._context is not None
        existing = self._warm_pages.get(domain)
        if existing is not None:
            with suppress(Exception):
                if not existing.is_closed() and is_ozon_host(urlparse(existing.url).hostname):
                    ex_title = (await existing.title()).strip()
                    if not self._is_challenge_title(ex_title):
                        return existing
            self._warm_pages.pop(domain, None)
            with suppress(Exception):
                await existing.close()

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
                if await self._has_abt_cookie() and not self._is_challenge_title(title):
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
                    if title and not self._is_challenge_title(title):
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
                            const isApi = ep.startsWith('/api/');
                            const headers = isApi
                                ? {
                                    'Accept': 'application/json, text/plain, */*',
                                    'x-o3-app-name': 'dweb_client',
                                    'x-o3-app-version': 'release_2026'
                                }
                                : {
                                    'Accept': 'text/html,application/xhtml+xml,*/*;q=0.8'
                                };
                            const resp = await fetch(ep, {
                                method: 'GET',
                                credentials: 'include',
                                redirect: 'follow',
                                headers
                            });
                            const finalUrl = resp.url || '';
                            if (
                                finalUrl.includes('/product/') ||
                                finalUrl.includes('/products/') ||
                                finalUrl.includes('/context/detail/id/') ||
                                finalUrl.includes('sku=')
                            ) {
                                return { url: finalUrl, body: '' };
                            }
                            const text = await resp.text();
                            if (
                                text && (
                                    text.includes('/product/') ||
                                    text.includes('/products/') ||
                                    text.includes('/context/detail/id/') ||
                                    text.includes('"redirect"') ||
                                    text.includes('"pageInfo"') ||
                                    text.includes('"sku"') ||
                                    text.includes('skuId') ||
                                    text.includes('itemId')
                                )
                            ) {
                                return { url: finalUrl, body: text.slice(0, 300000) };
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
                if direct_url and has_product_sku(direct_url):
                    return direct_url
                body = payload.get("body") or ""
                if body:
                    extracted = extract_product_url_from_text(body, page.url or base_url)
                    if extracted and has_product_sku(extracted):
                        return extracted
                debug_info = payload.get("debug")
                if debug_info:
                    log(f"In-page fetch ({page.url}): {debug_info}", logging.DEBUG)
        return None

    async def fetch_wb_product_card(
        self,
        sku: str,
        *,
        rub_to_byn_rate: float = 0.0345,
    ) -> ProductCardData | None:
        """Открывает карточку Wildberries в браузере Chromium, перехватывая сетевые ответы API и читая DOM."""
        if not self._available or not sku:
            return None

        async with self._lock:
            if not await self._ensure_context():
                return None

            self._last_used = time.time()
            assert self._context is not None
            nav_page = await self._context.new_page()
            await self._apply_cdp_stealth(nav_page)

            intercepted_cards: list[ProductCardData] = []

            async def _on_response(resp) -> None:
                url_str = resp.url or ""
                if resp.status != 200:
                    return
                if any(
                    k in url_str
                    for k in ("/cards/", "/detail", "/exactmatch/", "card.json")
                ):
                    with suppress(Exception):
                        body_bytes = await resp.body()
                        if not body_bytes:
                            return
                        data = json.loads(body_bytes.decode("utf-8", errors="ignore"))
                        if "card.json" in url_str and isinstance(data, dict):
                            imt_name = str(data.get("imt_name") or data.get("subj_name") or "").strip()
                            selling = data.get("selling")
                            b_name = str(selling.get("brand_name") or "").strip() if isinstance(selling, dict) else ""
                            if imt_name:
                                intercepted_cards.append(
                                    ProductCardData(
                                        title=clean_product_title(imt_name, b_name),
                                        brand=b_name,
                                    )
                                )
                        elif isinstance(data, dict):
                            curr = "BYN" if "curr=byn" in url_str.lower() else "RUB"
                            parsed_c = parse_wb_detail_payload(
                                data,
                                target_sku=sku,
                                currency=curr,
                                rub_to_byn_rate=rub_to_byn_rate,
                            )
                            if parsed_c is not None:
                                intercepted_cards.append(parsed_c)

            nav_page.on("response", _on_response)

            try:
                for domain, default_curr in (("www.wildberries.by", "BYN"), ("www.wildberries.ru", "RUB")):
                    target_url = f"https://{domain}/catalog/{sku}/detail.aspx"
                    try:
                        resp = await nav_page.goto(target_url, wait_until="domcontentloaded", timeout=12_000)
                        if resp and resp.status == 404:
                            return ProductCardData(in_stock=False)
                    except Exception as exc:
                        log(f"Ошибка навигации WB {target_url}: {exc}", logging.DEBUG)
                        continue

                    # Ждём до 4.5 секунд рендера цены или блока «Нет в наличии» непосредственно в DOM карточки
                    deadline = time.monotonic() + 4.5
                    while time.monotonic() < deadline:
                        with suppress(Exception):
                            has_dom_ready = await nav_page.evaluate(
                                """() => {
                                    const p = document.querySelector('ins.price-block__final-price, .price-block__final-price, [class*="price-block__final-price"], .sold-out-product');
                                    return Boolean(p && p.innerText && p.innerText.trim().length > 0);
                                }"""
                            )
                            if has_dom_ready:
                                break
                        if any(c.in_stock is False for c in intercepted_cards):
                            break
                        await asyncio.sleep(0.4)

                    dom_info = None
                    with suppress(Exception):
                        dom_info = await nav_page.evaluate(
                            """() => {
                                const h1 = document.querySelector('.product-page__title, h1');
                                const brandEl = document.querySelector('.product-page__header-brand, [class*="brand-name"]');
                                const finalPriceEl = (
                                    document.querySelector('ins.price-block__final-price') ||
                                    document.querySelector('.price-block__final-price') ||
                                    document.querySelector('[class*="price-block__final-price"]') ||
                                    document.querySelector('.price-block__wallet-price')
                                );
                                const imgEl = document.querySelector('.photo-zoom__preview, .slide__content img, .product-page img');
                                const soldOutEl = document.querySelector('.sold-out-product, [class*="sold-out"]');
                                const bodyText = document.body ? document.body.innerText.slice(0, 20000) : '';
                                return {
                                    title: h1 ? h1.innerText.trim() : '',
                                    brand: brandEl ? brandEl.innerText.trim() : '',
                                    finalPrice: finalPriceEl ? finalPriceEl.innerText.trim() : '',
                                    imgSrc: imgEl ? (imgEl.currentSrc || imgEl.src || '') : '',
                                    soldOutText: soldOutEl ? soldOutEl.innerText.trim() : '',
                                    bodyText: bodyText
                                };
                            }"""
                        )

                    combined_card: ProductCardData | None = None
                    for ic in intercepted_cards:
                        combined_card = merge_card_data(combined_card, ic)

                    if isinstance(dom_info, dict):
                        dom_title = clean_product_title(dom_info.get("title"), dom_info.get("brand"))
                        raw_final = dom_info.get("finalPrice") or ""
                        val_f, curr_f = parse_price_value_and_currency(raw_final, default_currency=default_curr)
                        price_byn = convert_to_byn(val_f, curr_f, rub_to_byn_rate)

                        sold_text = f"{dom_info.get('soldOutText') or ''}\n{dom_info.get('bodyText') or ''}".casefold()
                        if price_byn is not None and price_byn > 0:
                            dom_in_stock: bool | None = True
                        elif any(m in sold_text for m in ("нет в наличии", "продано", "товар распродан")):
                            dom_in_stock = False
                        else:
                            dom_in_stock = None

                        dom_img = normalize_image_url(dom_info.get("imgSrc")) or get_wb_card_image_url(sku)
                        dom_card = ProductCardData(
                            title=dom_title,
                            brand=str(dom_info.get("brand") or ""),
                            price_byn=price_byn if dom_in_stock is not False else None,
                            image_url=dom_img,
                            in_stock=dom_in_stock,
                        )
                        if dom_card.price_byn is not None:
                            combined_card = merge_card_data(dom_card, combined_card)
                        else:
                            combined_card = merge_card_data(combined_card, dom_card)

                    if combined_card and (
                        combined_card.in_stock is False
                        or (combined_card.title and combined_card.price_byn is not None)
                    ):
                        log(
                            f"Карточка WB {sku} получена через браузер ({domain}): "
                            f"in_stock={combined_card.in_stock}, price={combined_card.price_byn} BYN"
                        )
                        return combined_card

                return None
            finally:
                with suppress(Exception):
                    await nav_page.close()

    async def fetch_product_card(
        self,
        sku: str,
        *,
        rub_to_byn_rate: float = 0.0345,
    ) -> tuple[ProductCardData | None, list[dict]]:
        """Получает данные карточки товара Ozon (название, цена в BYN, средняя цена за месяц, главное фото, наличие) через браузер."""
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

            product_path = f"/product/{sku}/"
            encoded_path = quote(product_path, safe="/")

            for domain in storefronts:
                default_currency = "BYN" if domain == "ozon.by" else "RUB"
                warm_page = await self._warm_up_storefront(domain)
                inpage_card: ProductCardData | None = None

                if warm_page is not None:
                    with suppress(Exception):
                        raw_json_str = await warm_page.evaluate(
                            """async ({ encodedPath }) => {
                                const endpoints = [
                                    `/api/entrypoint-api.bx/page/json/v2?url=${encodedPath}`,
                                    `/api/composer-api.bx/page/json/v2?url=${encodedPath}`
                                ];
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
                                            return text;
                                        }
                                    } catch (e) {}
                                }
                                return '';
                            }""",
                            {"encodedPath": encoded_path},
                        )
                        if raw_json_str:
                            data = json.loads(raw_json_str)
                            if isinstance(data, dict):
                                if data.get("_not_found"):
                                    cookies = await self._export_cookies()
                                    return ProductCardData(in_stock=False), cookies
                                inpage_card = parse_ozon_composer_payload(
                                    data,
                                    default_currency=default_currency,
                                    rub_to_byn_rate=rub_to_byn_rate,
                                )
                                if inpage_card and inpage_card.in_stock is False:
                                    cookies = await self._export_cookies()
                                    return inpage_card, cookies

                                # Если в ответе есть ссылка на модальное окно «Стало дешевле» (/modal/...),
                                # запрашиваем его JSON прямо из прогретой вкладки, чтобы достать среднюю цену за месяц
                                if inpage_card and inpage_card.avg_month_price_byn is None:
                                    modal_eps = extract_ozon_modal_links(raw_json_str)
                                    for modal_ep in modal_eps[:3]:
                                        with suppress(Exception):
                                            modal_text = await warm_page.evaluate(
                                                """async (ep) => {
                                                    try {
                                                        const r = await fetch(ep, {
                                                            method: 'GET',
                                                            credentials: 'include',
                                                            headers: { 'Accept': 'application/json' }
                                                        });
                                                        return r.ok ? await r.text() : '';
                                                    } catch (e) { return ''; }
                                                }""",
                                                modal_ep,
                                            )
                                            if modal_text:
                                                modal_avg = extract_avg_month_price_from_text(
                                                    modal_text,
                                                    actual_price_byn=inpage_card.price_byn,
                                                    default_currency=default_currency,
                                                    rub_to_byn_rate=rub_to_byn_rate,
                                                )
                                                if modal_avg is None:
                                                    with suppress(Exception):
                                                        modal_json = json.loads(modal_text)
                                                        modal_avg = extract_ozon_avg_price_from_state(
                                                            modal_json,
                                                            actual_price_byn=inpage_card.price_byn,
                                                            default_currency=default_currency,
                                                            rub_to_byn_rate=rub_to_byn_rate,
                                                        )
                                                if modal_avg is not None:
                                                    inpage_card = replace(inpage_card, avg_month_price_byn=modal_avg)
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

                assert self._context is not None
                nav_page = await self._context.new_page()
                await self._apply_cdp_stealth(nav_page)
                intercepted_avg_prices: list[float] = []
                intercepted_cards: list[ProductCardData] = []

                async def _on_ozon_response(resp) -> None:
                    url_str = resp.url or ""
                    if resp.status != 200:
                        return
                    if any(k in url_str for k in ("entrypoint-api", "composer-api", "/modal/", "price")):
                        with suppress(Exception):
                            body_bytes = await resp.body()
                            if not body_bytes:
                                return
                            body_str = body_bytes.decode("utf-8", errors="ignore")
                            ref_p = (
                                (inpage_card.price_byn if inpage_card else None)
                                or next((c.price_byn for c in intercepted_cards if c.price_byn), None)
                            )
                            if "widgetStates" in body_str or '"seo"' in body_str:
                                with suppress(Exception):
                                    j_data = json.loads(body_str)
                                    if isinstance(j_data, dict):
                                        c_parsed = parse_ozon_composer_payload(
                                            j_data,
                                            default_currency=default_currency,
                                            rub_to_byn_rate=rub_to_byn_rate,
                                        )
                                        if c_parsed:
                                            intercepted_cards.append(c_parsed)
                                            if c_parsed.avg_month_price_byn:
                                                intercepted_avg_prices.append(c_parsed.avg_month_price_byn)
                                        st_avg = extract_ozon_avg_price_from_state(
                                            j_data,
                                            actual_price_byn=ref_p or (c_parsed.price_byn if c_parsed else None),
                                            default_currency=default_currency,
                                            rub_to_byn_rate=rub_to_byn_rate,
                                        )
                                        if st_avg:
                                            intercepted_avg_prices.append(st_avg)
                            txt_avg = extract_avg_month_price_from_text(
                                body_str,
                                actual_price_byn=ref_p,
                                default_currency=default_currency,
                                rub_to_byn_rate=rub_to_byn_rate,
                            )
                            if txt_avg:
                                intercepted_avg_prices.append(txt_avg)

                nav_page.on("response", _on_ozon_response)
                target_url = f"https://{domain}/product/{sku}/" if domain == "ozon.by" else f"https://www.{domain}/product/{sku}/"
                try:
                    resp = await nav_page.goto(target_url, wait_until="domcontentloaded", timeout=11_000)
                    if resp and resp.status == 404:
                        cookies = await self._export_cookies()
                        return ProductCardData(in_stock=False), cookies

                    # Ждём появления блока цены или кнопки «Стало дешевле» в DOM
                    with suppress(Exception):
                        await nav_page.wait_for_selector(
                            '[data-widget="webPrice"], text=/Стало дешевле/i',
                            timeout=3500,
                        )
                    await asyncio.sleep(0.5)

                    # Открываем попап «Стало дешевле» (клик + наведение + JS click по вложенной кнопке),
                    # чтобы Ozon подгрузил модальное окно со средней ценой за прошлый месяц
                    with suppress(Exception):
                        cheaper_loc = nav_page.get_by_text(re.compile(r"Стало дешевле", re.IGNORECASE)).first
                        if await cheaper_loc.count() > 0:
                            with suppress(Exception):
                                await cheaper_loc.scroll_into_view_if_needed(timeout=1500)
                            with suppress(Exception):
                                await cheaper_loc.hover(timeout=1500)
                            with suppress(Exception):
                                await cheaper_loc.click(timeout=2000, force=True)

                    with suppress(Exception):
                        await nav_page.evaluate(
                            """() => {
                                const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                                let node;
                                while ((node = walker.nextNode())) {
                                    if (node.nodeValue && /стало\s+дешевле/i.test(node.nodeValue)) {
                                        const el = node.parentElement;
                                        if (el) {
                                            const clickable = el.closest('button, a, [role="button"], [tabindex]') || el;
                                            clickable.dispatchEvent(new MouseEvent('mouseenter', { bubbles: true }));
                                            clickable.dispatchEvent(new MouseEvent('mouseover', { bubbles: true }));
                                            clickable.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
                                            clickable.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
                                            clickable.click();
                                        }
                                        break;
                                    }
                                }
                            }"""
                        )

                    with suppress(Exception):
                        await nav_page.wait_for_selector("text=/Средняя за/i", timeout=2800)
                    await asyncio.sleep(0.4)

                    html_content = await nav_page.content()
                    dom_card = parse_html_product_card(
                        html_content,
                        default_currency=default_currency,
                        rub_to_byn_rate=rub_to_byn_rate,
                    )

                    for ic in intercepted_cards:
                        dom_card = merge_card_data(dom_card, ic)

                    with suppress(Exception):
                        dom_extra = await nav_page.evaluate(
                            """() => {
                                const bodyText = document.body ? document.body.innerText : '';
                                const galleryImg = document.querySelector('[data-widget="webGallery"] img, [data-widget="webMobGallery"] img');
                                const priceEl = document.querySelector('[data-widget="webPrice"]');
                                return {
                                    bodyText: bodyText.slice(0, 80000),
                                    priceText: priceEl ? priceEl.innerText : '',
                                    imgSrc: galleryImg ? (galleryImg.currentSrc || galleryImg.src || '') : ''
                                };
                            }"""
                        )
                        if isinstance(dom_extra, dict):
                            extra_img = normalize_image_url(dom_extra.get("imgSrc"))
                            body_text = str(dom_extra.get("bodyText") or "")
                            price_text = str(dom_extra.get("priceText") or "")
                            ref_price = (
                                (inpage_card.price_byn if inpage_card else None)
                                or (dom_card.price_byn if dom_card else None)
                            )
                            if ref_price is None and price_text:
                                p_val, p_curr = parse_price_value_and_currency(price_text, default_currency=default_currency)
                                ref_price = convert_to_byn(p_val, p_curr, rub_to_byn_rate)

                            extra_avg = (
                                extract_avg_month_price_from_text(
                                    body_text,
                                    actual_price_byn=ref_price,
                                    default_currency=default_currency,
                                    rub_to_byn_rate=rub_to_byn_rate,
                                )
                                or (intercepted_avg_prices[0] if intercepted_avg_prices else None)
                            )
                            if dom_card:
                                dom_card = replace(
                                    dom_card,
                                    price_byn=dom_card.price_byn or ref_price,
                                    avg_month_price_byn=dom_card.avg_month_price_byn or extra_avg,
                                    image_url=dom_card.image_url or extra_img,
                                )
                            elif extra_avg or extra_img or ref_price:
                                dom_card = ProductCardData(
                                    price_byn=ref_price,
                                    avg_month_price_byn=extra_avg,
                                    image_url=extra_img,
                                )

                    merged = merge_card_data(inpage_card, dom_card)
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

    async def _inject_cached_cookies_unlocked(self) -> None:
        """Загружает сохранённые cookies аккаунта Ozon из файла/окружения в контекст браузера Playwright."""
        if self._context is None:
            return
        cookies_to_add: list[dict[str, Any]] = []
        raw_env = os.getenv("OZON_COOKIES", "").strip()
        if raw_env:
            cookies_to_add.extend(parse_ozon_cookies_input(raw_env))
        if os.path.isfile(self.settings.cookies_cache_path):
            with suppress(Exception):
                with open(self.settings.cookies_cache_path, "r", encoding="utf-8") as f:
                    raw_file = f.read()
                cookies_to_add.extend(parse_ozon_cookies_input(raw_file))
        if not cookies_to_add:
            return
        pw_cookies: list[dict[str, Any]] = []
        for c in cookies_to_add:
            if not isinstance(c, dict) or not c.get("name") or c.get("value") is None:
                continue
            pw_c: dict[str, Any] = {
                "name": str(c["name"]),
                "value": str(c["value"]),
                "domain": str(c.get("domain") or ".ozon.by"),
                "path": str(c.get("path") or "/"),
                "secure": bool(c.get("secure", True)),
            }
            pw_cookies.append(pw_c)
        if pw_cookies:
            with suppress(Exception):
                await self._context.add_cookies(pw_cookies)

    async def import_cookies(self, new_cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Сохраняет новые cookies аккаунта Ozon на диск и внедряет их в живой браузерный контекст."""
        if not new_cookies:
            return []
        async with self._lock:
            existing: list[dict[str, Any]] = []
            if os.path.isfile(self.settings.cookies_cache_path):
                with suppress(Exception):
                    with open(self.settings.cookies_cache_path, "r", encoding="utf-8") as f:
                        loaded = json.load(f)
                    if isinstance(loaded, list):
                        existing = [c for c in loaded if isinstance(c, dict)]

            merged_map: dict[tuple[str, str], dict[str, Any]] = {}
            for c in existing:
                name = str(c.get("name") or "").strip()
                dom = str(c.get("domain") or ".ozon.by").strip()
                if name:
                    merged_map[(name, dom)] = c
            for c in new_cookies:
                name = str(c.get("name") or "").strip()
                dom = str(c.get("domain") or ".ozon.by").strip()
                if name:
                    merged_map[(name, dom)] = c

            merged_list = list(merged_map.values())
            os.makedirs(os.path.dirname(self.settings.cookies_cache_path) or ".", exist_ok=True)
            with suppress(OSError):
                with open(self.settings.cookies_cache_path, "w", encoding="utf-8") as f:
                    json.dump(merged_list, f, ensure_ascii=False)

            if self._context is not None:
                await self._inject_cached_cookies_unlocked()
                # Сбрасываем прогретые вкладки, чтобы следующий запрос шёл уже от имени аккаунта
                for page in list(self._warm_pages.values()):
                    with suppress(Exception):
                        await page.close()
                self._warm_pages.clear()

            return merged_list

    async def clear_cookies(self) -> None:
        """Очищает сохранённые cookies аккаунта Ozon."""
        async with self._lock:
            with suppress(OSError):
                if os.path.isfile(self.settings.cookies_cache_path):
                    os.remove(self.settings.cookies_cache_path)
            if self._context is not None:
                with suppress(Exception):
                    await self._context.clear_cookies()
                for page in list(self._warm_pages.values()):
                    with suppress(Exception):
                        await page.close()
                self._warm_pages.clear()

    async def interactive_login(self, start_url: str = "https://ozon.by/") -> list[dict[str, Any]]:
        """Открывает видимое окно браузера Chromium (--login-ozon) для однократного входа в аккаунт Ozon."""
        if async_playwright_factory is None:
            raise RuntimeError("Playwright не установлен. Установите: pip install playwright && playwright install chromium")

        os.makedirs(self.settings.browser_profile_dir, exist_ok=True)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _ensure_playwright_browser_installed)

        pw = await async_playwright_factory().start()
        context = None
        try:
            launch_kwargs: dict[str, Any] = {
                "user_data_dir": self.settings.browser_profile_dir,
                "headless": False,
                "viewport": {"width": 1280, "height": 900},
                "locale": "ru-RU",
                "timezone_id": "Europe/Minsk",
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                ],
            }
            exec_path = _find_system_chromium()
            if exec_path:
                launch_kwargs["executable_path"] = exec_path
            if self.settings.ozon_proxy:
                launch_kwargs["proxy"] = {"server": self.settings.ozon_proxy}

            context = await pw.chromium.launch_persistent_context(**launch_kwargs)
            await context.add_init_script(MINIMAL_STEALTH_SCRIPT)
            page = context.pages[0] if context.pages else await context.new_page()
            log(f"Открыто окно браузера: {start_url} — войдите в аккаунт Ozon (после входа окно закроется автоматически)")
            await page.goto(start_url, wait_until="domcontentloaded", timeout=30_000)

            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                if page.is_closed():
                    break
                with suppress(Exception):
                    cookies = await context.cookies()
                    is_auth, label = extract_ozon_auth_info(cookies)
                    if is_auth:
                        log(f"Обнаружен успешный вход в аккаунт Ozon ({label})! Сохраняем сессию...")
                        await asyncio.sleep(2.0)
                        cookies = await context.cookies()
                        expanded = parse_ozon_cookies_input(json.dumps(cookies, ensure_ascii=False)) or cookies
                        with open(self.settings.cookies_cache_path, "w", encoding="utf-8") as f:
                            json.dump(expanded, f, ensure_ascii=False)
                        return expanded
                await asyncio.sleep(1.0)

            cookies = await context.cookies()
            if cookies:
                with open(self.settings.cookies_cache_path, "w", encoding="utf-8") as f:
                    json.dump(cookies, f, ensure_ascii=False)
            return cookies
        finally:
            if context is not None:
                with suppress(Exception):
                    await context.close()
            with suppress(Exception):
                await pw.stop()

    async def _export_cookies(self) -> list[dict]:
        if self._context is None:
            return []
        with suppress(Exception):
            cookies = await self._context.cookies()
            if cookies:
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

            # Сначала проверяем родной домен короткой ссылки (например ozon.ru для https://ozon.ru/t/...),
            # так как короткие ссылки /t/... привязаны к базе редиректов своего домена
            storefronts: list[str] = [orig_domain]
            for domain in self.settings.ozon_preferred_storefronts:
                if domain not in storefronts:
                    storefronts.append(domain)

            assert self._context is not None

            for domain in storefronts:
                warm_page = await self._warm_up_storefront(domain)
                if warm_page is not None:
                    via_inpage = await self._fetch_via_inpage_api(warm_page, short_path, clean_url)
                    if via_inpage and has_product_sku(via_inpage):
                        cookies = await self._export_cookies()
                        log(f"Ссылка раскрыта через прогретый браузер ({domain} in-page): {clean_url} -> {via_inpage}")
                        return via_inpage, cookies

                nav_page = await self._context.new_page()
                await self._apply_cdp_stealth(nav_page)
                captured_urls: list[str] = []

                def _on_url_candidate(candidate_url: str) -> None:
                    if not candidate_url:
                        return
                    if has_product_sku(candidate_url):
                        captured_urls.append(candidate_url)
                        return
                    if "entrypoint-api" in candidate_url or "composer-api" in candidate_url:
                        qs = parse_qs(urlparse(candidate_url).query)
                        for url_param in qs.get("url", []):
                            extracted = extract_product_url_from_text(url_param, clean_url)
                            if extracted and has_product_sku(extracted):
                                captured_urls.append(extracted)

                def _handle_request(req) -> None:
                    _on_url_candidate(req.url)

                async def _inspect_api_response(resp) -> None:
                    with suppress(Exception):
                        body_bytes = await resp.body()
                        if body_bytes:
                            extracted = extract_product_url_from_text(
                                body_bytes[:HEAD_READ_LIMIT].decode("utf-8", errors="ignore"),
                                resp.url or clean_url,
                            )
                            if extracted and has_product_sku(extracted):
                                captured_urls.append(extracted)

                def _handle_response(resp) -> None:
                    _on_url_candidate(resp.url)
                    loc = resp.headers.get("location") or resp.headers.get("Location")
                    if loc:
                        full_loc = urljoin(resp.url, loc)
                        _on_url_candidate(full_loc)
                    if ("entrypoint-api" in resp.url or "composer-api" in resp.url) and resp.status == 200:
                        asyncio.create_task(_inspect_api_response(resp))

                def _handle_frame_navigated(frame) -> None:
                    if frame == nav_page.main_frame:
                        _on_url_candidate(frame.url)

                nav_page.on("request", _handle_request)
                nav_page.on("response", _handle_response)
                nav_page.on("framenavigated", _handle_frame_navigated)

                try:
                    www_order = (True, False) if domain == 'ozon.ru' else (False, True)
                    for use_www in www_order:
                        if captured_urls:
                            break
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
                            if has_product_sku(candidate):
                                cookies = await self._export_cookies()
                                log(f"Ссылка раскрыта через браузер ({domain}): {clean_url} -> {candidate}")
                                return candidate, cookies

                        if has_product_sku(nav_page.url):
                            cookies = await self._export_cookies()
                            log(f"Ссылка раскрыта через браузер ({domain}): {clean_url} -> {nav_page.url}")
                            return nav_page.url, cookies

                        cur_url = nav_page.url or ""
                        if cur_url.startswith(("chrome-error://", "about:blank")):
                            log(
                                f"Витрина {target_url} вернула цикл редиректа (cur_url={cur_url}); "
                                f"переходим к следующему варианту",
                                logging.DEBUG,
                            )
                            continue

                        # Проверяем, не показал ли антибот страницу блокировки без JS-челленджа
                        cur_title = ""
                        with suppress(Exception):
                            cur_title = (await nav_page.title()).strip()

                        if nav_status == 403 and "похоже, нет соединения" in cur_title.lower():
                            # Пробуем нажать кнопку «Обновить» на странице заглушки Ozon, если она есть
                            clicked_reload = False
                            with suppress(Exception):
                                btn = await nav_page.query_selector("button, a.reload-button")
                                if btn:
                                    await btn.click(timeout=1500)
                                    clicked_reload = True
                                    await asyncio.sleep(1.5)
                            if not clicked_reload:
                                log(
                                    f"Статус вкладки {target_url}: HTTP {nav_status}, title={cur_title!r} "
                                    f"(IP заблокирован антиботом на прямом переходе)",
                                    logging.INFO,
                                )
                                continue

                        deadline = time.monotonic() + min(6, self.settings.browser_challenge_timeout)
                        while time.monotonic() < deadline:
                            for candidate in reversed(captured_urls):
                                if has_product_sku(candidate):
                                    cookies = await self._export_cookies()
                                    log(f"Ссылка раскрыта через браузер ({domain}): {clean_url} -> {candidate}")
                                    return candidate, cookies
                            if has_product_sku(nav_page.url):
                                cookies = await self._export_cookies()
                                log(f"Ссылка раскрыта через браузер ({domain}): {clean_url} -> {nav_page.url}")
                                return nav_page.url, cookies

                            await self._simulate_human_interaction(nav_page)
                            await asyncio.sleep(0.5)

                        with suppress(Exception):
                            title = (await nav_page.title()).strip()
                            html_body = await nav_page.content()
                            extracted = extract_product_url_from_text(html_body, target_url)
                            if extracted and has_product_sku(extracted):
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


class ParserApp:
    def __init__(self, settings: Settings):
        self.settings = settings
        os.makedirs(settings.data_dir, exist_ok=True)
        self.user_client = TelegramClient(settings.user_session_path, settings.api_id, settings.api_hash)
        self.bot_client = TelegramClient(settings.bot_session_path, settings.api_id, settings.api_hash)
        self.db = DuplicateDB(settings.db_path, settings.cooldown_hours, settings.pending_ttl_minutes)
        self.state = ParserState()
        self.http_session: aiohttp.ClientSession | None = None
        self.curl_session = None
        self.browser_resolver = OzonBrowserResolver(settings)
        self.target_entity = None
        self.target_peer_id: int | None = None
        self.current_sources: set[int] = set()
        self.queue: asyncio.Queue[Job] = asyncio.Queue(maxsize=500)
        self.workers: list[asyncio.Task] = []
        self.retry_tasks: set[asyncio.Task] = set()
        self.resolve_semaphore = asyncio.Semaphore(8)
        self.publish_lock = asyncio.Lock()
        self.album_messages: dict[int, list] = {}
        self.album_tasks: dict[int, asyncio.Task] = {}
        self.album_lock = asyncio.Lock()
        self.synced_cookie_count: int = 0
        self.ozon_auth_active: bool = False
        self.ozon_auth_label: str = ""
        self._awaiting_ozon_cookies: bool = False
        self._rub_to_byn_rate: float = settings.rub_to_byn_fallback_rate
        self._rate_updated_at: float = 0.0
        self._rate_lock = asyncio.Lock()
        self._wb_minsk_dest: int = settings.wb_dest_byn
        self._wb_minsk_xinfo: str = ""
        self._wb_geo_updated_at: float = 0.0
        self._wb_geo_lock = asyncio.Lock()
        self._register_admin_handlers()

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
        """Переносит сессионные и авторизационные cookies Ozon (__Secure-access-token, __Secure-refresh-token,
        __Secure-user-id, abt_data, xcid) в curl_cffi и aiohttp для получения персональных цен аккаунта."""
        if not cookies:
            return
        unique_names: set[str] = set()
        for cookie in cookies:
            if not isinstance(cookie, dict):
                continue
            name = str(cookie.get("name") or "").strip()
            value = cookie.get("value")
            domain = str(cookie.get("domain") or "").lstrip(".")
            if not name or value is None:
                continue
            val_str = str(value)
            unique_names.add(name)
            if self.curl_session is not None:
                with suppress(Exception):
                    self.curl_session.cookies.set(name, val_str, domain=domain or "ozon.by")
                for oz_domain in OZON_DOMAINS:
                    for prefix in ("", ".", "www.", "api."):
                        with suppress(Exception):
                            self.curl_session.cookies.set(name, val_str, domain=f"{prefix}{oz_domain}")
            if self.http_session is not None:
                for oz_domain in OZON_DOMAINS:
                    for sub in (f"https://{oz_domain}/", f"https://www.{oz_domain}/", f"https://api.{oz_domain}/"):
                        with suppress(Exception):
                            self.http_session.cookie_jar.update_cookies(
                                {name: val_str},
                                response_url=aiohttp.client.URL(sub),
                            )
        if unique_names:
            self.synced_cookie_count = len(unique_names)
        is_auth, auth_label = extract_ozon_auth_info(cookies)
        if is_auth:
            self.ozon_auth_active = True
            self.ozon_auth_label = auth_label

    def _load_cached_cookies_from_disk(self) -> None:
        combined: list[dict[str, Any]] = []
        raw_env_cookies = os.getenv("OZON_COOKIES", "").strip()
        if raw_env_cookies:
            combined.extend(parse_ozon_cookies_input(raw_env_cookies))

        if os.path.isfile(self.settings.cookies_cache_path):
            with suppress(Exception):
                with open(self.settings.cookies_cache_path, "r", encoding="utf-8") as f:
                    raw_disk = f.read()
                parsed_disk = parse_ozon_cookies_input(raw_disk)
                if parsed_disk:
                    combined.extend(parsed_disk)

        if combined:
            self._sync_cookies_to_http_clients(combined)
            with suppress(OSError):
                with open(self.settings.cookies_cache_path, "w", encoding="utf-8") as f:
                    json.dump(combined, f, ensure_ascii=False)
            auth_status = f"авторизован ({self.ozon_auth_label})" if self.ozon_auth_active else "гостевой режим"
            log(f"Загружены cookies Ozon ({self.synced_cookie_count} ключей, статус: {auth_status})")

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

    async def _fetch_json_any(self, url: str, headers: dict[str, str] | None = None) -> Any:
        """Выполняет GET-запрос и возвращает распарсенный JSON (dict или list) через curl_cffi или aiohttp.

        Важно для Wildberries и Ozon: при использовании curl_cffi(impersonate="chrome") не перезаписываем
        его нативный User-Agent и Sec-CH-UA дефолтными заголовками, чтобы не ломать совпадение TLS-фингерпринта.
        """
        if self.curl_session is not None:
            curl_headers: dict[str, str] = {
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            }
            if headers:
                for k, v in headers.items():
                    if k.lower() not in {"user-agent", "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform"} or "ozonapp" in v:
                        curl_headers[k] = v

            for attempt in range(2):
                with suppress(Exception):
                    async with self.resolve_semaphore:
                        resp = await self.curl_session.get(
                            url,
                            headers=curl_headers,
                            allow_redirects=True,
                            timeout=self.settings.request_timeout_seconds,
                        )
                        if resp.status_code == 404:
                            return {"_http_status": 404}
                        if resp.status_code == 429 and attempt == 0:
                            await asyncio.sleep(1.2)
                            continue
                        if 200 <= resp.status_code < 300 and resp.content:
                            return json.loads(resp.content.decode("utf-8", errors="ignore"))
                break

        if self.http_session is not None:
            aio_headers = dict(headers or DEFAULT_HEADERS)
            aio_headers.setdefault("Accept", "application/json, text/plain, */*")
            with suppress(Exception):
                async with self.resolve_semaphore:
                    async with self.http_session.get(
                        url,
                        headers=aio_headers,
                        allow_redirects=True,
                    ) as resp:
                        if resp.status == 404:
                            return {"_http_status": 404}
                        if 200 <= resp.status < 300:
                            raw = await resp.content.read(HEAD_READ_LIMIT)
                            return json.loads(raw.decode("utf-8", errors="ignore"))
        return None

    async def _fetch_json_url(self, url: str, headers: dict[str, str] | None = None) -> dict[str, Any] | None:
        data = await self._fetch_json_any(url, headers=headers)
        return data if isinstance(data, dict) else None

    async def download_product_image(self, image_url: str) -> io.BytesIO | None:
        """Скачивает главное фото из карточки товара и подготавливает его как JPEG для отправки в Telegram."""
        clean_url = normalize_image_url(image_url)
        if not clean_url:
            return None

        img_headers = {
            "Accept": "image/jpeg,image/png,image/*;q=0.8,*/*;q=0.5",
            "Accept-Language": "ru-RU,ru;q=0.9",
        }

        if self.curl_session is not None:
            with suppress(Exception):
                async with self.resolve_semaphore:
                    resp = await self.curl_session.get(
                        clean_url,
                        headers=img_headers,
                        allow_redirects=True,
                        timeout=self.settings.request_timeout_seconds,
                    )
                    if 200 <= resp.status_code < 300 and resp.content:
                        converted = convert_image_bytes_to_jpeg(resp.content[:IMAGE_READ_LIMIT])
                        if converted is not None:
                            return converted

        if self.http_session is not None:
            with suppress(Exception):
                async with self.resolve_semaphore:
                    async with self.http_session.get(
                        clean_url,
                        headers={**DEFAULT_HEADERS, **img_headers},
                        allow_redirects=True,
                    ) as resp:
                        if 200 <= resp.status < 300:
                            raw = await resp.content.read(IMAGE_READ_LIMIT)
                            converted = convert_image_bytes_to_jpeg(raw)
                            if converted is not None:
                                return converted

        return None

    async def _discover_wb_basket_card(self, sku_int: int) -> tuple[str, dict[str, Any] | None]:
        """Находит рабочий сервер корзины Wildberries (basket-XX.wbbasket.ru) и загружает card.json.

        Если расчётный шард вернул 404 (например, для новых 9-10-значных артикулов),
        проверяет соседние и новые шарды корзин и кэширует найденный хост для vol.
        """
        vol = sku_int // 100_000
        part = sku_int // 1_000

        req_headers = {
            "Accept": "application/json",
            "Referer": f"https://www.wildberries.ru/catalog/{sku_int}/detail.aspx",
        }

        primary_host = get_wb_basket_host(vol)
        primary_url = f"https://{primary_host}/vol{vol}/part{part}/{sku_int}/info/ru/card.json"
        data = await self._fetch_json_url(primary_url, headers=req_headers)
        if isinstance(data, dict) and data.get("_http_status") != 404 and (data.get("imt_name") or data.get("subj_name")):
            WB_BASKET_CACHE[vol] = primary_host
            return primary_host, data

        # Если расчётный баскет не подошёл — формируем список кандидатов вокруг расчётного и среди новых шардов (26..48)
        guessed_num = get_wb_basket_shard_number(vol)
        candidate_nums: list[int] = []
        for delta in (-2, -1, 1, 2, 3, -3, 4, 5, 6):
            n = guessed_num + delta
            if 1 <= n <= 50 and n != guessed_num:
                candidate_nums.append(n)
        for n in range(28, 49):
            if n not in candidate_nums and n != guessed_num:
                candidate_nums.append(n)

        # Проверяем кандидатов небольшими пакетами по 5 штук
        for i in range(0, min(len(candidate_nums), 15), 5):
            batch = candidate_nums[i:i + 5]
            hosts = [f"basket-{n:02d}.wbbasket.ru" for n in batch]
            urls = [f"https://{h}/vol{vol}/part{part}/{sku_int}/info/ru/card.json" for h in hosts]
            results = await asyncio.gather(
                *(self._fetch_json_url(u, headers=req_headers) for u in urls),
                return_exceptions=True,
            )
            for h, res in zip(hosts, results):
                if isinstance(res, dict) and res.get("_http_status") != 404 and (res.get("imt_name") or res.get("subj_name")):
                    WB_BASKET_CACHE[vol] = h
                    return h, res

        return primary_host, None

    async def get_wb_minsk_geo_info(self) -> tuple[int, str]:
        """Получает актуальные гео-параметры Wildberries для Минска (Беларусь, BYN) через user-geo-data.wildberries.ru."""
        now = time.time()
        if self._wb_geo_updated_at > 0 and (now - self._wb_geo_updated_at) < 21600:
            return self._wb_minsk_dest, self._wb_minsk_xinfo

        async with self._wb_geo_lock:
            if self._wb_geo_updated_at > 0 and (time.time() - self._wb_geo_updated_at) < 21600:
                return self._wb_minsk_dest, self._wb_minsk_xinfo

            geo_urls = (
                "https://user-geo-data.wildberries.ru/get-geo-info"
                "?currency=BYN&latitude=53.902284&longitude=27.561831&locale=ru&address=%D0%9C%D0%B8%D0%BD%D1%81%D0%BA",
                "https://user-geo-data.wildberries.ru/get-geo-info"
                "?currency=BYN&latitude=53.902284&longitude=27.561831&locale=by&address=%D0%9C%D0%B8%D0%BD%D1%81%D0%BA",
            )
            geo_headers = {
                "Accept": "application/json, text/plain, */*",
                "Referer": "https://www.wildberries.by/",
            }
            for g_url in geo_urls:
                data = await self._fetch_json_url(g_url, headers=geo_headers)
                if not isinstance(data, dict):
                    continue
                xinfo = str(data.get("xinfo") or "").strip().lstrip("?")
                dest_val = data.get("dest")
                if dest_val is None and xinfo:
                    qs = parse_qs(xinfo)
                    if "dest" in qs and qs["dest"]:
                        dest_val = qs["dest"][0]
                with suppress(TypeError, ValueError):
                    if dest_val is not None:
                        first_dest = str(dest_val).split(",")[0].strip()
                        self._wb_minsk_dest = int(first_dest)
                        if xinfo:
                            xinfo = re.sub(r"(?:^|&)curr=[^&]*", "", xinfo).strip("&")
                            xinfo = f"curr=byn&{xinfo}" if xinfo else f"appType=1&curr=byn&dest={self._wb_minsk_dest}&spp={self.settings.wb_spp}"
                        self._wb_minsk_xinfo = xinfo
                        self._wb_geo_updated_at = time.time()
                        log(f"Обновлены гео-параметры WB для Минска (BYN): dest={self._wb_minsk_dest}, xinfo={self._wb_minsk_xinfo}")
                        return self._wb_minsk_dest, self._wb_minsk_xinfo

            self._wb_geo_updated_at = time.time()
            return self._wb_minsk_dest, self._wb_minsk_xinfo

    async def _fetch_wb_basket_card_extras(
        self,
        sku: str,
        *,
        rub_to_byn_rate: float = 0.0345,
    ) -> tuple[str, str, float | None, str]:
        """Получает с сервера корзины Wildberries:
        - полное название и бренд (info/ru/card.json)
        - последнюю цену из истории на случай недоступности живого API (info/price-history.json)
        - ссылку на главное фото карточки товара (images/big/1.webp)
        """
        try:
            sku_int = int(sku)
        except ValueError:
            return "", "", None, ""

        vol = sku_int // 100_000
        part = sku_int // 1_000
        basket_host, card_data = await self._discover_wb_basket_card(sku_int)
        base_path = f"https://{basket_host}/vol{vol}/part{part}/{sku_int}"
        history_json_url = f"{base_path}/info/price-history.json"
        image_url = f"{base_path}/images/big/1.webp"

        req_headers = {
            "Accept": "application/json",
            "Referer": f"https://www.wildberries.by/catalog/{sku_int}/detail.aspx",
        }

        history_data = await self._fetch_json_any(history_json_url, headers=req_headers)

        title = ""
        brand_name = ""
        if isinstance(card_data, dict):
            imt_name = str(card_data.get("imt_name") or card_data.get("subj_name") or "").strip()
            selling = card_data.get("selling")
            if isinstance(selling, dict):
                brand_name = str(selling.get("brand_name") or "").strip()
            if not brand_name:
                brand_name = str(card_data.get("brand_name") or "").strip()
            title = clean_product_title(imt_name, brand_name)

        latest_history_price_byn = parse_wb_latest_history_price(
            history_data,
            rub_to_byn_rate=rub_to_byn_rate,
        )

        return title, brand_name, latest_history_price_byn, image_url

    async def fetch_wb_card_data(self, sku: str, messages: Iterable = ()) -> ProductCardData | None:
        """Парсит карточку товара Wildberries для региона Минск (BYN) через многоуровневый каскад:
        1) Статический CDN корзин wbbasket.ru (полное название, бренд, главное фото)
        2) Поисковые шарды u-search.wb.ru / search.wb.ru и детальные API card.wb.ru / u-card.wb.ru
           с приоритетом региона Минск (dest=-59208 / user-geo-data) и валюты BYN
        3) Браузерный резолвер Playwright (чтение цены напрямую с витрины wildberries.by / wildberries.ru)
        4) Фолбэк на центральный склад (-1257786), превью Telegram и price-history.json
        """
        rub_rate = await self.get_rub_to_byn_rate()
        minsk_dest, minsk_xinfo = await self.get_wb_minsk_geo_info()
        wb_headers = {
            "Accept": "*/*",
            "Accept-Language": "ru-RU,ru;q=0.9",
            "Referer": f"https://www.wildberries.by/catalog/{sku}/detail.aspx",
        }

        # Шаг 1: Получаем метаданные с wbbasket.ru (точное название, бренд и главное фото)
        basket_title, basket_brand, latest_hist_byn, basket_img = await self._fetch_wb_basket_card_extras(
            sku,
            rub_to_byn_rate=rub_rate,
        )

        card_data: ProductCardData | None = None
        if basket_title or basket_img:
            card_data = ProductCardData(
                title=basket_title,
                brand=basket_brand,
                image_url=basket_img,
            )

        explicitly_out_of_stock = False
        card_hosts = ("card.wb.ru", "u-card.wb.ru")
        api_paths = ("cards/v4/detail", "cards/v2/detail")

        # Если получена строка xinfo от гео-сервиса WB для Минска — сначала делаем запрос с ней (1-в-1 как фронтенд WB)
        if minsk_xinfo:
            xinfo_urls = (
                f"https://card.wb.ru/cards/v4/detail?{minsk_xinfo}&nm={sku}",
                f"https://u-card.wb.ru/cards/v4/detail?{minsk_xinfo}&nm={sku}",
                f"https://card.wb.ru/cards/v2/detail?{minsk_xinfo}&nm={sku}",
                f"https://u-search.wb.ru/exactmatch/ru/common/v18/search?{minsk_xinfo}&inheritFilters=false&lang=ru&page=1&query={sku}&resultset=catalog&sort=popular&suppressSpellcheck=false",
            )
            for x_url in xinfo_urls:
                payload = await self._fetch_json_url(x_url, headers=wb_headers)
                if not payload or payload.get("_http_status") == 404:
                    continue
                parsed = parse_wb_detail_payload(
                    payload,
                    target_sku=sku,
                    currency="BYN",
                    rub_to_byn_rate=rub_rate,
                )
                if parsed is not None:
                    card_data = replace(merge_card_data(card_data, parsed), avg_month_price_byn=None)
                    if card_data and card_data.in_stock is True and card_data.price_byn is not None:
                        log(f"Карточка WB {sku} получена с гео-параметрами Минска (xinfo): price={card_data.price_byn} BYN")
                        return card_data

        # Сначала проверяем белорусские регионы доставки (Минск: -59208, 123585595),
        # чтобы получить точную белорусскую цену с учётом логистики до РБ (например, 5,68 BYN вместо 4,96 BYN).
        # Центральный склад РФ (-1257786) используем только как резервный фолбэк.
        belarus_dests = list(dict.fromkeys((minsk_dest, self.settings.wb_dest_byn, -59208, 123585595)))
        belarus_dests = [d for d in belarus_dests if d != -1257786] or [-59208]
        all_dest_groups = (
            ("byn", belarus_dests),
            ("byn", [-1257786]),
            ("rub", belarus_dests[:1] + [-1257786]),
        )

        for curr, dest_list in all_dest_groups:
            for dest in dest_list:
                # 2а) Детальные эндпоинты карточки card.wb.ru / u-card.wb.ru с региональным dest
                for api_path in api_paths:
                    for host in card_hosts:
                        detail_url = (
                            f"https://{host}/{api_path}"
                            f"?appType=1&curr={curr}&dest={dest}&spp={self.settings.wb_spp}&nm={sku}"
                        )
                        payload = await self._fetch_json_url(detail_url, headers=wb_headers)
                        if not payload or payload.get("_http_status") == 404:
                            continue
                        parsed = parse_wb_detail_payload(
                            payload,
                            target_sku=sku,
                            currency=curr.upper(),
                            rub_to_byn_rate=rub_rate,
                        )
                        if parsed is not None:
                            if parsed.in_stock is False and dest == -1257786:
                                explicitly_out_of_stock = True
                            card_data = replace(merge_card_data(card_data, parsed), avg_month_price_byn=None)
                            if card_data and card_data.in_stock is True and card_data.price_byn is not None:
                                log(
                                    f"Карточка WB {sku} получена через {host}/{api_path} ({curr.upper()}, dest={dest}): "
                                    f"price={card_data.price_byn} BYN"
                                )
                                return card_data

                # 2б) Поисковые шарды u-search.wb.ru / search.wb.ru с тем же региональным dest
                for shard in WB_SEARCH_SHARDS[:3]:
                    search_urls = (
                        f"https://u-search.wb.ru/exactmatch/ru/{shard}/v18/search"
                        f"?ab_testid=new_benefit_sort&appType=1&curr={curr}&dest={dest}"
                        f"&inheritFilters=false&lang=ru&page=1&query={sku}&resultset=catalog"
                        f"&sort=popular&spp={self.settings.wb_spp}&suppressSpellcheck=false",
                        f"https://search.wb.ru/exactmatch/ru/{shard}/v18/search"
                        f"?appType=1&curr={curr}&dest={dest}&lang=ru&page=1&query={sku}"
                        f"&resultset=catalog&sort=popular&spp={self.settings.wb_spp}",
                    )
                    for s_url in search_urls:
                        payload = await self._fetch_json_url(s_url, headers=wb_headers)
                        if not payload or payload.get("_http_status") == 404:
                            continue
                        parsed = parse_wb_detail_payload(
                            payload,
                            target_sku=sku,
                            currency=curr.upper(),
                            rub_to_byn_rate=rub_rate,
                        )
                        if parsed is not None:
                            if parsed.in_stock is False and dest == -1257786:
                                explicitly_out_of_stock = True
                            card_data = replace(merge_card_data(card_data, parsed), avg_month_price_byn=None)
                            if card_data and card_data.in_stock is True and card_data.price_byn is not None:
                                log(
                                    f"Карточка WB {sku} получена через search API ({shard}, {curr.upper()}, dest={dest}): "
                                    f"price={card_data.price_byn} BYN"
                                )
                                return card_data

        # Шаг 3: Если HTTP API не отдали цену, открываем карточку WB в браузере Playwright Chromium
        if self.browser_resolver.is_available and (card_data is None or card_data.price_byn is None):
            browser_wb_card = await self.browser_resolver.fetch_wb_product_card(
                sku,
                rub_to_byn_rate=rub_rate,
            )
            if browser_wb_card is not None:
                if browser_wb_card.in_stock is False:
                    return replace(
                        merge_card_data(card_data, browser_wb_card) or browser_wb_card,
                        in_stock=False,
                        price_byn=None,
                        avg_month_price_byn=None,
                    )
                card_data = replace(merge_card_data(card_data, browser_wb_card), avg_month_price_byn=None)
                if card_data and card_data.price_byn is not None:
                    return card_data

        # Шаг 4: Проверяем Telegram WebPage-превью
        if card_data is None or card_data.price_byn is None:
            wp_obj = match_message_webpage_obj(messages, "", sku, 1)
            if wp_obj:
                wp_card = parse_webpage_preview_card(wp_obj, default_currency="BYN", rub_to_byn_rate=rub_rate)
                if wp_card is not None:
                    if wp_card.in_stock is False:
                        explicitly_out_of_stock = True
                    card_data = replace(merge_card_data(card_data, wp_card), avg_month_price_byn=None)

        # Шаг 5: Резервный фолбэк на последнюю цену из price-history.json (без средней цены за месяц)
        if (
            not explicitly_out_of_stock
            and (card_data is None or card_data.price_byn is None)
            and latest_hist_byn is not None
            and latest_hist_byn > 0
            and basket_title
        ):
            log(
                f"Для WB {sku} использована последняя цена из price-history.json: {latest_hist_byn} BYN"
            )
            fallback_hist_card = ProductCardData(
                title=basket_title,
                brand=basket_brand,
                price_byn=latest_hist_byn,
                image_url=basket_img,
                in_stock=True,
            )
            card_data = replace(merge_card_data(card_data, fallback_hist_card), avg_month_price_byn=None)

        if explicitly_out_of_stock and (card_data is None or card_data.price_byn is None):
            return ProductCardData(
                title=basket_title,
                brand=basket_brand,
                image_url=basket_img,
                in_stock=False,
            )

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

        storefronts = ["ozon.by"]
        for d in self.settings.ozon_preferred_storefronts:
            if d not in storefronts:
                storefronts.append(d)

        # Если привязан аккаунт Ozon и доступен браузерный резолвер — сначала запрашиваем карточку
        # через авторизованный контекст браузера (он автоматически продлевает __Secure-access-token
        # через __Secure-refresh-token и отдаёт персональную цену аккаунта со скидкой Ozon).
        if self.ozon_auth_active and self.browser_resolver.is_available:
            browser_card, fresh_cookies = await self.browser_resolver.fetch_product_card(
                sku,
                rub_to_byn_rate=rub_rate,
            )
            if fresh_cookies:
                self._sync_cookies_to_http_clients(fresh_cookies)
            if browser_card is not None:
                if browser_card.in_stock is False and browser_card.price_byn is None:
                    return browser_card
                card_data = merge_card_data(card_data, browser_card)
                if (
                    card_data.title
                    and card_data.price_byn is not None
                    and card_data.image_url
                    and card_data.avg_month_price_byn is not None
                ):
                    return card_data

        header_variants = (
            (OZON_WEB_HEADERS, OZON_APP_HEADERS)
            if self.ozon_auth_active
            else (OZON_APP_HEADERS, OZON_WEB_HEADERS)
        )

        for host in storefronts:
            default_curr = "BYN" if host == "ozon.by" else "RUB"
            endpoints = (
                f"https://www.{host}/api/entrypoint-api.bx/page/json/v2?url={encoded_path}",
                f"https://api.{host}/composer-api.bx/page/json/v2?url={encoded_path}",
                f"https://www.{host}/api/composer-api.bx/page/json/v2?url={encoded_path}",
            )
            for req_headers in header_variants:
                for ep_url in endpoints:
                    payload = await self._fetch_json_url(ep_url, headers=req_headers)
                    if not payload:
                        continue
                    if payload.get("_http_status") == 404:
                        return ProductCardData(in_stock=False)
                    parsed = parse_ozon_composer_payload(
                        payload,
                        default_currency=default_curr,
                        rub_to_byn_rate=rub_rate,
                    )
                    if parsed is not None:
                        if parsed.avg_month_price_byn is None and parsed.price_byn is not None:
                            for modal_ep in extract_ozon_modal_links(payload)[:2]:
                                modal_url = f"https://www.{host}{modal_ep}" if modal_ep.startswith("/") else modal_ep
                                modal_payload = await self._fetch_json_url(modal_url, headers=req_headers)
                                if modal_payload:
                                    modal_avg = extract_ozon_avg_price_from_state(
                                        modal_payload,
                                        actual_price_byn=parsed.price_byn,
                                        default_currency=default_curr,
                                        rub_to_byn_rate=rub_rate,
                                    ) or extract_avg_month_price_from_text(
                                        json.dumps(modal_payload, ensure_ascii=False),
                                        actual_price_byn=parsed.price_byn,
                                        default_currency=default_curr,
                                        rub_to_byn_rate=rub_rate,
                                    )
                                    if modal_avg is not None:
                                        parsed = replace(parsed, avg_month_price_byn=modal_avg)
                                        break
                        card_data = merge_card_data(card_data, parsed)
                        if card_data.in_stock is False and card_data.price_byn is None:
                            return card_data
                        if (
                            card_data.title
                            and card_data.price_byn is not None
                            and card_data.image_url
                            and card_data.avg_month_price_byn is not None
                        ):
                            return card_data

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
                if browser_card.in_stock is False and browser_card.price_byn is None:
                    return browser_card
                card_data = merge_card_data(card_data, browser_card)
                if card_data.title and card_data.price_byn is not None and card_data.image_url:
                    return card_data

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

        if self.user_client.is_connected() and "GetWebPageRequest" in globals() and (
            card_data is None or not card_data.title or card_data.price_byn is None
        ):
            tg_card_urls: list[tuple[str, str]] = [
                (f"https://ozon.by/product/{sku}/", "BYN"),
                (f"https://www.ozon.ru/product/{sku}/", "RUB"),
            ]
            if original_url and is_ozon_host(urlparse(original_url).hostname):
                orig_curr = "BYN" if "ozon.by" in original_url.lower() else "RUB"
                tg_card_urls.insert(0, (original_url, orig_curr))

            for target_url, default_curr in tg_card_urls:
                for wait_attempt in range(2):
                    with suppress(Exception):
                        res = await self.user_client(GetWebPageRequest(url=target_url, hash=0))
                        webpage = getattr(res, "webpage", res)
                        wp_type = type(webpage).__name__
                        wp_card = parse_webpage_preview_card(
                            webpage,
                            default_currency=default_curr,
                            rub_to_byn_rate=rub_rate,
                        )
                        if wp_card is not None:
                            card_data = merge_card_data(card_data, wp_card)
                            if card_data.in_stock is False or (card_data.title and card_data.price_byn is not None):
                                return card_data
                        if "Pending" in wp_type and wait_attempt == 0:
                            await asyncio.sleep(1.5)
                            continue
                    break

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
    ) -> ProductInfo | None:
        """Загружает карточку товара, проверяет наличие и обогащает ProductInfo названием, ценой в BYN, средней ценой и фото.

        Возвращает None, если товара нет в наличии или карточка недоступна.
        """
        if not product.sku:
            return None if self.settings.require_sku else product

        if product.platform == "Wildberries":
            card = await self.fetch_wb_card_data(product.sku, messages=messages)
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
            image_url=card.image_url or product.image_url,
            rating=card.rating,
            reviews_count=card.reviews_count,
        )

    async def _resolve_via_curl_cffi(self, clean_url: str) -> str | None:
        """Пошагово раскрывает ссылку через curl_cffi (сначала с нативным TLS Chrome, затем с мобильным User-Agent Ozon)."""
        if self.curl_session is None:
            return None

        parsed = urlparse(clean_url)
        orig_domain = resolve_ozon_canonical_host(parsed.hostname)
        storefronts: list[str] = [orig_domain]
        for domain in self.settings.ozon_preferred_storefronts:
            if domain not in storefronts:
                storefronts.append(domain)

        header_profiles: list[dict[str, str]] = [
            {"Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7"},
            {
                "User-Agent": OZON_APP_HEADERS["User-Agent"],
                "x-o3-app-name": OZON_APP_HEADERS["x-o3-app-name"],
                "x-o3-app-version": OZON_APP_HEADERS["x-o3-app-version"],
                "x-o3-device-type": "mobile",
                "Accept-Language": "ru-RU,ru;q=0.9",
            },
            {
                "User-Agent": "TelegramBot (like TwitterBot)",
                "Accept-Language": "ru-RU,ru;q=0.9",
            },
        ]

        for base_headers in header_profiles:
            for domain in storefronts:
                www_order = (True, False) if domain == "ozon.ru" else (False, True)
                for use_www in www_order:
                    current_url = (
                        build_storefront_url(clean_url, domain, use_www=use_www)
                        if is_ozon_host(parsed.hostname)
                        else clean_url
                    )
                    referer: str | None = None

                    for _step in range(1, self.settings.max_redirects + 2):
                        req_headers = dict(base_headers)
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

                            extracted_loc = extract_product_url_from_text(next_url, current_url)
                            if extracted_loc and has_product_sku(extracted_loc):
                                log(f"Ссылка раскрыта из Location curl_cffi ({domain}): {clean_url} -> {extracted_loc}")
                                return extracted_loc

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
        """Просит серверы Telegram получить превью короткой ссылки (Telegram сам переходит по редиректу Ozon со своих IP)."""
        if not self.user_client.is_connected():
            return None

        parsed = urlparse(clean_url)
        orig_domain = resolve_ozon_canonical_host(parsed.hostname)
        candidate_urls: list[str] = [clean_url]
        if is_ozon_host(parsed.hostname):
            for dom in (orig_domain, *self.settings.ozon_preferred_storefronts):
                for use_www in (False, True):
                    mirror = build_storefront_url(clean_url, dom, use_www=use_www)
                    if mirror not in candidate_urls:
                        candidate_urls.append(mirror)

        if "GetWebPageRequest" in globals():
            for target_url in candidate_urls[:4]:
                for wait_attempt in range(3):
                    try:
                        res = await self.user_client(GetWebPageRequest(url=target_url, hash=0))
                        webpage = getattr(res, "webpage", res)
                        wp_type = type(webpage).__name__
                        extracted = extract_url_from_webpage_obj(webpage, clean_url)
                        if extracted and has_product_sku(extracted):
                            log(f"Ссылка раскрыта через Telegram GetWebPageRequest ({target_url}): {clean_url} -> {extracted}")
                            return extracted
                        if "Pending" in wp_type and wait_attempt < 2:
                            await asyncio.sleep(1.5)
                            continue
                        break
                    except Exception as exc:
                        log(f"GetWebPageRequest ошибка для {target_url}: {type(exc).__name__}: {exc}", logging.DEBUG)
                        break

        if "GetWebPagePreviewRequest" in globals():
            for target_url in candidate_urls[:3]:
                for wait_attempt in range(2):
                    with suppress(Exception):
                        media = await self.user_client(GetWebPagePreviewRequest(message=target_url))
                        webpage = getattr(media, "webpage", media)
                        wp_type = type(webpage).__name__
                        extracted = extract_url_from_webpage_obj(webpage, clean_url)
                        if extracted and has_product_sku(extracted):
                            log(f"Ссылка раскрыта через Telegram GetWebPagePreviewRequest ({target_url}): {clean_url} -> {extracted}")
                            return extracted
                        if "Pending" in wp_type and wait_attempt == 0:
                            await asyncio.sleep(1.5)
                            continue
                    break

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
            # 1. Сначала пробуем раскрыть короткую ссылку через серверы Telegram (GetWebPageRequest / Preview),
            # так как серверы Telegram не блокируются антиботом Ozon по IP домашнего/VPS хоста
            via_tg = await self._resolve_via_telegram_webpage(clean_initial_url)
            if via_tg and has_product_sku(via_tg):
                self.state.http_resolved += 1
                return via_tg

            # 2. Пробуем через curl_cffi (Chrome TLS / Mobile App UA)
            if self.curl_session is not None:
                via_curl = await self._resolve_via_curl_cffi(clean_initial_url)
                if via_curl and has_product_sku(via_curl):
                    self.state.http_resolved += 1
                    return via_curl

            # 3. Пробуем через мобильное API Ozon (composer-api / entrypoint-api)
            via_api = await self._resolve_via_ozon_mobile_api(clean_initial_url)
            if via_api and has_product_sku(via_api):
                self.state.http_resolved += 1
                return via_api

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

    async def fetch_folder_channels(self) -> tuple[list[int], list[dict]]:
        target_name = self.settings.folder_name.casefold()
        response = await self.user_client(GetDialogFiltersRequest())
        filters = getattr(response, "filters", response)
        matched = None
        available: list[str] = []

        for dialog_filter in filters:
            title = getattr(dialog_filter, "title", None)
            if not title:
                continue
            title_text = title.text if hasattr(title, "text") else str(title)
            title_text = title_text.strip().strip("\"'")
            available.append(title_text)
            if title_text.casefold() == target_name:
                matched = dialog_filter
                break

        if matched is None:
            log(
                f"Папка {self.settings.folder_name!r} не найдена. Доступные папки: {available}",
                logging.WARNING,
            )
            return [], []

        include_peers = list(getattr(matched, "include_peers", []))
        pinned_peers = list(getattr(matched, "pinned_peers", []))
        log(
            f"Папка «{self.settings.folder_name}»: "
            f"include_peers={len(include_peers)}, pinned_peers={len(pinned_peers)}"
        )

        seen: set[int] = set()
        source_ids: list[int] = []
        channels: list[dict] = []

        for peer in include_peers:
            peer_id = get_peer_id(peer)
            if peer_id in seen:
                continue
            seen.add(peer_id)

            if peer_id == self.target_peer_id:
                log(f"Целевой канал {peer_id} исключён из источников", logging.WARNING)
                continue

            try:
                entity = await self.user_client.get_entity(peer)
                title = (
                    getattr(entity, "title", None)
                    or getattr(entity, "first_name", None)
                    or f"Чат {peer_id}"
                )
            except Exception as exc:
                log(
                    f"Источник {peer_id} пропущен: нет доступа или канал удалён. "
                    f"{type(exc).__name__}: {exc}",
                    logging.WARNING,
                )
                continue

            source_ids.append(peer_id)
            channels.append({"id": peer_id, "title": title})

        return source_ids, channels

    async def update_sources(self) -> list[dict]:
        source_ids, channels = await self.fetch_folder_channels()
        self.current_sources = set(source_ids)
        with suppress(Exception):
            self.user_client.remove_event_handler(self.incoming_post_handler)
        if self.current_sources:
            self.user_client.add_event_handler(
                self.incoming_post_handler,
                events.NewMessage(chats=list(self.current_sources)),
            )
        log(f"Источники обновлены: {len(self.current_sources)}")
        for channel in channels:
            log(f"Источник: [{channel['id']}] {channel['title']}")
        if not self.current_sources:
            log("Нет доступных источников: проверьте папку и её явно добавленные чаты", logging.WARNING)
        return channels

    async def incoming_post_handler(self, event) -> None:
        if self.state.is_paused:
            return
        self.state.incoming += 1
        message = event.message
        if message.grouped_id:
            await self.collect_album(message, event.chat_id)
            return
        await self.enqueue(Job(messages=[message], source_chat_id=event.chat_id))

    async def collect_album(self, message, source_chat_id: int) -> None:
        group_id = message.grouped_id
        async with self.album_lock:
            self.album_messages.setdefault(group_id, []).append(message)
            if group_id not in self.album_tasks:
                self.album_tasks[group_id] = asyncio.create_task(self.flush_album(group_id, source_chat_id))

    async def flush_album(self, group_id: int, source_chat_id: int) -> None:
        await asyncio.sleep(1.5)
        async with self.album_lock:
            messages = self.album_messages.pop(group_id, [])
            self.album_tasks.pop(group_id, None)
        if messages:
            await self.enqueue(Job(messages=messages, source_chat_id=source_chat_id))

    async def enqueue(self, job: Job) -> None:
        try:
            self.queue.put_nowait(job)
        except asyncio.QueueFull:
            self.state.queue_dropped += 1
            self.state.errors += 1
            log(f"Очередь переполнена; сообщение из {job.source_chat_id} пропущено", logging.ERROR)
            await self.notify_admin("⚠️ Очередь парсера переполнена. Одно сообщение пропущено.")

    async def requeue_later(self, job: Job, delay: int) -> None:
        await asyncio.sleep(delay)
        await self.enqueue(job)

    def schedule_retry(self, job: Job, delay: int) -> None:
        task = asyncio.create_task(self.requeue_later(job, delay))
        self.retry_tasks.add(task)
        task.add_done_callback(self.retry_tasks.discard)

    async def process_job(self, job: Job) -> None:
        source_text = next(
            (message.raw_text for message in job.messages if message.raw_text),
            "",
        )

        raw_links: list[str] = []
        for message in job.messages:
            raw_links.extend(extract_links(message))

        candidate_links = list(dict.fromkeys(url for url in raw_links if validate_marketplace_url(url)))
        if not candidate_links:
            self.state.rejected += 1
            return

        if len(candidate_links) > self.settings.max_links_per_post:
            log(
                f"В посте {job.source_chat_id} ссылок {len(candidate_links)}; "
                f"обрабатываются первые {self.settings.max_links_per_post}",
                logging.WARNING,
            )
            candidate_links = candidate_links[:self.settings.max_links_per_post]

        async def _resolve_candidate(url: str) -> str | None:
            if has_product_sku(url):
                return url
            preview_url = match_message_preview_url(job.messages, url, len(candidate_links))
            if preview_url and has_product_sku(preview_url):
                log(f"Ссылка раскрыта мгновенно из превью сообщения Telegram: {url} -> {preview_url}")
                self.state.http_resolved += 1
                return preview_url
            return await self.resolve_url(url)

        results = await asyncio.gather(
            *(_resolve_candidate(url) for url in candidate_links),
            return_exceptions=True,
        )

        text_skus = extract_skus_from_text(source_text)
        products: list[ProductInfo] = []

        for idx, (original_url, result) in enumerate(zip(candidate_links, results)):
            resolved_url: str | None = None
            if isinstance(result, Exception):
                log(f"Ошибка раскрытия {original_url}: {result}", logging.WARNING)
            else:
                resolved_url = result

            if not resolved_url or not has_product_sku(resolved_url):
                preview_url = match_message_preview_url(job.messages, original_url, len(candidate_links))
                if preview_url:
                    log(f"Используем URL из Telegram-превью для {original_url}: {preview_url}")
                    resolved_url = preview_url

            if (not resolved_url or not has_product_sku(resolved_url)) and text_skus:
                matched_sku = None
                if len(candidate_links) == 1 and len(text_skus) == 1:
                    matched_sku = text_skus[0]
                elif len(text_skus) == len(candidate_links):
                    matched_sku = text_skus[idx]

                if matched_sku:
                    host = normalize_host(urlparse(original_url).hostname)
                    if is_ozon_host(host):
                        canonical_host = resolve_ozon_canonical_host(host, self.settings.ozon_canonical_host)
                        resolved_url = f"https://{canonical_host}/product/{matched_sku}/"
                    else:
                        resolved_url = f"https://www.wildberries.by/catalog/{matched_sku}/detail.aspx"
                    log(f"Артикул {matched_sku} взят из текста поста для {original_url}")

            if resolved_url is None:
                continue

            product = parse_product_info(
                original_url,
                resolved_url,
                canonical_host_override=self.settings.ozon_canonical_host,
            )
            if self.settings.require_sku and not product.sku:
                log(
                    f"Ссылка отклонена (не удалось определить артикул товара): "
                    f"{original_url} -> {resolved_url}",
                    logging.WARNING,
                )
                continue

            if validate_marketplace_url(product.canonical_url):
                products.append(product)

        if not products:
            self.state.rejected += 1
            return

        unique_products: dict[str, ProductInfo] = {}
        for product in products:
            unique_products.setdefault(product.sku_key, product)
        products = list(unique_products.values())

        # Парсим карточки товаров (название, актуальная цена в BYN, средняя цена за месяц, фото, наличие)
        enriched_results = await asyncio.gather(
            *(self.enrich_product_with_card(product, messages=job.messages) for product in products),
            return_exceptions=True,
        )

        available_products: list[ProductInfo] = []
        for orig_prod, enriched in zip(products, enriched_results):
            if isinstance(enriched, Exception):
                log(
                    f"Ошибка парсинга карточки {orig_prod.platform} ({orig_prod.sku}): {enriched}",
                    logging.WARNING,
                )
                continue
            if enriched is None:
                self.state.out_of_stock += 1
                continue
            available_products.append(enriched)

        if not available_products:
            log(
                f"В сообщении {job.source_chat_id} нет доступных товаров в наличии — публикация пропущена"
            )
            return

        source_message_id = min(message.id for message in job.messages)
        fresh_keys = self.db.reserve_fresh(available_products, job.source_chat_id, source_message_id)
        if not fresh_keys:
            self.state.duplicates += 1
            log(f"Все товары из сообщения {job.source_chat_id}/{source_message_id} уже обработаны")
            return

        fresh_products = [product for product in available_products if product.sku_key in fresh_keys]
        text = build_publication_text(fresh_products)

        if self.settings.dry_run:
            log(f"DRY_RUN — публикация НЕ выполнена {job.source_chat_id}/{source_message_id}:\n{text}")
            self.db.release_reservations(fresh_keys)
            return

        try:
            await self.publish(text, job.messages, fresh_products)
        except FloodWaitError as exc:
            self.db.release_reservations(fresh_keys)
            self.state.errors += 1
            log(f"FloodWait на {exc.seconds} сек. Товары освобождены", logging.ERROR)
            if job.attempts < MAX_FLOOD_RETRIES:
                job.attempts += 1
                self.schedule_retry(job, exc.seconds + 1)
                await self.notify_admin(
                    f"⚠️ Telegram FloodWait: {exc.seconds} сек. "
                    f"Повтор {job.attempts}/{MAX_FLOOD_RETRIES}."
                )
            else:
                await self.notify_admin(
                    f"⚠️ FloodWait: пост {job.source_chat_id}/{source_message_id} "
                    f"пропущен после {MAX_FLOOD_RETRIES} попыток."
                )
        except Exception as exc:
            self.db.release_reservations(fresh_keys)
            self.state.errors += 1
            log(f"Ошибка публикации: {type(exc).__name__}: {exc}", logging.ERROR)
            await self.notify_admin(f"⚠️ Ошибка публикации:\n{type(exc).__name__}: {str(exc)[:300]}")
        else:
            self.db.mark_published(fresh_keys)
            self.state.published += 1
            log(f"Опубликован пост {job.source_chat_id}/{source_message_id}; SKU: {sorted(fresh_keys)}")

    async def publish(
        self,
        text: str,
        messages: list,
        products: list[ProductInfo] | None = None,
    ) -> None:
        """Публикует пост в целевой канал, прикрепляя чистое фото из карточки товара (при наличии)."""
        card_images: list[io.BytesIO] = []
        if self.settings.prefer_card_image and products:
            img_urls = [p.image_url for p in products if p.image_url]
            if img_urls:
                downloaded = await asyncio.gather(
                    *(self.download_product_image(url) for url in img_urls),
                    return_exceptions=True,
                )
                for item in downloaded:
                    if isinstance(item, io.BytesIO):
                        card_images.append(item)

        fallback_media = [
            message.media
            for message in messages
            if message.media and not isinstance(message.media, MessageMediaWebPage)
        ]

        media_to_send = card_images if card_images else fallback_media

        if not text and not media_to_send:
            raise RuntimeError("Не сформирован текст и отсутствуют медиа для публикации")

        async with self.publish_lock:
            if not media_to_send:
                for text_part in split_text(text, limit=4000):
                    await self.user_client.send_message(
                        self.target_entity,
                        text_part,
                        link_preview=False,
                    )
                return

            text_parts = split_text(text, limit=1024)
            caption = text_parts.pop(0) if text_parts else None

            await self.user_client.send_file(
                self.target_entity,
                media_to_send if len(media_to_send) > 1 else media_to_send[0],
                caption=caption,
                parse_mode=None,
            )

            for text_part in text_parts:
                await self.user_client.send_message(
                    self.target_entity,
                    text_part,
                    link_preview=False,
                )

    async def process_worker(self, worker_number: int) -> None:
        log(f"Worker {worker_number} запущен")
        while True:
            job = await self.queue.get()
            try:
                if not self.state.is_paused:
                    await self.process_job(job)
            except Exception as exc:
                self.state.errors += 1
                log(f"Необработанная ошибка worker {worker_number}: {type(exc).__name__}: {exc}", logging.ERROR)
                await self.notify_admin(f"⚠️ Критическая ошибка worker: {type(exc).__name__}: {str(exc)[:250]}")
            finally:
                self.queue.task_done()

    def get_stats_text(self) -> str:
        uptime = int(time.time() - self.state.start_time)
        hours, remainder = divmod(uptime, 3600)
        minutes, seconds = divmod(remainder, 60)
        published_db, pending_db = self.db.count_by_status()
        status = "⏸ НА ПАУЗЕ" if self.state.is_paused else "🟢 АКТИВЕН"
        mode = "\nРежим: DRY_RUN" if self.settings.dry_run else ""
        browser_state = (
            "🟢 запущен"
            if self.browser_resolver.is_running
            else ("🟡 готов (по требованию)" if self.browser_resolver.is_available else "🔴 недоступен")
        )
        ozon_account_str = (
            f"🟢 привязан ({self.ozon_auth_label})"
            if self.ozon_auth_active
            else "⚪️ гостевой режим (без аккаунта)"
        )
        return (
            "🤖 Панель управления парсером\n\n"
            f"Статус: {status}{mode}\n"
            f"Каналов в папке: {len(self.current_sources)}\n"
            f"Очередь: {self.queue.qsize()}/{self.queue.maxsize}\n"
            f"Аптайм: {hours}ч {minutes}м {seconds}с\n\n"
            f"🛡 Антибот и аккаунт Ozon:\n"
            f"• Аккаунт Ozon: {ozon_account_str}\n"
            f"• Браузер (Playwright): {browser_state}\n"
            f"• Cookies в сессии: {self.synced_cookie_count}\n"
            f"• Раскрыто через HTTP/curl: {self.state.http_resolved}\n"
            f"• Раскрыто через браузер: {self.state.browser_resolved}\n"
            f"• Спарсено карточек: {self.state.cards_parsed}\n\n"
            f"Получено сообщений: {self.state.incoming}\n"
            f"Опубликовано постов: {self.state.published}\n"
            f"Пропущено (нет в наличии): {self.state.out_of_stock}\n"
            f"Отсеяно дублей: {self.state.duplicates}\n"
            f"Отклонено: {self.state.rejected}\n"
            f"Ошибок: {self.state.errors}\n"
            f"Переполнений очереди: {self.state.queue_dropped}\n\n"
            f"SKU в БД — опубликовано: {published_db}, pending: {pending_db}"
        )

    def get_admin_keyboard(self):
        pause_button = (
            Button.inline("▶️ Возобновить", b"resume")
            if self.state.is_paused
            else Button.inline("⏸ Пауза", b"pause")
        )
        ozon_btn_label = "🍪 Аккаунт Ozon (привязан)" if self.ozon_auth_active else "🍪 Привязать аккаунт Ozon"
        return [
            [Button.inline("📊 Обновить статус", b"refresh"), pause_button],
            [Button.inline(ozon_btn_label, b"ozon_auth_menu")],
            [Button.inline("🔄 Обновить источники", b"reload_sources")],
            [Button.inline("🧹 Очистить дедупликацию", b"clear_db_confirm")],
        ]

    @staticmethod
    def format_channels_message(channels: list[dict], title: str) -> str:
        if not channels:
            return "⚠️ Нет доступных источников в настроенной папке."
        rows = [f"{index}. {channel['title']} ({channel['id']})" for index, channel in enumerate(channels, 1)]
        return f"📂 {title}\nВсего: {len(channels)}\n\n" + "\n".join(rows)

    async def send_long_admin_message(self, text: str) -> None:
        for part in split_text(text):
            await self.bot_client.send_message(self.settings.admin_id, part, link_preview=False)

    async def notify_admin(self, text: str) -> None:
        with suppress(Exception):
            await self.bot_client.send_message(self.settings.admin_id, text, link_preview=False)

    def _register_admin_handlers(self) -> None:
        @self.bot_client.on(events.NewMessage(chats=self.settings.admin_id, pattern=r"^/start$"))
        async def admin_start(event):
            if event.sender_id != self.settings.admin_id:
                return
            self._awaiting_ozon_cookies = False
            await event.respond(self.get_stats_text(), buttons=self.get_admin_keyboard())

        @self.bot_client.on(events.NewMessage(chats=self.settings.admin_id))
        async def admin_cookies_input_handler(event):
            if event.sender_id != self.settings.admin_id:
                return
            raw_text = (event.raw_text or "").strip()
            if raw_text == "/start":
                return

            is_cookie_cmd = raw_text.startswith("/ozon_cookies")
            if not self._awaiting_ozon_cookies and not is_cookie_cmd:
                return

            payload_text = raw_text[len("/ozon_cookies"):].strip() if is_cookie_cmd else raw_text

            if event.message.file and not payload_text:
                with suppress(Exception):
                    file_bytes = await event.message.download_media(bytes)
                    if file_bytes:
                        payload_text = file_bytes.decode("utf-8", errors="ignore").strip()

            if not payload_text:
                self._awaiting_ozon_cookies = True
                await event.respond(
                    "🍪 Отправьте cookies от вашего аккаунта Ozon текстом или файлом (.json / .txt).\n\n"
                    "Поддерживаются форматы:\n"
                    "1) JSON-экспорт из расширения Cookie-Editor / EditThisCookie\n"
                    "2) Строка заголовка Cookie из DevTools (key1=val1; key2=val2)\n"
                    "3) Файл cookies.txt (Netscape)",
                    buttons=[[Button.inline("↩️ Отмена", b"refresh")]],
                )
                return

            parsed_cookies = parse_ozon_cookies_input(payload_text)
            if not parsed_cookies:
                await event.respond(
                    "❌ Не удалось распознать cookies в сообщении. Проверьте формат (JSON или key=value; ...) и попробуйте ещё раз.",
                    buttons=[[Button.inline("↩️ Назад", b"refresh")]],
                )
                return

            self._awaiting_ozon_cookies = False
            merged = await self.browser_resolver.import_cookies(parsed_cookies)
            self._sync_cookies_to_http_clients(merged)
            is_auth, auth_label = extract_ozon_auth_info(merged)
            status_line = (
                f"🟢 Авторизационные токены найдены ({auth_label}) — парсер будет получать персональные цены аккаунта!"
                if is_auth
                else "⚠️ Cookies сохранены, но токен авторизации (__Secure-access-token / __Secure-refresh-token) не обнаружен. Убедитесь, что вы вошли в аккаунт перед экспортом."
            )
            await event.respond(
                f"✅ Cookies Ozon успешно загружены ({self.synced_cookie_count} ключей)!\n{status_line}",
                buttons=self.get_admin_keyboard(),
            )

        @self.bot_client.on(events.CallbackQuery(chats=self.settings.admin_id))
        async def admin_callback(event):
            if event.sender_id != self.settings.admin_id:
                await event.answer("Недостаточно прав", alert=True)
                return
            data = event.data
            if data == b"refresh":
                self._awaiting_ozon_cookies = False
                await event.edit(self.get_stats_text(), buttons=self.get_admin_keyboard())
                await event.answer("Данные обновлены")
            elif data == b"pause":
                self.state.is_paused = True
                await event.edit(self.get_stats_text(), buttons=self.get_admin_keyboard())
                await event.answer("Пауза включена")
            elif data == b"resume":
                self.state.is_paused = False
                await event.edit(self.get_stats_text(), buttons=self.get_admin_keyboard())
                await event.answer("Парсинг возобновлён")
            elif data == b"ozon_auth_menu":
                self._awaiting_ozon_cookies = True
                cur_status = (
                    f"🟢 Привязан ({self.ozon_auth_label})"
                    if self.ozon_auth_active
                    else "⚪️ Не привязан (гостевой режим)"
                )
                await event.edit(
                    f"🍪 Привязка аккаунта Ozon для персональных скидок\n"
                    f"Текущий статус: {cur_status}\n\n"
                    f"Как привязать отдельный аккаунт Ozon:\n"
                    f"1. Откройте ozon.by в браузере и войдите в нужный аккаунт.\n"
                    f"2. Экспортируйте cookies через расширение Cookie-Editor (Export -> JSON) "
                    f"или скопируйте заголовок Cookie из DevTools (F12 -> Network).\n"
                    f"3. Просто отправьте скопированный текст или .json/.txt файл следующим сообщением сюда в бот!\n\n"
                    f"Альтернативно можно прописать OZON_COOKIES в .env или запустить на ПК: python parser.py --login-ozon",
                    buttons=[
                        [Button.inline("🗑 Отвязать / очистить cookies", b"ozon_auth_clear")],
                        [Button.inline("↩️ Назад", b"refresh")],
                    ],
                )
                await event.answer("Ожидание cookies Ozon")
            elif data == b"ozon_auth_clear":
                self._awaiting_ozon_cookies = False
                await self.browser_resolver.clear_cookies()
                self.ozon_auth_active = False
                self.ozon_auth_label = ""
                self.synced_cookie_count = 0
                if self.curl_session is not None:
                    with suppress(Exception):
                        self.curl_session.cookies.clear()
                if self.http_session is not None:
                    with suppress(Exception):
                        self.http_session.cookie_jar.clear()
                await event.edit(self.get_stats_text(), buttons=self.get_admin_keyboard())
                await event.answer("Cookies аккаунта Ozon удалены")
            elif data == b"reload_sources":
                channels = await self.update_sources()
                await event.edit(self.get_stats_text(), buttons=self.get_admin_keyboard())
                await event.answer("Источники обновлены")
                await self.send_long_admin_message(self.format_channels_message(channels, "Источники"))
            elif data == b"clear_db_confirm":
                await event.edit(
                    "⚠️ Удалить всю историю дедупликации? "
                    "После этого старые товары могут быть опубликованы повторно.",
                    buttons=[
                        [Button.inline("✅ Да, удалить", b"clear_db_execute")],
                        [Button.inline("↩️ Отмена", b"refresh")],
                    ],
                )
            elif data == b"clear_db_execute":
                self.db.clear_published()
                await event.edit(self.get_stats_text(), buttons=self.get_admin_keyboard())
                await event.answer("База дедупликации очищена")
                log("Администратор очистил БД дедупликации", logging.WARNING)

    async def run(self) -> None:
        timeout = aiohttp.ClientTimeout(
            total=self.settings.request_timeout_seconds,
            connect=min(5, self.settings.request_timeout_seconds),
        )
        connector = aiohttp.TCPConnector(
            limit=20,
            ttl_dns_cache=300,
            ssl=build_ssl_context(),
        )
        self.http_session = aiohttp.ClientSession(
            timeout=timeout,
            connector=connector,
            headers=DEFAULT_HEADERS,
            cookie_jar=aiohttp.CookieJar(unsafe=True),
            raise_for_status=False,
        )
        if CurlAsyncSession is not None:
            curl_kwargs = {
                "impersonate": "chrome",
                "timeout": self.settings.request_timeout_seconds,
            }
            if self.settings.ozon_proxy:
                curl_kwargs["proxies"] = {
                    "http": self.settings.ozon_proxy,
                    "https": self.settings.ozon_proxy,
                }
            self.curl_session = CurlAsyncSession(**curl_kwargs)
            log("Активирован curl_cffi (Chrome TLS/HTTP2 impersonation)")
        else:
            log(
                "Пакет curl_cffi недоступен; используется браузерный резолвер Playwright + aiohttp",
                logging.WARNING,
            )

        self._load_cached_cookies_from_disk()
        await self.get_rub_to_byn_rate()
        await self.browser_resolver.start_idle_monitor()
        if self.browser_resolver.is_available:
            log(
                f"Активирован браузерный резолвер Ozon/WB (Playwright Chromium, "
                f"витрины: {', '.join(self.settings.ozon_preferred_storefronts)})"
            )
        else:
            log(
                "Playwright недоступен; для гарантированного обхода JS-антибота "
                "установите: pip install playwright && playwright install chromium",
                logging.WARNING,
            )

        try:
            log("Авторизация Telegram-клиентов")
            await self.user_client.start()
            await self.bot_client.start(bot_token=self.settings.bot_token)

            me = await self.user_client.get_me()
            log(
                f"User-клиент авторизован как: {me.first_name or ''} {me.last_name or ''} "
                f"(@{me.username or 'без_username'}, id={me.id})"
            )

            target_reference = self.settings.target_channel
            if target_reference.lstrip("-").isdigit():
                target_reference = int(target_reference)

            self.target_entity = await self.user_client.get_entity(target_reference)
            self.target_peer_id = get_peer_id(self.target_entity)

            channels = await self.update_sources()
            self.workers = [
                asyncio.create_task(self.process_worker(number), name=f"parser-worker-{number}")
                for number in range(1, self.settings.worker_count + 1)
            ]
            await self.bot_client.send_message(
                self.settings.admin_id,
                "🟢 Парсер запущен и слушает источники.",
                buttons=self.get_admin_keyboard(),
            )
            await self.send_long_admin_message(self.format_channels_message(channels, "Подключённые источники"))
            await asyncio.gather(
                self.user_client.run_until_disconnected(),
                self.bot_client.run_until_disconnected(),
            )
        finally:
            pending = [*self.workers, *self.album_tasks.values(), *self.retry_tasks]
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            await self.browser_resolver.close()
            if self.curl_session is not None:
                with suppress(Exception):
                    await self.curl_session.close()
            if self.http_session:
                await self.http_session.close()
            await self.user_client.disconnect()
            await self.bot_client.disconnect()


def configure_logging() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


async def main() -> None:
    configure_logging()
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        raise SystemExit(f"Ошибка конфигурации: {exc}") from exc

    if "--login-ozon" in sys.argv:
        resolver = OzonBrowserResolver(settings)
        cookies = await resolver.interactive_login("https://ozon.by/")
        is_auth, label = extract_ozon_auth_info(cookies)
        log(
            f"Сессия Ozon сохранена в {settings.cookies_cache_path} и {settings.browser_profile_dir} "
            f"(авторизация: {'да, ' + label if is_auth else 'гостевая'})"
        )
        return

    app = ParserApp(settings)
    await app.run()


if __name__ == "__main__":
    asyncio.run(main())