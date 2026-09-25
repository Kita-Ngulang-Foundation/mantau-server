# syntax=docker/dockerfile:1
FROM python:3.12-slim

# Server inference runs MediaPipe and needs these GL/EGL libraries.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 libegl1 libgles2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app

# The private detector wheel is added only to a clean local deployment archive.
# A required build fails if that wheel is missing, so a source-only deploy cannot
# silently remove server inference. The wheel is never committed to this repo.
ARG MANTAU_REQUIRE_DETECTOR=0
RUN set -eu; \
    ref="$(tr -d '[:space:]' < mantau-core.ref)"; \
    pip install --no-cache-dir "mantau-core[push] @ https://github.com/Kita-Ngulang-Foundation/mantau-core/archive/${ref}.tar.gz"; \
    wheel=private-deps/mantau_prototype-0.1.0-py3-none-any.whl; \
    if [ -f "$wheel" ]; then \
      pip install --no-cache-dir "$wheel"; \
    elif [ "$MANTAU_REQUIRE_DETECTOR" = 1 ]; then \
      echo 'Required private detector wheel is missing' >&2; \
      exit 1; \
    fi; \
    pip install --no-cache-dir -e .

ENV MANTAU_DB_PATH=/data/mantau_ld.db
ENV MANTAU_RECORDINGS_DIR=/data/recordings

EXPOSE 8100
CMD ["sh", "-c", "if [ -n \"$MANTAU_FCM_SERVICE_ACCOUNT_JSON\" ]; then printf '%s' \"$MANTAU_FCM_SERVICE_ACCOUNT_JSON\" > \"$MANTAU_FCM_SERVICE_ACCOUNT_PATH\"; fi; exec uvicorn mantau_ld.main:app --host 0.0.0.0 --port \"${PORT:-8100}\""]
