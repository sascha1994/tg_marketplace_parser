from dataclasses import dataclass, field
from collections import deque
import time

@dataclass(frozen=True)
class ProductCardData:
    title: str = ""
    brand: str = ""
    price_byn: float | None = None
    avg_month_price_byn: float | None = None
    old_price_byn: float | None = None
    image_url: str = ""
    in_stock: bool | None = None
    total_quantity: int | None = None
    rating: float | None = None
    reviews_count: int | None = None
    raw_currency: str = "BYN"

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
    in_stock: bool = True
    total_quantity: int | None = None
    rating: float | None = None
    reviews_count: int | None = None

    @property
    def display_title(self) -> str:
        from .common import clean_product_title

        cleaned = clean_product_title(self.title, self.brand)
        if cleaned:
            return cleaned
        return f"Товар {self.platform}"

    @property
    def formatted_price_byn(self) -> str | None:
        from .pricing import format_byn_price

        return format_byn_price(self.price_byn)

    @property
    def formatted_avg_month_price_byn(self) -> str | None:
        from .pricing import format_byn_price

        if (
            self.avg_month_price_byn is not None
            and self.price_byn is not None
            and self.avg_month_price_byn > self.price_byn * 1.01
        ):
            return format_byn_price(self.avg_month_price_byn)
        return None

    @property
    def formatted_old_price_byn(self) -> str | None:
        from .pricing import format_byn_price

        if (
            self.old_price_byn is not None
            and self.price_byn is not None
            and self.old_price_byn > self.price_byn * 1.02
        ):
            return format_byn_price(self.old_price_byn)
        return None

    @property
    def discount_percent(self) -> float | None:
        if (
            self.avg_month_price_byn is None
            or self.avg_month_price_byn <= 0
            or self.price_byn is None
            or self.price_byn <= 0
        ):
            return None
        return (self.avg_month_price_byn - self.price_byn) * 100 / self.avg_month_price_byn

@dataclass
class Job:
    messages: list
    source_chat_id: int
    attempts: int = 0
    target_channel: str | None = None


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
    active_jobs: int = 0
    last_published_at: float | None = None
    recent_jobs: deque = field(default_factory=lambda: deque(maxlen=15))
    recent_errors: deque = field(default_factory=lambda: deque(maxlen=10))
