# Keep the runtime from the published 1.0.7 image; add only the admin backport.
FROM ghcr.io/sascha1994/tg_marketplace_parser:1.0.7@sha256:3101618a059a609685f3d45dbbd93ee8664601ab752689cdcac650a174d07139

COPY --chown=app:app tg_marketplace_parser/admin.py \
    tg_marketplace_parser/app.py \
    tg_marketplace_parser/core.py \
    tg_marketplace_parser/market_data.py \
    tg_marketplace_parser/models.py \
    tg_marketplace_parser/pipeline.py \
    tg_marketplace_parser/runtime.py \
    tg_marketplace_parser/storage.py \
    /app/tg_marketplace_parser/
