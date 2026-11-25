# Keep the runtime from the published 1.0.7 image.
FROM ghcr.io/sascha1994/tg_marketplace_parser:1.0.7@sha256:3101618a059a609685f3d45dbbd93ee8664601ab752689cdcac650a174d07139

ARG IMAGE_VERSION=dev
ARG IMAGE_REVISION=unknown
ARG IMAGE_CREATED=unknown

LABEL org.opencontainers.image.title="tg_marketplace_parser" \
      org.opencontainers.image.source="https://github.com/sascha1994/tg_marketplace_parser" \
      org.opencontainers.image.version="${IMAGE_VERSION}" \
      org.opencontainers.image.revision="${IMAGE_REVISION}" \
      org.opencontainers.image.created="${IMAGE_CREATED}"

COPY --chown=app:app tg_marketplace_parser/ /app/tg_marketplace_parser/
