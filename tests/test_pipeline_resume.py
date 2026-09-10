import unittest
from unittest.mock import patch
from typing import Mapping

from core.state import new_usage_bucket
from core.usage_tracking import ReasoningContentLogger, TokenUsageTracker
from pipeline.base import BasePipeline


class _Pipeline(BasePipeline):
    def __init__(self) -> None:
        self.protocol_name = "demo"
        self.protocol_lower = "demo"
        self.state = {
            "packet_types": [],
            "data_type_analysis": {},
            "constraints": "",
            "token_usage_total": new_usage_bucket(),
            "token_usage_by_step": {},
            "current_step_index": 0,
        }
        self.executed: list[str] = []
        self.saved_indexes: list[int] = []

    def steps(self):
        return [("first", self._first), ("second", self._second)]

    def _first(self) -> None:
        self.executed.append("first")

    def _second(self) -> None:
        self.executed.append("second")

    def save_state(self) -> None:
        self.saved_indexes.append(self.state["current_step_index"])

    def print_token_usage_summary(self) -> None:
        pass


class _LLMResponse:
    def __init__(self, token_usage: Mapping[str, object]) -> None:
        self.llm_output: dict[str, Mapping[str, object]] = {
            "token_usage": token_usage,
        }


class _ReasoningMessage:
    def __init__(self, content: str) -> None:
        self.additional_kwargs: dict[str, str] = {"reasoning_content": content}


class _Generation:
    def __init__(self, content: str) -> None:
        self.message = _ReasoningMessage(content)


class _ReasoningResponse:
    def __init__(self, content: str) -> None:
        self.generations = [[_Generation(content)]]


class PipelineResumeTests(unittest.TestCase):
    def test_completed_step_persists_next_step_index(self) -> None:
        pipeline = _Pipeline()
        with patch("pipeline.base.ask_before_step", return_value=("continue", None)):
            pipeline()

        self.assertEqual(pipeline.executed, ["first", "second"])
        self.assertEqual(pipeline.state["current_step_index"], 2)
        self.assertEqual(pipeline.saved_indexes, [0, 1, 1, 2])

    def test_checkpoint_is_saved_once(self) -> None:
        pipeline = _Pipeline()
        pipeline.mark_checkpoint_completed("step_2_1")
        pipeline.mark_checkpoint_completed("step_2_1")

        self.assertTrue(pipeline.has_completed_checkpoint("step_2_1"))
        self.assertEqual(pipeline.saved_indexes, [0])

    def test_usage_tracks_peak_context_for_each_llm_call(self) -> None:
        tracker = TokenUsageTracker()
        tracker.start_step("demo")
        tracker.on_llm_end(
            _LLMResponse({
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "cached_tokens": 0,
                "total_tokens": 120,
                "calls": 1,
                "max_prompt_tokens_per_call": 100,
                "max_total_tokens_per_call": 120,
            })
        )
        tracker.on_llm_end(
            _LLMResponse({
                "prompt_tokens": 250,
                "completion_tokens": 30,
                "cached_tokens": 50,
                "total_tokens": 280,
                "calls": 1,
                "max_prompt_tokens_per_call": 250,
                "max_total_tokens_per_call": 280,
            })
        )

        usage = tracker.end_step()
        self.assertEqual(usage["prompt_tokens"], 350)
        self.assertEqual(usage["max_prompt_tokens_per_call"], 250)
        self.assertEqual(usage["max_total_tokens_per_call"], 280)

    def test_reasoning_logger_prints_each_llm_call(self) -> None:
        logger = ReasoningContentLogger("demo")
        with patch("core.usage_tracking.console.print") as printed:
            logger.on_llm_end(_ReasoningResponse("inspect the tool output"))

        printed.assert_any_call("[reasoning] demo (call 1)", style="dim")
        printed.assert_any_call("inspect the tool output", markup=False)
