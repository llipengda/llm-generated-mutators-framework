"""FastAPI dependency injection."""

from __future__ import annotations

from api.session_manager import SessionManager

_session_manager: SessionManager | None = None


def get_session_manager() -> SessionManager:
    global _session_manager
    if _session_manager is None:
        _session_manager = SessionManager(max_workers=2)
    return _session_manager
