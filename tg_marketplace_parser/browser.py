import asyncio
from contextlib import suppress
import json
import logging
import os
import random
import re
import time
from .common import log
from .config import Settings
from .constants import MINIMAL_STEALTH_SCRIPT
from .optional_dependencies import _ensure_playwright_browser_installed, _find_system_chromium, async_playwright_factory
from .browser_ozon import OzonBrowserMixin
from .browser_wb import WildberriesBrowserMixin


class OzonBrowserResolver(OzonBrowserMixin, WildberriesBrowserMixin):
    """Браузерный резолвер коротких ссылок и парсер карточек Ozon/WB с обходом антибота."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._playwright = None
        self._context = None
        self._warm_pages: dict[str, object] = {}
        self._lock = asyncio.Lock()
        self._last_used: float = 0.0
        self._idle_task: asyncio.Task | None = None
        self._available: bool = bool(settings.use_browser_resolver and async_playwright_factory is not None)
        self._cdp_ua_metadata: dict | None = None
        self.last_cookies: list[dict] = []
    @property
    def is_available(self) -> bool:
        return self._available
    @property
    def is_running(self) -> bool:
        return self._context is not None
    async def start_idle_monitor(self) -> None:
        if self._idle_task is None and self._available:
            self._idle_task = asyncio.create_task(self._idle_watchdog(), name="ozon-browser-watchdog")
    async def _idle_watchdog(self) -> None:
        while True:
            await asyncio.sleep(30)
            if self._context is not None and self._last_used > 0:
                idle_for = time.time() - self._last_used
                if idle_for >= self.settings.browser_idle_timeout:
                    async with self._lock:
                        if self._context is not None and (time.time() - self._last_used) >= self.settings.browser_idle_timeout:
                            log("Закрытие браузера Ozon после простоя для освобождения памяти")
                            await self._close_unlocked()
    async def _apply_cdp_stealth(self, page) -> None:
        """Применяет нативный C++ UserAgentOverride через CDP без расхождения ОС и без HeadlessChrome."""
        with suppress(Exception):
            if self._cdp_ua_metadata is None:
                raw_ua, raw_platform = await page.evaluate("[navigator.userAgent, navigator.platform]")
                clean_ua = raw_ua.replace("HeadlessChrome/", "Chrome/")
                ver_match = re.search(r"Chrome/(\d+)\.(\d+\.\d+\.\d+)", clean_ua)
                major = ver_match.group(1) if ver_match else "145"
                full_ver = f"{major}.{ver_match.group(2)}" if ver_match else "145.0.0.0"

                if "Linux" in raw_platform or "X11" in clean_ua:
                    platform_name = "Linux"
                    platform_ver = "6.5.0"
                elif "Mac" in raw_platform:
                    platform_name = "macOS"
                    platform_ver = "14.5.0"
                else:
                    platform_name = "Windows"
                    platform_ver = "10.0.0"

                self._cdp_ua_metadata = {
                    "userAgent": clean_ua,
                    "acceptLanguage": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
                    "platform": raw_platform or "Linux x86_64",
                    "userAgentMetadata": {
                        "brands": [
                            {"brand": "Not_A Brand", "version": "24"},
                            {"brand": "Chromium", "version": major},
                            {"brand": "Google Chrome", "version": major},
                        ],
                        "fullVersionList": [
                            {"brand": "Not_A Brand", "version": "24.0.0.0"},
                            {"brand": "Chromium", "version": full_ver},
                            {"brand": "Google Chrome", "version": full_ver},
                        ],
                        "fullVersion": full_ver,
                        "platform": platform_name,
                        "platformVersion": platform_ver,
                        "architecture": "x86",
                        "model": "",
                        "mobile": False,
                        "bitness": "64",
                        "wow64": False,
                    },
                }

            cdp = await page.context.new_cdp_session(page)
            await cdp.send("Emulation.setUserAgentOverride", self._cdp_ua_metadata)
    async def _ensure_context(self) -> bool:
        if not self._available:
            return False
        if self._context is not None:
            self._last_used = time.time()
            return True

        os.makedirs(self.settings.browser_profile_dir, exist_ok=True)
        for lock_file in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            lock_path = os.path.join(self.settings.browser_profile_dir, lock_file)
            with suppress(OSError):
                if os.path.lexists(lock_path):
                    os.unlink(lock_path)

        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, _ensure_playwright_browser_installed)

            self._playwright = await async_playwright_factory().start()
            cdp_url = os.getenv("OZON_BROWSER_CDP_URL", "").strip()
            if cdp_url:
                browser = await self._playwright.chromium.connect_over_cdp(cdp_url)
                contexts = browser.contexts
                self._context = contexts[0] if contexts else await browser.new_context()
                await self._context.add_init_script(MINIMAL_STEALTH_SCRIPT)
                log(f"Подключён внешний браузер по CDP: {cdp_url}")
            else:
                chrome_args = [
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-infobars",
                    "--window-size=1920,1080",
                    "--lang=ru-RU,ru",
                ]
                if self.settings.browser_headless:
                    chrome_args.append("--headless=new")

                launch_kwargs: dict = {
                    "user_data_dir": self.settings.browser_profile_dir,
                    "headless": self.settings.browser_headless,
                    "viewport": {"width": 1920, "height": 1080},
                    "locale": "ru-RU",
                    "timezone_id": "Europe/Minsk",
                    "java_script_enabled": True,
                    "ignore_https_errors": True,
                    "args": chrome_args,
                }
                if self.settings.browser_headless:
                    launch_kwargs["ignore_default_args"] = ["--headless"]

                exec_path = _find_system_chromium()
                if exec_path:
                    launch_kwargs["executable_path"] = exec_path
                else:
                    launch_kwargs["channel"] = "chromium"
                if self.settings.ozon_proxy:
                    launch_kwargs["proxy"] = {"server": self.settings.ozon_proxy}

                try:
                    self._context = await self._playwright.chromium.launch_persistent_context(**launch_kwargs)
                except Exception:
                    launch_kwargs.pop("ignore_default_args", None)
                    try:
                        self._context = await self._playwright.chromium.launch_persistent_context(**launch_kwargs)
                    except Exception:
                        launch_kwargs.pop("channel", None)
                        self._context = await self._playwright.chromium.launch_persistent_context(**launch_kwargs)

                await self._context.add_init_script(MINIMAL_STEALTH_SCRIPT)
                init_page = self._context.pages[0] if self._context.pages else await self._context.new_page()
                await self._apply_cdp_stealth(init_page)
                log("Запущен Chromium (--headless=new + CDP native UA) для обхода антибота Ozon")

            self._last_used = time.time()
            return True
        except Exception as exc:
            log(f"Не удалось запустить браузер Playwright для Ozon: {type(exc).__name__}: {exc}", logging.WARNING)
            await self._close_unlocked()
            return False
    async def _simulate_human_interaction(self, page) -> None:
        with suppress(Exception):
            for _ in range(3):
                x = random.randint(180, 1100)
                y = random.randint(140, 720)
                await page.mouse.move(x, y, steps=random.randint(4, 10))
                await asyncio.sleep(random.uniform(0.1, 0.25))
    async def _export_cookies(self) -> list[dict]:
        if self._context is None:
            return []
        with suppress(Exception):
            cookies = await self._context.cookies()
            if cookies:
                self.last_cookies = cookies
                with suppress(OSError):
                    with open(self.settings.cookies_cache_path, "w", encoding="utf-8") as f:
                        json.dump(cookies, f, ensure_ascii=False)
                return cookies
        return []
    async def _close_unlocked(self) -> None:
        for page in list(self._warm_pages.values()):
            with suppress(Exception):
                await page.close()
        self._warm_pages.clear()
        if self._context is not None:
            with suppress(Exception):
                await self._context.close()
            self._context = None
        if self._playwright is not None:
            with suppress(Exception):
                await self._playwright.stop()
            self._playwright = None
    async def close(self) -> None:
        if self._idle_task is not None:
            self._idle_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._idle_task
            self._idle_task = None
        async with self._lock:
            await self._close_unlocked()
