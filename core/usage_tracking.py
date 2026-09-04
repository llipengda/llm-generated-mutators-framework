from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from langchain_core.callbacks.base import BaseCallbackHandler


UsageDict = dict[str, int]


def _empty_usage() -> UsageDict:
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cached_tokens": 0,
        "total_tokens": 0,
        "calls": 0,
    }


def _safe_int(value: Any) -> int:
    try:
        if value is None:
            return 0
        return int(value)
    except Exception:
        return 0


def _extract_usage_from_output(llm_output: Any) -> UsageDict:
    if not isinstance(llm_output, dict):
        return _empty_usage()

    token_usage = llm_output.get("token_usage")
    if not isinstance(token_usage, dict):
        return _empty_usage()

    prompt = _safe_int(
        token_usage.get("prompt_tokens")
        or token_usage.get("input_tokens")
    )
    completion = _safe_int(
        token_usage.get("completion_tokens")
        or token_usage.get("output_tokens")
    )
    total = _safe_int(token_usage.get("total_tokens"))

    # OpenAI returns cached_tokens in prompt_tokens_details.cached_tokens
    details = token_usage.get("prompt_tokens_details")
    if isinstance(details, dict):
        cached = _safe_int(details.get("cached_tokens"))
    else:
        cached = _safe_int(
            token_usage.get("cached_tokens")
            or token_usage.get("cache_read_input_tokens")
        )

    if total == 0:
        total = prompt + completion

    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cached_tokens": cached,
        "total_tokens": total,
        "calls": 1,
    }


@dataclass
class TokenUsageTracker(BaseCallbackHandler):
    """Track token usage from LangChain callback events.

    This relies on provider-reported usage exposed through llm_output.token_usage.
    Thread-safe via an internal lock.
    """

    _lock: threading.Lock = field(default_factory=threading.Lock)
    _run_total: UsageDict = field(default_factory=_empty_usage)
    _step_name: str | None = None
    _step_usage: UsageDict = field(default_factory=_empty_usage)

    @property
    def run_total(self) -> UsageDict:
        with self._lock:
            return dict(self._run_total)

    def start_step(self, step_name: str) -> None:
        with self._lock:
            self._step_name = step_name
            self._step_usage = _empty_usage()

    def end_step(self) -> UsageDict:
        with self._lock:
            usage = dict(self._step_usage)
            self._step_name = None
            self._step_usage = _empty_usage()
            return usage

    def _add_usage(self, usage: UsageDict) -> None:
        with self._lock:
            for key in ("prompt_tokens", "completion_tokens", "cached_tokens", "total_tokens", "calls"):
                self._run_total[key] += usage.get(key, 0)

            if self._step_name is not None:
                for key in ("prompt_tokens", "completion_tokens", "cached_tokens", "total_tokens", "calls"):
                    self._step_usage[key] += usage.get(key, 0)

    def on_llm_end(self, response, **kwargs: Any) -> Any:
        usage = _extract_usage_from_output(getattr(response, "llm_output", None))
        self._add_usage(usage)
        return None
