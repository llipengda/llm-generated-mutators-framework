import os
import logging
import threading
from typing import Callable

from core.state import (
    PipelineState,
    pipeline_state_path,
    add_step_usage,
    load_pipeline_state,
    new_usage_bucket,
    save_pipeline_state,
)
from core.config import (
    get_protocol_name,
    get_rfc_paths,
    get_seed_dir,
    warn_if_rfc_missing,
)
from core.rag import build_retriever
from core.ui import ask_after_fix_failure, ask_before_step, ask_for_hint, ask_resume_state, ask_wait_for_fix, run_agent_step, UI

from langchain_core.runnables import RunnableConfig
from langchain_core.callbacks.base import BaseCallbackHandler
from langchain_core.retrievers import BaseRetriever
from core.agent_types import AgentGraph, AgentResponse
from core.usage_tracking import ReasoningContentLogger, TokenUsageTracker
from core.log import get_pipeline_logger

logger = logging.getLogger(__name__)

class BasePipeline:
    protocol_lower: str
    protocol_upper: str
    protocol_name: str
    agent_graph: AgentGraph
    config: RunnableConfig
    state: PipelineState 
    seed_dir: str
    retriever: BaseRetriever
    resumed_from_saved_state: bool

    def __init__(
        self,
    ):
        protocol_name = get_protocol_name()
        rfc_paths = get_rfc_paths()
        seed_dir = get_seed_dir()

        self.protocol_name = protocol_name
        self.protocol_lower = protocol_name.lower()
        self.protocol_upper = protocol_name.upper()

        warn_if_rfc_missing(rfc_paths)
        retriever = build_retriever(rfc_paths)

        config: RunnableConfig = {
            "configurable": {"thread_id": "session_001"},
        }

        state_path = pipeline_state_path(self.protocol_lower)
        if os.path.exists(state_path):
            existing = load_pipeline_state(self.protocol_lower)
            has_data = bool(
                existing.get("packet_types") or existing.get("constraints")
            )
            if has_data and ask_resume_state(self.protocol_lower):
                state = existing
                self.resumed_from_saved_state = True
            else:
                if has_data:
                    UI.dim(
                        "Discarding saved state, starting fresh."
                    )
                    os.remove(state_path)
                state: PipelineState = {
                    "packet_types": [],
                    "data_type_analysis": {},
                    "constraints": "",
                    "token_usage_total": new_usage_bucket(),
                    "token_usage_by_step": {},
                    "current_step_index": 0,
                }
                self.resumed_from_saved_state = False
        else:
            state = {
                "packet_types": [],
                "data_type_analysis": {},
                "constraints": "",
                "token_usage_total": new_usage_bucket(),
                "token_usage_by_step": {},
                "current_step_index": 0,
            }
            self.resumed_from_saved_state = False

        self.seed_dir = os.path.abspath(seed_dir)
        self.retriever = retriever
        self.config = config
        self.state = state
        self._state_lock = threading.Lock()
        self.tool_usage_logger = get_pipeline_logger(self.protocol_lower)


    def __call__(self):
        i = self.state.get("current_step_index", 0)
        steps = self.steps()
        if i >= len(steps):
            UI.panel(
                f"Generation pipeline for {self.protocol_name} was already completed.",
                style="bold green",
            )
            self.print_token_usage_summary()
            return
        if i > 0:
            UI.warning_rule(f"Resuming from step {i + 1}: {steps[i][0]}")
        while i < len(steps):
            step_title, step_fn = steps[i]
            action, extra_prompt = ask_before_step(step_title, has_previous=i > 0)

            if action == "exit":
                UI.error("Exiting pipeline.")
                return
            if action == "retry_prev":
                if i == 0:
                    UI.warn("This is the first step; there is no previous step to retry.")
                else:
                    UI.warning_rule(f"Going back to previous step: {steps[i-1][0]}")
                    i -= 1
                    self.state["current_step_index"] = i
                    self.save_state()
                continue
            if action == "skip":
                UI.warning_rule(f"Skipping: {step_title}")
                i += 1
                self.state["current_step_index"] = i
                self.save_state()
                continue

            self.state["current_step_index"] = i
            self.save_state()
            self._extra_prompt = extra_prompt
            logger.info("Starting %s", step_title, extra={"event": "step_start"})
            try:
                step_fn()
            except BaseException:
                logger.exception("Failed %s", step_title, extra={"event": "step_error"})
                raise
            logger.info("Completed %s", step_title, extra={"event": "step_end"})
            i += 1
            # A step can contain several agent calls, each of which persists state.
            # Persist the *next* top-level step separately so a completed step is
            # never repeated after a restart.
            self.state["current_step_index"] = i
            self.save_state()

        UI.panel(
            f"Generation pipeline execution for {self.protocol_name} completed successfully.",
            style="bold green",
        )
        self.print_token_usage_summary()

    def steps(self) -> list[tuple[str, Callable[[], None]]]:
        raise NotImplementedError("Subclasses must implement the steps method.")
    
    def call_agent(
        self,
        prompt_text: str,
        step_title: str,
        *,
        agent_graph: AgentGraph | None = None,
    ) -> AgentResponse:
        extra = getattr(self, "_extra_prompt", None)
        if extra:
            prompt_text = prompt_text + "\n\n" + extra
            UI.dim(f"  Appended extra prompt: {extra}")
            self._extra_prompt = None

        tracker = TokenUsageTracker()
        callbacks: list[BaseCallbackHandler] = [tracker, self.tool_usage_logger]
        if os.environ.get("LLM_PRINT_REASONING", "").lower() in {
            "1", "true", "yes", "on"
        } or bool(os.environ.get("LLM_REASONING_FORMAT", "").strip()):
            callbacks.append(ReasoningContentLogger(step_title))
        local_config: RunnableConfig = {
            **self.config,
            "callbacks": callbacks,
        }
        tracker.start_step(step_title)
        logger.info("Starting agent: %s", step_title, extra={"event": "agent_start"})
        try:
            response = run_agent_step(
                agent_graph=agent_graph or self.agent_graph,
                prompt_text=prompt_text,
                config=local_config,
                step_title=step_title,
            )
        except BaseException:
            logger.exception("Agent failed: %s", step_title, extra={"event": "agent_error"})
            raise
        logger.info("Completed agent: %s", step_title, extra={"event": "agent_end"})
        step_usage = tracker.end_step()
        with self._state_lock:
            add_step_usage(self.state, step_title=step_title, usage=step_usage)
            self.save_state()
        return response

    def fix_verify_loop(
        self,
        step_title: str,
        verify_fn: Callable[[], tuple[bool, str]],
        fix_fn: Callable[[str, str | None], None],
        *,
        max_retries: int = 3,
    ) -> bool:
        """Verify → fix → re-verify loop with auto-retry and human-in-the-loop fallback.

        Args:
            step_title: label used in UI messages.
            verify_fn: runs the test/validation; returns (success, output).
            fix_fn: performs the LLM fix; called with (output, hint) where hint
                    is None during auto-fix and a user-provided string otherwise.
            max_retries: number of auto-fix attempts before asking the user.

        Returns:
            True if verification passed (with or without fixes).
            False if the user chose to exit the pipeline.
        """
        def verify() -> tuple[bool, str]:
            success, output = verify_fn()
            logger.log(
                logging.INFO if success else logging.ERROR,
                "%s verification %s:\n%s", step_title,
                "passed" if success else "failed", output,
                extra={"event": "verification_result"},
            )
            return success, output

        success, output = verify()
        if success:
            return True

        for attempt in range(1, max_retries + 1):
            UI.warning_rule(
                f"{step_title} auto-fix attempt {attempt}/{max_retries}"
            )
            fix_fn(output, None)

            success, output = verify()
            if success:
                UI.success(f"{step_title} passed after fix attempt {attempt}!")
                return True

            UI.error(
                f"{step_title} still failing after fix attempt {attempt}/{max_retries}."
            )

        while True:
            choice = ask_after_fix_failure(step_title)
            if choice == "exit":
                UI.error(f"{step_title} halted. Exiting pipeline.")
                return False

            if choice == "wait":
                ask_wait_for_fix(step_title)
                UI.warning_rule(f"{step_title}: re-verifying after manual fix")
                success, output = verify()
                if success:
                    UI.success(f"{step_title} passed after manual fix!")
                    return True
                UI.error(f"{step_title} still failing after manual fix.")
                continue

            hint = ask_for_hint(step_title)
            UI.warning_rule(f"{step_title}: retrying with user-provided hint")
            fix_fn(output, hint)

            success, output = verify()
            if success:
                UI.success(f"{step_title} passed after manual-hint fix!")
                return True

            UI.error(f"{step_title} still failing after manual-hint fix.")

    def save_state(self):
        save_pipeline_state(self.state, self.protocol_lower)

    def has_completed_checkpoint(self, checkpoint: str) -> bool:
        """Return whether a durable sub-step checkpoint has completed."""
        return checkpoint in self.state.get("completed_checkpoints", [])

    def mark_checkpoint_completed(self, checkpoint: str) -> None:
        """Persist a completed sub-step before the following work begins."""
        checkpoints = self.state.setdefault("completed_checkpoints", [])
        if checkpoint not in checkpoints:
            checkpoints.append(checkpoint)
            self.save_state()

    def print_token_usage_summary(self) -> None:
        total = self.state.get("token_usage_total", {})
        by_step = self.state.get("token_usage_by_step", {})

        UI.title("[bold green]Token Usage Summary[/bold green]")
        prompt_val = total.get("prompt_tokens", 0)
        cached_val = total.get("cached_tokens", 0)
        UI.print(
            "[bold]Total:[/bold] "
            f"prompt={prompt_val} (cached={cached_val}, uncached={prompt_val - cached_val}), "
            f"completion={total.get('completion_tokens', 0)}, "
            f"total={total.get('total_tokens', 0)}, "
            f"LLM_calls={total.get('calls', 0)}, "
            f"peak_context={total.get('max_prompt_tokens_per_call', 0)}"
        )

        if not by_step:
            UI.dim("No per-step usage recorded.")
            return

        for step, usage in by_step.items():
            sp = usage.get("prompt_tokens", 0)
            sc = usage.get("cached_tokens", 0)
            calls = usage.get("calls", 0)
            average_context = int(sp) // int(calls) if int(calls) else 0
            UI.print(
                f"- [bold]{step}[/bold]: "
                f"prompt={sp} (cached={sc}), "
                f"completion={usage.get('completion_tokens', 0)}, "
                f"total={usage.get('total_tokens', 0)}, "
                f"calls={calls}, "
                f"context/call(avg={average_context}, "
                f"peak={usage.get('max_prompt_tokens_per_call', 0)}, "
                f"peak_with_output={usage.get('max_total_tokens_per_call', 0)})"
            )
