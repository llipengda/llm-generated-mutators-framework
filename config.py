import os
from typing import TypedDict

import dotenv

from log import console


class Config(TypedDict):
    protocol_name: str
    seed_dir: str
    rfc_path: str
    fixer: bool


config: Config | None = None


def build_config_from_args(
    protocol: str,
    seed_dir: str,
    rfc_path: str,
    fixer: bool = False,
) -> None:
    global config
    config = Config(
        protocol_name=protocol,
        seed_dir=seed_dir,
        rfc_path=rfc_path,
        fixer=fixer,
    )


def load_env() -> None:
    dotenv.load_dotenv(".env")


def get_protocol_name() -> str:
    if config is None:
        raise ValueError("Config not built yet.")
    return config["protocol_name"]


def get_seed_dir() -> str:
    if config is None:
        raise ValueError("Config not built yet.")
    return config["seed_dir"]


def get_rfc_path() -> str:
    if config is None:
        raise ValueError("Config not built yet.")
    return config["rfc_path"]


def get_fixer_enabled() -> bool:
    if config is None:
        raise ValueError("Config not built yet.")
    return config["fixer"]


def warn_if_rfc_missing(rfc_path: str) -> None:
    if not os.path.exists(rfc_path):
        console.print(
            f"[bold red]Warning:[/bold red] {rfc_path} not found. Ensure you have the RFC text file."
        )
