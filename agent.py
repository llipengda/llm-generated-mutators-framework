from langchain.agents import create_agent
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver

from tools import append_and_verify_code, make_rfc_search, read_file, save_and_verify_code


def build_agent_graph(*, retriever):
    llm = ChatOpenAI(temperature=0, model="gpt-5.2")
    rfc_search = make_rfc_search(retriever)

    tools = [rfc_search, save_and_verify_code,
             read_file, append_and_verify_code]

    memory = MemorySaver()

    return create_agent(
        model=llm,
        tools=tools,
        checkpointer=memory,
        system_prompt="""
        You are a helpful assistant expert in C programming and protocol fuzzing.
        """,
    )
