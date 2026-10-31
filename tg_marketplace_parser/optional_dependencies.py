import os
import shutil
import subprocess
import sys
from contextlib import suppress

def _load_curl_cffi():
    try:
        from curl_cffi.requests import AsyncSession as CurlAsyncSession
        return CurlAsyncSession
    except ImportError:
        pass

    if os.getenv("AUTO_INSTALL_CURL_CFFI", "true").strip().lower() not in {"1", "true", "yes", "on"}:
        return None

    for extra_args in ([], ["--break-system-packages"], ["--user"]):
        with suppress(Exception):
            res = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "-q",
                    "--disable-pip-version-check",
                    *extra_args,
                    "curl_cffi",
                ],
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )
            if res.returncode == 0:
                from curl_cffi.requests import AsyncSession as CurlAsyncSession
                return CurlAsyncSession
    return None

def _load_async_playwright():
    # Сначала пробуем patchright (undetected-форк Playwright), если он установлен
    with suppress(ImportError):
        from patchright.async_api import async_playwright
        return async_playwright

    with suppress(ImportError):
        from playwright.async_api import async_playwright
        return async_playwright

    if os.getenv("AUTO_INSTALL_PLAYWRIGHT", "true").strip().lower() not in {"1", "true", "yes", "on"}:
        return None

    for extra_args in ([], ["--break-system-packages"], ["--user"]):
        with suppress(Exception):
            res = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "-q",
                    "--disable-pip-version-check",
                    *extra_args,
                    "playwright",
                ],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            if res.returncode == 0:
                break

    with suppress(Exception):
        from playwright.async_api import async_playwright
        return async_playwright

    return None

def _find_system_chromium() -> str | None:
    custom_exec = os.getenv("OZON_CHROME_EXECUTABLE_PATH", "").strip()
    if custom_exec and os.path.isfile(custom_exec):
        return custom_exec

    candidates = (
        "google-chrome-stable",
        "google-chrome",
        "chromium",
        "chromium-browser",
        "chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    )
    for candidate in candidates:
        path = candidate if os.path.isabs(candidate) else shutil.which(candidate)
        if path and os.path.isfile(path):
            return path
    return None

def _ensure_playwright_browser_installed() -> None:
    if _find_system_chromium():
        return
    if os.getenv("AUTO_INSTALL_PLAYWRIGHT", "true").strip().lower() not in {"1", "true", "yes", "on"}:
        return
    with suppress(Exception):
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True,
            text=True,
            timeout=240,
            check=False,
        )

CurlAsyncSession = _load_curl_cffi()
async_playwright_factory = _load_async_playwright()

try:
    from PIL import Image as PILImage
except ImportError:
    PILImage = None
