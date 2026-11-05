from contextlib import suppress
import logging
import re
from telethon import (
    Button,
    events,
)
import time
from . import thresholds
from .common import log
from .common import validate_marketplace_url
from .message_parsing import extract_links
from .models import Job


class AdminMixin:
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
        last_published = (
            time.strftime("%d.%m %H:%M", time.localtime(self.state.last_published_at))
            if self.state.last_published_at is not None
            else "ещё не было"
        )
        quiet_start = self.db.get_setting("quiet_hours_start", "01:30")
        quiet_end = self.db.get_setting("quiet_hours_end", "07:00")
        quiet_hours_enabled = self.db.get_setting("quiet_hours_enabled", "true") == "true"
        discount_filter = self.db.get_setting("ozon_discount_filter", "true") == "true"
        wb_stock_filter = self.db.get_setting("wb_min_stock_filter", "true") == "true"
        ozon_min_disc = thresholds.ozon_min_discount(self)
        wb_min_stk = thresholds.wb_min_stock(self)
        selected_target = self.db.get_setting("publication_target", "main")
        target_label = "тестовый" if selected_target == "test" else "основной"
        return (
            "🤖 Панель управления парсером\n\n"
            f"Статус: {status}{mode}\n"
            f"Очередь: {self.queue.qsize()}/{self.queue.maxsize}\n"
            f"Заданий в работе: {self.state.active_jobs}\n"
            f"Источников: {len(self.current_sources)}\n"
            f"Канал публикаций: {target_label}\n"
            f"Тестовый канал: {'настроен' if self.settings.test_channel else 'не настроен'}\n"
            f"Ограничение по времени: {'включено' if quiet_hours_enabled else 'выключено'}\n"
            f"Перерыв публикаций: {quiet_start}–{quiet_end} ({self.settings.timezone})\n"
            f"Фильтр скидки Ozon ≥{ozon_min_disc:g}%: {'включён' if discount_filter else 'выключен'}\n"
            f"Фильтр остатков WB ≥{wb_min_stk}: {'включён' if wb_stock_filter else 'выключен'}\n"
            f"Аптайм: {hours}ч {minutes}м {seconds}с\n\n"
            f"🛡 Антибот и карточки:\n"
            f"• Браузер (Playwright): {browser_state}\n"
            f"• Cookies в сессии: {self.synced_cookie_count}\n"
            f"• Раскрыто через HTTP/curl: {self.state.http_resolved}\n"
            f"• Раскрыто через браузер: {self.state.browser_resolved}\n"
            f"• Спарсено карточек: {self.state.cards_parsed}\n\n"
            f"Получено сообщений: {self.state.incoming}\n"
            f"Опубликовано постов: {self.state.published}\n"
            f"Последняя публикация: {last_published}\n"
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
        ozon_min_disc = thresholds.ozon_min_discount(self)
        wb_min_stk = thresholds.wb_min_stock(self)
        discount_label = (
            f"✅ Фильтр Ozon ≥{ozon_min_disc:g}%: ВКЛ"
            if self.db.get_setting("ozon_discount_filter", "true") == "true"
            else f"⏸ Фильтр Ozon ≥{ozon_min_disc:g}%: ВЫКЛ"
        )
        quiet_hours_label = (
            "✅ Перерыв по времени: ВКЛ"
            if self.db.get_setting("quiet_hours_enabled", "true") == "true"
            else "⏸ Перерыв по времени: ВЫКЛ"
        )
        wb_stock_filter_label = (
            f"✅ Остаток WB ≥{wb_min_stk}: ВКЛ"
            if self.db.get_setting("wb_min_stock_filter", "true") == "true"
            else f"⏸ Остаток WB ≥{wb_min_stk}: ВЫКЛ"
        )
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
        return [
            [Button.inline("📊 Обновить статус", b"refresh"), pause_button],
            target_buttons,
            [Button.inline(quiet_hours_label, b"toggle_quiet_hours")],
            [Button.inline("⏰ Настроить интервал перерыва", b"edit_schedule")],
            [Button.inline(discount_label, b"toggle_ozon_discount")],
            [Button.inline(wb_stock_filter_label, b"toggle_wb_stock_filter")],
            [
                Button.inline("🎯 Порог Ozon %", b"edit_ozon_discount_threshold"),
                Button.inline("📦 Мин. остаток WB", b"edit_wb_stock_threshold"),
            ],
            [
                Button.inline("📈 Источники", b"source_stats"),
                Button.inline("🧪 Каскад", b"cascade_stats"),
                Button.inline("🚫 Блок-лист", b"blocklist_view"),
            ],
            [Button.inline("📜 Последняя обработка", b"recent_jobs")],
            [Button.inline("⚠️ Последние ошибки", b"recent_errors")],
            [Button.inline("🔄 Обновить источники", b"reload_sources")],
            [Button.inline("🧹 Очистить дедупликацию", b"clear_db_confirm")],
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
        stats = getattr(self.state, "cascade_stats", None) or {}
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
                await event.respond(block_reply, buttons=[[Button.inline("↩️ В панель", b"refresh")]])
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
                await event.respond(self.get_stats_text(), buttons=self.get_admin_keyboard())
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
                await event.respond(self.get_stats_text(), buttons=self.get_admin_keyboard())
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
            await event.respond(self.get_stats_text(), buttons=self.get_admin_keyboard())

        @self.bot_client.on(events.CallbackQuery(chats=self.settings.admin_id))
        async def admin_callback(event):
            if event.sender_id != self.settings.admin_id:
                await event.answer("Недостаточно прав", alert=True)
                return
            data = event.data
            if data == b"refresh":
                self._reset_admin_input_modes()
                await event.edit(self.get_stats_text(), buttons=self.get_admin_keyboard())
                await event.answer("Данные обновлены")
            elif data == b"recent_jobs":
                await event.edit(
                    self.get_recent_jobs_text(),
                    buttons=[[Button.inline("↩️ В панель", b"refresh")]],
                )
                await event.answer()
            elif data == b"recent_errors":
                await event.edit(
                    self.get_recent_errors_text(),
                    buttons=[[Button.inline("↩️ В панель", b"refresh")]],
                )
                await event.answer()
            elif data == b"source_stats":
                await event.edit(
                    self.get_source_stats_text(),
                    buttons=[[Button.inline("↩️ В панель", b"refresh")]],
                )
                await event.answer()
            elif data == b"cascade_stats":
                await event.edit(
                    self.get_cascade_stats_text(),
                    buttons=[[Button.inline("↩️ В панель", b"refresh")]],
                )
                await event.answer()
            elif data == b"blocklist_view":
                await event.edit(
                    self.get_blocklist_text(),
                    buttons=[[Button.inline("↩️ В панель", b"refresh")]],
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
                    buttons=[[Button.inline("↩️ В панель", b"refresh")]],
                )
                await event.answer()
            elif data == b"edit_ozon_discount_threshold":
                self._reset_admin_input_modes()
                self.awaiting_ozon_discount_input = True
                await event.edit(
                    "Отправьте минимальный процент скидки Ozon (от 1 до 95), например: 30",
                    buttons=[[Button.inline("↩️ В панель", b"refresh")]],
                )
                await event.answer()
            elif data == b"edit_wb_stock_threshold":
                self._reset_admin_input_modes()
                self.awaiting_wb_stock_input = True
                await event.edit(
                    "Отправьте минимальный остаток WB (целое число от 1 до 1000), например: 3",
                    buttons=[[Button.inline("↩️ В панель", b"refresh")]],
                )
                await event.answer()
            elif data == b"toggle_quiet_hours":
                enabled = self.db.get_setting("quiet_hours_enabled", "true") == "true"
                self.db.set_setting("quiet_hours_enabled", "false" if enabled else "true")
                await event.edit(self.get_stats_text(), buttons=self.get_admin_keyboard())
                await event.answer(
                    "Ограничение по времени включено"
                    if not enabled
                    else "Ограничение по времени отключено"
                )
            elif data == b"toggle_ozon_discount":
                enabled = self.db.get_setting("ozon_discount_filter", "true") == "true"
                self.db.set_setting("ozon_discount_filter", "false" if enabled else "true")
                await event.edit(self.get_stats_text(), buttons=self.get_admin_keyboard())
                await event.answer("Фильтр скидки Ozon обновлён")
            elif data == b"toggle_wb_stock_filter":
                enabled = self.db.get_setting("wb_min_stock_filter", "true") == "true"
                self.db.set_setting("wb_min_stock_filter", "false" if enabled else "true")
                await event.edit(self.get_stats_text(), buttons=self.get_admin_keyboard())
                await event.answer("Фильтр минимального остатка WB обновлён")
            elif data == b"select_main_channel":
                self.db.set_setting("publication_target", "main")
                await event.edit(self.get_stats_text(), buttons=self.get_admin_keyboard())
                await event.answer("Выбран основной канал")
            elif data == b"select_test_channel":
                if not self.settings.test_channel:
                    await event.answer("Тестовый канал не настроен", alert=True)
                    return
                self.db.set_setting("publication_target", "test")
                await event.edit(self.get_stats_text(), buttons=self.get_admin_keyboard())
                await event.answer("Выбран тестовый канал")
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
