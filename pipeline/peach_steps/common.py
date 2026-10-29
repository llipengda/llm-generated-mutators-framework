from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from core.agent import AgentConfig
from langchain_core.callbacks.base import BaseCallbackHandler
from langchain_core.retrievers import BaseRetriever
from core.state import PipelineState
from core.agent_types import AgentResponse


class PeachStepMixin:
    """Static contract supplied by the composed ``PeachPipeline`` at runtime."""

    protocol_lower: str
    protocol_upper: str
    protocol_name: str
    seed_dir: str
    state: PipelineState
    retriever: BaseRetriever
    agent_config: AgentConfig
    diagnosis_agent_config: AgentConfig
    datamodel_autofix_agent_config: AgentConfig
    tool_usage_logger: BaseCallbackHandler
    resumed_from_saved_state: bool

    call_agent: Callable[..., AgentResponse]
    save_state: Callable[[], None]
    has_completed_checkpoint: Callable[[str], bool]
    mark_checkpoint_completed: Callable[[str], None]
    fix_verify_loop: Callable[..., bool]

    def _data_type_paths(self) -> tuple[Path, Path, Path]:
        raise NotImplementedError

    def _load_data_type_analysis(self, report_path: Path) -> dict[str, Any]:
        raise NotImplementedError

    def _finalize_data_type_support(self, report: dict[str, Any]) -> None:
        raise NotImplementedError

    def _custom_data_element_context(self) -> str:
        raise NotImplementedError

    def _repair_datamodel_assembly(
        self, *, allow_packet_type_additions: bool = False
    ) -> None:
        raise NotImplementedError


def env_float(key: str, default: float) -> float:
    val = os.environ.get(key)
    if val is None:
        return default
    try:
        return float(val)
    except ValueError:
        return default


def env_int(key: str, default: int) -> int:
    val = os.environ.get(key)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        return default
