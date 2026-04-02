import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
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
        """
        执行 pipeline 步骤，支持单个步骤和并行步骤组。
        
        步骤格式支持：
        - 单个步骤：("Step Name", step_function)
        - 并行步骤组：[("Step A", func_a), ("Step B", func_b), ...]
        """
        i = 0
        steps = self.steps()
        
        while i < len(steps):
            current_step = steps[i]
            is_parallel_group = isinstance(current_step, list)
            
            # 生成显示标题
            if is_parallel_group:
                step_titles = [step[0] for step in current_step]
                display_title = f"[PARALLEL] {', '.join(step_titles)}"
            else:
                display_title = current_step[0]
            
            # 询问用户是否继续
            action = ask_before_step(display_title, has_previous=i > 0)
            
            if action == "exit":
                UI.error("Exiting pipeline.")
                return
            if action == "retry_prev":
                if i == 0:
                    UI.warn("This is the first step; there is no previous step to retry.")
                else:
                    prev_step = steps[i-1]
                    if isinstance(prev_step, list):
                        prev_title = f"[PARALLEL] {', '.join([s[0] for s in prev_step])}"
                    else:
                        prev_title = prev_step[0]
                    UI.warning_rule(f"Going back to previous step: {prev_title}")
                    i -= 1
                continue
            if action == "skip":
                UI.warning_rule(f"Skipping: {display_title}")
                i += 1
                continue
            
            # 执行步骤：单个或并行
            if is_parallel_group:
                self._execute_parallel_steps(current_step)
            else:
                step_title, step_fn = current_step
                step_fn()
            
            i += 1
        
        UI.panel(
            f"Generation pipeline execution for {self.protocol_name} completed successfully.",
            style="bold green",
        )
        self.print_token_usage_summary()
    
    def _execute_parallel_steps(self, steps_group):
        """
        并行执行一组步骤。
        
        Args:
            steps_group: 列表，每个元素是 (step_title, step_function) 元组
        """
        UI.dim(f"🚀 Starting parallel execution of {len(steps_group)} steps...")
        
        with ThreadPoolExecutor(max_workers=len(steps_group)) as executor:
            # 提交所有任务
            future_to_step = {}
            for step_title, step_fn in steps_group:
                future = executor.submit(step_fn)
                future_to_step[future] = step_title
            
            # 等待所有任务完成并收集结果
            completed = 0
            for future in as_completed(future_to_step):
                step_title = future_to_step[future]
                try:
                    future.result()
                    completed += 1
                    UI.success(f"✓ {step_title} completed")
                except Exception as e:
                    UI.error(f"✗ {step_title} failed with error: {e}")
                    raise
        
        UI.dim(f"✅ All {len(steps_group)} parallel steps completed")

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
