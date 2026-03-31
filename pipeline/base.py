import os

from typing import Callable

from state import PipelineState, add_step_usage, save_pipeline_state
from agent import build_agent_graph
from config import (
    get_protocol_name,
    get_rfc_path,
    get_seed_dir,
    warn_if_rfc_missing,
)
from rag import build_retriever
from state import load_pipeline_state, PipelineState
from ui import ask_before_step, run_agent_step, UI

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from langchain_core.retrievers import BaseRetriever
from usage_tracking import TokenUsageTracker

class BasePipeline:
    protocol_lower: str
    protocol_upper: str
    protocol_name: str
    agent_graph: CompiledStateGraph
    config: RunnableConfig
    state: PipelineState 
    seed_dir: str
    retriever: BaseRetriever
    usage_tracker: TokenUsageTracker

    def __init__(
        self,
    ):
        protocol_name = get_protocol_name()
        rfc_path = get_rfc_path()
        seed_dir = get_seed_dir()

        warn_if_rfc_missing(rfc_path)
        retriever = build_retriever(rfc_path)

        agent_graph = build_agent_graph(retriever=retriever)
        usage_tracker = TokenUsageTracker()

        config: RunnableConfig = {
            "configurable": {"thread_id": "session_001"},
            "callbacks": [usage_tracker],
        }

        state: PipelineState = {
            "packet_types": [],
            "constraints": "",
            **load_pipeline_state(),
        }

        self.protocol_name = protocol_name
        self.protocol_lower = protocol_name.lower()
        self.protocol_upper = protocol_name.upper()
        self.seed_dir = os.path.abspath(seed_dir)
        self.agent_graph = agent_graph
        self.retriever = retriever
        self.config = config
        self.state = state
        self.usage_tracker = usage_tracker


    def __call__(self):
        i = 0
        steps = self.steps()
        while i < len(steps):
            step_title, step_fn = steps[i]
            action = ask_before_step(step_title, has_previous=i > 0)

            if action == "exit":
                UI.error("Exiting pipeline.")
                return
            if action == "retry_prev":
                if i == 0:
                    UI.warn("This is the first step; there is no previous step to retry.")
                else:
                    UI.warning_rule(f"Going back to previous step: {steps[i-1][0]}")
                    i -= 1
                continue
            if action == "skip":
                UI.warning_rule(f"Skipping: {step_title}")
                i += 1
                continue

            step_fn()
            i += 1

        UI.panel(
            f"Generation pipeline execution for {self.protocol_name} completed successfully.",
            style="bold green",
        )
        self.print_token_usage_summary()

    def steps(self) -> list[tuple[str, Callable[[], None]]]:
        raise NotImplementedError("Subclasses must implement the steps method.")
    
    def call_agent(self, prompt_text: str, step_title: str, *, agent_graph: CompiledStateGraph | None = None):
        self.usage_tracker.start_step(step_title)
        response = run_agent_step(
            agent_graph=agent_graph or self.agent_graph,
            prompt_text=prompt_text,
            config=self.config,
            step_title=step_title,
        )
        step_usage = self.usage_tracker.end_step()
        add_step_usage(self.state, step_title=step_title, usage=step_usage)
        self.save_state()
        return response
    
    def new_agent(self):
        return build_agent_graph(retriever=self.retriever)
    
    def save_state(self):
        save_pipeline_state(self.state)

    def print_token_usage_summary(self) -> None:
        total = self.state.get("token_usage_total", {})
        by_step = self.state.get("token_usage_by_step", {})

        UI.title("[bold green]Token Usage Summary[/bold green]")
        UI.print(
            "[bold]Total:[/bold] "
            f"prompt={total.get('prompt_tokens', 0)}, "
            f"completion={total.get('completion_tokens', 0)}, "
            f"total={total.get('total_tokens', 0)}, "
            f"calls={total.get('calls', 0)}"
        )

        if not by_step:
            UI.dim("No per-step usage recorded.")
            return

        for step, usage in by_step.items():
            UI.print(
                f"- [bold]{step}[/bold]: "
                f"prompt={usage.get('prompt_tokens', 0)}, "
                f"completion={usage.get('completion_tokens', 0)}, "
                f"total={usage.get('total_tokens', 0)}, "
                f"calls={usage.get('calls', 0)}"
            )
