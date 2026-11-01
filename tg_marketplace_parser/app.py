import aiohttp
import asyncio
import os
from telethon import TelegramClient
from .admin import AdminMixin
from .browser import OzonBrowserResolver
from .config import Settings
from .market_data import MarketDataMixin
from .models import (
    Job,
    ParserState,
)
from .pipeline import PipelineMixin
from .runtime import RuntimeMixin
from .sources import SourcesMixin
from .storage import DuplicateDB


class ParserApp(SourcesMixin, MarketDataMixin, PipelineMixin, AdminMixin, RuntimeMixin):
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
        self.channel_entities: dict[str, object] = {}
        self.current_sources: set[int] = set()
        self.queue: asyncio.Queue[Job] = asyncio.Queue(maxsize=500)
        self.workers: list[asyncio.Task] = []
        self.publication_scheduler_task: asyncio.Task | None = None
        self.retry_tasks: set[asyncio.Task] = set()
        self.resolve_semaphore = asyncio.Semaphore(8)
        self.publish_lock = asyncio.Lock()
        self.publication_gate_lock = asyncio.Lock()
        self.album_messages: dict[int, list] = {}
        self.album_tasks: dict[int, asyncio.Task] = {}
        self.album_lock = asyncio.Lock()
        self.synced_cookie_count: int = 0
        self._rub_to_byn_rate: float = settings.rub_to_byn_fallback_rate
        self._rate_updated_at: float = 0.0
        self._rate_lock = asyncio.Lock()
        self.awaiting_schedule_input = False
        self._last_ozon_stock_check_at = 0.0
        self._register_admin_handlers()
