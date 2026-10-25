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
        "wildberries.kz",
        "wb.ru",
        "ozon.ru",
        "ozon.by",
        "ozon.kz",
    }
)
OZON_DOMAINS = ("ozon.ru", "ozon.by", "ozon.kz")
WB_DOMAINS = ("wildberries.ru", "wildberries.by", "wildberries.kz", "wb.ru")
WB_COOKIE_DOMAINS = (
    "wildberries.by",
    "wildberries.ru",
    "wb.ru",
    "card.wb.ru",
    "search.wb.ru",
    "u-card.wb.ru",
)

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

# Поддержка любых поддоменов wildberries.(ru|by|kz), короткого домена wb.ru, путей /catalog/<sku> и ?card=<sku> / ?nm=<sku>
WB_SKU_RE = re.compile(
    r"(?:[a-z0-9-]+\.)?(?:wildberries\.(?:ru|by|kz)|wb\.ru)/"
    r"(?:catalog/(\d+)|product/?\?(?:[^#\"'\s<>]*&)?card=(\d+)|[^#\"'\s<>]*?[?&]nm=(\d+))",
    re.IGNORECASE,
)

WB_BASKET_HOST_IN_URL_RE = re.compile(
    r"https?://(basket-\d+\.(?:wbbasket\.ru|wbcontent\.net))/vol(\d+)/",
    re.IGNORECASE,
)

# Глобальный кэш соответствия vol -> basket-XX.wbbasket.ru (обновляется динамически при пробинге и из браузера)
WB_BASKET_VOL_CACHE: dict[int, str] = {}

# Поддержка любых поддоменов ozon.(ru|by|kz), путей /product/...-<sku> и /context/detail/id/<sku>
OZON_SKU_RE = re.compile(
    r"(?:[a-z0-9-]+\.)?ozon\.(?:ru|by|kz)/(?:product/(?:[^/?#\"'\s<>\\]*-)?|context/detail/id/)(\d{5,15})(?:[/?#\"'\s<>\\]|$)",
    re.IGNORECASE,
)

# Поиск полного пути /product/<slug>-<sku>/ в теле ответа/JSON
OZON_PRODUCT_PATH_RE = re.compile(
    r"((?:https?://(?:[a-z0-9-]+\.)?ozon\.(?:ru|by|kz))?/(?:product/(?:[^/?#\"'\s<>\\]+-)?|context/detail/id/)(\d{5,15})(?:/|\b)[^\s\"'<>\\]*)",
    re.IGNORECASE,
)

# Поиск артикула в HTML/JSON (<head>, body, composer-api, deeplink, относительные ссылки /product/...-<sku>)
HEAD_PRODUCT_RE = re.compile(
    r"(?:"
    r"(?:ozon\.(?:ru|by|kz))?/(?:product/(?:[^/\"'?#\s<>\\]*-)?|context/detail/id/)|"
    r"ozon://(?:products|product)/|"
    r"\"(?:sku|skuId|itemId)\"\s*:\s*\"?"
    r")"
    r"(\d{5,15})(?:[/?#\"'\\&\s<>]|$)",
    re.IGNORECASE,
)

# Извлечение артикула из текста поста («Артикул: 12345678», «Арт. 12345678», «Код товара: 12345678»)
TEXT_SKU_RE = re.compile(
    r"(?:артикул|арт\.?|код\s+товара|sku)\s*[:#№\-–—]?\s*(\d{6,15})\b",
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
    r"(byn|бел\.?\s*руб\.?|бр|br|р\.|руб\.?|₽|rub|rur)?",
    re.IGNORECASE,
)

# Поиск средней цены за месяц в Ozon («13,22 BYN ... Средняя за сентябрь» или «Средняя за месяц: 13,22 BYN»)
AVG_MONTH_LABEL_RE = re.compile(
    r"средн(?:яя|ей)\s+(?:цена\s+)?за\s+"
    r"(?:прошлый\s+месяц|месяц|январ[ья]|феврал[ья]|март[а]?|апрел[ья]|ма[йя]|июн[ья]|"
    r"июл[ья]|август[а]?|сентябр[ья]|октябр[ья]|ноябр[ья]|декабр[ья])",
    re.IGNORECASE,
)

AVG_PRICE_BEFORE_LABEL_RE = re.compile(
    r"(\d[\d\s\u00a0\u202f\u2009]*(?:[.,]\d{1,2})?)\s*"
    r"(byn|бел\.?\s*руб\.?|бр|br|р\.|руб\.?|₽|rub|rur)"
    r"[^0-9]{0,60}?"
    r"средн(?:яя|ей)\s+(?:цена\s+)?за\s+"
    r"(?:прошлый\s+месяц|месяц|январ[ья]|феврал[ья]|март[а]?|апрел[ья]|ма[йя]|июн[ья]|"
    r"июл[ья]|август[а]?|сентябр[ья]|октябр[ья]|ноябр[ья]|декабр[ья])",
    re.IGNORECASE,
)

AVG_PRICE_AFTER_LABEL_RE = re.compile(
    r"средн(?:яя|ей)\s+(?:цена\s+)?за\s+"
    r"(?:прошлый\s+месяц|месяц|январ[ья]|феврал[ья]|март[а]?|апрел[ья]|ма[йя]|июн[ья]|"
    r"июл[ья]|август[а]?|сентябр[ья]|октябр[ья]|ноябр[ья]|декабр[ья])"
    r"[^0-9]{0,60}?"
    r"(\d[\d\s\u00a0\u202f\u2009]*(?:[.,]\d{1,2})?)\s*"
    r"(byn|бел\.?\s*руб\.?|бр|br|р\.|руб\.?|₽|rub|rur)?",
    re.IGNORECASE,
)

# Сумма скидки на бейдже «Стало дешевле» («↓ 4,83 BYN» или в JSON рядом со «Стало дешевле»)
PRICE_DROP_BADGE_RE = re.compile(
    r"[↓⬇]\s*(\d[\d\s\u00a0\u202f\u2009]*(?:[.,]\d{1,2})?)\s*"
    r"(byn|бел\.?\s*руб\.?|бр|br|р\.|руб\.?|₽|rub|rur)",
    re.IGNORECASE,
)

CHEAPER_BADGE_AMOUNT_RE = re.compile(
    r"(?:"
    r"стало\s+дешевле[^0-9]{0,50}?(\d[\d\s\u00a0\u202f\u2009]*(?:[.,]\d{1,2})?)\s*(byn|бел\.?\s*руб\.?|бр|br|р\.|руб\.?|₽|rub|rur)|"
    r"(\d[\d\s\u00a0\u202f\u2009]*(?:[.,]\d{1,2})?)\s*(byn|бел\.?\s*руб\.?|бр|br|р\.|руб\.?|₽|rub|rur)[^0-9]{0,50}?стало\s+дешевле"
    r")",
    re.IGNORECASE,
)

OZON_MODAL_URL_RE = re.compile(
    r"(?:/modal/[A-Za-z0-9_/?=&.%-]+|/[A-Za-z0-9_/-]*priceDecrease[A-Za-z0-9_/?=&.%-]*)",
    re.IGNORECASE,
)

EXPLICIT_POST_PRICE_RE = re.compile(
    r"(?:цена|стоимость|всего|за|по\s+цене)?\s*[:\-–—]?\s*"
    r"(\d[\d\s\u00a0\u202f]*(?:[.,]\d{1,2})?)\s*"
    r"(byn|бел\.?\s*руб\.?|бр|₽|рос\.?\s*руб\.?|руб\.?|rub)\b",
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
    include_source_notes: bool = False
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

    @property
    def wb_baskets_cache_path(self) -> str:
        return os.path.join(self.data_dir, "wb_baskets_cache.json")

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
            worker_count=env_int("WORKER_COUNT", default=4, minimum=1),
            max_links_per_post=env_int("MAX_LINKS_PER_POST", default=5, minimum=1),
            request_timeout_seconds=env_int("REQUEST_TIMEOUT_SECONDS", default=5, minimum=1),
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
            include_source_notes=env_bool("INCLUDE_SOURCE_NOTES", default=False),
        )


@dataclass(frozen=True)
class ProductCardData:
    title: str = ""
    brand: str = ""
    price_byn: float | None = None
    avg_month_price_byn: float | None = None
    old_price_byn: float | None = None
    image_url: str = ""
    in_stock: bool | None = None
    rating: float | None = None
    reviews_count: int | None = None
    raw_currency: str = "BYN"
    has_price_drop_badge: bool = False
    price_drop_modal_url: str = ""


@dataclass(frozen=True)
class ProductInfo:
    original_url: str
    resolved_url: str
    platform: str
    sku: str | None
    canonical_url: str
    sku_key: str
    title: str = ""
    brand: str = ""
    price_byn: float | None = None
    avg_month_price_byn: float | None = None
    old_price_byn: float | None = None
    image_url: str = ""
    image_bytes: bytes | None = None
    in_stock: bool = True
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
        if (
            self.avg_month_price_byn is not None
            and self.price_byn is not None
            and self.avg_month_price_byn > self.price_byn * 1.01
        ):
            return format_byn_price(self.avg_month_price_byn)
        return None

    @property
    def formatted_old_price_byn(self) -> str | None:
        if (
            self.old_price_byn is not None
            and self.price_byn is not None
            and self.old_price_byn > self.price_byn * 1.02
        ):
            return format_byn_price(self.old_price_byn)
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
    """SQLite-дедупликация с постоянным соединением и краткоживущими резервированиями pending."""

    def __init__(self, db_path: str, cooldown_hours: int, pending_ttl_minutes: int):
        self.db_path = db_path
        self.cooldown_seconds = cooldown_hours * 3600
        self.pending_ttl_seconds = pending_ttl_minutes * 60
        self._last_cleanup_ts: int = 0
        self._conn = self._connect()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            timeout=10,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _init_db(self) -> None:
        self._conn.execute(
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
        self._conn.execute(
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

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            if (now - self._last_cleanup_ts) >= 300:
                self._conn.execute(
                    "DELETE FROM seen_items WHERE status = 'published' AND published_at < ?",
                    (published_cutoff,),
                )
                self._conn.execute(
                    "DELETE FROM seen_items WHERE status = 'pending' AND created_at < ?",
                    (pending_cutoff,),
                )
                self._last_cleanup_ts = now
            placeholders = ",".join("?" for _ in products_by_key)
            rows = self._conn.execute(
                f"SELECT sku_key FROM seen_items WHERE sku_key IN ({placeholders})",
                tuple(products_by_key),
            ).fetchall()
            occupied = {row[0] for row in rows}
            fresh_keys = set(products_by_key) - occupied
            self._conn.executemany(
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
            self._conn.execute("COMMIT")
            return fresh_keys
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def mark_published(self, sku_keys: Iterable[str]) -> None:
        keys = tuple(set(sku_keys))
        if not keys:
            return
        now = int(time.time())
        placeholders = ",".join("?" for _ in keys)
        self._conn.execute(
            f"UPDATE seen_items SET status = 'published', published_at = ? "
            f"WHERE status = 'pending' AND sku_key IN ({placeholders})",
            (now, *keys),
        )

    def release_reservations(self, sku_keys: Iterable[str]) -> None:
        keys = tuple(set(sku_keys))
        if not keys:
            return
        placeholders = ",".join("?" for _ in keys)
        self._conn.execute(
            f"DELETE FROM seen_items WHERE status = 'pending' AND sku_key IN ({placeholders})",
            keys,
        )

    def clear_published(self) -> None:
        self._conn.execute("DELETE FROM seen_items")

    def count_by_status(self) -> tuple[int, int]:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) FROM seen_items GROUP BY status"
        ).fetchall()
        counts = {status: count for status, count in rows}
        return counts.get("published", 0), counts.get("pending", 0)

    def close(self) -> None:
        with suppress(Exception):
            self._conn.close()


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


def convert_image_bytes_to_jpeg(raw_bytes: bytes) -> io.BytesIO | None:
    """Готовит изображение в памяти с именем product.jpg, конвертируя WEBP/PNG в JPEG (чтобы Telegram не отправлял его как стикер)."""
    if not raw_bytes or len(raw_bytes) < 32:
        return None

    # Если скачанные байты уже являются готовым JPEG, возвращаем буфер мгновенно без перекодирования через Pillow
    if raw_bytes[:3] == b"\xff\xd8\xff":
        buf = io.BytesIO(raw_bytes)
        buf.name = "product.jpg"
        return buf

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

    # Если Pillow не установлен, проверяем, что это не WEBP (WEBP без конвертации Telegram считает стикером)
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
    if any(marker in text_lower for marker in ("byn", "бел", "бр", "br")):
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

    unit = (match.group(2) or "").casefold()
    if unit:
        if any(m in unit for m in ("byn", "бел", "бр", "br")):
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


def extract_avg_month_price_from_text(
    text: str,
    *,
    actual_price_byn: float | None = None,
    default_currency: str = "BYN",
    rub_to_byn_rate: float = 0.0345,
) -> float | None:
    """Ищет среднюю цену за месяц в блоке «Стало дешевле» (например: «13,22 BYN Средняя за сентябрь»)."""
    if not text:
        return None

    clean_text = clean_html_tags(text)

    # 1. Вариант «13,22 BYN ... Средняя за сентябрь» (как в модальном окне Ozon «Стало дешевле»)
    m_before = AVG_PRICE_BEFORE_LABEL_RE.search(clean_text)
    if m_before:
        raw_p = f"{m_before.group(1)} {m_before.group(2)}"
        val, curr = parse_price_value_and_currency(raw_p, default_currency=default_currency)
        byn_val = convert_to_byn(val, curr, rub_to_byn_rate)
        if byn_val is not None:
            return byn_val

    # 2. Вариант «Средняя за сентябрь: 13,22 BYN»
    m_after = AVG_PRICE_AFTER_LABEL_RE.search(clean_text)
    if m_after:
        raw_p = f"{m_after.group(1)} {m_after.group(2) or default_currency}"
        val, curr = parse_price_value_and_currency(raw_p, default_currency=default_currency)
        byn_val = convert_to_byn(val, curr, rub_to_byn_rate)
        if byn_val is not None:
            return byn_val

    # 3. Если есть актуальная цена и зелёный бейдж снижения «↓ 4,83 BYN» или сумма рядом со «Стало дешевле»
    if actual_price_byn is not None and actual_price_byn > 0:
        m_drop = PRICE_DROP_BADGE_RE.search(clean_text)
        if m_drop:
            raw_drop = f"{m_drop.group(1)} {m_drop.group(2)}"
            drop_val, drop_curr = parse_price_value_and_currency(raw_drop, default_currency=default_currency)
            drop_byn = convert_to_byn(drop_val, drop_curr, rub_to_byn_rate)
            if drop_byn is not None and drop_byn > 0:
                return round(actual_price_byn + drop_byn, 2)

        m_cheaper = CHEAPER_BADGE_AMOUNT_RE.search(clean_text)
        if m_cheaper:
            amt_part = m_cheaper.group(1) or m_cheaper.group(3)
            cur_part = m_cheaper.group(2) or m_cheaper.group(4)
            if amt_part:
                raw_drop = f"{amt_part} {cur_part or default_currency}"
                drop_val, drop_curr = parse_price_value_and_currency(raw_drop, default_currency=default_currency)
                drop_byn = convert_to_byn(drop_val, drop_curr, rub_to_byn_rate)
                if drop_byn is not None and 0 < drop_byn < actual_price_byn * 10:
                    return round(actual_price_byn + drop_byn, 2)

    return None


def _collect_scalar_strings(node: Any) -> list[str]:
    """Быстро собирает строковые/числовые значения из узла без дорогого json.dumps."""
    if isinstance(node, str):
        return [node] if node else []
    if isinstance(node, (int, float)) and not isinstance(node, bool):
        return [str(node)]
    if isinstance(node, dict):
        out: list[str] = []
        for v in node.values():
            if isinstance(v, str) and v:
                out.append(v)
            elif isinstance(v, (int, float)) and not isinstance(v, bool):
                out.append(str(v))
        return out
    return []


def extract_ozon_avg_price_from_state(
    state: Any,
    *,
    actual_price_byn: float | None = None,
    default_currency: str = "BYN",
    rub_to_byn_rate: float = 0.0345,
) -> float | None:
    """Рекурсивно ищет среднюю цену за месяц или дельту снижения в JSON-состоянии виджетов Ozon без json.dumps."""
    if isinstance(state, dict):
        # 1. Прямые ключи средней цены
        for key in ("averagePrice", "avgPrice", "monthAvgPrice", "averageMonthPrice", "prevMonthAvgPrice"):
            if state.get(key):
                val, curr = parse_price_value_and_currency(state[key], default_currency=default_currency)
                byn_val = convert_to_byn(val, curr, rub_to_byn_rate)
                if byn_val is not None:
                    return byn_val

        # 2. Прямые ключи дельты снижения цены (если есть актуальная цена)
        if actual_price_byn is not None and actual_price_byn > 0:
            for drop_key in ("priceDrop", "priceDecrease", "discountAmount", "dropAmount"):
                raw_d = state.get(drop_key)
                if isinstance(raw_d, (str, int, float)):
                    d_val, d_curr = parse_price_value_and_currency(raw_d, default_currency=default_currency)
                    d_byn = convert_to_byn(d_val, d_curr, rub_to_byn_rate)
                    if d_byn is not None and 0 < d_byn < actual_price_byn * 10:
                        return round(actual_price_byn + d_byn, 2)

        # 3. Если внутри словаря есть «Средняя за <месяц>» или «Стало дешевле», ищем сумму в том же словаре
        dict_strings = _collect_scalar_strings(state)
        if dict_strings:
            combined_dict_str = " | ".join(dict_strings)
            if AVG_MONTH_LABEL_RE.search(combined_dict_str):
                for s in dict_strings:
                    if not AVG_MONTH_LABEL_RE.search(s):
                        val, curr = parse_price_value_and_currency(s, default_currency=default_currency)
                        byn_val = convert_to_byn(val, curr, rub_to_byn_rate)
                        if byn_val is not None and (actual_price_byn is None or abs(byn_val - actual_price_byn) > 0.01):
                            return byn_val

            if actual_price_byn is not None and actual_price_byn > 0 and "стало дешевле" in combined_dict_str.casefold():
                for s in dict_strings:
                    if "стало дешевле" not in s.casefold():
                        d_val, d_curr = parse_price_value_and_currency(s, default_currency=default_currency)
                        d_byn = convert_to_byn(d_val, d_curr, rub_to_byn_rate)
                        if d_byn is not None and 0 < d_byn < actual_price_byn * 10 and abs(d_byn - actual_price_byn) > 0.01:
                            return round(actual_price_byn + d_byn, 2)

        for v in state.values():
            found = extract_ozon_avg_price_from_state(
                v,
                actual_price_byn=actual_price_byn,
                default_currency=default_currency,
                rub_to_byn_rate=rub_to_byn_rate,
            )
            if found is not None:
                return found

    elif isinstance(state, list):
        for idx, item in enumerate(state):
            found = extract_ozon_avg_price_from_state(
                item,
                actual_price_byn=actual_price_byn,
                default_currency=default_currency,
                rub_to_byn_rate=rub_to_byn_rate,
            )
            if found is not None:
                return found

            item_strings = _collect_scalar_strings(item)
            if item_strings:
                item_combined = " | ".join(item_strings)
                is_avg_label = bool(AVG_MONTH_LABEL_RE.search(item_combined))
                is_cheaper_label = (
                    actual_price_byn is not None
                    and actual_price_byn > 0
                    and "стало дешевле" in item_combined.casefold()
                )
                if is_avg_label or is_cheaper_label:
                    for neighbor_idx in (idx - 1, idx + 1):
                        if 0 <= neighbor_idx < len(state):
                            for ns in _collect_scalar_strings(state[neighbor_idx]):
                                val, curr = parse_price_value_and_currency(ns, default_currency=default_currency)
                                byn_val = convert_to_byn(val, curr, rub_to_byn_rate)
                                if byn_val is not None and (actual_price_byn is None or abs(byn_val - actual_price_byn) > 0.01):
                                    if is_avg_label:
                                        return byn_val
                                    if is_cheaper_label and actual_price_byn is not None and byn_val < actual_price_byn * 10:
                                        return round(actual_price_byn + byn_val, 2)

    return None


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
    for match in EXPLICIT_POST_PRICE_RE.finditer(clean):
        raw_fragment = f"{match.group(1)} {match.group(2)}"
        val, curr = parse_price_value_and_currency(raw_fragment, default_currency="BYN")
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

    total_quantity = item.get("totalQuantity")
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

    return ProductCardData(
        title=clean_product_title(raw_name, raw_brand),
        brand=raw_brand,
        price_byn=price_byn,
        old_price_byn=old_price_byn,
        image_url=image_url,
        in_stock=in_stock,
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
    rating: float | None = None
    reviews_count: int | None = None
    has_price_drop_badge = False
    price_drop_modal_url = ""

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

        # Виджет кнопки покупки / корзины (webSale / webAddToCart)
        elif "websale" in key_lower or "addtocart" in key_lower:
            if state.get("isAvailable") is False or state.get("outOfStock") is True:
                in_stock = False
            raw_lower = _raw_state.casefold()
            if any(m in raw_lower for m in OUT_OF_STOCK_MARKERS):
                in_stock = False

        # Виджет рейтинга и отзывов
        elif "reviewproductscore" in key_lower or "singleproductscore" in key_lower:
            with suppress(TypeError, ValueError):
                if state.get("score") is not None:
                    rating = float(state["score"])
            with suppress(TypeError, ValueError):
                if state.get("count") is not None:
                    reviews_count = int(state["count"])

    # Второй проход по widgetStates: детектируем плашку «Стало дешевле», ссылку на модалку и среднюю цену за месяц
    for key_lower, state, raw_state in parsed_states:
        raw_lower = raw_state.casefold()
        is_price_related = (
            "webprice" in key_lower
            or "pricedecrease" in key_lower
            or "pricehistory" in key_lower
            or "средняя за" in raw_lower
            or "стало дешевле" in raw_lower
        )
        if not is_price_related:
            continue

        if (
            "стало дешевле" in raw_lower
            or "pricedecrease" in key_lower
            or "pricedecrease" in raw_lower
            or "средняя за" in raw_lower
        ):
            has_price_drop_badge = True
            if not price_drop_modal_url:
                clean_raw = raw_state.replace("\\/", "/")
                m_modal = OZON_MODAL_URL_RE.search(clean_raw)
                if m_modal:
                    price_drop_modal_url = m_modal.group(0)

        if avg_month_price_byn is None:
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
                has_price_drop_badge = True

    if in_stock is None:
        if price_byn is not None and price_byn > 0:
            in_stock = True
        else:
            combined_raw = " ".join(r.casefold() for _, _, r in parsed_states)
            if any(marker in combined_raw for marker in NOT_FOUND_MARKERS):
                in_stock = False
            elif any(marker in combined_raw for marker in OUT_OF_STOCK_MARKERS):
                in_stock = False

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
        has_price_drop_badge=has_price_drop_badge,
        price_drop_modal_url=price_drop_modal_url,
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
    elif primary.in_stock is False and secondary.in_stock is not True:
        in_stock = False
    elif secondary.in_stock is False and primary.in_stock is not True:
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

    return ProductCardData(
        title=title,
        brand=brand,
        price_byn=price_byn,
        avg_month_price_byn=avg_month_price_byn,
        old_price_byn=old_price_byn,
        image_url=image_url,
        in_stock=in_stock,
        rating=primary.rating if primary.rating is not None else secondary.rating,
        reviews_count=primary.reviews_count if primary.reviews_count is not None else secondary.reviews_count,
        raw_currency=primary.raw_currency or secondary.raw_currency,
        has_price_drop_badge=bool(primary.has_price_drop_badge or secondary.has_price_drop_badge),
        price_drop_modal_url=primary.price_drop_modal_url or secondary.price_drop_modal_url,
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
        sku = ozon_match.group(1)
        orig_host = normalize_host(urlparse(original_url).hostname)
        base_host = orig_host if is_ozon_host(orig_host) else urlparse(resolved_url).hostname
        canonical_host = resolve_ozon_canonical_host(base_host, canonical_host_override)
        return ProductInfo(
            original_url=original_url,
            resolved_url=resolved_url,
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
        resolved_url=resolved_url,
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


def build_ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    with suppress(ssl.SSLError):
        ctx.set_ciphers(
            "ECDHE+AESGCM:ECDHE+CHACHA20:DHE+AESGCM:DHE+CHACHA20:"
            "ECDH+AESGCM:DH+AESGCM:ECDH+AES:DH+AES:RSA+AESGCM:RSA+AES:!aNULL:!eNULL:!MD5:!DSS"
        )
    return ctx


class OzonBrowserResolver:
    """Браузерный резолвер коротких ссылок и парсер карточек Ozon с обходом Variti antibot."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._playwright = None
        self._context = None
        self._warm_pages: dict[str, object] = {}
        self._lock = asyncio.Lock()
        self._page_semaphore = asyncio.Semaphore(3)
        self._last_used: float = 0.0
        self._idle_task: asyncio.Task | None = None
        self._available: bool = bool(settings.use_browser_resolver and async_playwright_factory is not None)
        self._cdp_ua_metadata: dict | None = None
        self.last_cookies: list[dict] = []

    async def warmup(self) -> list[dict]:
        """Фоновый прогрев браузера и основной витрины Ozon при старте, чтобы первый пост не ждал холодного запуска."""
        if not self._available:
            return []
        async with self._lock:
            if not await self._ensure_context():
                return []
            primary_domain = self.settings.ozon_canonical_host or "ozon.by"
            await self._warm_up_storefront(primary_domain)
            return await self._export_cookies()

    async def _configure_lightweight_page(self, page) -> None:
        """Блокирует загрузку шрифтов, видео и сторонних трекеров на рабочих вкладках для ускорения рендера."""
        async def _block_heavy_resources(route) -> None:
            with suppress(Exception):
                req = route.request
                if req.resource_type in {"font", "media"}:
                    await route.abort()
                    return
                r_url = req.url.lower()
                if any(
                    tracker in r_url
                    for tracker in ("mc.yandex", "google-analytics", "googletagmanager", "top-fwz1.mail.ru", "vk.com/rtrg")
                ):
                    await route.abort()
                    return
                await route.continue_()

        with suppress(Exception):
            await page.route("**/*", _block_heavy_resources)

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

    async def fetch_product_card(
        self,
        sku: str,
        *,
        rub_to_byn_rate: float = 0.0345,
        only_avg_price: bool = False,
        existing_card: ProductCardData | None = None,
    ) -> tuple[ProductCardData | None, list[dict]]:
        """Получает данные карточки товара Ozon (название, цена в BYN, средняя цена за месяц, главное фото, наличие) через браузер."""
        if not self._available or not sku:
            return None, []

        async with self._page_semaphore:
            async with self._lock:
                if not await self._ensure_context():
                    return None, []
                self._last_used = time.time()

            storefronts: list[str] = ["ozon.by"]
            if not only_avg_price:
                for domain in self.settings.ozon_preferred_storefronts:
                    if domain not in storefronts:
                        storefronts.append(domain)

            product_path = f"/product/{sku}/"
            encoded_path = quote(product_path, safe="/")

            for domain in storefronts:
                default_currency = "BYN" if domain == "ozon.by" else "RUB"
                async with self._lock:
                    warm_page = await self._warm_up_storefront(domain)
                inpage_card: ProductCardData | None = existing_card if (only_avg_price and domain == "ozon.by") else None

                if warm_page is not None:
                    # Если нам нужна только средняя цена и уже известен URL модального окна — запрашиваем сразу его
                    if (
                        inpage_card is not None
                        and inpage_card.price_drop_modal_url
                        and inpage_card.avg_month_price_byn is None
                    ):
                        modal_enc = quote(inpage_card.price_drop_modal_url, safe="/?=&")
                        with suppress(Exception):
                            modal_json_str = await warm_page.evaluate(
                                """async ({ modalEnc }) => {
                                    const eps = [
                                        `/api/entrypoint-api.bx/page/json/v2?url=${modalEnc}`,
                                        `/api/composer-api.bx/page/json/v2?url=${modalEnc}`
                                    ];
                                    for (const ep of eps) {
                                        try {
                                            const r = await fetch(ep, {
                                                method: 'GET',
                                                credentials: 'include',
                                                headers: { 'Accept': 'application/json' }
                                            });
                                            if (r.ok) {
                                                const t = await r.text();
                                                if (t) return t.slice(0, 150000);
                                            }
                                        } catch (e) {}
                                    }
                                    return '';
                                }""",
                                {"modalEnc": modal_enc},
                            )
                            if modal_json_str:
                                avg_from_modal = extract_avg_month_price_from_text(
                                    modal_json_str,
                                    actual_price_byn=inpage_card.price_byn,
                                    default_currency=default_currency,
                                    rub_to_byn_rate=rub_to_byn_rate,
                                )
                                if avg_from_modal is not None:
                                    inpage_card = replace(inpage_card, avg_month_price_byn=avg_from_modal)
                                    cookies = await self._export_cookies()
                                    return inpage_card, cookies

                    if inpage_card is None:
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

                                    # Если у товара обнаружена плашка «Стало дешевле» со ссылкой на модалку, дозапрашиваем модалку через in-page fetch
                                    if (
                                        inpage_card
                                        and inpage_card.has_price_drop_badge
                                        and inpage_card.avg_month_price_byn is None
                                        and inpage_card.price_drop_modal_url
                                    ):
                                        modal_enc = quote(inpage_card.price_drop_modal_url, safe="/?=&")
                                        modal_txt = await warm_page.evaluate(
                                            """async ({ modalEnc }) => {
                                                try {
                                                    const r = await fetch(`/api/entrypoint-api.bx/page/json/v2?url=${modalEnc}`, {
                                                        method: 'GET',
                                                        credentials: 'include',
                                                        headers: { 'Accept': 'application/json' }
                                                    });
                                                    if (r.ok) return (await r.text()).slice(0, 150000);
                                                } catch (e) {}
                                                return '';
                                            }""",
                                            {"modalEnc": modal_enc},
                                        )
                                        if modal_txt:
                                            avg_m = extract_avg_month_price_from_text(
                                                modal_txt,
                                                actual_price_byn=inpage_card.price_byn,
                                                default_currency=default_currency,
                                                rub_to_byn_rate=rub_to_byn_rate,
                                            )
                                            if avg_m is not None:
                                                inpage_card = replace(inpage_card, avg_month_price_byn=avg_m)

                                    # Мгновенный возврат, если карточка получена и либо средняя цена найдена, либо плашки «Стало дешевле» у товара нет!
                                    if (
                                        inpage_card
                                        and inpage_card.title
                                        and inpage_card.price_byn is not None
                                        and inpage_card.image_url
                                        and (
                                            inpage_card.avg_month_price_byn is not None
                                            or not inpage_card.has_price_drop_badge
                                        )
                                    ):
                                        cookies = await self._export_cookies()
                                        log(
                                            f"Карточка Ozon {sku} быстро получена через in-page API ({domain}): "
                                            f"price={inpage_card.price_byn} BYN, avg={inpage_card.avg_month_price_byn} BYN, "
                                            f"has_badge={inpage_card.has_price_drop_badge}"
                                        )
                                        return inpage_card, cookies

                # Открываем страницу товара во вкладке ТОЛЬКО если in-page API не дал карточку или нужно прочитать попап «Стало дешевле» из DOM
                assert self._context is not None
                nav_page = await self._context.new_page()
                await self._apply_cdp_stealth(nav_page)
                await self._configure_lightweight_page(nav_page)
                target_url = f"https://{domain}/product/{sku}/" if domain == "ozon.by" else f"https://www.{domain}/product/{sku}/"
                try:
                    nav_timeout = 5_500 if only_avg_price else 9_000
                    resp = await nav_page.goto(target_url, wait_until="domcontentloaded", timeout=nav_timeout)
                    if resp and resp.status == 404:
                        cookies = await self._export_cookies()
                        return ProductCardData(in_stock=False), cookies

                    with suppress(Exception):
                        await nav_page.wait_for_selector(
                            "[data-widget='webPrice'], h1",
                            timeout=1800 if only_avg_price else 2500,
                        )

                    # Если на странице есть плашка «Стало дешевле», быстро кликаем по ней (до 400 мс)
                    with suppress(Exception):
                        cheaper_el = await nav_page.query_selector("text=Стало дешевле")
                        if cheaper_el:
                            await cheaper_el.click(timeout=400)
                            with suppress(Exception):
                                await nav_page.wait_for_selector("text=Средняя за", timeout=450)

                    html_content = await nav_page.content()
                    dom_card = parse_html_product_card(
                        html_content,
                        default_currency=default_currency,
                        rub_to_byn_rate=rub_to_byn_rate,
                    )

                    with suppress(Exception):
                        dom_extra = await nav_page.evaluate(
                            """() => {
                                const bodyText = document.body ? document.body.innerText : '';
                                const galleryImg = document.querySelector('[data-widget="webGallery"] img, [data-widget="webMobGallery"] img');
                                return {
                                    bodyText: bodyText.slice(0, 60000),
                                    imgSrc: galleryImg ? (galleryImg.currentSrc || galleryImg.src || '') : ''
                                };
                            }"""
                        )
                        if isinstance(dom_extra, dict):
                            extra_img = normalize_image_url(dom_extra.get("imgSrc"))
                            body_text = str(dom_extra.get("bodyText") or "")
                            ref_price = (
                                (inpage_card.price_byn if inpage_card else None)
                                or (dom_card.price_byn if dom_card else None)
                            )
                            extra_avg = extract_avg_month_price_from_text(
                                body_text,
                                actual_price_byn=ref_price,
                                default_currency=default_currency,
                                rub_to_byn_rate=rub_to_byn_rate,
                            )
                            if dom_card:
                                dom_card = replace(
                                    dom_card,
                                    avg_month_price_byn=dom_card.avg_month_price_byn or extra_avg,
                                    image_url=dom_card.image_url or extra_img,
                                )
                            elif extra_avg or extra_img:
                                dom_card = ProductCardData(
                                    avg_month_price_byn=extra_avg,
                                    image_url=extra_img,
                                    raw_currency=default_currency,
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
            return inpage_card, cookies

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

        async with self._page_semaphore:
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
                await self._configure_lightweight_page(nav_page)

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
                        await asyncio.sleep(0.35)

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

    def _write_cookies_sync(self, cookies: list[dict]) -> None:
        with suppress(OSError):
            with open(self.settings.cookies_cache_path, "w", encoding="utf-8") as f:
                json.dump(cookies, f, ensure_ascii=False)

    async def _export_cookies(self) -> list[dict]:
        if self._context is None:
            return []
        with suppress(Exception):
            cookies = await self._context.cookies()
            if cookies:
                self.last_cookies = cookies
                await asyncio.to_thread(self._write_cookies_sync, cookies)
                return cookies
        return []

    async def resolve_short_url(self, clean_url: str) -> tuple[str | None, list[dict]]:
        """Раскрывает короткую ссылку Ozon через прогретый Chromium и возвращает (resolved_url, cookies)."""
        if not self._available:
            return None, []

        async with self._page_semaphore:
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
                async with self._lock:
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
                loop_aborted = False

                def _on_url_candidate(candidate_url: str) -> None:
                    if candidate_url and has_product_sku(candidate_url):
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
                    if has_product_sku(req_url):
                        captured_urls.append(req_url)
                    if "__rr=" in req_url and not has_product_sku(req_url):
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
                            if has_product_sku(candidate):
                                cookies = await self._export_cookies()
                                log(f"Ссылка раскрыта через браузер ({domain}): {clean_url} -> {candidate}")
                                return candidate, cookies

                        if has_product_sku(nav_page.url):
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
        self.resolve_semaphore = asyncio.Semaphore(16)
        self.publish_lock = asyncio.Lock()
        self.album_messages: dict[int, list] = {}
        self.album_tasks: dict[int, asyncio.Task] = {}
        self.album_lock = asyncio.Lock()
        self.synced_cookie_count: int = 0
        self._verified_hosts: set[str] = set()
        self._rub_to_byn_rate: float = settings.rub_to_byn_fallback_rate
        self._rate_updated_at: float = 0.0
        self._rate_lock = asyncio.Lock()
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

    def _write_wb_baskets_sync(self, serializable: dict[str, str]) -> None:
        with suppress(OSError):
            with open(self.settings.wb_baskets_cache_path, "w", encoding="utf-8") as f:
                json.dump(serializable, f, ensure_ascii=False)

    def _save_wb_baskets_cache_to_disk(self) -> None:
        if not WB_BASKET_VOL_CACHE:
            return
        serializable = {str(k): v for k, v in WB_BASKET_VOL_CACHE.items()}
        with suppress(RuntimeError):
            loop = asyncio.get_running_loop()
            loop.create_task(asyncio.to_thread(self._write_wb_baskets_sync, serializable))
            return
        self._write_wb_baskets_sync(serializable)

    async def host_has_only_public_ips(self, hostname: str) -> bool:
        """Дополнительная защита от SSRF с in-memory кэшированием проверенных хостов."""
        host_norm = normalize_host(hostname)
        if not host_norm:
            return False
        if host_norm in self._verified_hosts:
            return True
        try:
            is_pub = ipaddress.ip_address(host_norm).is_global
            if is_pub:
                self._verified_hosts.add(host_norm)
            return is_pub
        except ValueError:
            pass
        try:
            loop = asyncio.get_running_loop()
            results = await loop.getaddrinfo(host_norm, None, type=0)
            ips = {item[4][0] for item in results}
            is_pub = bool(ips) and all(ipaddress.ip_address(ip).is_global for ip in ips)
            if is_pub:
                self._verified_hosts.add(host_norm)
            return is_pub
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

    async def download_product_image(self, image_url: str) -> io.BytesIO | None:
        """Скачивает главное фото из карточки товара и подготавливает его как JPEG для отправки в Telegram."""
        clean_url = normalize_image_url(image_url)
        if not clean_url:
            return None

        candidate_urls = [clean_url]
        if "wbbasket.ru/" in clean_url or "wbcontent.net/" in clean_url:
            if clean_url.endswith("/images/big/1.webp"):
                candidate_urls.append(clean_url[:-5] + ".jpg")
                candidate_urls.append(clean_url.replace("/images/big/1.webp", "/images/c516x688/1.webp"))
                candidate_urls.append(clean_url.replace("/images/big/1.webp", "/images/c516x688/1.jpg"))

        img_headers = {
            "User-Agent": BROWSER_USER_AGENT,
            "Accept": "image/avif,image/webp,image/apng,image/jpeg,image/png,image/*;q=0.8,*/*;q=0.5",
            "Accept-Language": "ru-RU,ru;q=0.9",
        }

        for target_url in candidate_urls:
            if self.curl_session is not None:
                with suppress(Exception):
                    async with self.resolve_semaphore:
                        resp = await self.curl_session.get(
                            target_url,
                            headers=img_headers,
                            allow_redirects=True,
                            timeout=self.settings.request_timeout_seconds,
                        )
                        if 200 <= resp.status_code < 300 and resp.content:
                            converted = await asyncio.to_thread(
                                convert_image_bytes_to_jpeg,
                                resp.content[:IMAGE_READ_LIMIT],
                            )
                            if converted is not None:
                                return converted
                        if resp.status_code == 404:
                            continue

            if self.http_session is not None:
                with suppress(Exception):
                    async with self.resolve_semaphore:
                        async with self.http_session.get(
                            target_url,
                            headers=img_headers,
                            allow_redirects=True,
                        ) as resp:
                            if 200 <= resp.status < 300:
                                raw = await resp.content.read(IMAGE_READ_LIMIT)
                                converted = await asyncio.to_thread(convert_image_bytes_to_jpeg, raw)
                                if converted is not None:
                                    return converted

        return None

    async def _resolve_wb_basket_host(
        self,
        sku_int: int,
        req_headers: dict[str, str],
    ) -> tuple[str, dict[str, Any] | None]:
        """Определяет рабочий шард basket-XX.wbbasket.ru для артикула WB и возвращает (basket_host, card_json).

        Если расчётный шард возвращает 404 (например, для новых 9- и 10-значных артикулов),
        выполняет быстрый параллельный пробинг соседних шардов и кэширует найденный хост для vol.
        """
        vol = sku_int // 100_000
        part = sku_int // 1_000
        candidates = get_wb_candidate_basket_hosts(vol)

        # 1. Проверяем первый (кэшированный или расчётный) хост
        primary_host = candidates[0]
        primary_url = f"https://{primary_host}/vol{vol}/part{part}/{sku_int}/info/ru/card.json"
        card_data = await self._fetch_json_url(primary_url, headers=req_headers, timeout_override=4.0)
        if isinstance(card_data, dict) and "_http_status" not in card_data and (
            card_data.get("imt_name") or card_data.get("subj_name") or card_data.get("nm_id") or card_data.get("imt_id")
        ):
            if WB_BASKET_VOL_CACHE.get(vol) != primary_host:
                WB_BASKET_VOL_CACHE[vol] = primary_host
                self._save_wb_baskets_cache_to_disk()
            return primary_host, card_data

        # 2. Если расчётный баскет вернул 404 / пусто, пробируем остальные шарды батчами по 10
        remaining = candidates[1:]
        batch_size = 10
        for i in range(0, len(remaining), batch_size):
            batch = remaining[i:i + batch_size]

            async def _probe_host(host: str) -> tuple[str, dict[str, Any] | None]:
                u = f"https://{host}/vol{vol}/part{part}/{sku_int}/info/ru/card.json"
                res = await self._fetch_json_url(u, headers=req_headers, timeout_override=3.2)
                if isinstance(res, dict) and "_http_status" not in res and (
                    res.get("imt_name") or res.get("subj_name") or res.get("nm_id") or res.get("imt_id")
                ):
                    return host, res
                return host, None

            results = await asyncio.gather(*(_probe_host(h) for h in batch), return_exceptions=True)
            for item in results:
                if isinstance(item, tuple) and item[1] is not None:
                    found_host, found_card = item
                    WB_BASKET_VOL_CACHE[vol] = found_host
                    self._save_wb_baskets_cache_to_disk()
                    log(f"Найден рабочий шард корзины WB для vol={vol} (арт. {sku_int}): {found_host}")
                    return found_host, found_card

        return primary_host, None

    async def _fetch_wb_basket_card_extras(
        self,
        sku: str,
        *,
        rub_to_byn_rate: float = 0.0345,
    ) -> tuple[str, str, float | None, float | None, str, float | None, int | None, bool]:
        """Получает с сервера корзины Wildberries (basket-XX.wbbasket.ru):
        - полное название и бренд (info/ru/card.json)
        - среднюю цену за месяц и последнюю известную цену (info/price-history.json)
        - ссылку на главное фото карточки товара (images/big/1.webp)
        - рейтинг и число отзывов через публичный CDN отзывов (feedbacks1/2.wb.ru) при наличии imt_id
        - флаг card_exists (подтверждение существования карточки товара на CDN)
        """
        try:
            sku_int = int(sku)
        except ValueError:
            return "", "", None, None, "", None, None, False

        vol = sku_int // 100_000
        part = sku_int // 1_000

        req_headers = {
            "User-Agent": BROWSER_USER_AGENT,
            "Accept": "application/json",
            "Origin": "https://www.wildberries.by",
            "Referer": f"https://www.wildberries.by/catalog/{sku_int}/detail.aspx",
        }

        basket_host, card_data = await self._resolve_wb_basket_host(sku_int, req_headers)
        base_path = f"https://{basket_host}/vol{vol}/part{part}/{sku_int}"
        history_json_url = f"{base_path}/info/price-history.json"
        image_url = f"{base_path}/images/big/1.webp"

        title = ""
        brand_name = ""
        imt_id = None
        card_exists = False

        if isinstance(card_data, dict) and "_http_status" not in card_data:
            card_exists = True
            imt_id = card_data.get("imt_id")
            imt_name = str(card_data.get("imt_name") or card_data.get("subj_name") or "").strip()
            selling = card_data.get("selling")
            if isinstance(selling, dict):
                brand_name = str(selling.get("brand_name") or "").strip()
            if not brand_name:
                brand_name = str(card_data.get("brand_name") or "").strip()
            title = clean_product_title(imt_name, brand_name)

        async def _fetch_feedbacks_fast() -> tuple[float | None, int | None]:
            if not imt_id:
                return None, None
            for fb_host in ("feedbacks1.wb.ru", "feedbacks2.wb.ru"):
                fb_url = f"https://{fb_host}/feedbacks/v2/{imt_id}"
                fb_data = await self._fetch_json_url(fb_url, headers=req_headers, timeout_override=1.2)
                if isinstance(fb_data, dict) and "_http_status" not in fb_data:
                    r_val: float | None = None
                    c_val: int | None = None
                    with suppress(TypeError, ValueError):
                        val = fb_data.get("valuation")
                        if val is not None and float(val) > 0:
                            r_val = float(val)
                    with suppress(TypeError, ValueError):
                        cnt = fb_data.get("feedbackCount") or fb_data.get("feedbackCountWithText")
                        if cnt is not None and int(cnt) >= 0:
                            c_val = int(cnt)
                    if r_val is not None or c_val is not None:
                        return r_val, c_val
            return None, None

        history_data, (rating, reviews_count) = await asyncio.gather(
            self._fetch_json_any(history_json_url, headers=req_headers, timeout_override=2.5),
            _fetch_feedbacks_fast(),
        )

        avg_month_price_byn = None
        latest_history_price_byn = None
        if isinstance(history_data, list):
            avg_month_price_byn = parse_wb_price_history_avg(
                history_data,
                rub_to_byn_rate=rub_to_byn_rate,
            )
            latest_history_price_byn = parse_wb_price_history_latest(
                history_data,
                rub_to_byn_rate=rub_to_byn_rate,
            )

        return (
            title,
            brand_name,
            avg_month_price_byn,
            latest_history_price_byn,
            image_url,
            rating,
            reviews_count,
            card_exists,
        )

    async def fetch_wb_card_data(
        self,
        sku: str,
        messages: Iterable = (),
        source_text: str = "",
    ) -> ProductCardData | None:
        """Парсит карточку товара Wildberries: название, актуальная цена в BYN, средняя цена за месяц, фото и наличие.

        Запускает запрос к Basket CDN (название, история цен, фото) и к живому эндпоинту цен card.wb.ru параллельно,
        чтобы минимизировать задержку перед публикацией.
        """
        rub_rate = await self.get_rub_to_byn_rate()
        wb_headers = {
            "User-Agent": BROWSER_USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ru-RU,ru;q=0.9",
            "Origin": "https://www.wildberries.by",
            "Referer": f"https://www.wildberries.by/catalog/{sku}/detail.aspx",
            "x-requested-with": "XMLHttpRequest",
        }

        primary_detail_url = (
            f"https://card.wb.ru/cards/v4/detail"
            f"?appType=1&curr=byn&dest={self.settings.wb_dest_byn}&spp={self.settings.wb_spp}&nm={sku}"
        )

        # 1. Параллельно запрашиваем Basket CDN и основной JSON API цен card.wb.ru
        (
            (
                basket_title,
                basket_brand,
                avg_month_byn,
                latest_history_byn,
                basket_img,
                basket_rating,
                basket_reviews,
                card_exists,
            ),
            primary_detail_payload,
        ) = await asyncio.gather(
            self._fetch_wb_basket_card_extras(sku, rub_to_byn_rate=rub_rate),
            self._fetch_json_url(primary_detail_url, headers=wb_headers, timeout_override=2.8),
        )

        card_data: ProductCardData | None = None
        if basket_title or basket_img or avg_month_byn is not None:
            card_data = ProductCardData(
                title=basket_title,
                brand=basket_brand,
                avg_month_price_byn=avg_month_byn,
                image_url=basket_img,
                rating=basket_rating,
                reviews_count=basket_reviews,
            )

        if primary_detail_payload and "_http_status" not in primary_detail_payload:
            fast_parsed = parse_wb_detail_payload(
                primary_detail_payload,
                sku=sku,
                currency="BYN",
                rub_to_byn_rate=rub_rate,
            )
            if fast_parsed is not None and fast_parsed.in_stock is not False:
                card_data = merge_card_data(fast_parsed, card_data)
                if card_data and card_data.title and card_data.price_byn is not None:
                    return card_data

        card_data: ProductCardData | None = None
        if basket_title or basket_img or avg_month_byn is not None:
            card_data = ProductCardData(
                title=basket_title,
                brand=basket_brand,
                avg_month_price_byn=avg_month_byn,
                image_url=basket_img,
                rating=basket_rating,
                reviews_count=basket_reviews,
            )

        # 2. Опрашиваем JSON API карточек (v4 / v2) на всех хостах и регионах (BYN и RUB)
        dest_candidates = (self.settings.wb_dest_byn, -59208, -59202, -1257786)
        unique_dests = list(dict.fromkeys(dest_candidates))

        api_hosts = (
            ("https://card.wb.ru/cards/v4/detail", "https://www.wildberries.by"),
            ("https://card.wb.ru/cards/v2/detail", "https://www.wildberries.by"),
            ("https://www.wildberries.by/__internal/card/cards/v4/detail", "https://www.wildberries.by"),
            ("https://www.wildberries.ru/__internal/card/cards/v4/detail", "https://www.wildberries.ru"),
            ("https://u-card.wb.ru/cards/v4/detail", "https://www.wildberries.by"),
        )

        explicit_oos_responses = 0
        successful_api_responses = 0
        blocked_hosts: set[str] = set()

        for base_endpoint, origin_url in api_hosts:
            host_key = urlparse(base_endpoint).netloc
            if host_key in blocked_hosts:
                continue

            headers = dict(wb_headers)
            headers["Origin"] = origin_url
            headers["Referer"] = f"{origin_url}/catalog/{sku}/detail.aspx"

            for dest in unique_dests:
                detail_url = (
                    f"{base_endpoint}"
                    f"?appType=1&curr=byn&dest={dest}&spp={self.settings.wb_spp}&nm={sku}"
                )
                payload = await self._fetch_json_url(detail_url, headers=headers, timeout_override=5.0)
                if not payload:
                    continue
                http_status = payload.get("_http_status")
                if http_status in {403, 429, 404}:
                    if http_status in {403, 429}:
                        blocked_hosts.add(host_key)
                    break

                parsed = parse_wb_detail_payload(
                    payload,
                    sku=sku,
                    currency="BYN",
                    rub_to_byn_rate=rub_rate,
                )
                if parsed is not None:
                    successful_api_responses += 1
                    if parsed.in_stock is False:
                        explicit_oos_responses += 1
                    else:
                        card_data = merge_card_data(parsed, card_data)
                        if card_data and card_data.title and card_data.price_byn is not None:
                            return card_data

            if card_data and card_data.title and card_data.price_byn is not None:
                return card_data

            # Пробуем также RUB-витрину (Москва -1257786 и 123585595), если для BYN цены не оказалось
            if host_key not in blocked_hosts:
                for rub_dest in (-1257786, 123585595):
                    rub_url = (
                        f"{base_endpoint}"
                        f"?appType=1&curr=rub&dest={rub_dest}&spp={self.settings.wb_spp}&nm={sku}"
                    )
                    payload_rub = await self._fetch_json_url(rub_url, headers=headers, timeout_override=5.0)
                    if not payload_rub:
                        continue
                    if payload_rub.get("_http_status") in {403, 429, 404}:
                        if payload_rub.get("_http_status") in {403, 429}:
                            blocked_hosts.add(host_key)
                        break
                    parsed_rub = parse_wb_detail_payload(
                        payload_rub,
                        sku=sku,
                        currency="RUB",
                        rub_to_byn_rate=rub_rate,
                    )
                    if parsed_rub is not None:
                        successful_api_responses += 1
                        if parsed_rub.in_stock is False:
                            explicit_oos_responses += 1
                        else:
                            card_data = merge_card_data(parsed_rub, card_data)
                            if card_data and card_data.title and card_data.price_byn is not None:
                                return card_data

        # 3. Пробуем поисковый API WB (search.wb.ru/exactmatch/ru/{shard}/v18/search и v9/search) по артикулу
        if card_data is None or card_data.price_byn is None:
            search_shards = ("common", "preset", "msk")
            search_blocked = False
            for api_v in ("v18", "v9"):
                if search_blocked:
                    break
                for shard in search_shards:
                    for curr_code, s_dest in (("byn", self.settings.wb_dest_byn), ("rub", -1257786)):
                        search_url = (
                            f"https://search.wb.ru/exactmatch/ru/{shard}/{api_v}/search"
                            f"?appType=1&curr={curr_code}&dest={s_dest}&page=1&query={sku}"
                            f"&resultset=catalog&sort=popular&spp={self.settings.wb_spp}"
                        )
                        s_payload = await self._fetch_json_url(search_url, headers=wb_headers, timeout_override=5.0)
                        if not s_payload:
                            continue
                        s_status = s_payload.get("_http_status")
                        if s_status == 403:
                            search_blocked = True
                            break
                        if s_status in {429, 404}:
                            continue
                        parsed_search = parse_wb_detail_payload(
                            s_payload,
                            sku=sku,
                            currency=curr_code.upper(),
                            rub_to_byn_rate=rub_rate,
                        )
                        if parsed_search is not None and parsed_search.price_byn is not None:
                            card_data = merge_card_data(parsed_search, card_data)
                            if card_data and card_data.title and card_data.price_byn is not None:
                                return card_data

        # Если API карточек успешно ответили (HTTP 200) на нескольких регионах и везде подтвердили отсутствие товара
        if successful_api_responses >= 2 and explicit_oos_responses == successful_api_responses and (
            card_data is None or card_data.price_byn is None
        ):
            return replace(card_data, in_stock=False, price_byn=None) if card_data else ProductCardData(in_stock=False)

        # 4. Браузерный резолвер Playwright (обходит блокировку 403 на card.wb.ru и читает живой DOM карточки WB)
        if self.browser_resolver.is_available and (card_data is None or card_data.price_byn is None):
            browser_card, fresh_cookies = await self.browser_resolver.fetch_wb_product_card(
                sku,
                rub_to_byn_rate=rub_rate,
            )
            if fresh_cookies:
                self._sync_cookies_to_http_clients(fresh_cookies)
            if browser_card is not None:
                if browser_card.in_stock is False and (card_data is None or card_data.price_byn is None):
                    return replace(
                        merge_card_data(card_data, browser_card),
                        in_stock=False,
                        price_byn=None,
                    )
                card_data = merge_card_data(browser_card, card_data)
                if card_data and card_data.title and card_data.price_byn is not None:
                    return card_data

        # 5. Проверяем Telegram GetWebPageRequest и WebPage-превью исходного сообщения
        if self.user_client.is_connected() and "GetWebPageRequest" in globals() and (
            card_data is None or not card_data.title or card_data.price_byn is None
        ):
            for wb_host, def_curr in (("www.wildberries.by", "BYN"), ("www.wildberries.ru", "RUB")):
                target_url = f"https://{wb_host}/catalog/{sku}/detail.aspx"
                with suppress(Exception):
                    res = await self.user_client(GetWebPageRequest(url=target_url, hash=0))
                    webpage = getattr(res, "webpage", res)
                    wp_card = parse_webpage_preview_card(
                        webpage,
                        default_currency=def_curr,
                        rub_to_byn_rate=rub_rate,
                    )
                    if wp_card is not None:
                        if wp_card.in_stock is False and (card_data is None or card_data.price_byn is None):
                            return replace(merge_card_data(card_data, wp_card), in_stock=False, price_byn=None)
                        card_data = merge_card_data(card_data, wp_card)
                        if card_data and card_data.title and card_data.price_byn is not None:
                            return card_data

        wp_obj = match_message_webpage_obj(messages, "", sku, 1)
        if wp_obj:
            wp_card = parse_webpage_preview_card(wp_obj, default_currency="BYN", rub_to_byn_rate=rub_rate)
            if wp_card is not None:
                if wp_card.in_stock is False and (card_data is None or card_data.price_byn is None):
                    return replace(merge_card_data(card_data, wp_card), in_stock=False, price_byn=None)
                card_data = merge_card_data(card_data, wp_card)

        # 6. Если живой эндпоинт цен закрыт антиботом (403), но карточка существует и не помечена как «нет в наличии»:
        # используем последнюю известную цену из price-history.json или цену из текста поста
        if card_data is not None and card_data.in_stock is not False and card_data.price_byn is None:
            fallback_price = latest_history_byn
            if fallback_price is None and source_text:
                fallback_price = extract_price_from_post_text(source_text, rub_to_byn_rate=rub_rate)
            if fallback_price is not None and fallback_price > 0:
                log(
                    f"Для товара Wildberries (арт. {sku}) использована резервная цена {fallback_price} BYN "
                    f"(история цен / текст поста)"
                )
                card_data = replace(card_data, price_byn=fallback_price, in_stock=True)
            elif card_exists and card_data.title:
                card_data = replace(card_data, in_stock=True)

        return card_data

    async def _fetch_ozon_price_drop_modal_avg(
        self,
        host: str,
        modal_path: str,
        *,
        actual_price_byn: float | None,
        default_currency: str,
        rub_to_byn_rate: float,
    ) -> float | None:
        """Быстро дозапрашивает JSON модального окна «Стало дешевле» (до 1.2 сек) и извлекает среднюю цену за месяц."""
        if not modal_path:
            return None
        encoded_modal = quote(modal_path, safe="/?=&")
        modal_endpoints = (
            f"https://www.{host}/api/entrypoint-api.bx/page/json/v2?url={encoded_modal}",
            f"https://api.{host}/composer-api.bx/page/json/v2?url={encoded_modal}",
        )
        for m_url in modal_endpoints:
            m_payload = await self._fetch_json_url(m_url, headers=OZON_APP_HEADERS, timeout_override=1.2)
            if not m_payload or m_payload.get("_http_status") in {403, 404, 429}:
                continue
            m_card = parse_ozon_composer_payload(
                m_payload,
                default_currency=default_currency,
                rub_to_byn_rate=rub_to_byn_rate,
            )
            if m_card and m_card.avg_month_price_byn is not None:
                return m_card.avg_month_price_byn
            ws = m_payload.get("widgetStates")
            if isinstance(ws, dict):
                for raw_st in ws.values():
                    if isinstance(raw_st, str) and ("средняя за" in raw_st.casefold() or "стало дешевле" in raw_st.casefold()):
                        avg_val = extract_avg_month_price_from_text(
                            raw_st,
                            actual_price_byn=actual_price_byn,
                            default_currency=default_currency,
                            rub_to_byn_rate=rub_to_byn_rate,
                        )
                        if avg_val is not None:
                            return avg_val
        return None

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

        # 1. Пробуем JSON API Ozon (entrypoint-api / composer-api) сначала на ozon.by
        storefronts = ["ozon.by"]
        for d in self.settings.ozon_preferred_storefronts:
            if d not in storefronts:
                storefronts.append(d)

        for host in storefronts:
            default_curr = "BYN" if host == "ozon.by" else "RUB"
            endpoints = (
                f"https://www.{host}/api/entrypoint-api.bx/page/json/v2?url={encoded_path}",
                f"https://api.{host}/composer-api.bx/page/json/v2?url={encoded_path}",
                f"https://www.{host}/api/composer-api.bx/page/json/v2?url={encoded_path}",
            )
            for ep_url in endpoints:
                payload = await self._fetch_json_url(
                    ep_url,
                    headers=OZON_APP_HEADERS,
                    timeout_override=3.5,
                )
                if not payload:
                    continue
                http_st = payload.get("_http_status")
                if http_st == 404:
                    return ProductCardData(in_stock=False)
                if http_st in {403, 429}:
                    continue

                parsed = parse_ozon_composer_payload(
                    payload,
                    default_currency=default_curr,
                    rub_to_byn_rate=rub_rate,
                )
                if parsed is not None:
                    card_data = merge_card_data(card_data, parsed)
                    if card_data.in_stock is False:
                        return card_data

                    # Если базовая карточка получена
                    if card_data.title and card_data.price_byn is not None and card_data.image_url:
                        # Случай А: средняя цена за месяц уже найдена в JSON
                        if card_data.avg_month_price_byn is not None:
                            return card_data

                        # Случай Б: у товара в JSON вообще нет плашки «Стало дешевле» — выходим мгновенно без потери времени!
                        if not card_data.has_price_drop_badge:
                            return card_data

                        # Случай В: плашка «Стало дешевле» есть, пробуем быстро (до 1.2 с) дозапросить JSON модального окна
                        if card_data.price_drop_modal_url:
                            modal_avg = await self._fetch_ozon_price_drop_modal_avg(
                                host,
                                card_data.price_drop_modal_url,
                                actual_price_byn=card_data.price_byn,
                                default_currency=default_curr,
                                rub_to_byn_rate=rub_rate,
                            )
                            if modal_avg is not None:
                                return replace(card_data, avg_month_price_byn=modal_avg)

                        # Не перебираем остальные эндпоинты и витрины (.ru/.kz), так как карточка на текущей витрине уже получена
                        break

            if card_data and card_data.title and card_data.price_byn is not None and card_data.image_url:
                break

        # 2. Обращаемся к прогретому браузеру ТОЛЬКО если карточка не получена по HTTP
        #    или если у товара подтверждена плашка «Стало дешевле» (has_price_drop_badge=True), но цифра ещё не извлечена
        need_full_card = (
            card_data is None
            or not card_data.title
            or card_data.price_byn is None
            or not card_data.image_url
        )
        need_only_avg_badge = (
            not need_full_card
            and card_data is not None
            and card_data.has_price_drop_badge
            and card_data.avg_month_price_byn is None
        )

        if self.browser_resolver.is_available and (need_full_card or need_only_avg_badge):
            browser_card, fresh_cookies = await self.browser_resolver.fetch_product_card(
                sku,
                rub_to_byn_rate=rub_rate,
                only_avg_price=need_only_avg_badge,
                existing_card=card_data,
            )
            if fresh_cookies:
                self._sync_cookies_to_http_clients(fresh_cookies)
            if browser_card is not None:
                card_data = merge_card_data(card_data, browser_card)
                if card_data.in_stock is False:
                    return card_data
                if card_data.title and card_data.price_byn is not None and card_data.image_url:
                    return card_data

        # 3. Пробуем прямой HTML карточки на ozon.by через curl_cffi
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

        # 4. Пробуем Telegram GetWebPageRequest для https://ozon.by/product/<sku>/ и превью сообщения
        if self.user_client.is_connected() and "GetWebPageRequest" in globals() and (
            card_data is None or not card_data.title or card_data.price_byn is None
        ):
            for host in ("ozon.by", "www.ozon.ru"):
                default_curr = "BYN" if "ozon.by" in host else "RUB"
                target_url = f"https://{host}/product/{sku}/"
                with suppress(Exception):
                    res = await self.user_client(GetWebPageRequest(url=target_url, hash=0))
                    webpage = getattr(res, "webpage", res)
                    wp_card = parse_webpage_preview_card(
                        webpage,
                        default_currency=default_curr,
                        rub_to_byn_rate=rub_rate,
                    )
                    if wp_card is not None:
                        card_data = merge_card_data(card_data, wp_card)
                        if card_data.in_stock is False or (card_data.title and card_data.price_byn is not None):
                            return card_data

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
        source_text: str = "",
    ) -> ProductInfo | None:
        """Загружает карточку товара, проверяет наличие и обогащает ProductInfo названием, ценой в BYN, средней ценой и фото.

        Возвращает None, если товара нет в наличии или карточка недоступна.
        """
        if not product.sku:
            return None if self.settings.require_sku else product

        if product.platform == "Wildberries":
            card = await self.fetch_wb_card_data(
                product.sku,
                messages=messages,
                source_text=source_text,
            )
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
            # Если для Wildberries публичный API цен заблокирован (403), но карточка товара подтверждена на CDN/в браузере
            # и товар не числится отсутствующим, публикуем карточку без строки цены вместо ложной отмены
            if (
                product.platform == "Wildberries"
                and card is not None
                and card.in_stock is True
                and (card.title or product.title)
            ):
                log(
                    f"Для товара Wildberries (арт. {product.sku}) API цен временно недоступен, "
                    f"но карточка активна — публикуем без указания цены"
                )
            else:
                log(
                    f"Для товара {product.platform} (арт. {product.sku}) не найдена актуальная цена / нет в наличии — "
                    f"публикация отменена"
                )
                return None

        if card is None:
            return product

        final_img_url = card.image_url or product.image_url
        preloaded_img_bytes: bytes | None = None
        if self.settings.prefer_card_image and final_img_url:
            img_buf = await self.download_product_image(final_img_url)
            if img_buf is not None:
                preloaded_img_bytes = img_buf.getvalue()

        return replace(
            product,
            title=card.title or product.title,
            brand=card.brand or product.brand,
            price_byn=card.price_byn,
            avg_month_price_byn=card.avg_month_price_byn,
            old_price_byn=card.old_price_byn,
            image_url=final_img_url,
            image_bytes=preloaded_img_bytes,
            in_stock=card.in_stock is not False,
            rating=card.rating,
            reviews_count=card.reviews_count,
        )

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
                        if has_product_sku(next_url):
                            log(f"Ссылка раскрыта через curl_cffi ({domain}): {clean_url} -> {next_url}")
                            return next_url

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

        if "GetWebPageRequest" in globals():
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

        if "GetWebPagePreviewRequest" in globals():
            for target_url in candidate_urls[:2]:
                with suppress(Exception):
                    media = await self.user_client(GetWebPagePreviewRequest(message=target_url))
                    webpage = getattr(media, "webpage", media)
                    extracted = extract_url_from_webpage_obj(webpage, clean_url)
                    if extracted:
                        log(f"Ссылка раскрыта через Telegram GetWebPagePreviewRequest: {clean_url} -> {extracted}")
                        return extracted

        return None

    async def upgrade_to_product_url(
        self,
        url: str,
        prefetched_body: bytes | None = None,
        *,
        skip_curl_cffi: bool = False,
    ) -> str:
        """Если в URL нет артикула (например, ozon.ru/p/oXXXX), ищем его через быстрый параллельный каскад обхода антибота."""
        if has_product_sku(url):
            return url

        clean_url = strip_antibot_params(url)
        host = normalize_host(urlparse(clean_url).hostname)
        if not (is_ozon_host(host) or is_wb_host(host)):
            return clean_url

        if prefetched_body:
            upgraded = self._extract_sku_from_html(prefetched_body, clean_url)
            if upgraded:
                self.state.http_resolved += 1
                return upgraded

        # Параллельно запускаем быстрые безбраузерные методы: Telegram WebPage + Ozon Mobile API
        fast_tasks = [
            asyncio.create_task(self._resolve_via_telegram_webpage(clean_url)),
            asyncio.create_task(self._resolve_via_ozon_mobile_api(clean_url)),
        ]
        try:
            done, pending = await asyncio.wait(fast_tasks, timeout=2.8, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                with suppress(Exception):
                    res_url = t.result()
                    if res_url and has_product_sku(res_url):
                        for p in pending:
                            p.cancel()
                        self.state.http_resolved += 1
                        return res_url
            if pending:
                done_rest, _ = await asyncio.wait(pending, timeout=1.5)
                for t in done_rest:
                    with suppress(Exception):
                        res_url = t.result()
                        if res_url and has_product_sku(res_url):
                            self.state.http_resolved += 1
                            return res_url
        finally:
            for t in fast_tasks:
                if not t.done():
                    t.cancel()

        if not skip_curl_cffi and self.curl_session is not None:
            via_curl = await self._resolve_via_curl_cffi(clean_url)
            if via_curl and has_product_sku(via_curl):
                self.state.http_resolved += 1
                return via_curl

        if self.browser_resolver.is_available:
            via_browser, fresh_cookies = await self.browser_resolver.resolve_short_url(clean_url)
            if fresh_cookies:
                self._sync_cookies_to_http_clients(fresh_cookies)
            if via_browser and has_product_sku(via_browser):
                self.state.browser_resolved += 1
                return via_browser

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

        curl_attempted = False
        if (
            is_ozon_host(parsed_initial.hostname)
            and OZON_SHORT_PATH_RE.match(parsed_initial.path or "")
        ):
            if self.curl_session is not None:
                curl_attempted = True
                via_curl = await self._resolve_via_curl_cffi(clean_initial_url)
                if via_curl and has_product_sku(via_curl):
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

        return await self.upgrade_to_product_url(
            clean_initial_url,
            prefetched_body=last_body,
            skip_curl_cffi=curl_attempted,
        )

    async def fetch_folder_channels(self) -> list[int]:
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
            return []

        include_peers = list(getattr(matched, "include_peers", []))
        pinned_peers = list(getattr(matched, "pinned_peers", []))
        log(
            f"Папка «{self.settings.folder_name}»: "
            f"include_peers={len(include_peers)}, pinned_peers={len(pinned_peers)}"
        )

        seen: set[int] = set()
        source_ids: list[int] = []

        for peer in include_peers:
            peer_id = get_peer_id(peer)
            if peer_id in seen:
                continue
            seen.add(peer_id)

            if peer_id == self.target_peer_id:
                log(f"Целевой канал {peer_id} исключён из источников", logging.WARNING)
                continue

            source_ids.append(peer_id)

        return source_ids

    async def update_sources(self) -> int:
        source_ids = await self.fetch_folder_channels()
        self.current_sources = set(source_ids)
        with suppress(Exception):
            self.user_client.remove_event_handler(self.incoming_post_handler)
        if self.current_sources:
            self.user_client.add_event_handler(
                self.incoming_post_handler,
                events.NewMessage(chats=list(self.current_sources)),
            )
        log(f"Источники обновлены: {len(self.current_sources)}")
        if not self.current_sources:
            log("Нет доступных источников: проверьте папку и её явно добавленные чаты", logging.WARNING)
        return len(self.current_sources)

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
        await asyncio.sleep(0.2)
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

        text_skus = extract_skus_from_text(source_text)
        pre_resolved: dict[int, str] = {}
        urls_to_resolve: list[tuple[int, str]] = []

        for idx, original_url in enumerate(candidate_links):
            if has_product_sku(original_url):
                pre_resolved[idx] = original_url
                continue

            preview_url = match_message_preview_url(job.messages, original_url, len(candidate_links))
            if preview_url and has_product_sku(preview_url):
                log(f"Используем URL из Telegram-превью без сетевого запроса для {original_url}: {preview_url}")
                pre_resolved[idx] = preview_url
                continue

            matched_sku = None
            if text_skus:
                if len(candidate_links) == 1 and len(text_skus) == 1:
                    matched_sku = text_skus[0]
                elif len(text_skus) == len(candidate_links):
                    matched_sku = text_skus[idx]

            if matched_sku:
                host = normalize_host(urlparse(original_url).hostname)
                if is_ozon_host(host):
                    canonical_host = resolve_ozon_canonical_host(host, self.settings.ozon_canonical_host)
                    pre_resolved[idx] = f"https://{canonical_host}/product/{matched_sku}/"
                else:
                    pre_resolved[idx] = f"https://www.wildberries.by/catalog/{matched_sku}/detail.aspx"
                log(f"Артикул {matched_sku} взят из текста поста без сетевого запроса для {original_url}")
                continue

            urls_to_resolve.append((idx, original_url))

        if urls_to_resolve:
            net_results = await asyncio.gather(
                *(self.resolve_url(url) for _, url in urls_to_resolve),
                return_exceptions=True,
            )
            for (idx, original_url), result in zip(urls_to_resolve, net_results):
                if isinstance(result, Exception):
                    log(f"Ошибка раскрытия {original_url}: {result}", logging.WARNING)
                elif result:
                    pre_resolved[idx] = result

        products: list[ProductInfo] = []

        for idx, original_url in enumerate(candidate_links):
            resolved_url = pre_resolved.get(idx)
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

        # Ранняя дедупликация ДО парсинга карточек товаров
        source_message_id = min(message.id for message in job.messages)
        fresh_keys = self.db.reserve_fresh(products, job.source_chat_id, source_message_id)
        if not fresh_keys:
            self.state.duplicates += 1
            log(f"Все товары из сообщения {job.source_chat_id}/{source_message_id} уже обработаны")
            return

        fresh_candidates = [product for product in products if product.sku_key in fresh_keys]

        # Парсим карточки только для новых (не публиковавшихся ранее) товаров
        enriched_results = await asyncio.gather(
            *(
                self.enrich_product_with_card(
                    product,
                    messages=job.messages,
                    source_text=source_text if len(fresh_candidates) == 1 else "",
                )
                for product in fresh_candidates
            ),
            return_exceptions=True,
        )

        available_products: list[ProductInfo] = []
        failed_keys: list[str] = []
        for orig_prod, enriched in zip(fresh_candidates, enriched_results):
            if isinstance(enriched, Exception):
                log(
                    f"Ошибка парсинга карточки {orig_prod.platform} ({orig_prod.sku}): {enriched}",
                    logging.WARNING,
                )
                failed_keys.append(orig_prod.sku_key)
                continue
            if enriched is None:
                self.state.out_of_stock += 1
                failed_keys.append(orig_prod.sku_key)
                continue
            available_products.append(enriched)

        if failed_keys:
            self.db.release_reservations(failed_keys)

        if not available_products:
            log(
                f"В сообщении {job.source_chat_id} нет доступных товаров в наличии — публикация пропущена"
            )
            return

        fresh_products = available_products
        fresh_keys = {product.sku_key for product in fresh_products}
        text = build_publication_text(
            source_text,
            fresh_products,
            include_source_notes=self.settings.include_source_notes,
        )

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
            missing_urls: list[str] = []
            for p in products:
                if p.image_bytes:
                    buf = io.BytesIO(p.image_bytes)
                    buf.name = "product.jpg"
                    card_images.append(buf)
                elif p.image_url:
                    missing_urls.append(p.image_url)
            if missing_urls:
                downloaded = await asyncio.gather(
                    *(self.download_product_image(url) for url in missing_urls),
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
        return (
            "🤖 Панель управления парсером\n\n"
            f"Статус: {status}{mode}\n"
            f"Очередь: {self.queue.qsize()}/{self.queue.maxsize}\n"
            f"Аптайм: {hours}ч {minutes}м {seconds}с\n\n"
            f"🛡 Антибот и карточки:\n"
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
        return [
            [Button.inline("📊 Обновить статус", b"refresh"), pause_button],
            [Button.inline("🔄 Обновить источники", b"reload_sources")],
            [Button.inline("🧹 Очистить дедупликацию", b"clear_db_confirm")],
        ]

    async def notify_admin(self, text: str) -> None:
        with suppress(Exception):
            await self.bot_client.send_message(self.settings.admin_id, text, link_preview=False)

    def _register_admin_handlers(self) -> None:
        @self.bot_client.on(events.NewMessage(chats=self.settings.admin_id, pattern=r"^/start$"))
        async def admin_start(event):
            if event.sender_id != self.settings.admin_id:
                return
            await event.respond(self.get_stats_text(), buttons=self.get_admin_keyboard())

        @self.bot_client.on(events.CallbackQuery(chats=self.settings.admin_id))
        async def admin_callback(event):
            if event.sender_id != self.settings.admin_id:
                await event.answer("Недостаточно прав", alert=True)
                return
            data = event.data
            if data == b"refresh":
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
            elif data == b"reload_sources":
                await self.update_sources()
                await event.edit(self.get_stats_text(), buttons=self.get_admin_keyboard())
                await event.answer("Источники обновлены")
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
                f"Активирован браузерный резолвер Ozon (Playwright Chromium, "
                f"витрины: {', '.join(self.settings.ozon_preferred_storefronts)})"
            )
            async def _bg_warmup() -> None:
                with suppress(Exception):
                    warm_cookies = await self.browser_resolver.warmup()
                    if warm_cookies:
                        self._sync_cookies_to_http_clients(warm_cookies)
            asyncio.create_task(_bg_warmup(), name="ozon-browser-initial-warmup")
        else:
            log(
                "Playwright недоступен; для гарантированного обхода JS-антибота Ozon "
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

            await self.update_sources()
            self.workers = [
                asyncio.create_task(self.process_worker(number), name=f"parser-worker-{number}")
                for number in range(1, self.settings.worker_count + 1)
            ]
            await self.bot_client.send_message(
                self.settings.admin_id,
                "🟢 Парсер запущен и слушает источники.",
                buttons=self.get_admin_keyboard(),
            )
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
            self.db.close()
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
    app = ParserApp(settings)
    await app.run()


if __name__ == "__main__":
    asyncio.run(main())