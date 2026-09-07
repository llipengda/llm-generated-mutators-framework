"""Typed boundary for LangGraph agents used by the pipeline."""

from __future__ import annotations

from typing import Protocol, TypedDict

from langchain_core.messages import BaseMessage
from langchain_core.runnables import RunnableConfig


class AgentResponse(TypedDict):
    messages: list[BaseMessage]


class AgentGraph(Protocol):
    def invoke(self, input: object, config: RunnableConfig | None = None) -> AgentResponse: ...
