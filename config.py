import os
import threading
from typing import TypedDict

import dotenv

from log import console


class Config(TypedDict):
    protocol_name: str
    seed_dir: str
    rfc_path: str
    fixer: bool


# Thread-local storage for API session isolation.
# Each session runs in its own thread, so thread-local naturally isolates
# concurrent runs without changing the existing API.
_config_local = threading.local()

# Backward-compatible global for the main thread (CLI usage).
config: Config | None = None


def build_config_from_args(
    protocol: str,
    seed_dir: str,
    rfc_path: str,
    fixer: bool = False,
) -> None:
    cfg = Config(
        protocol_name=protocol,
        seed_dir=seed_dir,
        rfc_path=rfc_path,
        fixer=fixer,
    )
    global config
    config = cfg
    _config_local.config = cfg


def load_env() -> None:
    # Use absolute path so it works regardless of CWD (e.g. from uvicorn).
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    dotenv.load_dotenv(env_path)


def _get_config() -> Config:
    # Prefer thread-local config (set by API session), fall back to global.
    cfg = getattr(_config_local, "config", None)
    if cfg is not None:
        return cfg
    if config is not None:
        return config
    raise ValueError("Config not built yet.")


def get_protocol_name() -> str:
    return _get_config()["protocol_name"]


def get_seed_dir() -> str:
    return _get_config()["seed_dir"]


def get_rfc_path() -> str:
    return _get_config()["rfc_path"]


def get_fixer_enabled() -> bool:
    return _get_config()["fixer"]


def warn_if_rfc_missing(rfc_path: str) -> None:
    if not os.path.exists(rfc_path):
        console.print(
            f"[bold red]Warning:[/bold red] {rfc_path} not found. Ensure you have the RFC text file."
        )
