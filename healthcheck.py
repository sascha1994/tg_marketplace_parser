"""Проверка работоспособности контейнера по свежести файла heartbeat."""

import os
import sys
import time

MAX_AGE_SECONDS = int(os.getenv("HEALTHCHECK_MAX_AGE_SECONDS", "300"))
HEARTBEAT_PATH = os.getenv("HEARTBEAT_PATH", os.path.join("data", "heartbeat"))


def is_healthy() -> bool:
    try:
        age = time.time() - os.path.getmtime(HEARTBEAT_PATH)
    except OSError:
        return False
    return age <= MAX_AGE_SECONDS


if __name__ == "__main__":
    sys.exit(0 if is_healthy() else 1)
