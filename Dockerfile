FROM python:3.12-slim

WORKDIR /app

# Вариант 1: перенос строк через обратный слэш (\)
ENV PYTHONDONTWRITEBYTECODE=1 \
    AUTO_INSTALL_CURL_CFFI=false \
    AUTO_INSTALL_PLAYWRIGHT=false \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    TZ=Europe/Minsk \
    PYTHONUNBUFFERED=1

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        tzdata \
        fonts-liberation \
        fonts-noto-core && \
    playwright install-deps chromium && \
    rm -rf /var/lib/apt/lists/*

RUN playwright install chromium

# Непривилегированный пользователь; каталог данных должен быть доступен uid 1000
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin app && \
    mkdir -p /app/data && \
    chown -R app:app /app /ms-playwright

COPY --chown=app:app . .

USER app

HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
    CMD ["python", "healthcheck.py"]

CMD ["python", "parser.py"]
