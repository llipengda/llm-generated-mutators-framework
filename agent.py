from dataclasses import dataclass
from typing import Literal

from langchain.agents import create_agent
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langchain_core.retrievers import BaseRetriever

from tools import tools, make_rfc_search


@dataclass
class AgentConfig:
    temperature: float = 0.0
    model: str = "gpt-5.2"
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
