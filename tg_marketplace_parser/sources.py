from contextlib import suppress
import logging
from telethon import events
from telethon.tl.functions.messages import GetDialogFiltersRequest
from telethon.utils import get_peer_id
from .common import log


class SourcesMixin:
    async def fetch_folder_channels(self) -> list[int]:
        target_name = self.settings.folder_name.casefold()
        response = await self.user_client(GetDialogFiltersRequest())
        filters = getattr(response, "filters", response)
        matched = None
        available: list[str] = []

        for dialog_filter in filters:
            title = getattr(dialog_filter, "title", None)
            if not title:
                continue
            title_text = title.text if hasattr(title, "text") else str(title)
            title_text = title_text.strip().strip("\"'")
            available.append(title_text)
            if title_text.casefold() == target_name:
                matched = dialog_filter
                break

        if matched is None:
            log(
                f"Папка {self.settings.folder_name!r} не найдена. Доступные папки: {available}",
                logging.WARNING,
            )
            return []

        include_peers = list(getattr(matched, "include_peers", []))
        pinned_peers = list(getattr(matched, "pinned_peers", []))
        log(
            f"Папка «{self.settings.folder_name}»: "
            f"include_peers={len(include_peers)}, pinned_peers={len(pinned_peers)}"
        )

        seen: set[int] = set()
        source_ids: list[int] = []

        for peer in [*include_peers, *pinned_peers]:
            peer_id = get_peer_id(peer)
            if peer_id in seen:
                continue
            seen.add(peer_id)

            if peer_id == self.target_peer_id:
                log(f"Целевой канал {peer_id} исключён из источников", logging.WARNING)
                continue

            source_ids.append(peer_id)

        return source_ids

    async def update_sources(self) -> int:
        source_ids = await self.fetch_folder_channels()
        self.current_sources = set(source_ids)
        with suppress(Exception):
            self.user_client.remove_event_handler(self.incoming_post_handler)
        if self.current_sources:
            self.user_client.add_event_handler(
                self.incoming_post_handler,
                events.NewMessage(chats=list(self.current_sources)),
            )
        log(f"Источники обновлены: {len(self.current_sources)}")
        if not self.current_sources:
            log("Нет доступных источников: проверьте папку и её явно добавленные чаты", logging.WARNING)
        return len(self.current_sources)
