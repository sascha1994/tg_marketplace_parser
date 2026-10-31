from contextlib import suppress
import logging
from telethon import (
    Button,
    events,
)
import time
from .common import log


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
        return (
            "🤖 Панель управления парсером\n\n"
            f"Статус: {status}{mode}\n"
            f"Очередь: {self.queue.qsize()}/{self.queue.maxsize}\n"
            f"Заданий в работе: {self.state.active_jobs}\n"
            f"Источников: {len(self.current_sources)}\n"
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
        return [
            [Button.inline("📊 Обновить статус", b"refresh"), pause_button],
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
