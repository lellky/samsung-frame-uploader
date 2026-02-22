FROM python:3.12-slim-bookworm

# Pillow wheels usually work without system libs, but install basic JPEG/Zlib runtime libs.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git \
        libjpeg62-turbo \
        zlib1g \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies first (better layer caching)
COPY pyproject.toml /app/pyproject.toml
COPY src /app/src

# Optional override to install samsungtvws from a specific fork/branch before installing this project.
# By default, we rely on the `samsungtvws` dependency declared in `pyproject.toml` (PyPI).
# Example:
#   docker build --build-arg SAMSUNGTVWS_PIP_SPEC='git+https://github.com/lellky/samsung-tv-ws-api.git@fix-0-97' .
ARG SAMSUNGTVWS_PIP_SPEC=

RUN pip install --no-cache-dir --upgrade pip \
    && if [ -n "$SAMSUNGTVWS_PIP_SPEC" ]; then pip install --no-cache-dir "$SAMSUNGTVWS_PIP_SPEC"; fi \
    && pip install --no-cache-dir .

ENV INPUT_DIR=/data/incoming \
    OPTIMIZED_DIR=/data/optimized \
    DB_PATH=/data/db/frame_uploader.sqlite3 \
    INTERVAL_SECONDS=300 \
    UPLOAD_INTERVAL_SECONDS=300 \
    KEEP_ON_TV=20 \
    JPEG_QUALITY=85 \
    CLEANUP_MODE=app_only \
    SELECT_AFTER_UPLOAD=true \
    MATTE=none \
    RESIZE_MODE=pad \
    BACKGROUND_COLOR=black

CMD ["frame-uploader-app"]
