import asyncio
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
from dataclasses import dataclass, field
from typing import Iterable
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

ALLOWED_DOMAINS = frozenset({"wildberries.ru", "wb.ru", "ozon.ru", "ozon.by", "ozon.kz"})
OZON_DOMAINS = ("ozon.ru", "ozon.by", "ozon.kz")

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
# (Variti проверяет Object.getOwnPropertyNames(navigator).length === 0 и [native code] у WebGL)
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
HEAD_READ_LIMIT = 350_000

URL_RE = re.compile(r"https?://[^\s<>\[\]\(\)\"']+", re.IGNORECASE)

# Поддержка любых поддоменов wildberries.ru и короткого домена wb.ru
WB_SKU_RE = re.compile(
    r"(?:[a-z0-9-]+\.)?(?:wildberries\.ru|wb\.ru)/catalog/(\d+)",
    re.IGNORECASE,
)

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

# Заведомо нетоварные разделы (магазины, продавцы, бренды, категории, подборки),
# которые не нужно пытаться публиковать как карточку товара
NON_PRODUCT_PATH_RE = re.compile(
    r"^/(?:s|seller|brand|Brands|category|highlight|search|actions|special)/",
    re.IGNORECASE,
)

PROMO_RE = re.compile(
    r"(мы в тг|мы в telegram|мы в вк|подпишись|наш канал|источник:|t\.me/|vk\.com/)",
    re.IGNORECASE,
)

NOISE_PHRASES = (
    "мы в тг",
    "мы в telegram",
    "мы в вк",
    "мы во вконтакте",
    "подпишись",
    "подписаться",
    "подписка на канал",
    "наш канал",
    "наш телеграм",
    "телеграм канал",
    "telegram канал",
    "ссылка = клик",
    "ссылка клик",
    "клик по артикулу",
    "кликни по артикулу",
    "жми по артикулу",
    "жми на артикул",
    "ссылка выше",
    "жми сюда",
    "нажми сюда",
    "ставь реакцию",
    "поставь реакцию",
    "ставь лайк",
    "поставь лайк",
    "хочу больше подобного",
    "обе карточки",
    "дешевле нет",
    "скидки каждый день",
    "акции каждый день",
)

REACTION_RE = re.compile(r"^(круто|хочу больше|хочу ещё|хочу еще|лайк|огонь)\b")

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
    ozon_canonical_host: str = ""
    ozon_preferred_storefronts: tuple[str, ...] = ("ozon.by", "ozon.ru", "ozon.kz")
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
            ozon_canonical_host=os.getenv("OZON_CANONICAL_HOST", "").strip().lower(),
            ozon_preferred_storefronts=storefronts,
        )


@dataclass(frozen=True)
class ProductInfo:
    original_url: str
    resolved_url: str
    platform: str
    sku: str | None
    canonical_url: str
    sku_key: str


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
    rejected: int = 0
    errors: int = 0
    queue_dropped: int = 0
    browser_resolved: int = 0
    http_resolved: int = 0


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
    """Определяет зацикливание антибота Ozon.

    Первый переход на ?__rr=1 (с установкой cookie) разрешается, а переход
    на ?__rr=2 и выше (или повторный переход на тот же __rr=1) считается петлёй.
    """
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


def extract_product_url_from_text(raw_text: str, base_url: str) -> str | None:
    """Извлекает ссылку на товар или SKU из HTML, JSON (composer-api) или редиректа."""
    if not raw_text:
        return None

    normalized_variants = [raw_text]
    if "\\/" in raw_text:
        normalized_variants.append(raw_text.replace("\\/", "/"))
    if "%2F" in raw_text or "%2f" in raw_text:
        with suppress(Exception):
            normalized_variants.append(unquote(raw_text))

    canonical_host = resolve_ozon_canonical_host(urlparse(base_url).hostname)

    for variant in normalized_variants:
        # 1. Ищем полный путь вида /product/<slug>-<sku>/...
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

        # 2. Ищем SKU в <head> и затем во всём теле
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


def extract_skus_from_text(text: str) -> list[str]:
    """Находит явно указанные артикулы в тексте поста."""
    if not text:
        return []
    return list(dict.fromkeys(TEXT_SKU_RE.findall(text)))


def normalize_filter_text(text: str) -> str:
    """Убирает emoji и знаки препинания, приводит к нижнему регистру."""
    text = text.casefold()
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def clean_external_promo(text: str) -> str:
    cleaned_lines: list[str] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        normalized = normalize_filter_text(line)

        if PROMO_RE.search(line):
            continue

        if any(phrase in normalized for phrase in NOISE_PHRASES):
            continue

        if not re.search(r"[A-Za-zА-Яа-яЁё0-9]", line):
            continue

        if len(normalized) <= 35 and REACTION_RE.match(normalized):
            continue

        cleaned_lines.append(line)

    return "\n".join(cleaned_lines).strip()


def resolve_ozon_canonical_host(hostname: str | None, preferred_override: str = "") -> str:
    if preferred_override and preferred_override in OZON_DOMAINS:
        return preferred_override
    host = normalize_host(hostname)
    for domain in OZON_DOMAINS:
        if host == domain or host.endswith("." + domain):
            return domain
    return "ozon.ru"


def parse_product_info(
    original_url: str,
    resolved_url: str,
    canonical_host_override: str = "",
) -> ProductInfo:
    wb_match = WB_SKU_RE.search(resolved_url)
    if wb_match:
        sku = wb_match.group(1)
        return ProductInfo(
            original_url=original_url,
            resolved_url=resolved_url,
            platform="Wildberries",
            sku=sku,
            canonical_url=f"https://www.wildberries.ru/catalog/{sku}/detail.aspx",
            sku_key=f"wildberries:{sku}",
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
            canonical_url=f"https://www.{canonical_host}/product/{sku}/",
            sku_key=f"ozon:{sku}",
        )

    parsed = urlparse(resolved_url)
    canonical_url = urlunparse(parsed._replace(query="", fragment=""))
    host = normalize_host(parsed.hostname)
    platform = (
        "Wildberries"
        if host == "wb.ru" or host.endswith(".wb.ru") or host == "wildberries.ru" or host.endswith(".wildberries.ru")
        else "Ozon"
    )
    return ProductInfo(
        original_url=original_url,
        resolved_url=resolved_url,
        platform=platform,
        sku=None,
        canonical_url=canonical_url,
        sku_key=f"{platform.lower()}:raw:{canonical_url}",
    )


def build_publication_text(source_text: str, products: list[ProductInfo]) -> str:
    source_text = clean_external_promo(source_text)

    def remove_marketplace_url(match: re.Match) -> str:
        raw_url = match.group(0)
        clean_url = raw_url.rstrip(".,;:!?)>\"'")
        if validate_marketplace_url(clean_url):
            return raw_url[len(clean_url):]
        return raw_url

    source_text = URL_RE.sub(remove_marketplace_url, source_text)
    source_text = re.sub(r"[ \t]+\n", "\n", source_text)
    source_text = re.sub(r"\n{3,}", "\n\n", source_text).strip()

    lines: list[str] = []

    if source_text:
        lines.append(source_text)
    elif len(products) == 1:
        lines.append(f"🔥 Новая находка на {products[0].platform}")
    elif len(products) > 1:
        lines.append(f"🔥 Подборка товаров: {len(products)} шт.")

    if products:
        product_lines = ["🛍 Товары"]
        for product in products:
            icon = "🟣" if product.platform == "Wildberries" else "🔵"
            product_lines.append(f"{icon} {product.platform}")
            if product.sku:
                product_lines.append(f"Артикул: {product.sku}")
            product_lines.append(product.canonical_url)
        lines.append("\n".join(product_lines))

    return "\n\n".join(lines).strip()


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
    """Браузерный резолвер коротких ссылок Ozon (/p/o..., /t/...) с обходом Variti antibot.

    Ключевые отличия:
    1) Использует новый режим `--headless=new` и нативный User-Agent самого Chromium
       через CDP `Emulation.setUserAgentOverride` (убирает `HeadlessChrome` на уровне C++,
       сохраняя полное совпадение ОС Linux/Windows, версии браузера, WebGL и `navigator`).
    2) Сначала прогревает сессию на главной странице витрины (`https://www.ozon.ru/` / `https://ozon.by/`),
       держит вкладку открытой и вызывает внутренний `entrypoint-api.bx` / `composer-api.bx` прямо из неё.
    3) Не ждёт 18 секунд впустую при `ERR_TOO_MANY_REDIRECTS` или жёстком блоке IP (`fab_chlg`),
       а сразу логирует точную диагностику и переходит к следующему методу.
    """

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
        """Применяет нативный C++ UserAgentOverride через CDP без расхождения ОС (Linux/Windows) и без HeadlessChrome."""
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
                # Важно: НЕ передаём чужой Windows User-Agent и extra_http_headers на Linux,
                # чтобы Variti не блокировал из-за несовпадения navigator.platform и Sec-CH-UA!
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
        """Открывает и прогревает главную страницу витрины (например https://www.ozon.ru/ или https://ozon.by/),
        чтобы получить cookie abt_data и держать страницу открытой для внутристраничных запросов.
        """
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

            # Если попали на страницу антибот-проверки, ждём её завершения (до browser_challenge_timeout сек)
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

            # Приоритет витрин: сначала ozon.by (на ней редирект часто проходит мягче, как в вашем браузере),
            # затем исходный домен (ozon.ru) и остальные из настроек.
            storefronts: list[str] = []
            for domain in (*self.settings.ozon_preferred_storefronts, orig_domain):
                if domain not in storefronts:
                    storefronts.append(domain)

            assert self._context is not None

            for domain in storefronts:
                # Шаг 1: Прогреваем главную страницу витрины и пробуем внутристраничный fetch API
                warm_page = await self._warm_up_storefront(domain)
                if warm_page is not None:
                    via_inpage = await self._fetch_via_inpage_api(warm_page, short_path, clean_url)
                    if via_inpage and has_product_sku(via_inpage):
                        cookies = await self._export_cookies()
                        log(f"Ссылка раскрыта через прогретый браузер ({domain} in-page): {clean_url} -> {via_inpage}")
                        return via_inpage, cookies

                # Шаг 2: Открываем короткую ссылку в отдельной вкладке с перехватом всех Location и защитой от петли __rr=2+
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
                    # Если антибот зациклился на ?__rr=2 и выше, не ждём ERR_TOO_MANY_REDIRECTS,
                    # а перенаправляем на ?__rr=1&abt_att=1 или прерываем цикл
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

                        # Если цикл __rr=2 прерван или страница упала в chrome-error, не тратим 18 секунд!
                        cur_url = nav_page.url or ""
                        if loop_aborted or cur_url.startswith(("chrome-error://", "about:blank")):
                            log(
                                f"Витрина {target_url} вернула цикл редиректа (cur_url={cur_url}); "
                                f"переходим к следующему варианту",
                                logging.DEBUG,
                            )
                            continue

                        # Если открылась страница челленджа на домене Ozon, ждём до 8 секунд
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
                            html = await nav_page.content()
                            extracted = extract_product_url_from_text(html, target_url)
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
        self._register_admin_handlers()

    def _sync_cookies_to_http_clients(self, cookies: list[dict]) -> None:
        """Переносит антибот-cookies (abt_data, __Secure-ab-group, xcid, rfuid) из браузера в curl_cffi и aiohttp."""
        if not cookies:
            return
        synced = 0
        for cookie in cookies:
            name = cookie.get("name")
            value = cookie.get("value")
            domain = (cookie.get("domain") or "").lstrip(".")
            if not name or value is None:
                continue
            synced += 1
            if self.curl_session is not None:
                with suppress(Exception):
                    self.curl_session.cookies.set(name, value, domain=domain or "ozon.ru")
                for oz_domain in OZON_DOMAINS:
                    with suppress(Exception):
                        self.curl_session.cookies.set(name, value, domain=oz_domain)
            if self.http_session is not None:
                for oz_domain in OZON_DOMAINS:
                    with suppress(Exception):
                        self.http_session.cookie_jar.update_cookies(
                            {name: value},
                            response_url=aiohttp.client.URL(f"https://www.{oz_domain}/"),
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
                    env_list.append({"name": k.strip(), "value": v.strip(), "domain": "ozon.ru"})
            self._sync_cookies_to_http_clients(env_list)

        if os.path.isfile(self.settings.cookies_cache_path):
            with suppress(Exception):
                with open(self.settings.cookies_cache_path, "r", encoding="utf-8") as f:
                    cached = json.load(f)
                if isinstance(cached, list) and cached:
                    self._sync_cookies_to_http_clients(cached)
                    log(f"Загружено {len(cached)} кэшированных антибот-cookies Ozon с диска")

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

        # Важно: не переопределяем User-Agent и Sec-CH-UA поверх curl_cffi(impersonate="chrome"),
        # чтобы не ломать совпадение TLS-отпечатка и версии Chrome внутри самой библиотеки curl_cffi
        for domain in storefronts:
            for use_www in (False, True):
                current_url = (
                    build_storefront_url(clean_url, domain, use_www=use_www)
                    if is_ozon_host(parsed.hostname)
                    else clean_url
                )
                referer: str | None = None

                for step in range(1, self.settings.max_redirects + 2):
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

    async def upgrade_to_product_url(self, url: str, prefetched_body: bytes | None = None) -> str:
        """Если в URL нет артикула (например, ozon.ru/p/oXXXX), ищем его через каскад обхода антибота."""
        if has_product_sku(url):
            return url

        clean_url = strip_antibot_params(url)
        host = normalize_host(urlparse(clean_url).hostname)
        if not is_ozon_host(host):
            return clean_url

        # 1. Проверяем уже прочитанное тело ответа
        if prefetched_body:
            upgraded = self._extract_sku_from_html(prefetched_body, clean_url)
            if upgraded:
                self.state.http_resolved += 1
                return upgraded

        # 2. Быстрая проверка через Telegram GetWebPageRequest (работает с IP серверов Telegram,
        # не зависит от блокировки IP вашего VPS со стороны антибота Ozon)
        via_tg = await self._resolve_via_telegram_webpage(clean_url)
        if via_tg and has_product_sku(via_tg):
            self.state.http_resolved += 1
            return via_tg

        # 3. Открываем ссылку в прогретом браузере Chromium (--headless=new + CDP native UA)
        if self.browser_resolver.is_available:
            via_browser, fresh_cookies = await self.browser_resolver.resolve_short_url(clean_url)
            if fresh_cookies:
                self._sync_cookies_to_http_clients(fresh_cookies)
            if via_browser and has_product_sku(via_browser):
                self.state.browser_resolved += 1
                return via_browser

        # 4. Пробуем curl_cffi со свежими cookies после прогрева браузера
        via_curl = await self._resolve_via_curl_cffi(clean_url)
        if via_curl and has_product_sku(via_curl):
            self.state.http_resolved += 1
            return via_curl

        # 5. Пробуем мобильное API Ozon (composer-api / entrypoint-api) со свежими cookies
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

        # Для коротких ссылок Ozon (/p/o..., /t/...) сначала пробуем быстрый curl_cffi
        if (
            is_ozon_host(parsed_initial.hostname)
            and OZON_SHORT_PATH_RE.match(parsed_initial.path or "")
        ):
            if self.curl_session is not None:
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

        results = await asyncio.gather(
            *(self.resolve_url(url) for url in candidate_links),
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

            # Фолбэк 1: проверяем WebPage-превью в самом сообщении Telegram
            if not resolved_url or not has_product_sku(resolved_url):
                preview_url = match_message_preview_url(job.messages, original_url, len(candidate_links))
                if preview_url:
                    log(f"Используем URL из Telegram-превью для {original_url}: {preview_url}")
                    resolved_url = preview_url

            # Фолбэк 2: если в тексте поста явно указан артикул («Артикул: 12345678»)
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
                        resolved_url = f"https://www.{canonical_host}/product/{matched_sku}/"
                    else:
                        resolved_url = f"https://www.wildberries.ru/catalog/{matched_sku}/detail.aspx"
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

        source_message_id = min(message.id for message in job.messages)
        fresh_keys = self.db.reserve_fresh(products, job.source_chat_id, source_message_id)
        if not fresh_keys:
            self.state.duplicates += 1
            log(f"Все товары из сообщения {job.source_chat_id}/{source_message_id} уже обработаны")
            return

        fresh_products = [product for product in products if product.sku_key in fresh_keys]
        text = build_publication_text(source_text, fresh_products)

        if self.settings.dry_run:
            log(f"DRY_RUN — публикация НЕ выполнена {job.source_chat_id}/{source_message_id}:\n{text}")
            self.db.release_reservations(fresh_keys)
            return

        try:
            await self.publish(text, job.messages)
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

    async def publish(self, text: str, messages: list) -> None:
        if not text and not any(getattr(message, "media", None) for message in messages):
            raise RuntimeError("Не сформирован текст и отсутствуют медиа для публикации")

        async with self.publish_lock:
            media = [
                message.media
                for message in messages
                if message.media and not isinstance(message.media, MessageMediaWebPage)
            ]

            if not media:
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
                media if len(media) > 1 else media[0],
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
            f"Каналов в папке: {len(self.current_sources)}\n"
            f"Очередь: {self.queue.qsize()}/{self.queue.maxsize}\n"
            f"Аптайм: {hours}ч {minutes}м {seconds}с\n\n"
            f"🛡 Антибот Ozon:\n"
            f"• Браузер (Playwright): {browser_state}\n"
            f"• Cookies в сессии: {self.synced_cookie_count}\n"
            f"• Раскрыто через HTTP/curl: {self.state.http_resolved}\n"
            f"• Раскрыто через браузер: {self.state.browser_resolved}\n\n"
            f"Получено сообщений: {self.state.incoming}\n"
            f"Опубликовано постов: {self.state.published}\n"
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
        await self.browser_resolver.start_idle_monitor()
        if self.browser_resolver.is_available:
            log(
                f"Активирован браузерный резолвер Ozon (Playwright Chromium, "
                f"витрины: {', '.join(self.settings.ozon_preferred_storefronts)})"
            )
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
    app = ParserApp(settings)
    await app.run()


if __name__ == "__main__":
    asyncio.run(main())