"""Console output and structured, per-protocol tool-call logging."""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from langchain_core.callbacks import BaseCallbackHandler
from rich.console import Console


console = Console()


def _json_default(value: Any) -> str:
    """Keep logging from breaking a tool call on non-JSON LangChain values."""
    try:
        return str(value)
    except Exception:
        return f"<{type(value).__name__}>"


class ToolUsageLogger(BaseCallbackHandler):
    """Write complete tool lifecycle events to one JSONL file per protocol."""

    def __init__(self, protocol: str, *, log_root: Path | None = None) -> None:
        self.protocol = protocol.lower()
        safe_protocol = re.sub(r"[^A-Za-z0-9_.-]+", "_", self.protocol).strip("._")
        if not safe_protocol:
            safe_protocol = "unknown"
        root = log_root or Path(__file__).resolve().parent / "logs"
        self.path = root / safe_protocol / "tool_usage.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.session_id = str(uuid4())
        self._lock = threading.Lock()
        self._started_at: dict[str, float] = {}
        self._tool_names: dict[str, str] = {}

        # Start a fresh log for this protocol run without touching other protocols.
        self.path.write_text("", encoding="utf-8")
        self._write({"event": "session_start"})

    def _write(self, payload: dict[str, Any]) -> None:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "session_id": self.session_id,
            "protocol": self.protocol,
            **payload,
        }
        line = json.dumps(record, ensure_ascii=False, default=_json_default)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        inputs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        call_id = str(run_id)
        tool_name = str(serialized.get("name") or kwargs.get("name") or "unknown")
        with self._lock:
            self._started_at[call_id] = time.perf_counter()
            self._tool_names[call_id] = tool_name
        console.print(
            f"[{datetime.now().astimezone():%H:%M:%S}] "
            f"Tool: {tool_name}{self._console_input_summary(inputs)}",
            style="dim",
            markup=False,
        )
        self._write(
            {
                "event": "tool_start",
                "call_id": call_id,
                "parent_call_id": str(parent_run_id) if parent_run_id else None,
                "tool": tool_name,
                "input": inputs if inputs is not None else input_str,
            }
        )

    def on_tool_end(
        self,
        output: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        call_id = str(run_id)
        started_at, tool_name = self._finish(call_id)
        self._write(
            {
                "event": "tool_end",
                "call_id": call_id,
                "parent_call_id": str(parent_run_id) if parent_run_id else None,
                "tool": tool_name,
                "status": "success",
                "duration_ms": self._duration_ms(started_at),
                "output": output,
            }
        )

    def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        call_id = str(run_id)
        started_at, tool_name = self._finish(call_id)
        self._write(
            {
                "event": "tool_error",
                "call_id": call_id,
                "parent_call_id": str(parent_run_id) if parent_run_id else None,
                "tool": tool_name,
                "status": "error",
                "duration_ms": self._duration_ms(started_at),
                "error_type": type(error).__name__,
                "error": str(error),
            }
        )

    def _finish(self, call_id: str) -> tuple[float | None, str]:
        with self._lock:
            return (
                self._started_at.pop(call_id, None),
                self._tool_names.pop(call_id, "unknown"),
            )

    @staticmethod
    def _duration_ms(started_at: float | None) -> float | None:
        if started_at is None:
            return None
        return round((time.perf_counter() - started_at) * 1000, 3)

    @staticmethod
    def _console_input_summary(inputs: dict[str, Any] | None) -> str:
        """Show useful identifiers without dumping code or payloads to the console."""
        if not inputs:
            return ""
        visible_keys = (
            "filepath",
            "filename",
            "xml_path",
            "entry_path",
            "source_file_or_dir",
            "output_dll",
            "query",
        )
        parts = []
        for key in visible_keys:
            if key not in inputs:
                continue
            value = str(inputs[key]).replace("\n", " ")
            if len(value) > 120:
                value = value[:117] + "..."
            parts.append(f"{key}={value}")
        return f" ({', '.join(parts)})" if parts else ""


class _LegacyToolLogger:
    """Compatibility sink for old per-tool logging calls during migration."""

    def log(self, *_args: Any, **_kwargs: Any) -> None:
        pass


# Tool lifecycle logging is now handled centrally by ToolUsageLogger.
file_logger = _LegacyToolLogger()
