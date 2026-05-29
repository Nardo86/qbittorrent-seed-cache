FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# --- build stage: install into a venv --------------------------------------
FROM base AS build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --upgrade pip && pip install .

# --- runtime stage ---------------------------------------------------------
FROM base AS runtime

# Non-root user that matches the project's PUID/PGID convention
ARG PUID=1000
ARG PGID=1000
RUN groupadd -g ${PGID} app && useradd -u ${PUID} -g ${PGID} -s /usr/sbin/nologin -M app

COPY --from=build /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

RUN mkdir -p /var/lib/seed-cache /etc/qbittorrent-seed-cache && \
    chown -R app:app /var/lib/seed-cache /etc/qbittorrent-seed-cache

USER app
VOLUME ["/var/lib/seed-cache"]

ENV QBSC_CONFIG=/etc/qbittorrent-seed-cache/config.yaml

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -m qbittorrent_seed_cache.healthcheck || exit 1

ENTRYPOINT ["qbittorrent-seed-cache"]
