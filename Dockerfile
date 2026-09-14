# Build context is the mantau-prototype/ PARENT directory (this repo's
# sibling), e.g.: docker build -f Dockerfile -t mantau-server ..
# Needs the mantau-core sibling package, which isn't published anywhere yet.
FROM python:3.12-slim

WORKDIR /app
COPY mantau-core /app/mantau-core
COPY mantau-server /app/server

RUN pip install --no-cache-dir -e /app/mantau-core \
 && pip install --no-cache-dir -e /app/server

WORKDIR /app/server
ENV MANTAU_DB_PATH=/data/mantau_ld.db
VOLUME ["/data"]

EXPOSE 8100
CMD ["uvicorn", "mantau_ld.main:app", "--host", "0.0.0.0", "--port", "8100"]
