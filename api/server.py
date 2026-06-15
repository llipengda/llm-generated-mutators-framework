"""FastAPI application for the Peach pipeline HTTP API."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.deps import get_session_manager
from api.routes import router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown logic."""
    # Load .env so LLM / embedding credentials are available.
    from config import load_env

    load_env()

    # Validate SDK availability at startup (non-fatal — reported via /health).
    sdk_ok = os.path.isdir("./peach/sdk/")
    if not sdk_ok:
        print(
            "[WARNING] peach/sdk/ not found. "
            "Run `./setup.sh peach` first. "
            "Peach pipeline steps will fail until the SDK is installed."
        )

    # Recover sessions from persisted pipeline state files.
    manager = get_session_manager()
    recovered = manager.recover_sessions()
    if recovered:
        print(f"[INFO] Recovered {recovered} session(s) from disk.")

    yield


app = FastAPI(
    title="Peach Pipeline API",
    description="HTTP API for the LLM-assisted Peach fuzzer pipeline.",
    version="0.1.0",
    lifespan=lifespan,
)

# Allow all origins for development; tighten in production.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


@app.get("/")
def root():
    return {"service": "Peach Pipeline API", "version": "0.1.0"}
