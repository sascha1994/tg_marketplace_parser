from contextlib import suppress
from datetime import datetime, timedelta
import logging
import re
from telethon.errors import MessageNotModifiedError
from telethon import (
    Button,
    events,
)
import time
from zoneinfo import ZoneInfo
from . import __version__, thresholds, topics
from .common import log
from .common import validate_marketplace_url
from .message_parsing import extract_links
from .models import Job


STAT_KEYS = (
    "incoming",
    "published",
    "duplicates",
    "out_of_stock",
    "rejected",
    "errors",
    "queue_dropped",
    "browser_resolved",
    "http_resolved",
    "cards_parsed",
    "filtered_out_of_stock",
    "filtered_wb_stock",
    "filtered_ozon_discount",
    "filtered_blocklist",
    "filtered_no_price",
    "rejected_no_links",
    "rejected_invalid_url",
    "posts_deleted",
    "deleted_oos",
    "deleted_price_up",
    "posts_marked_unavailable",
    "marked_oos",
    "marked_price_up",
    "forum_posts_deleted",
    "forum_deleted_oos",
    "forum_deleted_price_up",
    "forum_posts_marked_unavailable",
    "forum_marked_oos",
    "forum_marked_price_up",
    "scheduled_queued",
    "scheduled_cancelled",
)


STATS_SECTIONS = ("overview", "views", "forum", "lifecycle", "filters")


class AdminMixin:
    def restore_stats_from_db(self) -> None:
        """Восстанавливает накопленные счётчики статистики, время последней публикации и каскад из БД при старте."""
        if not hasattr(self, "db") or self.db is None or not hasattr(self, "state") or self.state is None:
            return
        if hasattr(self.db, "get_stats"):
            with suppress(Exception):
                totals = self.db.get_stats(since_ts=None)
                for key, val in totals.items():
                    if hasattr(self.state, key):
                        setattr(self.state, key, int(val))
                for key in ("http_resolved", "browser_resolved"):
                    self.state.saved_resolutions[key] = getattr(self.state, key)
                if self.state.filtered_out_of_stock > self.state.out_of_stock:
                    self.state.out_of_stock = self.state.filtered_out_of_stock
                elif self.state.out_of_stock > self.state.filtered_out_of_stock:
                    self.state.filtered_out_of_stock = self.state.out_of_stock
        if hasattr(self.db, "get_last_published_at"):
            with suppress(Exception):
                last_pub = self.db.get_last_published_at()
                if last_pub is not None:
                    self.state.last_published_at = last_pub
        if hasattr(self.db, "get_cascade_stats"):
            with suppress(Exception):
                cascade = self.db.get_cascade_stats()
                if cascade:
                    self.state.cascade_stats = cascade

    def reset_all_stats(self) -> None:
        """Обнуляет статистику в БД и в оперативной памяти (без удаления базы дедупликации товаров)."""
        if hasattr(self, "db") and self.db is not None and hasattr(self.db, "reset_stats"):
            with suppress(Exception):
                self.db.reset_stats()
        if hasattr(self, "state") and self.state is not None:
            if hasattr(self.state, "reset_counters"):
                self.state.reset_counters()

    def _get_selected_stats_period(self) -> str:
        if hasattr(self, "db") and self.db is not None and hasattr(self.db, "get_setting"):
            with suppress(Exception):
                period = self.db.get_setting("stats_period", "day")
                if period in {"day", "week", "month", "all"}:
                    return period
        return "day"

    def _get_selected_stats_section(self) -> str:
        if hasattr(self, "db") and self.db is not None and hasattr(self.db, "get_setting"):
            with suppress(Exception):
                section = self.db.get_setting("stats_section", "overview")
                if section in STATS_SECTIONS:
                    return section
        return "overview"

    def _get_period_bounds(self, period: str, now_ts: float | None = None) -> tuple[int | None, str]:
        tz_name = getattr(getattr(self, "settings", None), "timezone", "Europe/Minsk") or "Europe/Minsk"
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo("Europe/Minsk")
            tz_name = "Europe/Minsk"

        current_ts = time.time() if now_ts is None else float(now_ts)
        now_dt = datetime.fromtimestamp(current_ts, tz)
        today_midnight = datetime(now_dt.year, now_dt.month, now_dt.day, 0, 0, 0, tzinfo=tz)

        if period == "day":
            return int(today_midnight.timestamp()), f"За день (с 00:00, {tz_name})"
        if period == "week":
            start_dt = today_midnight - timedelta(days=6)
            return int(start_dt.timestamp()), f"За неделю (7 дн., с {start_dt:%d.%m})"
        if period == "month":
            start_dt = today_midnight - timedelta(days=29)
            return int(start_dt.timestamp()), f"За месяц (30 дн., с {start_dt:%d.%m})"
        return None, "За всё время"

    def _collect_stats_for_period(self, period: str = "all", now_ts: float | None = None) -> dict[str, int]:
        since_ts, _ = self._get_period_bounds(period, now_ts=now_ts)
        db_counts: dict[str, int] = {}
        if hasattr(self, "db") and self.db is not None and hasattr(self.db, "get_stats"):
            with suppress(Exception):
                db_counts = self.db.get_stats(since_ts=since_ts)

        result: dict[str, int] = {}
        state = getattr(self, "state", None)
        for key in STAT_KEYS:
            val = int(db_counts.get(key, 0))
            if period == "all" and state is not None and hasattr(state, key):
                val = max(val, int(getattr(state, key, 0) or 0))
            result[key] = val

        # Согласование агрегатов и подкатегорий
        result["filtered_out_of_stock"] = max(result["filtered_out_of_stock"], result["out_of_stock"])
        result["out_of_stock"] = result["filtered_out_of_stock"]
        result["posts_deleted"] = max(
            result["posts_deleted"],
            result["deleted_oos"] + result["deleted_price_up"],
        )
        result["posts_marked_unavailable"] = max(
            result["posts_marked_unavailable"],
            result["marked_oos"] + result["marked_price_up"],
        )
        result["forum_posts_deleted"] = max(
            result["forum_posts_deleted"],
            result["forum_deleted_oos"] + result["forum_deleted_price_up"],
        )
        result["forum_posts_marked_unavailable"] = max(
            result["forum_posts_marked_unavailable"],
            result["forum_marked_oos"] + result["forum_marked_price_up"],
        )
        return result

    def _format_last_published(self) -> str:
        last_ts = getattr(getattr(self, "state", None), "last_published_at", None)
        if last_ts is None and hasattr(self, "db") and self.db is not None and hasattr(self.db, "get_last_published_at"):
            with suppress(Exception):
                last_ts = self.db.get_last_published_at()
                if last_ts is not None and hasattr(self, "state") and self.state is not None:
                    self.state.last_published_at = last_ts
        if last_ts is None:
            return "ещё не было"
        return time.strftime("%d.%m %H:%M", time.localtime(last_ts))

    def get_stats_text(self) -> str:
        status = "⏸ НА ПАУЗЕ" if self.state.is_paused else "🟢 АКТИВЕН"
        mode = " (DRY_RUN)" if self.settings.dry_run else ""
        return f"Статус: {status}{mode}"

    def _compute_filter_totals(self, s: dict[str, int]) -> tuple[int, int]:
        base_filtered = (
            s["duplicates"]
            + s["filtered_out_of_stock"]
            + s["filtered_wb_stock"]
            + s["filtered_ozon_discount"]
            + s["filtered_blocklist"]
            + s["filtered_no_price"]
            + s["rejected_no_links"]
            + s["rejected_invalid_url"]
        )
        other_rejected = max(
            0,
            s["rejected"]
            - (
                s["filtered_blocklist"]
                + s["filtered_ozon_discount"]
                + s["rejected_no_links"]
                + s["rejected_invalid_url"]
            ),
        )
        return base_filtered + other_rejected, other_rejected

    def get_stats_overview_text(self, period: str | None = None, *, now_ts: float | None = None) -> str:
        chosen_period = period or self._get_selected_stats_period()
        now = time.time() if now_ts is None else now_ts
        since_ts, period_label = self._get_period_bounds(chosen_period, now_ts=now)
        s = self._collect_stats_for_period(chosen_period, now_ts=now)
        posts, measured, views = self.db.get_publication_view_stats(since_ts)
        total_views = str(views) if measured or not posts else "нет данных"
        average_views = f"{views / measured:.1f}".replace(".", ",") if measured else "—"
        conv = f" ({s['published'] * 100.0 / s['incoming']:.1f}%)" if s["incoming"] > 0 else ""
        last_published = self._format_last_published()
        total_removed = s["posts_deleted"] + s["posts_marked_unavailable"]
        forum_total_removed = s["forum_posts_deleted"] + s["forum_posts_marked_unavailable"]
        total_filtered, _ = self._compute_filter_totals(s)

        lines = [
            f"📊 Статистика (сводка) — {period_label}\n",
            "📥 Поток и публикации:",
            f"• Получено: {s['incoming']} · Опубликовано: {s['published']}{conv}",
            f"• Последняя публикация: {last_published}",
            f"• Тихие часы: отложено {s['scheduled_queued']} (отменено: {s['scheduled_cancelled']})\n",
            "👁 Основной канал:",
            f"• Постов: {posts} (с замером: {measured}/{posts})",
            f"• Просмотров: {total_views} · Среднее на пост: {average_views}\n",
        ]

        forum_id = getattr(self.settings, "test_forum_id", None)
        if forum_id or forum_total_removed > 0:
            forum_copies = 0
            if forum_id and hasattr(self.db, "get_forum_topic_stats"):
                forum_copies = sum(row[1] for row in self.db.get_forum_topic_stats(forum_id, since_ts))
            lines.extend(
                [
                    "🗂 Доп. группа:",
                    f"• Опубликовано в темах: {forum_copies}",
                    f"• Снято/помечено: {forum_total_removed} "
                    f"(❌ удалено: {s['forum_posts_deleted']} · 🏷 помечено: {s['forum_posts_marked_unavailable']})\n",
                ]
            )

        lines.extend(
            [
                f"🗑 Снятие в канале: {total_removed} "
                f"(❌ удалено: {s['posts_deleted']} · 🏷 помечено: {s['posts_marked_unavailable']})",
                f"🚫 Отсеяно фильтрами: {total_filtered} · Ошибок: {s['errors']}",
            ]
        )
        return "\n".join(lines)

    def get_stats_views_text(self, period: str | None = None, *, now_ts: float | None = None) -> str:
        chosen_period = period or self._get_selected_stats_period()
        now = time.time() if now_ts is None else now_ts
        since_ts, period_label = self._get_period_bounds(chosen_period, now_ts=now)
        if hasattr(self.db, "get_channel_view_breakdown"):
            b = self.db.get_channel_view_breakdown(since_ts, top_limit=5)
        else:
            posts, measured, views = self.db.get_publication_view_stats(since_ts)
            b = {
                "total_posts": posts,
                "measured_posts": measured,
                "total_views": views,
                "max_views": 0,
                "marked_posts": 0,
                "deleted_posts": 0,
                "deleted_measured": 0,
                "deleted_views": 0,
                "top_posts": [],
            }
        total_posts = b["total_posts"]
        measured = b["measured_posts"]
        views = b["total_views"]
        total_views = str(views) if measured or not total_posts else "нет данных"
        average_views = f"{views / measured:.1f}".replace(".", ",") if measured else "—"
        max_views = str(b["max_views"]) if measured else "—"
        in_feed = max(0, total_posts - b["deleted_posts"])

        lines = [
            f"👁 Просмотры основного канала — {period_label}\n",
            f"• Постов за период: {total_posts} (в ленте: {in_feed}, удалено парсером: {b['deleted_posts']})",
            f"• Постов с замером: {measured} из {total_posts}",
            f"• Суммарно просмотров: {total_views}",
            f"• Среднее на пост с замером: {average_views} · Максимум: {max_views}",
        ]
        if b["deleted_measured"] > 0:
            lines.append(
                f"• Учтено просмотров до удаления постов: {b['deleted_views']} "
                f"(постов: {b['deleted_measured']})"
            )

        top_posts = b.get("top_posts") or []
        if top_posts:
            lines.append("\n🏆 Топ постов по просмотрам:")
            for idx, item in enumerate(top_posts, start=1):
                raw_title = (item.get("title") or "Товар").strip()
                short_title = raw_title if len(raw_title) <= 44 else raw_title[:41].rstrip() + "..."
                disc = item.get("discount_percent")
                disc_part = f", −{disc:.0f}%" if isinstance(disc, (int, float)) and disc > 0 else ""
                plat = item.get("platform") or ""
                meta = f" ({plat}{disc_part})" if plat else ""
                status = item.get("status")
                badge = " [❌ неакт.]" if status == "unavailable_marked" else (" [удалён]" if status == "deleted" else "")
                lines.append(f"{idx}. {item['views']} 👁 — {short_title}{meta}{badge}")
        elif total_posts > 0 and measured == 0:
            lines.append(
                "\nℹ️ Первичный замер выполняется через 2–5 мин после выхода поста, "
                "далее каждые 15 мин в первый час, раз в час и перед удалением."
            )
        return "\n".join(lines)

    def get_stats_forum_text(self, period: str | None = None, *, now_ts: float | None = None) -> str:
        chosen_period = period or self._get_selected_stats_period()
        now = time.time() if now_ts is None else now_ts
        since_ts, period_label = self._get_period_bounds(chosen_period, now_ts=now)
        s = self._collect_stats_for_period(chosen_period, now_ts=now)
        max_price_inc = thresholds.max_price_increase_percent(self)
        forum_id = getattr(self.settings, "test_forum_id", None)
        forum_total_removed = s["forum_posts_deleted"] + s["forum_posts_marked_unavailable"]

        lines = [f"🗂 Доп. группа и темы — {period_label}\n"]
        if not forum_id and forum_total_removed == 0:
            lines.append("Дополнительная группа не настроена (укажите TG_TEST_FORUM_ID в .env).")
            return "\n".join(lines)

        topic_rows: list[dict] = []
        if forum_id:
            if hasattr(self.db, "get_forum_topic_breakdown"):
                topic_rows = self.db.get_forum_topic_breakdown(forum_id, since_ts)
            elif hasattr(self.db, "get_forum_topic_stats"):
                topic_rows = [
                    {
                        "topic": r[0],
                        "copies": r[1],
                        "marked": 0,
                        "deleted": 0,
                        "originals": r[2],
                        "covered": r[3],
                        "channel_views": r[4],
                    }
                    for r in self.db.get_forum_topic_stats(forum_id, since_ts)
                ]
        total_copies = sum(item["copies"] for item in topic_rows)
        lines.extend(
            [
                f"• Опубликовано в темах за период: {total_copies}",
                f"• Снято и помечено в группе ({forum_total_removed}):",
                f"  — ❌ Удалено из группы: {s['forum_posts_deleted']} "
                f"(нет в наличии: {s['forum_deleted_oos']} · рост цены ≥{max_price_inc:g}%: {s['forum_deleted_price_up']})",
                f"  — 🏷 Помечено «Неактуально»: {s['forum_posts_marked_unavailable']} "
                f"(нет в наличии: {s['forum_marked_oos']} · рост цены ≥{max_price_inc:g}%: {s['forum_marked_price_up']})\n",
                "📂 Распределение по темам:",
            ]
        )
        if not topic_rows:
            lines.append("За этот период публикаций в группе нет.")
        else:
            for item in topic_rows:
                topic_label = item["topic"] or "Старые публикации без сохранённой темы"
                line = f"• {topic_label} — {item['copies']} шт."
                if item["marked"] or item["deleted"]:
                    parts = []
                    if item["marked"]:
                        parts.append(f"🏷 {item['marked']}")
                    if item["deleted"]:
                        parts.append(f"❌ {item['deleted']}")
                    line += f" ({' · '.join(parts)})"
                if item["originals"] and item["covered"]:
                    avg_v = f"{item['channel_views'] / item['covered']:.1f}".replace(".", ",")
                    line += f" · 👁 в канале {item['channel_views']} (ср. {avg_v})"
                lines.append(line)
        return "\n".join(lines)

    def get_stats_lifecycle_text(self, period: str | None = None, *, now_ts: float | None = None) -> str:
        chosen_period = period or self._get_selected_stats_period()
        now = time.time() if now_ts is None else now_ts
        _, period_label = self._get_period_bounds(chosen_period, now_ts=now)
        s = self._collect_stats_for_period(chosen_period, now_ts=now)
        max_price_inc = thresholds.max_price_increase_percent(self)
        unavail_disc = thresholds.unavailable_mark_discount(self)
        total_removed = s["posts_deleted"] + s["posts_marked_unavailable"]
        forum_total_removed = s["forum_posts_deleted"] + s["forum_posts_marked_unavailable"]
        forum_id = getattr(self.settings, "test_forum_id", None)

        lines = [
            f"🗑 Снятие и пометки постов — {period_label}\n",
            f"📢 Основной канал ({total_removed}):",
            f"• ❌ Удалено из канала: {s['posts_deleted']} "
            f"(нет в наличии: {s['deleted_oos']} · рост цены ≥{max_price_inc:g}%: {s['deleted_price_up']})",
            f"• 🏷 Помечено «Неактуально» (скидка >{unavail_disc:g}%): {s['posts_marked_unavailable']} "
            f"(нет в наличии: {s['marked_oos']} · рост цены ≥{max_price_inc:g}%: {s['marked_price_up']})",
        ]
        if forum_id or forum_total_removed > 0:
            lines.extend(
                [
                    f"\n🗂 Доп. группа ({forum_total_removed}):",
                    f"• ❌ Удалено из группы: {s['forum_posts_deleted']} "
                    f"(нет в наличии: {s['forum_deleted_oos']} · рост цены ≥{max_price_inc:g}%: {s['forum_deleted_price_up']})",
                    f"• 🏷 Помечено «Неактуально» в группе: {s['forum_posts_marked_unavailable']} "
                    f"(нет в наличии: {s['forum_marked_oos']} · рост цены ≥{max_price_inc:g}%: {s['forum_marked_price_up']})",
                ]
            )
        lines.extend(
            [
                "\n⏰ Отложенные публикации (тихие часы):",
                f"• Поставлено в очередь: {s['scheduled_queued']}",
                f"• Отменено до выхода: {s['scheduled_cancelled']}",
            ]
        )
        return "\n".join(lines)

    def get_stats_filters_text(self, period: str | None = None, *, now_ts: float | None = None) -> str:
        chosen_period = period or self._get_selected_stats_period()
        now = time.time() if now_ts is None else now_ts
        _, period_label = self._get_period_bounds(chosen_period, now_ts=now)
        s = self._collect_stats_for_period(chosen_period, now_ts=now)
        ozon_min_disc = thresholds.ozon_min_discount(self)
        wb_min_stk = thresholds.wb_min_stock(self)
        conv = f" ({s['published'] * 100.0 / s['incoming']:.1f}%)" if s["incoming"] > 0 else ""
        total_filtered, other_rejected = self._compute_filter_totals(s)

        lines = [
            f"🚫 Отсев и система — {period_label}\n",
            f"📥 Поток: получено {s['incoming']} → опубликовано {s['published']}{conv}\n",
            f"🚫 Отсеяно по фильтрам и причинам ({total_filtered}):",
            f"• 🔄 Дубликаты (уже в БД): {s['duplicates']}",
            f"• 📦 Нет в наличии: {s['filtered_out_of_stock']}",
            f"• 📉 Малый остаток WB (<{wb_min_stk} шт.): {s['filtered_wb_stock']}",
            f"• 🎯 Низкая скидка Ozon (<{ozon_min_disc:g}%): {s['filtered_ozon_discount']}",
            f"• 🚫 Чёрный список (бренд/слово): {s['filtered_blocklist']}",
            f"• 💲 Нет цены / недоступна карточка: {s['filtered_no_price']}",
            f"• 🔗 Нет ссылок на маркетплейсы: {s['rejected_no_links']}",
            f"• ⚠️ Не удалось определить товар/артикул: {s['rejected_invalid_url']}",
        ]
        if other_rejected > 0:
            lines.append(f"• ❔ Прочие отклонения: {other_rejected}")
        lines.extend(
            [
                "\n🛡 Антибот и система:",
                f"• Спарсено карточек: {s['cards_parsed']}",
                f"• Раскрыто ссылок: HTTP/curl {s['http_resolved']} · Браузер {s['browser_resolved']}",
                f"• Ошибок: {s['errors']} · Переполнений очереди: {s['queue_dropped']}",
            ]
        )
        return "\n".join(lines)

    def get_stats_section_text(
        self,
        period: str | None = None,
        section: str | None = None,
    ) -> str:
        chosen_period = period or self._get_selected_stats_period()
        chosen_section = section or self._get_selected_stats_section()
        if chosen_section == "views":
            return self.get_stats_views_text(chosen_period)
        if chosen_section == "forum":
            return self.get_stats_forum_text(chosen_period)
        if chosen_section == "lifecycle":
            return self.get_stats_lifecycle_text(chosen_period)
        if chosen_section == "filters":
            return self.get_stats_filters_text(chosen_period)
        return self.get_stats_overview_text(chosen_period)

    def get_detailed_stats_text(self, period: str | None = None) -> str:
        chosen_period = period or self._get_selected_stats_period()
        now = time.time()
        since_ts, period_label = self._get_period_bounds(chosen_period, now_ts=now)
        s = self._collect_stats_for_period(chosen_period, now_ts=now)
        posts, measured, views = self.db.get_publication_view_stats(since_ts)
        total_views = str(views) if measured or not posts else "нет данных"
        average_views = f"{views / measured:.1f}".replace(".", ",") if measured else "—"
        forum_stats = []
        forum_id = getattr(self.settings, "test_forum_id", None)
        if forum_id:
            forum_stats.append("\n🗂 Темы группы")
            for title, copies, originals, covered, topic_views in self.db.get_forum_topic_stats(forum_id, since_ts):
                topic_label = title or "Старые публикации без сохранённой темы"
                line = f"• {topic_label} — постов в группе {copies}"
                if originals:
                    if covered:
                        line += f" · просмотры в основном канале {topic_views} (замеров {covered}/{originals})"
                    else:
                        line += f" · просмотры в основном канале ещё не замерены (постов {originals})"
                forum_stats.append(line)
            if len(forum_stats) == 1:
                forum_stats.append("За этот период публикаций в группе нет.")
        ozon_min_disc = thresholds.ozon_min_discount(self)
        wb_min_stk = thresholds.wb_min_stock(self)
        max_price_inc = thresholds.max_price_increase_percent(self)
        last_published = self._format_last_published()

        conv = (
            f" ({s['published'] * 100.0 / s['incoming']:.1f}%)"
            if s["incoming"] > 0
            else ""
        )
        total_removed = s["posts_deleted"] + s["posts_marked_unavailable"]
        forum_total_removed = s["forum_posts_deleted"] + s["forum_posts_marked_unavailable"]
        forum_removed_lines = []
        if forum_id or forum_total_removed > 0:
            forum_removed_lines = [
                f"🗑 Доп. группа — удалённые и неактуальные ({forum_total_removed}):",
                f"• ❌ Удалено из группы: {s['forum_posts_deleted']} "
                f"(нет в наличии: {s['forum_deleted_oos']} · рост цены ≥{max_price_inc:g}%: {s['forum_deleted_price_up']})",
                f"• 🏷 Помечено «Неактуально» в группе: {s['forum_posts_marked_unavailable']} "
                f"(нет в наличии: {s['forum_marked_oos']} · рост цены ≥{max_price_inc:g}%: {s['forum_marked_price_up']})\n",
            ]
        total_filtered = (
            s["duplicates"]
            + s["filtered_out_of_stock"]
            + s["filtered_wb_stock"]
            + s["filtered_ozon_discount"]
            + s["filtered_blocklist"]
            + s["filtered_no_price"]
            + s["rejected_no_links"]
            + s["rejected_invalid_url"]
        )
        other_rejected = max(
            0,
            s["rejected"]
            - (
                s["filtered_blocklist"]
                + s["filtered_ozon_discount"]
                + s["rejected_no_links"]
                + s["rejected_invalid_url"]
            ),
        )
        total_filtered += other_rejected

        lines = [
            f"📊 Статистика парсера — {period_label}\n",
            "👁 Основной канал:",
            f"• Постов за период: {posts}",
            f"• Просмотров по последним замерам: {total_views}",
            f"• Среднее просмотров на пост с замером: {average_views}",
            f"• Постов с замером: {measured} из {posts}\n",
            *forum_stats,
            "📥 Поток и публикации:",
            f"• Получено сообщений: {s['incoming']}",
            f"• Опубликовано постов: {s['published']}{conv}",
            f"• Последняя публикация: {last_published}",
            f"• Отложено на тихие часы: {s['scheduled_queued']} (отменено до выхода: {s['scheduled_cancelled']})\n",
            f"🗑 Удалённые и неактуальные посты ({total_removed}):",
            f"• ❌ Удалено из канала: {s['posts_deleted']} "
            f"(нет в наличии: {s['deleted_oos']} · рост цены ≥{max_price_inc:g}%: {s['deleted_price_up']})",
            f"• 🏷 Помечено «Неактуально»: {s['posts_marked_unavailable']} "
            f"(нет в наличии: {s['marked_oos']} · рост цены ≥{max_price_inc:g}%: {s['marked_price_up']})\n",
            *forum_removed_lines,
            f"🚫 Отсеяно по фильтрам и причинам ({total_filtered}):",
            f"• 🔄 Дубликаты (уже в БД): {s['duplicates']}",
            f"• 📦 Нет в наличии: {s['filtered_out_of_stock']}",
            f"• 📉 Малый остаток WB (<{wb_min_stk} шт.): {s['filtered_wb_stock']}",
            f"• 🎯 Низкая скидка Ozon (<{ozon_min_disc:g}%): {s['filtered_ozon_discount']}",
            f"• 🚫 Чёрный список (бренд/слово): {s['filtered_blocklist']}",
            f"• 💲 Нет цены / недоступна карточка: {s['filtered_no_price']}",
            f"• 🔗 Нет ссылок на маркетплейсы: {s['rejected_no_links']}",
            f"• ⚠️ Не удалось определить товар/артикул: {s['rejected_invalid_url']}",
        ]
        if other_rejected > 0:
            lines.append(f"• ❔ Прочие отклонения: {other_rejected}")

        lines.extend(
            [
                "\n🛡 Антибот и система:",
                f"• Спарсено карточек: {s['cards_parsed']}",
                f"• Раскрыто ссылок: HTTP/curl {s['http_resolved']} · Браузер {s['browser_resolved']}",
                f"• Ошибок: {s['errors']} · Переполнений очереди: {s['queue_dropped']}",
            ]
        )
        return "\n".join(lines)

    def get_stats_keyboard(self, period: str | None = None, section: str | None = None):
        chosen = period or self._get_selected_stats_period()
        sec = section or self._get_selected_stats_section()
        return [
            [
                Button.inline(f"{'✅ ' if sec == 'overview' else ''}📊 Сводка", b"stats_sec_overview"),
                Button.inline(f"{'✅ ' if sec == 'views' else ''}👁 Просмотры", b"stats_sec_views"),
            ],
            [
                Button.inline(f"{'✅ ' if sec == 'forum' else ''}🗂 Группа", b"stats_sec_forum"),
                Button.inline(f"{'✅ ' if sec == 'lifecycle' else ''}🗑 Удаления", b"stats_sec_lifecycle"),
                Button.inline(f"{'✅ ' if sec == 'filters' else ''}🚫 Отсев", b"stats_sec_filters"),
            ],
            [
                Button.inline(f"{'✅ ' if chosen == 'day' else ''}За день", b"stats_period_day"),
                Button.inline(f"{'✅ ' if chosen == 'week' else ''}За неделю", b"stats_period_week"),
            ],
            [
                Button.inline(f"{'✅ ' if chosen == 'month' else ''}За месяц", b"stats_period_month"),
                Button.inline(f"{'✅ ' if chosen == 'all' else ''}За всё время", b"stats_period_all"),
            ],
            [
                Button.inline("🔄 Обновить", b"menu_stats"),
                Button.inline("🧹 Обнулить статистику", b"reset_stats_confirm"),
            ],
            [Button.inline("↩️ Назад в панель", b"refresh")],
        ]

    def get_admin_keyboard(self):
        pause_button = (
            Button.inline("▶️ Возобновить", b"resume")
            if self.state.is_paused
            else Button.inline("⏸ Пауза", b"pause")
        )
        return [
            [Button.inline("🔄 Обновить", b"refresh"), pause_button],
            [
                Button.inline("⚙️ Фильтры", b"menu_filters"),
                Button.inline("⏰ Расписание", b"menu_schedule"),
            ],
            [
                Button.inline("🎯 Пороги", b"menu_thresholds"),
                Button.inline("📈 Мониторинг", b"menu_monitoring"),
            ],
            [
                Button.inline("📊 Статистика", b"menu_stats"),
                Button.inline("🛠 Сервис", b"menu_service"),
            ],
        ]

    def get_filters_text(self) -> str:
        ozon_min_disc = thresholds.ozon_min_discount(self)
        wb_min_stk = thresholds.wb_min_stock(self)
        discount_filter = self.db.get_setting("ozon_discount_filter", "true") == "true"
        wb_stock_filter = self.db.get_setting("wb_min_stock_filter", "true") == "true"
        wb_fallback_default = "true" if getattr(self.settings, "wb_allow_fallback_price", True) else "false"
        wb_fallback_enabled = self.db.get_setting("wb_allow_fallback_price", wb_fallback_default) == "true"
        return (
            "⚙️ Настройки фильтров:\n\n"
            f"• Фильтр скидки Ozon (≥{ozon_min_disc:g}%): {'✅ включён' if discount_filter else '⏸ выключен'}\n"
            f"• Фильтр остатка WB (≥{wb_min_stk} шт.): {'✅ включён' if wb_stock_filter else '⏸ выключен'}\n"
            f"• Резерв цены WB (история/текст при 403): {'✅ включён' if wb_fallback_enabled else '⏸ выключен'}"
        )

    def get_filters_keyboard(self):
        ozon_min_disc = thresholds.ozon_min_discount(self)
        wb_min_stk = thresholds.wb_min_stock(self)
        discount_label = (
            f"✅ Ozon ≥{ozon_min_disc:g}%: ВКЛ"
            if self.db.get_setting("ozon_discount_filter", "true") == "true"
            else f"⏸ Ozon ≥{ozon_min_disc:g}%: ВЫКЛ"
        )
        wb_stock_filter_label = (
            f"✅ Остаток WB ≥{wb_min_stk}: ВКЛ"
            if self.db.get_setting("wb_min_stock_filter", "true") == "true"
            else f"⏸ Остаток WB ≥{wb_min_stk}: ВЫКЛ"
        )
        wb_fallback_default = "true" if getattr(self.settings, "wb_allow_fallback_price", True) else "false"
        wb_fallback_label = (
            "✅ Резерв цены WB: ВКЛ"
            if self.db.get_setting("wb_allow_fallback_price", wb_fallback_default) == "true"
            else "⏸ Резерв цены WB: ВЫКЛ"
        )
        return [
            [
                Button.inline(discount_label, b"toggle_ozon_discount"),
                Button.inline("✏️ %", b"edit_ozon_discount_threshold"),
            ],
            [
                Button.inline(wb_stock_filter_label, b"toggle_wb_stock_filter"),
                Button.inline("✏️ шт.", b"edit_wb_stock_threshold"),
            ],
            [Button.inline(wb_fallback_label, b"toggle_wb_fallback_price")],
            [Button.inline("↩️ Назад в панель", b"refresh")],
        ]

    def get_schedule_text(self) -> str:
        quiet_start = self.db.get_setting("quiet_hours_start", "01:30")
        quiet_end = self.db.get_setting("quiet_hours_end", "07:00")
        quiet_hours_enabled = self.db.get_setting("quiet_hours_enabled", "true") == "true"
        return (
            "⏰ Расписание публикаций:\n\n"
            f"• Ограничение по времени: {'✅ включено' if quiet_hours_enabled else '⏸ выключено'}\n"
            f"• Тихие часы: {quiet_start}–{quiet_end} ({self.settings.timezone})"
        )

    def get_schedule_keyboard(self):
        quiet_start = self.db.get_setting("quiet_hours_start", "01:30")
        quiet_end = self.db.get_setting("quiet_hours_end", "07:00")
        quiet_hours_label = (
            "✅ Перерыв по времени: ВКЛ"
            if self.db.get_setting("quiet_hours_enabled", "true") == "true"
            else "⏸ Перерыв по времени: ВЫКЛ"
        )
        return [
            [Button.inline(quiet_hours_label, b"toggle_quiet_hours")],
            [Button.inline(f"✏️ Изменить интервал ({quiet_start}–{quiet_end})", b"edit_schedule")],
            [Button.inline("↩️ Назад в панель", b"refresh")],
        ]

    def get_thresholds_text(self) -> str:
        ozon_min_disc = thresholds.ozon_min_discount(self)
        wb_min_stk = thresholds.wb_min_stock(self)
        max_price_inc = thresholds.max_price_increase_percent(self)
        unavail_disc = thresholds.unavailable_mark_discount(self)
        ozon_check_int = thresholds.ozon_check_interval_seconds(self)
        return (
            "🎯 Пороговые значения и интервалы:\n\n"
            f"• Мин. скидка Ozon: ≥{ozon_min_disc:g}%\n"
            f"• Мин. остаток WB: ≥{wb_min_stk} шт.\n"
            f"• Порог роста цены (снятие поста): ≥{max_price_inc:g}%\n"
            f"• Порог пометки «❌ Неактуально» (вместо удаления): >{unavail_disc:g}%\n"
            f"• Интервал между проверками Ozon: {ozon_check_int} сек."
        )

    def get_thresholds_keyboard(self):
        ozon_min_disc = thresholds.ozon_min_discount(self)
        wb_min_stk = thresholds.wb_min_stock(self)
        max_price_inc = thresholds.max_price_increase_percent(self)
        unavail_disc = thresholds.unavailable_mark_discount(self)
        ozon_check_int = thresholds.ozon_check_interval_seconds(self)
        return [
            [
                Button.inline(f"🎯 Ozon ≥{ozon_min_disc:g}%", b"edit_ozon_discount_threshold"),
                Button.inline(f"📦 WB ≥{wb_min_stk} шт.", b"edit_wb_stock_threshold"),
            ],
            [
                Button.inline(f"📈 Рост ≥{max_price_inc:g}%", b"edit_price_increase_threshold"),
                Button.inline(f"🏷 Пометка >{unavail_disc:g}%", b"edit_unavailable_discount_threshold"),
            ],
            [Button.inline(f"⏱ Интервал Ozon: {ozon_check_int}с", b"edit_ozon_check_interval")],
            [Button.inline("↩️ Назад в панель", b"refresh")],
        ]

    def get_monitoring_text(self) -> str:
        uptime = int(time.time() - self.state.start_time)
        hours, remainder = divmod(uptime, 3600)
        minutes, seconds = divmod(remainder, 60)
        browser_state = (
            "🟢 запущен"
            if self.browser_resolver.is_running
            else ("🟡 готов (по требованию)" if self.browser_resolver.is_available else "🔴 недоступен")
        )
        active_posts, due_posts = (
            self.db.count_published_post_checks()
            if hasattr(self.db, "count_published_post_checks")
            else (0, 0)
        )
        return (
            "📈 Мониторинг и диагностика:\n\n"
            f"• Версия: {__version__} · Время работы: {hours}ч {minutes}м {seconds}с\n"
            f"• Очередь: {self.queue.qsize()}/{self.queue.maxsize} · В работе: {self.state.active_jobs}\n"
            f"• Браузер: {browser_state} · Cookies HTTP: {self.synced_cookie_count}\n"
            f"• Активных постов на проверке: {active_posts} (ожидают сейчас: {due_posts})\n"
            f"• Обработано карточек: {self.state.cards_parsed}\n"
            f"• Ошибок всего: {self.state.errors}"
        )

    def get_monitoring_keyboard(self):
        return [
            [
                Button.inline("📈 Источники", b"source_stats"),
                Button.inline("🧪 Каскад", b"cascade_stats"),
            ],
            [
                Button.inline("📜 Последняя обработка", b"recent_jobs"),
                Button.inline("⚠️ Ошибки", b"recent_errors"),
            ],
            [Button.inline("↩️ Назад в панель", b"refresh")],
        ]

    def get_service_text(self) -> str:
        published_db, pending_db = self.db.count_by_status()
        target = self.db.get_setting("publication_target", "main")
        channel = "тестовый" if target == "test" else "основной"
        test_status = "настроен" if self.settings.test_channel else "не настроен"
        forum_status = "не настроена"
        if getattr(self.settings, "test_forum_id", None):
            forum_status = "включена" if topics.forum_publication_enabled(self.settings, self.db) else "выключена"
        return (
            "🛠 Сервисное управление:\n\n"
            f"• Канал: {channel} · Тестовый: {test_status}\n"
            f"• Публикация в группу: {forum_status}\n"
            f"• Подключено источников: {len(self.current_sources)}\n"
            f"• Правил в чёрном списке: {len(self.db.list_blocks())}\n"
            f"• Записей дедупликации в БД: опубликовано {published_db}, pending {pending_db}"
        )

    def get_service_keyboard(self):
        selected_target = self.db.get_setting("publication_target", "main")
        target_buttons = [
            Button.inline(
                f"{'✅ ' if selected_target == 'main' else ''}Основной канал",
                b"select_main_channel",
            )
        ]
        if self.settings.test_channel:
            target_buttons.append(
                Button.inline(
                    f"{'✅ ' if selected_target == 'test' else ''}Тестовый канал",
                    b"select_test_channel",
                )
            )
        keyboard = [target_buttons]
        if getattr(self.settings, "test_forum_id", None):
            enabled = topics.forum_publication_enabled(self.settings, self.db)
            keyboard.append([Button.inline(
                "⏸ Выключить публикацию в группу" if enabled else "▶️ Включить публикацию в группу",
                b"disable_forum_publication" if enabled else b"enable_forum_publication",
            )])
        return keyboard + [
            [
                Button.inline("🚫 Блок-лист", b"blocklist_view"),
                Button.inline("🔄 Обновить источники", b"reload_sources"),
            ],
            [Button.inline("🧹 Очистить дедупликацию", b"clear_db_confirm")],
            [Button.inline("↩️ Назад в панель", b"refresh")],
        ]

    def get_recent_jobs_text(self) -> str:
        entries = list(self.state.recent_jobs)
        if not entries:
            return "📜 История обработки пока пуста."

        rows = ["📜 Последние задания:"]
        for timestamp, source_id, message_id, result, item_count in reversed(entries):
            when = time.strftime("%d.%m %H:%M:%S", time.localtime(timestamp))
            suffix = f" · товаров: {item_count}" if item_count else ""
            rows.append(
                f"{when} · чат {source_id}/{message_id} · {result}{suffix}"
            )
        return "\n".join(rows)

    def get_recent_errors_text(self) -> str:
        entries = list(self.state.recent_errors)
        if not entries:
            return "✅ За время работы ошибок не зарегистрировано."

        rows = ["⚠️ Последние ошибки:"]
        for timestamp, message in reversed(entries):
            when = time.strftime("%d.%m %H:%M:%S", time.localtime(timestamp))
            rows.append(f"{when} · {message}")
        return "\n".join(rows)

    def get_source_stats_text(self) -> str:
        rows_data = self.db.get_top_sources(limit=15)
        if not rows_data:
            return "📈 Статистика по источникам пока пуста."
        lines = ["📈 Статистика по источникам (топ-15):"]
        for item in rows_data:
            lines.append(
                f"• {item['chat_id']}: получено {item['received']}, "
                f"опубл. {item['published']}, откл. {item['rejected']}, дубл. {item['duplicates']}"
            )
        return "\n".join(lines)

    def get_cascade_stats_text(self) -> str:
        stats = dict(getattr(self.state, "cascade_stats", None) or {})
        if not stats and hasattr(self, "db") and self.db is not None and hasattr(self.db, "get_cascade_stats"):
            with suppress(Exception):
                stats = self.db.get_cascade_stats()
        if not stats:
            return "🧪 Статистика каскада пока пуста."
        lines = ["🧪 Успешность шагов каскада:"]
        for step, counters in sorted(stats.items()):
            ok, fail = counters[0], counters[1]
            total = ok + fail
            rate = (ok * 100.0 / total) if total > 0 else 0.0
            lines.append(f"• {step}: {ok}/{total} ({rate:.0f}%)")
        return "\n".join(lines)

    def get_blocklist_text(self) -> str:
        items = self.db.list_blocks()
        if not items:
            return (
                "🚫 Чёрный список пуст.\n\n"
                "Команды управления:\n"
                "/block brand <название>\n"
                "/block keyword <слово>\n"
                "/unblock brand <название>\n"
                "/unblock keyword <слово>"
            )
        lines = ["🚫 Чёрный список:"]
        for kind, val in items:
            label = "Бренд" if kind == "brand" else "Слово"
            lines.append(f"• {label}: {val}")
        lines.append(
            "\nДля удаления: /unblock brand <название> или /unblock keyword <слово>"
        )
        return "\n".join(lines)

    def handle_admin_block_command(self, raw_text: str) -> str | None:
        text = (raw_text or "").strip()
        if text == "/blocklist":
            return self.get_blocklist_text()
        m = re.match(r"^/(block|unblock)\s+(brand|keyword)\s+(.+)$", text, flags=re.IGNORECASE)
        if not m:
            return None
        action, kind, val = m.group(1).lower(), m.group(2).lower(), m.group(3).strip()
        if action == "block":
            added = self.db.add_block(kind, val)
            return f"✅ Добавлено в чёрный список ({kind}): {val}" if added else "ℹ️ Уже есть в чёрном списке."
        removed = self.db.remove_block(kind, val)
        return f"✅ Удалено из чёрного списка ({kind}): {val}" if removed else "ℹ️ Не найдено в чёрном списке."

    async def notify_admin(self, text: str) -> None:
        with suppress(Exception):
            await self.bot_client.send_message(self.settings.admin_id, text, link_preview=False)

    async def enqueue_admin_test_message(self, event) -> bool:
        message = event.message
        is_forwarded = getattr(message, "fwd_from", None) is not None
        has_marketplace_link = not is_forwarded and any(
            validate_marketplace_url(url) for url in extract_links(message)
        )
        if not is_forwarded and not has_marketplace_link:
            return False
        if not self.settings.test_channel:
            await event.respond("Тестовый канал не настроен. Добавьте TG_TEST_CHANNEL в .env.")
            return True
        await self.enqueue(
            Job(
                messages=[message],
                source_chat_id=event.chat_id,
                target_channel="test",
            )
        )
        source = "Пересланное сообщение" if is_forwarded else "Ссылка"
        await event.respond(f"{source} отправлена на проверку и публикацию в тестовый канал.")
        return True

    def _reset_admin_input_modes(self) -> None:
        self.awaiting_schedule_input = False
        self.awaiting_ozon_discount_input = False
        self.awaiting_wb_stock_input = False
        self.awaiting_price_increase_input = False
        self.awaiting_unavailable_discount_input = False
        self.awaiting_ozon_interval_input = False

    def _register_admin_handlers(self) -> None:
        @self.bot_client.on(events.NewMessage(chats=self.settings.admin_id, pattern=r"^/start$"))
        async def admin_start(event):
            if event.sender_id != self.settings.admin_id:
                return
            self._reset_admin_input_modes()
            await event.respond(self.get_stats_text(), buttons=self.get_admin_keyboard())

        @self.bot_client.on(events.NewMessage(chats=self.settings.admin_id))
        async def admin_settings_input(event):
            if event.sender_id != self.settings.admin_id:
                return
            value = (event.raw_text or "").strip()
            block_reply = self.handle_admin_block_command(value)
            if block_reply is not None:
                await event.respond(
                    block_reply,
                    buttons=[[Button.inline("↩️ К сервису", b"menu_service"), Button.inline("🏠 В панель", b"refresh")]],
                )
                return
            if await self.enqueue_admin_test_message(event):
                return
            if value.startswith("/"):
                return
            if getattr(self, "awaiting_ozon_discount_input", False):
                try:
                    num = float(value.replace(",", "."))
                    if not (1 <= num <= 95):
                        raise ValueError
                except ValueError:
                    await event.respond("Введите число от 1 до 95 (например, 30).")
                    return
                self.db.set_setting("ozon_min_discount_percent", f"{num:g}")
                self._reset_admin_input_modes()
                await event.respond(self.get_thresholds_text(), buttons=self.get_thresholds_keyboard())
                return
            if getattr(self, "awaiting_wb_stock_input", False):
                try:
                    num_int = int(value)
                    if not (1 <= num_int <= 1000):
                        raise ValueError
                except ValueError:
                    await event.respond("Введите целое число от 1 до 1000 (например, 3).")
                    return
                self.db.set_setting("wb_min_stock", str(num_int))
                self._reset_admin_input_modes()
                await event.respond(self.get_thresholds_text(), buttons=self.get_thresholds_keyboard())
                return
            if getattr(self, "awaiting_price_increase_input", False):
                try:
                    num = float(value.replace(",", "."))
                    if not (1 <= num <= 500):
                        raise ValueError
                except ValueError:
                    await event.respond("Введите число от 1 до 500 (например, 15).")
                    return
                self.db.set_setting("max_price_increase_percent", f"{num:g}")
                self._reset_admin_input_modes()
                await event.respond(self.get_thresholds_text(), buttons=self.get_thresholds_keyboard())
                return
            if getattr(self, "awaiting_unavailable_discount_input", False):
                try:
                    num = float(value.replace(",", "."))
                    if not (0 <= num <= 99):
                        raise ValueError
                except ValueError:
                    await event.respond("Введите число от 0 до 99 (например, 50).")
                    return
                self.db.set_setting("unavailable_mark_discount_percent", f"{num:g}")
                self._reset_admin_input_modes()
                await event.respond(self.get_thresholds_text(), buttons=self.get_thresholds_keyboard())
                return
            if getattr(self, "awaiting_ozon_interval_input", False):
                try:
                    num_int = int(value)
                    if not (5 <= num_int <= 3600):
                        raise ValueError
                except ValueError:
                    await event.respond("Введите целое число от 5 до 3600 секунд (например, 30).")
                    return
                self.db.set_setting("ozon_check_interval_seconds", str(num_int))
                self._reset_admin_input_modes()
                await event.respond(self.get_thresholds_text(), buttons=self.get_thresholds_keyboard())
                return
            if not self.awaiting_schedule_input:
                return
            match = re.fullmatch(r"(\d{1,2}:\d{2})\s*[-–]\s*(\d{1,2}:\d{2})", value)
            if not match:
                await event.respond("Неверный формат. Отправьте время как ЧЧ:ММ-ЧЧ:ММ, например 01:30-07:00.")
                return
            try:
                start_hour, start_minute = map(int, match.group(1).split(":"))
                end_hour, end_minute = map(int, match.group(2).split(":"))
                if (
                    start_hour > 23
                    or end_hour > 23
                    or start_minute > 59
                    or end_minute > 59
                    or (start_hour, start_minute) == (end_hour, end_minute)
                ):
                    raise ValueError
            except ValueError:
                await event.respond("Укажите корректные часы и минуты; начало и конец должны различаться.")
                return
            start_value = f"{start_hour:02d}:{start_minute:02d}"
            end_value = f"{end_hour:02d}:{end_minute:02d}"
            self.db.set_setting("quiet_hours_start", start_value)
            self.db.set_setting("quiet_hours_end", end_value)
            self._reset_admin_input_modes()
            await event.respond(self.get_schedule_text(), buttons=self.get_schedule_keyboard())

        @self.bot_client.on(events.CallbackQuery(chats=self.settings.admin_id))
        async def admin_callback(event):
            if event.sender_id != self.settings.admin_id:
                await event.answer("Недостаточно прав", alert=True)
                return
            data = event.data
            if data == b"refresh":
                self._reset_admin_input_modes()
                with suppress(MessageNotModifiedError):
                    await event.edit(self.get_stats_text(), buttons=self.get_admin_keyboard())
                await event.answer("Данные обновлены")
            elif data == b"menu_filters":
                self._reset_admin_input_modes()
                await event.edit(self.get_filters_text(), buttons=self.get_filters_keyboard())
                await event.answer()
            elif data == b"menu_schedule":
                self._reset_admin_input_modes()
                await event.edit(self.get_schedule_text(), buttons=self.get_schedule_keyboard())
                await event.answer()
            elif data == b"menu_thresholds":
                self._reset_admin_input_modes()
                await event.edit(self.get_thresholds_text(), buttons=self.get_thresholds_keyboard())
                await event.answer()
            elif data == b"menu_monitoring":
                self._reset_admin_input_modes()
                await event.edit(self.get_monitoring_text(), buttons=self.get_monitoring_keyboard())
                await event.answer()
            elif data == b"menu_service":
                self._reset_admin_input_modes()
                await event.edit(self.get_service_text(), buttons=self.get_service_keyboard())
                await event.answer()
            elif data == b"menu_stats":
                self._reset_admin_input_modes()
                period = self._get_selected_stats_period()
                section = self._get_selected_stats_section()
                if section in {"overview", "views", "forum"} and hasattr(self, "maybe_refresh_views_on_demand"):
                    await self.maybe_refresh_views_on_demand()
                with suppress(MessageNotModifiedError):
                    await event.edit(
                        self.get_stats_section_text(period, section),
                        buttons=self.get_stats_keyboard(period, section),
                    )
                await event.answer("Статистика обновлена")
            elif data in {
                b"stats_sec_overview",
                b"stats_sec_views",
                b"stats_sec_forum",
                b"stats_sec_lifecycle",
                b"stats_sec_filters",
            }:
                self._reset_admin_input_modes()
                section = data.decode("utf-8").removeprefix("stats_sec_")
                self.db.set_setting("stats_section", section)
                period = self._get_selected_stats_period()
                if section in {"overview", "views", "forum"} and hasattr(self, "maybe_refresh_views_on_demand"):
                    await self.maybe_refresh_views_on_demand()
                with suppress(MessageNotModifiedError):
                    await event.edit(
                        self.get_stats_section_text(period, section),
                        buttons=self.get_stats_keyboard(period, section),
                    )
                sec_labels = {
                    "overview": "Сводка",
                    "views": "Просмотры канала",
                    "forum": "Доп. группа и темы",
                    "lifecycle": "Снятие и пометки",
                    "filters": "Отсев и система",
                }
                await event.answer(sec_labels.get(section, "Раздел обновлён"))
            elif data in {b"stats_period_day", b"stats_period_week", b"stats_period_month", b"stats_period_all"}:
                self._reset_admin_input_modes()
                period = data.decode("utf-8").removeprefix("stats_period_")
                self.db.set_setting("stats_period", period)
                section = self._get_selected_stats_section()
                with suppress(MessageNotModifiedError):
                    await event.edit(
                        self.get_stats_section_text(period, section),
                        buttons=self.get_stats_keyboard(period, section),
                    )
                labels = {
                    "day": "Статистика за день",
                    "week": "Статистика за неделю",
                    "month": "Статистика за месяц",
                    "all": "Статистика за всё время",
                }
                await event.answer(labels.get(period, "Период обновлён"))
            elif data == b"reset_stats_confirm":
                await event.edit(
                    "⚠️ Обнулить всю статистику парсера (за день, неделю, месяц и всё время)?\n\n"
                    "База дедупликации опубликованных товаров НЕ будет затронута (повторных публикаций не возникнет).",
                    buttons=[
                        [Button.inline("✅ Да, обнулить статистику", b"reset_stats_execute")],
                        [Button.inline("↩️ Отмена", b"menu_stats")],
                    ],
                )
                await event.answer()
            elif data == b"reset_stats_execute":
                self.reset_all_stats()
                period = self._get_selected_stats_period()
                section = self._get_selected_stats_section()
                with suppress(MessageNotModifiedError):
                    await event.edit(
                        self.get_stats_section_text(period, section),
                        buttons=self.get_stats_keyboard(period, section),
                    )
                await event.answer("Статистика обнулена")
                log("Администратор обнулил статистику парсера", logging.WARNING)
            elif data == b"recent_jobs":
                await event.edit(
                    self.get_recent_jobs_text(),
                    buttons=[[Button.inline("↩️ Назад", b"menu_monitoring"), Button.inline("🏠 В панель", b"refresh")]],
                )
                await event.answer()
            elif data == b"recent_errors":
                await event.edit(
                    self.get_recent_errors_text(),
                    buttons=[[Button.inline("↩️ Назад", b"menu_monitoring"), Button.inline("🏠 В панель", b"refresh")]],
                )
                await event.answer()
            elif data == b"source_stats":
                await event.edit(
                    self.get_source_stats_text(),
                    buttons=[[Button.inline("↩️ Назад", b"menu_monitoring"), Button.inline("🏠 В панель", b"refresh")]],
                )
                await event.answer()
            elif data == b"cascade_stats":
                await event.edit(
                    self.get_cascade_stats_text(),
                    buttons=[[Button.inline("↩️ Назад", b"menu_monitoring"), Button.inline("🏠 В панель", b"refresh")]],
                )
                await event.answer()
            elif data == b"blocklist_view":
                await event.edit(
                    self.get_blocklist_text(),
                    buttons=[[Button.inline("↩️ Назад", b"menu_service"), Button.inline("🏠 В панель", b"refresh")]],
                )
                await event.answer()
            elif data == b"pause":
                self.state.is_paused = True
                await event.edit(self.get_stats_text(), buttons=self.get_admin_keyboard())
                await event.answer("Пауза включена")
            elif data == b"resume":
                self.state.is_paused = False
                await event.edit(self.get_stats_text(), buttons=self.get_admin_keyboard())
                await event.answer("Парсинг возобновлён")
            elif data == b"edit_schedule":
                self._reset_admin_input_modes()
                self.awaiting_schedule_input = True
                await event.edit(
                    "Отправьте интервал запрета публикаций в формате ЧЧ:ММ-ЧЧ:ММ "
                    f"(временная зона {self.settings.timezone}). Например: 01:30-07:00.",
                    buttons=[[Button.inline("↩️ Назад", b"menu_schedule"), Button.inline("🏠 В панель", b"refresh")]],
                )
                await event.answer()
            elif data == b"edit_ozon_discount_threshold":
                self._reset_admin_input_modes()
                self.awaiting_ozon_discount_input = True
                await event.edit(
                    "Отправьте минимальный процент скидки Ozon (от 1 до 95), например: 30",
                    buttons=[[Button.inline("↩️ Назад", b"menu_thresholds"), Button.inline("🏠 В панель", b"refresh")]],
                )
                await event.answer()
            elif data == b"edit_wb_stock_threshold":
                self._reset_admin_input_modes()
                self.awaiting_wb_stock_input = True
                await event.edit(
                    "Отправьте минимальный остаток WB (целое число от 1 до 1000), например: 3",
                    buttons=[[Button.inline("↩️ Назад", b"menu_thresholds"), Button.inline("🏠 В панель", b"refresh")]],
                )
                await event.answer()
            elif data == b"edit_price_increase_threshold":
                self._reset_admin_input_modes()
                self.awaiting_price_increase_input = True
                await event.edit(
                    "Отправьте порог подорожания в процентах для снятия неактуальных постов "
                    "(от 1 до 500), например: 15",
                    buttons=[[Button.inline("↩️ Назад", b"menu_thresholds"), Button.inline("🏠 В панель", b"refresh")]],
                )
                await event.answer()
            elif data == b"edit_unavailable_discount_threshold":
                self._reset_admin_input_modes()
                self.awaiting_unavailable_discount_input = True
                await event.edit(
                    "Отправьте порог скидки (%) для пометки поста «❌ Неактуально» вместо удаления "
                    "(от 0 до 99), например: 50",
                    buttons=[[Button.inline("↩️ Назад", b"menu_thresholds"), Button.inline("🏠 В панель", b"refresh")]],
                )
                await event.answer()
            elif data == b"edit_ozon_check_interval":
                self._reset_admin_input_modes()
                self.awaiting_ozon_interval_input = True
                await event.edit(
                    "Отправьте минимальный интервал между проверками карточек Ozon в секундах "
                    "(от 5 до 3600), например: 30",
                    buttons=[[Button.inline("↩️ Назад", b"menu_thresholds"), Button.inline("🏠 В панель", b"refresh")]],
                )
                await event.answer()
            elif data == b"toggle_quiet_hours":
                enabled = self.db.get_setting("quiet_hours_enabled", "true") == "true"
                self.db.set_setting("quiet_hours_enabled", "false" if enabled else "true")
                await event.edit(self.get_schedule_text(), buttons=self.get_schedule_keyboard())
                await event.answer(
                    "Ограничение по времени включено"
                    if not enabled
                    else "Ограничение по времени отключено"
                )
            elif data == b"toggle_ozon_discount":
                enabled = self.db.get_setting("ozon_discount_filter", "true") == "true"
                self.db.set_setting("ozon_discount_filter", "false" if enabled else "true")
                await event.edit(self.get_filters_text(), buttons=self.get_filters_keyboard())
                await event.answer("Фильтр скидки Ozon обновлён")
            elif data == b"toggle_wb_stock_filter":
                enabled = self.db.get_setting("wb_min_stock_filter", "true") == "true"
                self.db.set_setting("wb_min_stock_filter", "false" if enabled else "true")
                await event.edit(self.get_filters_text(), buttons=self.get_filters_keyboard())
                await event.answer("Фильтр минимального остатка WB обновлён")
            elif data == b"toggle_wb_fallback_price":
                wb_fallback_default = "true" if getattr(self.settings, "wb_allow_fallback_price", True) else "false"
                enabled = self.db.get_setting("wb_allow_fallback_price", wb_fallback_default) == "true"
                self.db.set_setting("wb_allow_fallback_price", "false" if enabled else "true")
                await event.edit(self.get_filters_text(), buttons=self.get_filters_keyboard())
                await event.answer("Настройка резервной цены WB обновлена")
            elif data == b"select_main_channel":
                self.db.set_setting("publication_target", "main")
                with suppress(MessageNotModifiedError):
                    await event.edit(self.get_service_text(), buttons=self.get_service_keyboard())
                await event.answer("Выбран основной канал")
            elif data == b"select_test_channel":
                if not self.settings.test_channel:
                    await event.answer("Тестовый канал не настроен", alert=True)
                    return
                self.db.set_setting("publication_target", "test")
                with suppress(MessageNotModifiedError):
                    await event.edit(self.get_service_text(), buttons=self.get_service_keyboard())
                await event.answer("Выбран тестовый канал")
            elif data in {b"enable_forum_publication", b"disable_forum_publication"}:
                if not getattr(self.settings, "test_forum_id", None):
                    await event.answer("Укажите TG_TEST_FORUM_ID в настройках парсера", alert=True)
                    return
                enabled = data == b"enable_forum_publication"
                self.db.set_setting("forum_publication_enabled", "true" if enabled else "false")
                with suppress(MessageNotModifiedError):
                    await event.edit(self.get_service_text(), buttons=self.get_service_keyboard())
                await event.answer("Публикация в группу включена" if enabled else "Публикация в группу выключена")
            elif data == b"reload_sources":
                await self.update_sources()
                await event.edit(self.get_service_text(), buttons=self.get_service_keyboard())
                await event.answer("Источники обновлены")
            elif data == b"clear_db_confirm":
                await event.edit(
                    "⚠️ Удалить всю историю дедупликации? "
                    "После этого старые товары могут быть опубликованы повторно.",
                    buttons=[
                        [Button.inline("✅ Да, удалить", b"clear_db_execute")],
                        [Button.inline("↩️ Отмена", b"menu_service")],
                    ],
                )
            elif data == b"clear_db_execute":
                self.db.clear_published()
                await event.edit(self.get_service_text(), buttons=self.get_service_keyboard())
                await event.answer("База дедупликации очищена")
                log("Администратор очистил БД дедупликации", logging.WARNING)
