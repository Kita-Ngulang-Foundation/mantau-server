# Standalone build: this repo alone is the build context (e.g. `docker
# build -t mantau-server .`, or Railway's GitHub-connected build). Fetches
# mantau-core from its own repo at a pinned commit instead of a local
# sibling checkout -- see requirements.txt's note on this transition.
FROM python:3.12-slim

WORKDIR /app
COPY . /app

RUN pip install --no-cache-dir \
      "mantau-core[push] @ https://github.com/Kita-Ngulang-Foundation/mantau-core/archive/bfebdcc0419ba57982171b45b5375979991cb906.tar.gz" \
 && pip install --no-cache-dir -e .

ENV MANTAU_DB_PATH=/data/mantau_ld.db

EXPOSE 8100
CMD ["uvicorn", "mantau_ld.main:app", "--host", "0.0.0.0", "--port", "8100"]
