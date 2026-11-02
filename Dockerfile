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

COPY . .

CMD ["python", "parser.py"]