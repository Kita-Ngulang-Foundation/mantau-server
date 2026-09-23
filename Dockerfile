# syntax=docker/dockerfile:1
FROM python:3.12-slim

# Server inference runs MediaPipe (mantau-AI via mantau-core's `detection`
# extra), which needs these GL/EGL libraries and git for the pinned install.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git libgl1 libglib2.0-0 libegl1 libgles2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app

# mantau-AI is a private repository. Build with a GitHub token that can read
# it to enable server inference:
#   docker build --secret id=github_token,env=GITHUB_TOKEN .
# The token is passed through git's environment only; it is never written to
# a file or an image layer. Without it the server still runs and
# GET /inference/capability reports inference as unavailable.
RUN --mount=type=secret,id=github_token \
    ref="$(tr -d '[:space:]' < mantau-core.ref)"; \
    core="mantau-core[push] @ https://github.com/Kita-Ngulang-Foundation/mantau-core/archive/${ref}.tar.gz"; \
    if [ -s /run/secrets/github_token ]; then \
      GIT_CONFIG_COUNT=1 \
      GIT_CONFIG_KEY_0="url.https://x-access-token:$(cat /run/secrets/github_token)@github.com/.insteadOf" \
      GIT_CONFIG_VALUE_0="https://github.com/" \
      pip install --no-cache-dir "mantau-core[push,detection] @ https://github.com/Kita-Ngulang-Foundation/mantau-core/archive/${ref}.tar.gz"; \
    else \
      pip install --no-cache-dir "$core"; \
    fi \
 && pip install --no-cache-dir -e .

ENV MANTAU_DB_PATH=/data/mantau_ld.db
ENV MANTAU_RECORDINGS_DIR=/data/recordings

EXPOSE 8100
CMD ["sh", "-c", "if [ -n \"$MANTAU_FCM_SERVICE_ACCOUNT_JSON\" ]; then printf '%s' \"$MANTAU_FCM_SERVICE_ACCOUNT_JSON\" > \"$MANTAU_FCM_SERVICE_ACCOUNT_PATH\"; fi; exec uvicorn mantau_ld.main:app --host 0.0.0.0 --port \"${PORT:-8100}\""]
