"""Uvicorn entrypoint: `uvicorn mantau_ld.main:app` or `python -m mantau_ld.main`."""

from __future__ import annotations

import uvicorn

from .api.app import create_app

app = create_app()

if __name__ == "__main__":
    uvicorn.run("mantau_ld.main:app", host="0.0.0.0", port=8100, reload=False)
