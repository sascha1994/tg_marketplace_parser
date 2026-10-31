import asyncio
import io
import logging
from telethon.errors import FloodWaitError
from telethon.tl.types import MessageMediaWebPage
import time
from urllib.parse import urlparse
from .common import (
    has_product_sku,
    is_ozon_host,
    log,
    normalize_host,
    validate_marketplace_url,
)
from .constants import MAX_FLOOD_RETRIES
from .message_parsing import (
    extract_links,
    extract_skus_from_text,
    match_message_preview_url,
    resolve_ozon_canonical_host,
)
from .models import (
    Job,
    ProductInfo,
)
from .presentation import (
    build_publication_text,
    parse_product_info,
    split_text,
)


class PipelineMixin:
    def record_job_result(self, job: Job, result: str, item_count: int = 0) -> None:
        message_id = min((message.id for message in job.messages), default=0)
        self.state.recent_jobs.append(
            (time.time(), job.source_chat_id, message_id, result, item_count)
        )

    def record_admin_error(self, message: str) -> None:
        self.state.recent_errors.append((time.time(), message[:240]))

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
        await asyncio.sleep(0.5)
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
            self.record_admin_error(f"Очередь переполнена: источник {job.source_chat_id}")
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
            self.record_job_result(job, "отклонено: нет ссылок на маркетплейсы")
            return

        if len(candidate_links) > self.settings.max_links_per_post:
            log(
                f"В посте {job.source_chat_id} ссылок {len(candidate_links)}; "
                f"обрабатываются первые {self.settings.max_links_per_post}",
                logging.WARNING,
            )
            candidate_links = candidate_links[:self.settings.max_links_per_post]

        text_skus = extract_skus_from_text(source_text)
        pre_resolved: dict[int, str] = {}
        urls_to_resolve: list[tuple[int, str]] = []

        for idx, original_url in enumerate(candidate_links):
            if has_product_sku(original_url):
                pre_resolved[idx] = original_url
                continue

            preview_url = match_message_preview_url(job.messages, original_url, len(candidate_links))
            if preview_url and has_product_sku(preview_url):
                log(f"Используем URL из Telegram-превью без сетевого запроса для {original_url}: {preview_url}")
                pre_resolved[idx] = preview_url
                continue

            matched_sku = None
            if text_skus:
                if len(candidate_links) == 1 and len(text_skus) == 1:
                    matched_sku = text_skus[0]
                elif len(text_skus) == len(candidate_links):
                    matched_sku = text_skus[idx]

            if matched_sku:
                host = normalize_host(urlparse(original_url).hostname)
                if is_ozon_host(host):
                    canonical_host = resolve_ozon_canonical_host(host, self.settings.ozon_canonical_host)
                    pre_resolved[idx] = f"https://{canonical_host}/product/{matched_sku}/"
                else:
                    pre_resolved[idx] = f"https://www.wildberries.by/catalog/{matched_sku}/detail.aspx"
                log(f"Артикул {matched_sku} взят из текста поста без сетевого запроса для {original_url}")
                continue

            urls_to_resolve.append((idx, original_url))

        if urls_to_resolve:
            net_results = await asyncio.gather(
                *(self.resolve_url(url) for _, url in urls_to_resolve),
                return_exceptions=True,
            )
            for (idx, original_url), result in zip(urls_to_resolve, net_results):
                if isinstance(result, Exception):
                    log(f"Ошибка раскрытия {original_url}: {result}", logging.WARNING)
                    self.record_admin_error(f"Не удалось раскрыть ссылку: {type(result).__name__}")
                elif result:
                    pre_resolved[idx] = result

        products: list[ProductInfo] = []

        for idx, original_url in enumerate(candidate_links):
            resolved_url = pre_resolved.get(idx)
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
            self.record_job_result(job, "отклонено: товар не определён")
            return

        unique_products: dict[str, ProductInfo] = {}
        for product in products:
            unique_products.setdefault(product.sku_key, product)
        products = list(unique_products.values())

        # Ранняя дедупликация ДО парсинга карточек товаров
        source_message_id = min(message.id for message in job.messages)
        fresh_keys = self.db.reserve_fresh(products, job.source_chat_id, source_message_id)
        if not fresh_keys:
            self.state.duplicates += 1
            self.record_job_result(job, "дубликат")
            log(f"Все товары из сообщения {job.source_chat_id}/{source_message_id} уже обработаны")
            return

        fresh_candidates = [product for product in products if product.sku_key in fresh_keys]

        # Парсим карточки только для новых (не публиковавшихся ранее) товаров
        enriched_results = await asyncio.gather(
            *(
                self.enrich_product_with_card(
                    product,
                    messages=job.messages,
                    source_text=source_text,
                )
                for product in fresh_candidates
            ),
            return_exceptions=True,
        )

        available_products: list[ProductInfo] = []
        failed_keys: list[str] = []
        for orig_prod, enriched in zip(fresh_candidates, enriched_results):
            if isinstance(enriched, Exception):
                log(
                    f"Ошибка парсинга карточки {orig_prod.platform} ({orig_prod.sku}): {enriched}",
                    logging.WARNING,
                )
                self.record_admin_error(
                    f"{orig_prod.platform} {orig_prod.sku}: {type(enriched).__name__}: {enriched}"
                )
                failed_keys.append(orig_prod.sku_key)
                continue
            if enriched is None:
                self.state.out_of_stock += 1
                failed_keys.append(orig_prod.sku_key)
                continue
            available_products.append(enriched)

        if failed_keys:
            self.db.release_reservations(failed_keys)

        if not available_products:
            log(
                f"В сообщении {job.source_chat_id} нет доступных товаров в наличии — публикация пропущена"
            )
            self.record_job_result(job, "пропущено: карточка недоступна")
            return

        fresh_products = available_products
        fresh_keys = {product.sku_key for product in fresh_products}
        text = build_publication_text(
            source_text,
            fresh_products,
            include_source_notes=self.settings.include_source_notes,
        )

        if self.settings.dry_run:
            log(f"DRY_RUN — публикация НЕ выполнена {job.source_chat_id}/{source_message_id}:\n{text}")
            self.db.release_reservations(fresh_keys)
            self.record_job_result(job, "DRY_RUN", len(fresh_products))
            return

        try:
            await self.publish(text, job.messages, fresh_products)
        except FloodWaitError as exc:
            self.db.release_reservations(fresh_keys)
            self.state.errors += 1
            self.record_admin_error(f"FloodWait {exc.seconds} сек. для сообщения {source_message_id}")
            self.record_job_result(job, f"ошибка: FloodWait {exc.seconds} сек.")
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
            self.record_admin_error(
                f"Публикация {job.source_chat_id}/{source_message_id}: {type(exc).__name__}: {exc}"
            )
            self.record_job_result(job, f"ошибка публикации: {type(exc).__name__}")
            log(f"Ошибка публикации: {type(exc).__name__}: {exc}", logging.ERROR)
            await self.notify_admin(f"⚠️ Ошибка публикации:\n{type(exc).__name__}: {str(exc)[:300]}")
        else:
            self.db.mark_published(fresh_keys)
            self.state.published += 1
            self.state.last_published_at = time.time()
            self.record_job_result(job, "опубликовано", len(fresh_products))
            log(f"Опубликован пост {job.source_chat_id}/{source_message_id}; SKU: {sorted(fresh_keys)}")

    async def publish(
        self,
        text: str,
        messages: list,
        products: list[ProductInfo] | None = None,
    ) -> None:
        """Публикует пост в целевой канал, прикрепляя чистое фото из карточки товара (при наличии)."""
        card_images: list[io.BytesIO] = []
        if self.settings.prefer_card_image and products:
            img_urls = [p.image_url for p in products if p.image_url]
            if img_urls:
                downloaded = await asyncio.gather(
                    *(self.download_product_image(url) for url in img_urls),
                    return_exceptions=True,
                )
                for item in downloaded:
                    if isinstance(item, io.BytesIO):
                        card_images.append(item)

        fallback_media = [
            message.media
            for message in messages
            if message.media and not isinstance(message.media, MessageMediaWebPage)
        ]

        media_to_send = card_images if card_images else fallback_media

        if not text and not media_to_send:
            raise RuntimeError("Не сформирован текст и отсутствуют медиа для публикации")

        async with self.publish_lock:
            if not media_to_send:
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
                media_to_send if len(media_to_send) > 1 else media_to_send[0],
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
            active = False
            try:
                if not self.state.is_paused:
                    self.state.active_jobs += 1
                    active = True
                    await self.process_job(job)
            except Exception as exc:
                self.state.errors += 1
                self.record_admin_error(
                    f"Worker {worker_number}: {type(exc).__name__}: {exc}"
                )
                self.record_job_result(job, f"ошибка обработки: {type(exc).__name__}")
                log(f"Необработанная ошибка worker {worker_number}: {type(exc).__name__}: {exc}", logging.ERROR)
                await self.notify_admin(f"⚠️ Критическая ошибка worker: {type(exc).__name__}: {str(exc)[:250]}")
            finally:
                if active:
                    self.state.active_jobs -= 1
                self.queue.task_done()
