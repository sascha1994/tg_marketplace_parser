"""Пороговые значения: приоритет у настроек из БД (админ-панель), затем Settings, затем значения по умолчанию."""

from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")

DEFAULT_OZON_MIN_DISCOUNT_PERCENT = 30.0
DEFAULT_WB_MIN_STOCK = 3
DEFAULT_UNAVAILABLE_MARK_DISCOUNT_PERCENT = 50.0
DEFAULT_MAX_PRICE_INCREASE_PERCENT = 15.0
DEFAULT_OZON_CHECK_INTERVAL_SECONDS = 60
DEFAULT_SCHEDULED_MAX_ATTEMPTS = 20
DEFAULT_RECHECK_DELAY_SECONDS = 15 * 60


def read_threshold(
    owner: Any,
    db_key: str | None,
    attr: str,
    default: T,
    cast: Callable[[Any], T],
) -> T:
    """Читает порог из БД-настроек, затем из Settings; при некорректном значении возвращает default."""
    db = getattr(owner, "db", None)
    if db is not None and db_key:
        try:
            raw = db.get_setting(db_key, "")
            if raw not in (None, ""):
                return cast(raw)
        except (ValueError, TypeError, AttributeError):
            pass
    settings = getattr(owner, "settings", None)
    value = getattr(settings, attr, None)
    if value is not None:
        try:
            return cast(value)
        except (ValueError, TypeError):
            pass
    return default


def ozon_min_discount(owner: Any) -> float:
    return read_threshold(
        owner, "ozon_min_discount_percent", "ozon_min_discount_percent",
        DEFAULT_OZON_MIN_DISCOUNT_PERCENT, float,
    )


def wb_min_stock(owner: Any) -> int:
    return read_threshold(owner, "wb_min_stock", "wb_min_stock", DEFAULT_WB_MIN_STOCK, int)


def unavailable_mark_discount(owner: Any) -> float:
    return read_threshold(
        owner, "unavailable_mark_discount_percent", "unavailable_mark_discount_percent",
        DEFAULT_UNAVAILABLE_MARK_DISCOUNT_PERCENT, float,
    )


def max_price_increase_percent(owner: Any) -> float:
    return read_threshold(
        owner, "max_price_increase_percent", "max_price_increase_percent",
        DEFAULT_MAX_PRICE_INCREASE_PERCENT, float,
    )


def ozon_check_interval_seconds(owner: Any) -> int:
    return read_threshold(
        owner, "ozon_check_interval_seconds", "ozon_check_interval_seconds",
        DEFAULT_OZON_CHECK_INTERVAL_SECONDS, int,
    )


def scheduled_max_attempts(owner: Any) -> int:
    return read_threshold(
        owner, None, "scheduled_max_attempts", DEFAULT_SCHEDULED_MAX_ATTEMPTS, int,
    )


def recheck_delay_seconds(owner: Any) -> int:
    return read_threshold(
        owner, None, "recheck_delay_seconds", DEFAULT_RECHECK_DELAY_SECONDS, int,
    )
