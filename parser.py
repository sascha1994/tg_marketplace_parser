import asyncio
import ipaddress
import logging
import os
import re
import sqlite3
import time
from contextlib import closing, suppress
from dataclasses import dataclass, field
from typing import Iterable
from urllib.parse import urljoin, urlparse, urlunparse

import aiohttp
from dotenv import load_dotenv
from telethon import Button, TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.tl.functions.messages import GetDialogFiltersRequest
from telethon.tl.types import MessageMediaWebPage
from telethon.utils import get_peer_id

load_dotenv()

ALLOWED_DOMAINS = frozenset({"wildberries.ru", "wb.ru", "ozon.ru", "ozon.by", "ozon.kz"})
OZON_DOMAINS = ("ozon.ru", "ozon.by", "ozon.kz")
USER_AGENT = "TGDealsParser/2.2 (+https://t.me/)"
MAX_FLOOD_RETRIES = 3
HEAD_READ_LIMIT = 150_000

URL_RE = re.compile(r"https?://[^\s<>\[\]\(\)\"']+", re.IGNORECASE)
WB_SKU_RE = re.compile(r"(?:www\.)?wildberries\.ru/catalog/(\d+)", re.IGNORECASE)
OZON_SKU_RE = re.compile(
    r"(?:www\.)?ozon\.(?:ru|by|kz)/product/(?:[^/?#]*-)?(\d+)(?:[/?#]|$)",
    re.IGNORECASE,
)
# Артикул из <head> страницы (canonical / og:url), если в самом URL его нет,
# например у реферальных ссылок вида ozon.ru/p/oXXXXXXX.
HEAD_PRODUCT_RE = re.compile(
    r"ozon\.(?:ru|by|kz)/product/(?:[^/\"'?#\s<>]*-)?(\d+)",
    re.IGNORECASE,
)
PROMO_RE = re.compile(
    r"(мы в тг|мы в telegram|мы в вк|подпишись|наш канал|источник:|t\.me/|vk\.com/)",
    re.IGNORECASE,
)

# Слово «источник» намеренно не включено: оно встречается в описаниях товаров.
# Рекламные строки «Источник:» отсекаются через PROMO_RE.
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

# Короткие реакции ищем только в начале строки и по границе слова,
# чтобы не удалять «Женский топ», «Стоп-сигнал» и подобные названия.
REACTION_RE = re.compile(r"^(круто|хочу больше|хочу ещё|хочу еще|лайк|огонь)\b")


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
            request_timeout_seconds=env_int("REQUEST_TIMEOUT_SECONDS", default=10, minimum=1),
            max_redirects=env_int("MAX_REDIRECTS", default=5, minimum=1),
            dry_run=os.getenv("DRY_RUN", "false").strip().lower() in {"1", "true", "yes", "on"},
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


class DuplicateDB:
    """SQLite-дедупликация с краткоживущими резервированиями pending.

    SKU резервируется атомарно перед публикацией. При ошибке резервирование
    удаляется, а после успеха переводится в published. Это предотвращает как
    параллельные дубли, так и потерю товара при ошибке отправки.
    """

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


def extract_links(message) -> list[str]:
    """Возвращает ссылки в порядке появления без дублей."""
    urls = list(URL_RE.findall(message.raw_text or ""))
    for entity in message.entities or []:
        url = getattr(entity, "url", None)
        if url:
            urls.append(url)
    cleaned = (url.rstrip(".,;:!?)>\"'") for url in urls)
    return list(dict.fromkeys(cleaned))


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

        # Строки только из emoji, символов или разделителей.
        if not re.search(r"[A-Za-zА-Яа-яЁё0-9]", line):
            continue

        # Короткие строки-реакции: «🔥 - круто», «❤️ хочу ещё».
        if len(normalized) <= 35 and REACTION_RE.match(normalized):
            continue

        cleaned_lines.append(line)

    return "\n".join(cleaned_lines).strip()


def parse_product_info(original_url: str, resolved_url: str) -> ProductInfo:
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
        host = normalize_host(urlparse(resolved_url).hostname)
        host = host[4:] if host.startswith("www.") else host
        canonical_host = host if host in OZON_DOMAINS else "ozon.ru"
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
    platform = "Wildberries" if host == "wb.ru" or host.endswith("wildberries.ru") else "Ozon"
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

    # Исходные WB/Ozon-ссылки убираем: ниже добавляется canonical-ссылка.
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


class ParserApp:
    def __init__(self, settings: Settings):
        self.settings = settings
        os.makedirs(settings.data_dir, exist_ok=True)
        self.user_client = TelegramClient(settings.user_session_path, settings.api_id, settings.api_hash)
        self.bot_client = TelegramClient(settings.bot_session_path, settings.api_id, settings.api_hash)
        self.db = DuplicateDB(settings.db_path, settings.cooldown_hours, settings.pending_ttl_minutes)
        self.state = ParserState()
        self.http_session: aiohttp.ClientSession | None = None
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
        self._register_admin_handlers()

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

    async def upgrade_to_product_url(self, url: str, status: int) -> str:
        """Если в URL нет артикула (например, ozon.ru/p/oXXXX), ищем его в <head> страницы."""
        if WB_SKU_RE.search(url) or OZON_SKU_RE.search(url):
            return url

        host = normalize_host(urlparse(url).hostname)
        if not is_ozon_host(host) or status != 200:
            return url

        assert self.http_session is not None
        try:
            async with self.resolve_semaphore:
                async with self.http_session.get(url, allow_redirects=False) as response:
                    if response.status != 200:
                        return url
                    raw = await response.content.read(HEAD_READ_LIMIT)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log(f"Не удалось прочитать страницу {url}: {type(exc).__name__}: {exc}", logging.WARNING)
            return url

        head = raw.decode("utf-8", errors="ignore").split("</head>", 1)[0]
        match = HEAD_PRODUCT_RE.search(head)
        if not match:
            log(f"Артикул в <head> не найден: {url}", logging.WARNING)
            return url

        sku = match.group(1)
        log(f"Артикул {sku} получен из <head> страницы {url}")
        return f"https://{host}/product/{sku}/"

    async def resolve_url(self, url: str) -> str | None:
        if not validate_marketplace_url(url):
            return None
        assert self.http_session is not None
        current_url = url
        max_redirects = self.settings.max_redirects

        # +1 итерация нужна, чтобы запросить итоговую страницу
        # после последнего разрешённого редиректа.
        for step in range(1, max_redirects + 2):
            host = normalize_host(urlparse(current_url).hostname)
            if not validate_marketplace_url(current_url) or not await self.host_has_only_public_ips(host):
                log(f"Отклонён небезопасный URL/редирект: {current_url}", logging.WARNING)
                return None

            antibot = None
            try:
                async with self.resolve_semaphore:
                    async with self.http_session.head(current_url, allow_redirects=False) as response:
                        status = response.status
                        location = response.headers.get("Location")
                        antibot = response.headers.get("ozon-antibot")
                        response.release()

                    if status in {403, 405, 501}:
                        async with self.http_session.get(
                            current_url,
                            allow_redirects=False,
                            read_until_eof=False,
                        ) as response:
                            status = response.status
                            location = response.headers.get("Location")
                            antibot = response.headers.get("ozon-antibot")
                            response.release()
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                log(f"Не удалось раскрыть URL {url}: {type(exc).__name__}: {exc}", logging.WARNING)
                return None

            if location and 300 <= status < 400:
                if step > max_redirects:
                    break
                next_url = urljoin(current_url, location)
                log(f"Редирект {step}/{max_redirects}: {status} {current_url} -> {next_url}")
                current_url = next_url
                continue

            log(f"Конец цепочки: HTTP {status}, ozon-antibot={antibot}, {current_url}")
            return await self.upgrade_to_product_url(current_url, status)

        log(f"Превышен лимит редиректов для {url}", logging.WARNING)
        return None

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

        # Для парсинга используем только явно включённые источники.
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
        # Для альбома берём первый непустой текст, а не склеиваем подписи всех фото.
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
        products: list[ProductInfo] = []
        for original_url, result in zip(candidate_links, results):
            if isinstance(result, Exception):
                log(f"Ошибка раскрытия {original_url}: {result}", logging.WARNING)
                continue
            if result is None:
                continue
            product = parse_product_info(original_url, result)
            if validate_marketplace_url(product.canonical_url):
                products.append(product)

        if not products:
            self.state.rejected += 1
            return

        # Один товар мог прийти дважды (короткая + прямая ссылка).
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

        # DRY_RUN: ничего не публикуем и не оставляем SKU в базе.
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

            # Лимит подписи к медиа — 1024 символа, остаток уходит отдельно.
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
        return (
            "🤖 Панель управления парсером\n\n"
            f"Статус: {status}{mode}\n"
            f"Каналов в папке: {len(self.current_sources)}\n"
            f"Очередь: {self.queue.qsize()}/{self.queue.maxsize}\n"
            f"Аптайм: {hours}ч {minutes}м {seconds}с\n\n"
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
        connector = aiohttp.TCPConnector(limit=20, ttl_dns_cache=300)
        self.http_session = aiohttp.ClientSession(
            timeout=timeout,
            connector=connector,
            headers={"User-Agent": USER_AGENT},
            raise_for_status=False,
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
