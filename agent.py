from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Mapping, Literal

from langchain.agents import create_agent
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.retrievers import BaseRetriever

from tools import tools, make_rfc_search


# ---------------------------------------------------------------------------
# Per-session LLM configuration overrides (API mode).
# ---------------------------------------------------------------------------


@dataclass
class LlmOverrides:
    """LLM configuration that can be set per session via the HTTP API.

    All fields default to ``None``, meaning "use the environment variable".
    """

    api_key: str | None = None
    base_url: str | None = None
    model: str | None = None
    temperature: float | None = None
    # Embedding overrides.
    embedding_model: str | None = None
    embedding_base_url: str | None = None
    embedding_api_key: str | None = None

# ---------------------------------------------------------------------------
# Monkey-patch: preserve reasoning_content round-trip through LangChain
# ---------------------------------------------------------------------------
import langchain_openai.chat_models.base as _lc_base

_original_dict_to_message = _lc_base._convert_dict_to_message
_original_message_to_dict = _lc_base._convert_message_to_dict


def _patched_dict_to_message(_dict: Mapping[str, Any]) -> BaseMessage:
    msg = _original_dict_to_message(_dict)
    if isinstance(msg, AIMessage):
        reasoning = _dict.get("reasoning_content")
        if reasoning:
            msg.additional_kwargs["reasoning_content"] = reasoning
    return msg


def _patched_message_to_dict(
    message: BaseMessage,
    api: Literal["chat/completions", "responses"] = "chat/completions",
) -> dict:
    msg_dict = _original_message_to_dict(message, api)
    if isinstance(message, AIMessage):
        reasoning = message.additional_kwargs.get("reasoning_content")
        if reasoning:
            msg_dict["reasoning_content"] = reasoning
    return msg_dict


_lc_base._convert_dict_to_message = _patched_dict_to_message
_lc_base._convert_message_to_dict = _patched_message_to_dict
# ---------------------------------------------------------------------------


def _env_float(key: str, default: float) -> float:
    val = os.environ.get(key)
    if val is None:
        return default
    try:
        return float(val)
    except ValueError:
        return default


@dataclass
class AgentConfig:
    model: str = field(
        default_factory=lambda: os.environ.get("LLM_MODEL", "gpt-5.2")
    )
    temperature: float = field(
        default_factory=lambda: _env_float("LLM_TEMPERATURE", 0.0)
    )
    system_prompt: str = """
You are a helpful assistant expert in C programming and protocol fuzzing.
"""

    def apply_overrides(self, overrides: LlmOverrides | None) -> None:
        """Apply per-session overrides, falling back to env vars."""
        if overrides is None:
            return
        if overrides.model is not None:
            self.model = overrides.model
        if overrides.temperature is not None:
            self.temperature = overrides.temperature


def build_agent_graph(
    *,
    retriever: BaseRetriever,
    config: AgentConfig | None = None,
    target: Literal["aflnet", "peach"] = "aflnet",
    llm_overrides: LlmOverrides | None = None,
):
    if config is None:
        config = AgentConfig()
    config.apply_overrides(llm_overrides)

    # Merge overrides with env vars for ChatOpenAI kwargs.
    openai_api_key = (
        (llm_overrides.api_key if llm_overrides else None)
        or os.environ.get("OPENAI_API_KEY")
    )
    openai_base_url = (
        (llm_overrides.base_url if llm_overrides else None)
        or os.environ.get("OPENAI_BASE_URL")
    )

    llm_kwargs: dict[str, Any] = {
        "temperature": config.temperature,
        "model": config.model,
    }
    if openai_api_key:
        llm_kwargs["openai_api_key"] = openai_api_key
    if openai_base_url:
        llm_kwargs["openai_api_base"] = openai_base_url

    llm = ChatOpenAI(**llm_kwargs)
    rfc_search = make_rfc_search(retriever)

    memory = MemorySaver()

    return create_agent(
        model=llm,
        tools=[rfc_search] + tools[target],
        checkpointer=memory,
        system_prompt=config.system_prompt,
    )
