"""Console output and unified, per-protocol JSONL logging."""

from __future__ import annotations

import json
from copy import copy
from contextvars import ContextVar
from dataclasses import dataclass
import logging
import traceback
from contextlib import contextmanager
from collections.abc import Generator
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, ParamSpec, TypeVar
from uuid import UUID, uuid4

from langchain_core.callbacks import BaseCallbackHandler
from rich.console import Console


console = Console()


@dataclass(frozen=True)
class LogTask:
    task_id: str
    task_name: str


_current_task: ContextVar[LogTask | None] = ContextVar("log_task", default=None)
P = ParamSpec("P")
T = TypeVar("T")


@contextmanager
def task_scope(name: str, *, reuse: bool = False) -> Generator[LogTask, None, None]:
    existing = _current_task.get()
    task = existing if reuse and existing is not None else LogTask(str(uuid4()), name)
    token = _current_task.set(task)
    try:
        yield task
    finally:
        _current_task.reset(token)


def run_logged_task(name: str, function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Establish context inside executor workers, including setup and postchecks."""
    with task_scope(name):
        logger = logging.getLogger(__name__)
        logger.info("Starting %s", name, extra={"event": "task_start"})
        try:
            result = function(*args, **kwargs)
        except BaseException:
            logger.exception("Failed %s", name, extra={"event": "task_error"})
            raise
        logger.info("Completed %s", name, extra={"event": "task_end"})
        return result


def _json_default(value: Any) -> str:
    """Serialize untyped LangChain callback payloads without breaking tool calls."""
    try:
        return str(value)
    except Exception:
        return f"<{type(value).__name__}>"


class PipelineLogger(BaseCallbackHandler):
    """Write tool lifecycle events and runtime diagnostics to one protocol log."""

    def __init__(self, protocol: str, *, log_root: Path | None = None) -> None:
        self.bound_task: LogTask | None = None
        self.protocol = protocol.lower()
        safe_protocol = re.sub(r"[^A-Za-z0-9_.-]+", "_", self.protocol).strip("._")
        if not safe_protocol:
            safe_protocol = "unknown"
        root = log_root or Path(__file__).resolve().parent.parent / "logs"
        self.path = root / safe_protocol / "log.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.session_id = str(uuid4())
        self._lock = threading.Lock()
        self._started_at: dict[str, float] = {}
        self._tool_names: dict[str, str] = {}

        # Start a fresh log for this protocol run without touching other protocols.
        self.path.write_text("", encoding="utf-8")
        self.write_event({"event": "session_start"})

    def for_task(self, task: LogTask) -> PipelineLogger:
        """Bind callbacks without reopening the file or sharing mutable task state."""
        bound = copy(self)
        bound.bound_task = task
        return bound

    def write_event(self, payload: dict[str, object]) -> None:
        """Append a structured event with the current session metadata."""
        task = self.bound_task or _current_task.get()
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "session_id": self.session_id,
            "protocol": self.protocol,
            "level": "INFO",
            "task_id": task.task_id if task else None,
            "task_name": task.task_name if task else None,
            "thread_name": threading.current_thread().name,
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
        self.write_event(
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
        self.write_event(
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
        self.write_event(
            {
                "event": "tool_error",
                "level": "ERROR",
                "traceback": "".join(traceback.format_exception(error)),
                "call_id": call_id,
                "parent_call_id": str(parent_run_id) if parent_run_id else None,
                "tool": tool_name,
                "status": "error",
                "duration_ms": self._duration_ms(started_at),
                "error_type": type(error).__name__,
                "error": str(error),
            }
        )

    def on_llm_error(
        self, error: BaseException, *, run_id: UUID,
        parent_run_id: UUID | None = None, **kwargs: Any,
    ) -> None:
        self._callback_error("llm_error", error, run_id, parent_run_id)

    def on_chain_error(
        self, error: BaseException, *, run_id: UUID,
        parent_run_id: UUID | None = None, **kwargs: Any,
    ) -> None:
        self._callback_error("chain_error", error, run_id, parent_run_id)

    def _callback_error(
        self, event: str, error: BaseException, run_id: UUID,
        parent_run_id: UUID | None,
    ) -> None:
        self.write_event({
            "event": event,
            "level": "ERROR",
            "call_id": str(run_id),
            "parent_call_id": str(parent_run_id) if parent_run_id else None,
            "error_type": type(error).__name__,
            "error": str(error),
            "traceback": "".join(traceback.format_exception(error)),
        })

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
        parts: list[str] = []
        for key in visible_keys:
            if key not in inputs:
                continue
            value = str(inputs[key]).replace("\n", " ")
            if len(value) > 120:
                value = value[:117] + "..."
            parts.append(f"{key}={value}")
        return f" ({', '.join(parts)})" if parts else ""


# Compatibility for callers that previously used the tool-only callback.
ToolUsageLogger = PipelineLogger


class _JsonlHandler(logging.Handler):
    def __init__(self, logger: PipelineLogger) -> None:
        super().__init__(logging.INFO)
        self.pipeline_logger = logger

    def emit(self, record: logging.LogRecord) -> None:
        try:
            payload: dict[str, object] = {
                "event": getattr(record, "event", "log"),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            }
            if record.exc_info:
                payload["traceback"] = "".join(traceback.format_exception(*record.exc_info))
                error = record.exc_info[1]
                if error is not None:
                    payload["error_type"] = type(error).__name__
                    payload["error"] = str(error)
            self.pipeline_logger.write_event(payload)
        except Exception:
            self.handleError(record)


def get_pipeline_logger(protocol: str) -> PipelineLogger:
    for handler in logging.getLogger().handlers:
        if isinstance(handler, _JsonlHandler) and handler.pipeline_logger.protocol == protocol.lower():
            return handler.pipeline_logger
    return PipelineLogger(protocol)


@contextmanager
def log_session(protocol: str, *, log_root: Path | None = None) -> Generator[PipelineLogger, None, None]:
    """Capture initialization, worker-thread diagnostics and uncaught CLI errors."""
    logger = PipelineLogger(protocol, log_root=log_root)
    handler = _JsonlHandler(logger)
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(handler)
    root.setLevel(min(previous_level, logging.INFO))
    status = "success"
    try:
        yield logger
    except BaseException:
        status = "error"
        logging.getLogger(__name__).exception("Pipeline terminated", extra={"event": "pipeline_error"})
        raise
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)
        handler.close()
        logger.write_event({"event": "session_end", "status": status})
