FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    AUTO_INSTALL_CURL_CFFI=false \
    AUTO_INSTALL_PLAYWRIGHT=false \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    TZ=Europe/Minsk

# 1. Создаём непривилегированного пользователя (uid 1000) и рабочий каталог данных
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin app && \
    mkdir -p /app/data /ms-playwright && \
    chown -R app:app /app

# 2. Тяжёлый базовый слой: системные библиотеки + Playwright Chromium (редко меняется, кэшируется на сервере)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        tzdata \
        fonts-liberation \
        fonts-noto-core && \
    pip install "playwright>=1.47.0" && \
    playwright install-deps chromium && \
    playwright install chromium && \
    chmod -R a+rX /ms-playwright && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/* /root/.cache

# 3. Слой Python-зависимостей (пересобирается только при изменении requirements.txt)
COPY requirements.txt .
RUN pip install -r requirements.txt

# 4. Лёгкий слой с кодом приложения (~100 КБ — при обновлении кода скачивается только он)
COPY --chown=app:app tg_marketplace_parser ./tg_marketplace_parser
COPY --chown=app:app parser.py healthcheck.py ./

USER app

HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
    CMD ["python", "healthcheck.py"]

CMD ["python", "parser.py"]
