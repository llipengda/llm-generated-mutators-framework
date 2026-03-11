from typing import Callable

from state import PipelineState, save_pipeline_state
from agent import build_agent_graph
from config import (
    get_protocol_name,
    get_rfc_path,
    get_seed_dir,
    warn_if_rfc_missing,
)
from console import console
from rag import build_retriever
from state import load_pipeline_state, PipelineState
from ui import ask_before_step, run_agent_step

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from langchain_core.retrievers import BaseRetriever

class BasePipeline:
    protocol: str
    protocol_name: str
    agent_graph: CompiledStateGraph
    config: RunnableConfig
    state: PipelineState 
    seed_dir: str
    retriever: BaseRetriever

    def __init__(
        self,
    ):
        protocol_name = get_protocol_name()
        rfc_path = get_rfc_path()
        seed_dir = get_seed_dir()

        warn_if_rfc_missing(rfc_path)
        retriever = build_retriever(rfc_path)

        agent_graph = build_agent_graph(retriever=retriever)

        config: RunnableConfig = {"configurable": {"thread_id": "session_001"}}

        state: PipelineState = {
            "packet_types": [],
            "constraints": "",
            **load_pipeline_state(),
        }

        self.protocol_name = protocol_name
        self.protocol = protocol_name.lower()
        self.seed_dir = seed_dir
        self.agent_graph = agent_graph
        self.retriever = retriever
        self.config = config
        self.state = state


    def __call__(self):
        i = 0
        steps = self.steps()
        while i < len(steps):
            step_title, step_fn = steps[i]
            action = ask_before_step(step_title, has_previous=i > 0)

            if action == "exit":
                console.print("[bold red]Exiting pipeline.[/bold red]")
                return
            if action == "retry_prev":
                if i == 0:
                    console.print(
                        "[yellow]This is the first step; there is no previous step to retry.[/yellow]"
                    )
                else:
                    console.rule(
                        f"[yellow]Going back to previous step: {steps[i-1][0]}[/yellow]",
                        style="yellow",
                    )
                    i -= 1
                continue
            if action == "skip":
                console.rule(
                    f"[yellow]Skipping: {step_title}[/yellow]", style="yellow")
                i += 1
                continue

            step_fn()
            i += 1

        from rich.panel import Panel

        console.print(
            Panel(
                f"Generation pipeline execution for {self.protocol_name} completed successfully.",
                style="bold green",
            )
        )

    def steps(self) -> list[tuple[str, Callable[[], None]]]:
        raise NotImplementedError("Subclasses must implement the steps method.")
    
    def call_agent(self, prompt_text: str, step_title: str, *, agent_graph: CompiledStateGraph | None = None):
        return run_agent_step(
            agent_graph=agent_graph or self.agent_graph,
            prompt_text=prompt_text,
            config=self.config,
            step_title=step_title,
        )
    
    def new_agent(self):
        return build_agent_graph(retriever=self.retriever)
    
    def save_state(self):
        save_pipeline_state(self.state)
