import aiohttp
import asyncio
from contextlib import suppress
import logging
import time
from telethon.utils import get_peer_id
from .common import (
    build_ssl_context,
    log,
)
from .constants import DEFAULT_HEADERS
from .optional_dependencies import CurlAsyncSession


class RuntimeMixin:
    def write_heartbeat(self) -> None:
        """Обновляет файл heartbeat для проверки работоспособности контейнера."""
        with suppress(OSError):
            with open(self.settings.heartbeat_path, "w", encoding="utf-8") as handle:
                handle.write(str(time.time()))

    async def heartbeat_worker(self) -> None:
        while True:
            if self.user_client.is_connected() and self.bot_client.is_connected():
                self.write_heartbeat()
            await asyncio.sleep(self.settings.heartbeat_interval_seconds)

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
            self.channel_entities["main"] = self.target_entity

            await self.update_sources()
            await self.restore_pending_jobs()
            await self.catch_up_sources()
            self.workers = [
                asyncio.create_task(self.process_worker(number), name=f"parser-worker-{number}")
                for number in range(1, self.settings.worker_count + 1)
            ]
            self.publication_scheduler_task = asyncio.create_task(
                self.scheduled_publication_worker(),
                name="publication-scheduler",
            )
            self.heartbeat_task = asyncio.create_task(self.heartbeat_worker(), name="heartbeat")
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
            pending = [
                *self.workers,
                *self.album_tasks.values(),
                *self.retry_tasks,
                *([self.publication_scheduler_task] if self.publication_scheduler_task else []),
                *([self.heartbeat_task] if self.heartbeat_task else []),
            ]
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
