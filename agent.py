from dataclasses import dataclass

from langchain.agents import create_agent
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langchain_core.retrievers import BaseRetriever

from tools import append_and_verify_code, make_rfc_search, read_file, save_and_verify_code


@dataclass
class AgentConfig:
    temperature: float = 0.0
    model: str = "gpt-5.2"
    system_prompt: str = """
You are a helpful assistant expert in C programming and protocol fuzzing.
"""

def build_agent_graph(*, retriever: BaseRetriever, config: AgentConfig | None = None):
    if config is None:
        config = AgentConfig()

    llm = ChatOpenAI(temperature=config.temperature, model=config.model)
    rfc_search = make_rfc_search(retriever)

    tools = [rfc_search, save_and_verify_code,
             read_file, append_and_verify_code]

    memory = MemorySaver()

    return create_agent(
        model=llm,
        tools=tools,
        checkpointer=memory,
        system_prompt=config.system_prompt
    )
