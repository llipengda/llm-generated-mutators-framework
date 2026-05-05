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

def build_agent_graph(*, retriever: BaseRetriever, config: AgentConfig | None = None, target: Literal["aflnet", "peach"] = "aflnet"):
    if config is None:
        config = AgentConfig()

    llm = ChatOpenAI(temperature=config.temperature, model=config.model)
    rfc_search = make_rfc_search(retriever)

    memory = MemorySaver()

    return create_agent(
        model=llm,
        tools=[rfc_search] + tools[target],
        checkpointer=memory,
        system_prompt=config.system_prompt
    )
