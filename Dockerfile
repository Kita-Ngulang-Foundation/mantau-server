# Standalone build: this repo alone is the build context (e.g. `docker
# build -t mantau-server .`, or Railway's GitHub-connected build). Fetches
# mantau-core from its own repo at a pinned commit instead of a local
# sibling checkout -- see requirements.txt's note on this transition.
FROM python:3.12-slim

WORKDIR /app
COPY . /app

RUN pip install --no-cache-dir \
      "mantau-core[push] @ https://github.com/Kita-Ngulang-Foundation/mantau-core/archive/75111dd24f03324a60cf4a06ae9218defbc0595a.tar.gz" \
 && pip install --no-cache-dir -e .

ENV MANTAU_DB_PATH=/data/mantau_ld.db

EXPOSE 8100
# MANTAU_FCM_SERVICE_ACCOUNT_JSON (raw key content, set as a plain Railway
# variable -- Railway has no MCP-automatable file-variable upload) is
# materialized to disk on every boot; MANTAU_FCM_SERVICE_ACCOUNT_PATH then
# just points at it. No-op when that var is unset (console-only alerts).
CMD ["sh", "-c", "if [ -n \"$MANTAU_FCM_SERVICE_ACCOUNT_JSON\" ]; then printf '%s' \"$MANTAU_FCM_SERVICE_ACCOUNT_JSON\" > \"$MANTAU_FCM_SERVICE_ACCOUNT_PATH\"; fi; exec uvicorn mantau_ld.main:app --host 0.0.0.0 --port \"${PORT:-8100}\""]
