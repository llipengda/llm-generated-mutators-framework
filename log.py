from collections.abc import Callable

from rich.console import Console

console = Console()

import os
if os.path.exists("tool_usage.log"):
    os.remove("tool_usage.log")
file_logger = Console(file=open("tool_usage.log", "a"))

_runtime_listener: Callable[[str], None] | None = None


def set_runtime_listener(listener: Callable[[str], None] | None) -> None:
    """Mirror concise runtime messages to an optional local UI consumer."""
    global _runtime_listener
    _runtime_listener = listener


def runtime_log(message: str) -> None:
    console.log(message)
    if _runtime_listener:
        _runtime_listener(message)
