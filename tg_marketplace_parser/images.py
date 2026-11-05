import asyncio
from contextlib import suppress
import io
from typing import Any
from .common import convert_image_bytes_to_jpeg, normalize_image_url
from .constants import BROWSER_USER_AGENT, IMAGE_READ_LIMIT


class ImageDownloadMixin:
    @staticmethod
    def _is_body_truncated_by_headers(raw_bytes: bytes, headers: Any) -> bool:
        """Проверяет, не оборвалась ли загрузка файла раньше заявленного Content-Length."""
        if not raw_bytes:
            return True
        if not headers:
            return False
        encoding = (headers.get("content-encoding") or headers.get("Content-Encoding") or "").strip().lower()
        if encoding and encoding != "identity":
            return False
        raw_len = headers.get("content-length") or headers.get("Content-Length")
        if raw_len is None:
            return False
        with suppress(TypeError, ValueError):
            expected_len = int(str(raw_len).strip())
            if expected_len > IMAGE_READ_LIMIT:
                return True
            if expected_len > 0 and len(raw_bytes) < expected_len:
                return True
        return False
    async def download_product_image(self, image_url: str) -> io.BytesIO | None:
        """Скачивает главное фото из карточки товара и подготавливает его как JPEG для отправки в Telegram."""
        clean_url = normalize_image_url(image_url)
        if not clean_url:
            return None

        candidate_urls = [clean_url]
        if "wbbasket.ru/" in clean_url or "wbcontent.net/" in clean_url:
            if clean_url.endswith("/images/big/1.webp"):
                candidate_urls.append(clean_url[:-5] + ".jpg")
                candidate_urls.append(clean_url.replace("/images/big/1.webp", "/images/c516x688/1.webp"))
                candidate_urls.append(clean_url.replace("/images/big/1.webp", "/images/c516x688/1.jpg"))
        elif ("ozone.ru/" in clean_url or "ozonusercontent.com/" in clean_url) and "/wc1000/" in clean_url:
            orig_cleaned = (image_url or "").strip()
            if orig_cleaned.startswith("//"):
                orig_cleaned = "https:" + orig_cleaned
            if orig_cleaned.startswith(("http://", "https://")) and orig_cleaned not in candidate_urls:
                candidate_urls.append(orig_cleaned)
            for fallback_wc in ("/wc750/", "/wc500/"):
                fb_url = clean_url.replace("/wc1000/", fallback_wc)
                if fb_url not in candidate_urls:
                    candidate_urls.append(fb_url)

        img_headers = {
            "User-Agent": BROWSER_USER_AGENT,
            "Accept": "image/jpeg,image/png,image/webp,image/apng,image/*;q=0.8,*/*;q=0.5",
            "Accept-Language": "ru-RU,ru;q=0.9",
        }

        for target_url in candidate_urls:
            if self.curl_session is not None:
                got_404 = False
                for attempt in range(2):
                    with suppress(Exception):
                        async with self.resolve_semaphore:
                            resp = await self.curl_session.get(
                                target_url,
                                headers=img_headers,
                                allow_redirects=True,
                                timeout=self.settings.request_timeout_seconds,
                            )
                            if resp.status_code == 404:
                                got_404 = True
                                break
                            if 200 <= resp.status_code < 300 and resp.content:
                                raw = resp.content[:IMAGE_READ_LIMIT]
                                if not self._is_body_truncated_by_headers(raw, getattr(resp, "headers", None)):
                                    converted = await asyncio.to_thread(convert_image_bytes_to_jpeg, raw)
                                    if converted is not None:
                                        return converted
                    if attempt == 0:
                        await asyncio.sleep(0.25)
                if got_404:
                    continue

            if self.http_session is not None:
                for attempt in range(2):
                    got_404 = False
                    with suppress(Exception):
                        async with self.resolve_semaphore:
                            async with self.http_session.get(
                                target_url,
                                headers=img_headers,
                                allow_redirects=True,
                            ) as resp:
                                if resp.status == 404:
                                    got_404 = True
                                elif 200 <= resp.status < 300:
                                    raw = await resp.content.read(IMAGE_READ_LIMIT)
                                    if not self._is_body_truncated_by_headers(raw, getattr(resp, "headers", None)):
                                        converted = await asyncio.to_thread(convert_image_bytes_to_jpeg, raw)
                                        if converted is not None:
                                            return converted
                    if got_404:
                        break
                    if attempt == 0:
                        await asyncio.sleep(0.25)

        return None
