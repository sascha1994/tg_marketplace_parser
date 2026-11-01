import asyncio
import io
import json
import logging
from dataclasses import replace
from datetime import datetime, time as datetime_time, timedelta
from telethon.errors import FloodWaitError
from telethon.tl.types import MessageMediaWebPage
import time
from urllib.parse import urlparse
from zoneinfo import ZoneInfo
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

    def quiet_period_remaining(self, timestamp: float | None = None) -> int:
        now = datetime.fromtimestamp(
            time.time() if timestamp is None else timestamp,
            ZoneInfo(self.settings.timezone),
        )
        start = datetime_time.fromisoformat(self.db.get_setting("quiet_hours_start", "01:30"))
        end = datetime_time.fromisoformat(self.db.get_setting("quiet_hours_end", "07:00"))
        current = now.time().replace(tzinfo=None)
        if start < end:
            is_quiet = start <= current < end
            end_date = now.date()
        else:
            is_quiet = current >= start or current < end
            end_date = now.date() if current < end else now.date() + timedelta(days=1)
        if not is_quiet:
            return 0
        end_dt = datetime.combine(end_date, end, tzinfo=now.tzinfo)
        return max(1, int((end_dt - now).total_seconds()))

    async def fetch_card_for_product(self, product: ProductInfo, messages=(), source_text: str = ""):
        if product.platform == "Wildberries":
            return await self.fetch_wb_card_data(
                product.sku or "",
                messages=messages,
                source_text=source_text,
            )
        return await self.fetch_ozon_card_data(
            product.sku or "",
            original_url=product.original_url,
            messages=messages,
        )

    def ozon_discount_filter_enabled(self) -> bool:
        return self.db.get_setting("ozon_discount_filter", "true") == "true"

    def passes_ozon_discount_filter(self, product: ProductInfo) -> bool:
        if product.platform != "Ozon" or not self.ozon_discount_filter_enabled():
            return True
        discount = product.discount_percent
        return discount is None or discount >= 30

    def active_target_channel(self) -> str:
        return self.db.get_setting("publication_target", "main")

    async def get_channel_entity(self, channel_key: str):
        if channel_key == "main":
            return self.target_entity
        if channel_key != "test" or not self.settings.test_channel:
            raise RuntimeError(f"Канал {channel_key!r} не настроен")
        entity = self.channel_entities.get(channel_key)
        if entity is None:
            target_reference = self.settings.test_channel
            if target_reference.lstrip("-").isdigit():
                target_reference = int(target_reference)
            entity = await self.user_client.get_entity(target_reference)
            self.channel_entities[channel_key] = entity
        return entity

    async def publish_product(
        self,
        product: ProductInfo,
        source_text: str,
        messages: list,
        source_chat_id: int,
        source_message_id: int,
        target_channel: str | None = None,
    ) -> bool:
        target_channel = target_channel or self.active_target_channel()
        async with self.publication_gate_lock:
            pause_seconds = self.quiet_period_remaining()
            if pause_seconds:
                self.db.save_scheduled_publication(
                    product,
                    source_text,
                    source_chat_id,
                    int(time.time()) + pause_seconds,
                    target_channel=target_channel,
                )
                log(f"Публикация {product.platform} {product.sku} отложена до окончания перерыва")
                return False

            text = build_publication_text(
                source_text,
                [product],
                include_source_notes=self.settings.include_source_notes,
            )
            message_ids = await self.publish(text, messages, [product], target_channel)
            if not message_ids:
                raise RuntimeError("Telegram не вернул ID опубликованного сообщения")
            published_at = int(time.time())
            self.db.save_published_post(
                product,
                text,
                message_ids,
                product.discount_percent if product.platform == "Ozon" else None,
                published_at,
                target_channel=target_channel,
            )
            return True

    async def handle_published_post_check(self, row: dict, product: ProductInfo, card, now: int) -> None:
        try:
            target_entity = await self.get_channel_entity(row["target_channel"])
            if card is not None and card.in_stock is False:
                message_ids = json.loads(row["message_ids"])
                if not message_ids:
                    raise RuntimeError("в базе отсутствуют ID сообщения для товара")
                if (row["discount_percent"] or 0) > 50:
                    await self.user_client.edit_message(
                        target_entity,
                        message_ids[0],
                        row["publication_text"] + "\n\n❌ Неактуально",
                        parse_mode=None,
                    )
                    if len(message_ids) > 1:
                        await self.user_client.delete_messages(target_entity, message_ids[1:])
                    self.db.set_published_post_status(row["post_id"], "unavailable_marked")
                    log(f"Пост {product.platform} {product.sku} помечен как неактуальный")
                else:
                    await self.user_client.delete_messages(target_entity, message_ids)
                    self.db.set_published_post_status(row["post_id"], "deleted")
                    log(f"Пост {product.platform} {product.sku} удалён: товара нет в наличии")
            else:
                delay = 3600 if card is not None and card.in_stock is True else 15 * 60
                self.db.defer_published_post_check(row["post_id"], now + delay)
                if card is None:
                    log(
                        f"Проверка наличия {product.platform} {product.sku} не дала результата; "
                        "пост оставлен без изменений",
                        logging.WARNING,
                    )
        except Exception as exc:
            self.db.defer_published_post_check(row["post_id"], now + 15 * 60)
            self.record_admin_error(
                f"Проверка наличия {product.platform} {product.sku}: {type(exc).__name__}: {exc}"
            )
            log(
                f"Ошибка проверки наличия {product.platform} {product.sku}: {exc}",
                logging.WARNING,
            )

    async def handle_scheduled_publication(self, row: dict, card) -> None:
        product = ProductInfo(**json.loads(row["product_json"]))
        now = int(time.time())
        try:
            if card is None:
                raise RuntimeError("карточка недоступна или проверка наличия не дала результата")
            if card.in_stock is False:
                self.db.release_reservations([product.sku_key])
                log(f"Отложенная публикация отменена: {product.platform} {product.sku} отсутствует")
                return
            if card.in_stock is not True:
                raise RuntimeError("наличие товара не подтверждено")
            if (
                product.platform == "Wildberries"
                and card.total_quantity is not None
                and card.total_quantity < 3
            ):
                self.db.release_reservations([product.sku_key])
                log(
                    f"Отложенная публикация WB {product.sku} отменена: "
                    f"остаток {card.total_quantity} (< 3)"
                )
                return
            if card.price_byn is None or card.price_byn <= 0:
                raise RuntimeError("не удалось подтвердить актуальную цену")

            refreshed = replace(
                product,
                title=card.title or product.title,
                brand=card.brand or product.brand,
                price_byn=card.price_byn,
                avg_month_price_byn=card.avg_month_price_byn,
                old_price_byn=card.old_price_byn,
                image_url=card.image_url or product.image_url,
                in_stock=card.in_stock is not False,
                total_quantity=card.total_quantity,
                rating=card.rating,
                reviews_count=card.reviews_count,
            )
            if refreshed.platform == "Ozon" and not self.passes_ozon_discount_filter(refreshed):
                discount = refreshed.discount_percent
                if discount is None:
                    raise RuntimeError("не найдена средняя цена для проверки скидки Ozon")
                self.db.release_reservations([product.sku_key])
                log(
                    f"Отложенная публикация Ozon {product.sku} отменена: "
                    f"скидка {discount:.1f}% (< 30%)"
                )
                return

            published = await self.publish_product(
                refreshed,
                row["source_text"],
                [],
                row["source_chat_id"],
                0,
                row["target_channel"],
            )
            if published:
                self.state.published += 1
                self.state.last_published_at = time.time()
        except Exception as exc:
            attempts = row["attempts"] + 1
            if attempts >= 20:
                self.db.release_reservations([product.sku_key])
                self.record_admin_error(
                    f"Отложенная публикация {product.platform} {product.sku} прекращена "
                    f"после {attempts} проверок: {type(exc).__name__}: {exc}"
                )
                await self.notify_admin(
                    f"⚠️ Отложенная публикация {product.platform} {product.sku} "
                    "снята после 20 неудачных проверок."
                )
            else:
                self.db.reschedule_publication(product.sku_key, now + 15 * 60)
                log(
                    f"Повтор проверки отложенной публикации {product.platform} {product.sku} "
                    f"через 15 минут: {type(exc).__name__}: {exc}",
                    logging.WARNING,
                )

    async def scheduled_publication_worker(self) -> None:
        while True:
            now = int(time.time())
            if not self.quiet_period_remaining():
                scheduled = self.db.get_due_scheduled_publications(now, limit=20)
                if scheduled:
                    wb_rows = [
                        row for row in scheduled
                        if json.loads(row["product_json"]).get("platform") == "Wildberries"
                    ]
                    if len(wb_rows) > 1:
                        try:
                            cards = await self.fetch_wb_cards_batch(
                                json.loads(row["product_json"]).get("sku") or ""
                                for row in wb_rows
                            )
                        except Exception as exc:
                            cards = {}
                            self.record_admin_error(f"Пакетная проверка отложенных WB: {type(exc).__name__}: {exc}")
                        for row in wb_rows:
                            product = ProductInfo(**json.loads(row["product_json"]))
                            await self.handle_scheduled_publication(row, cards.get(product.sku or ""))
                        await asyncio.sleep(1)
                        continue

                    row = scheduled[0]
                    product = ProductInfo(**json.loads(row["product_json"]))
                    if product.platform == "Ozon":
                        if time.time() - self._last_ozon_stock_check_at >= 60:
                            self._last_ozon_stock_check_at = time.time()
                            try:
                                card = await self.fetch_card_for_product(
                                    product,
                                    source_text=row["source_text"],
                                )
                            except Exception:
                                card = None
                            await self.handle_scheduled_publication(row, card)
                            await asyncio.sleep(1)
                            continue
                    else:
                        try:
                            card = await self.fetch_card_for_product(
                                product,
                                source_text=row["source_text"],
                            )
                        except Exception:
                            card = None
                        await self.handle_scheduled_publication(row, card)
                        await asyncio.sleep(1)
                        continue

            due_posts = self.db.get_due_published_posts(now, limit=20)
            due_wb_posts = [
                row for row in due_posts
                if json.loads(row["product_json"]).get("platform") == "Wildberries"
            ]
            if len(due_wb_posts) > 1:
                products_by_key = {
                    row["sku_key"]: ProductInfo(**json.loads(row["product_json"]))
                    for row in due_wb_posts
                }
                try:
                    cards = await self.fetch_wb_cards_batch(
                        product.sku or "" for product in products_by_key.values()
                    )
                except Exception as exc:
                    cards = {}
                    self.record_admin_error(f"Пакетная проверка WB: {type(exc).__name__}: {exc}")
                    log(f"Ошибка пакетной проверки WB: {exc}", logging.WARNING)
                for row in due_wb_posts:
                    product = products_by_key[row["sku_key"]]
                    card = cards.get(product.sku or "")
                    await self.handle_published_post_check(row, product, card, now)
                await asyncio.sleep(1)
                continue
            if due_posts:
                row = due_posts[0]
                product = ProductInfo(**json.loads(row["product_json"]))
                is_ozon = product.platform == "Ozon"
                if is_ozon and time.time() - self._last_ozon_stock_check_at < 60:
                    await asyncio.sleep(5)
                    continue
                if is_ozon:
                    self._last_ozon_stock_check_at = time.time()
                try:
                    card = await self.fetch_card_for_product(product)
                except Exception as exc:
                    card = None
                    self.record_admin_error(
                        f"Проверка наличия {product.platform} {product.sku}: "
                        f"{type(exc).__name__}: {exc}"
                    )
                await self.handle_published_post_check(row, product, card, now)
                await asyncio.sleep(1)
                continue
            await asyncio.sleep(10)

    async def process_job(self, job: Job) -> None:
        job.target_channel = job.target_channel or self.active_target_channel()
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
            if job.target_channel == "test":
                product = replace(product, sku_key=f"test:{product.sku_key}")
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

        fresh_products = []
        for product in available_products:
            if product.platform == "Ozon" and not self.passes_ozon_discount_filter(product):
                discount = product.discount_percent
                if discount is None:
                    log(
                        f"Ozon {product.sku}: публикация пропущена — "
                        "не найдена средняя цена для проверки скидки",
                        logging.WARNING,
                    )
                    failed_keys.append(product.sku_key)
                    continue
                log(
                    f"Ozon {product.sku}: публикация пропущена — "
                    f"скидка {discount:.1f}% меньше 30%"
                )
                failed_keys.append(product.sku_key)
                continue
            fresh_products.append(product)

        if failed_keys:
            self.db.release_reservations(failed_keys)
        if not fresh_products:
            self.record_job_result(job, "пропущено: фильтр остатков/скидки")
            return

        fresh_keys = {product.sku_key for product in fresh_products}

        if self.settings.dry_run:
            for product in fresh_products:
                text = build_publication_text(
                    source_text,
                    [product],
                    include_source_notes=self.settings.include_source_notes,
                )
                log(f"DRY_RUN — публикация НЕ выполнена {job.source_chat_id}/{source_message_id}:\n{text}")
            self.db.release_reservations(fresh_keys)
            self.record_job_result(job, "DRY_RUN", len(fresh_products))
            return

        published_count = 0
        scheduled_count = 0
        for index, product in enumerate(fresh_products):
            try:
                was_published = await self.publish_product(
                    product,
                    source_text,
                    job.messages,
                    job.source_chat_id,
                    source_message_id,
                    job.target_channel,
                )
            except FloodWaitError as exc:
                remaining = {item.sku_key for item in fresh_products[index:]}
                self.db.release_reservations(remaining)
                self.state.errors += 1
                self.record_admin_error(f"FloodWait {exc.seconds} сек. для сообщения {source_message_id}")
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
                self.record_job_result(job, f"ошибка: FloodWait {exc.seconds} сек.")
                return
            except Exception as exc:
                self.db.release_reservations([product.sku_key])
                self.state.errors += 1
                self.record_admin_error(
                    f"Публикация {job.source_chat_id}/{source_message_id}: {type(exc).__name__}: {exc}"
                )
                log(f"Ошибка публикации: {type(exc).__name__}: {exc}", logging.ERROR)
                await self.notify_admin(f"⚠️ Ошибка публикации:\n{type(exc).__name__}: {str(exc)[:300]}")
                continue
            if was_published:
                published_count += 1
                self.state.published += 1
                self.state.last_published_at = time.time()
                log(f"Опубликован товар {job.source_chat_id}/{source_message_id}: {product.sku_key}")
            else:
                scheduled_count += 1

        result = "опубликовано" if published_count else "отложено до окончания перерыва"
        if published_count and scheduled_count:
            result = "частично опубликовано, остаток отложен"
        self.record_job_result(job, result, published_count + scheduled_count)

    async def publish(
        self,
        text: str,
        messages: list,
        products: list[ProductInfo] | None = None,
        target_channel: str | None = None,
    ) -> list[int]:
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

        destination = await self.get_channel_entity(target_channel or self.active_target_channel())
        async with self.publish_lock:
            sent_messages = []
            if not media_to_send:
                for text_part in split_text(text, limit=4000):
                    sent_messages.append(await self.user_client.send_message(
                        destination,
                        text_part,
                        link_preview=False,
                    ))
                return [int(message.id) for message in sent_messages if getattr(message, "id", None)]

            text_parts = split_text(text, limit=1024)
            caption = text_parts.pop(0) if text_parts else None

            sent = await self.user_client.send_file(
                destination,
                media_to_send if len(media_to_send) > 1 else media_to_send[0],
                caption=caption,
                parse_mode=None,
            )
            sent_messages.extend(sent if isinstance(sent, list) else [sent])

            for text_part in text_parts:
                sent_messages.append(await self.user_client.send_message(
                    destination,
                    text_part,
                    link_preview=False,
                ))
            return [int(message.id) for message in sent_messages if getattr(message, "id", None)]

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
