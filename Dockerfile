FROM python:3.12-slim

WORKDIR /app

# Вариант 1: перенос строк через обратный слэш (\)
ENV PYTHONDONTWRITEBYTECODE=1 \
    AUTO_INSTALL_CURL_CFFI=false \
    AUTO_INSTALL_PLAYWRIGHT=false \
    TZ=Europe/Minsk \
    PYTHONUNBUFFERED=1

COPY requirements.txt .

# Все строки внутри одного RUN объединены через && и \ без пустых строк
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        tzdata \
        fonts-liberation \
        fonts-noto-core && \
    pip install --no-cache-dir -U pip && \
    pip install --no-cache-dir -r requirements.txt && \
    playwright install --with-deps chromium && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

COPY . .

CMD ["python", "parser.py"]