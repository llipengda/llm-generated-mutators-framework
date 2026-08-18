import os
from typing import TypedDict

import dotenv

from log import console


class Config(TypedDict):
    protocol_name: str
    seed_dir: str
    rfc_paths: list[str]
    fixer: bool
    state_dir: str | None


config: Config | None = None


def build_config_from_args(
    protocol: str,
    seed_dir: str,
    rfc_paths: list[str],
    fixer: bool = False,
    state_dir: str | None = None,
) -> None:
    global config
    config = Config(
        protocol_name=protocol,
        seed_dir=seed_dir,
        rfc_paths=rfc_paths,
        fixer=fixer,
        state_dir=state_dir,
    )


def load_env() -> None:
    dotenv.load_dotenv(".env", override=True, verbose=True)


def get_protocol_name() -> str:
    if config is None:
        raise ValueError("Config not built yet.")
    return config["protocol_name"]


def get_seed_dir() -> str:
    if config is None:
        raise ValueError("Config not built yet.")
    return config["seed_dir"]


def get_rfc_paths() -> list[str]:
    if config is None:
        raise ValueError("Config not built yet.")
    return config["rfc_paths"]


def get_fixer_enabled() -> bool:
    if config is None:
        raise ValueError("Config not built yet.")
    return config["fixer"]


def get_state_dir() -> str | None:
    if config is None:
        raise ValueError("Config not built yet.")
    return config.get("state_dir")


def warn_if_rfc_missing(rfc_paths: list[str]) -> None:
    for p in rfc_paths:
        if not os.path.exists(p):
            console.print(
                f"[bold red]Warning:[/bold red] {p} not found. Ensure you have the RFC text file."
            )
