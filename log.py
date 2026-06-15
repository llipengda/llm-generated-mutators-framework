from __future__ import annotations

import os
import re
import threading
from typing import TextIO

from rich.console import Console

# ── Global consoles (unchanged, for CLI mode) ─────────────────────

console = Console()
_log_path: str = "tool_usage.log"
_log_file: TextIO | None = None
_log_lock = threading.Lock()


def _open_log(path: str) -> TextIO:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    return open(path, "a", encoding="utf-8")


if os.path.exists(_log_path):
    os.remove(_log_path)
_log_file = _open_log(_log_path)
file_logger = Console(file=_log_file)

# ── Thread-local API session ──────────────────────────────────────

_session_local = threading.local()
_RICH_TAG = re.compile(r"\[/?(?:dim|bold|red|green|yellow|blue|cyan|white|magenta|italic|underline|reverse|strike)(?:\s[^\]]*)?\]")


def set_api_session(session_id: str) -> None:
    """Mark the current thread as executing inside an API session.

    After this call, :func:`tool_status` and :func:`tool_log` will also
    broadcast messages to the session's WebSocket clients.
    """
    _session_local.session_id = session_id


def clear_api_session() -> None:
    """Clear the API session marker for the current thread."""
    _session_local.session_id = None


def _broadcast(message: str) -> None:
    """Push a plain-text message to the current session's WS queue."""
    sid = getattr(_session_local, "session_id", None)
    if not sid:
        return
    # Also append to the session log so we can verify the broadcast was attempted.
    path = os.path.join("logs", sid, "tool_usage.log")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"[WS] {message}\n")
    from api.log_broadcaster import broadcaster
    broadcaster.broadcast(sid, message)


def _strip_rich(text: str) -> str:
    """Remove Rich markup tags like ``[dim]``, ``[bold red]``, etc."""
    return _RICH_TAG.sub("", text)


# ── Unified logging API ────────────────────────────────────────────

def tool_status(message: str) -> None:
    """Short status line (e.g. "Tool: Searching RFC for ...").

    - CLI mode  → terminal (via console.log)
    - API mode  → terminal + session log file + WebSocket
    """
    console.log(message)
    _broadcast(_strip_rich(message))


def tool_log(message: str) -> None:
    """Detailed tool log (e.g. TOOL CALL / TOOL RESPONSE blocks).

    - CLI mode  → ``tool_usage.log`` in project root (no terminal, no WS)
    - API mode  → ``logs/<session_id>/tool_usage.log`` (no terminal, no WS)
    """
    sid = getattr(_session_local, "session_id", None)
    if sid:
        path = os.path.join("logs", sid, "tool_usage.log")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(message + "\n")
    else:
        file_logger.log(message)


def step_title(message: str) -> None:
    """Step title / milestone."""
    console.rule(f"[bold blue]{message}[/bold blue]")
    _broadcast(f"[TITLE] {message}")


def step_success(message: str) -> None:
    console.print(f"[bold green]{message}[/bold green]")
    _broadcast(f"[OK] {message}")


def step_error(message: str) -> None:
    console.print(f"[bold red]{message}[/bold red]")
    _broadcast(f"[ERROR] {message}")


def step_warn(message: str) -> None:
    console.print(f"[bold yellow]{message}[/bold yellow]")
    _broadcast(f"[WARN] {message}")


def step_info(message: str) -> None:
    console.print(f"[dim]{message}[/dim]")
    _broadcast(message)


# ── Legacy helpers ─────────────────────────────────────────────────

def setup_session_log(session_id: str) -> None:
    """Legacy alias — prefer set_api_session."""
    set_api_session(session_id)


def get_session_log_path(session_id: str) -> str:
    """Return absolute path to the tool-usage log for *session_id*."""
    return os.path.abspath(os.path.join("logs", session_id, "tool_usage.log"))
