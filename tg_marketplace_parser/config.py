import os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv
from dataclasses import dataclass

from .constants import OZON_DOMAINS

load_dotenv()

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
    timezone: str = "Europe/Minsk"
    test_channel: str = ""
    ozon_min_discount_percent: float = 30.0
    wb_min_stock: int = 3
    unavailable_mark_discount_percent: float = 50.0
    max_price_increase_percent: float = 15.0
    scheduled_max_attempts: int = 20
    recheck_delay_seconds: int = 15 * 60
    catchup_max_messages: int = 50
    catchup_max_age_seconds: int = 6 * 3600
    heartbeat_interval_seconds: int = 30

    @property
    def heartbeat_path(self) -> str:
        return os.path.join(self.data_dir, "heartbeat")

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
        test_channel = os.getenv("TG_TEST_CHANNEL", "").strip()
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

        timezone = os.getenv("TIMEZONE", "Europe/Minsk").strip() or "Europe/Minsk"
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise ConfigError(f"TIMEZONE содержит неизвестную временную зону: {timezone!r}") from exc

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
            include_source_notes=env_bool("INCLUDE_SOURCE_NOTES", default=False),
            timezone=timezone,
            test_channel=test_channel,
            ozon_min_discount_percent=env_float("OZON_MIN_DISCOUNT_PERCENT", default=30.0, minimum=0),
            wb_min_stock=env_int("WB_MIN_STOCK", default=3, minimum=0),
            unavailable_mark_discount_percent=env_float(
                "UNAVAILABLE_MARK_DISCOUNT_PERCENT", default=50.0, minimum=0
            ),
            max_price_increase_percent=env_float(
                "MAX_PRICE_INCREASE_PERCENT", default=15.0, minimum=0
            ),
            scheduled_max_attempts=env_int("SCHEDULED_MAX_ATTEMPTS", default=20, minimum=1),
            recheck_delay_seconds=env_int("RECHECK_DELAY_SECONDS", default=15 * 60, minimum=30),
            catchup_max_messages=env_int("CATCHUP_MAX_MESSAGES", default=50, minimum=0),
            catchup_max_age_seconds=env_int("CATCHUP_MAX_AGE_SECONDS", default=6 * 3600, minimum=0),
            heartbeat_interval_seconds=env_int("HEARTBEAT_INTERVAL_SECONDS", default=30, minimum=5),
        )
