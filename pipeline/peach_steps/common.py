import os
from pathlib import Path
from typing import Callable, Protocol, TypedDict

from agent import AgentConfig
from langchain_core.retrievers import BaseRetriever
from langgraph.graph.state import CompiledStateGraph
from state import PipelineState


class AgentMessage(Protocol):
    content: str


class AgentResponse(TypedDict):
    messages: list[AgentMessage]


class PeachStepMixin:
    """Static contract supplied by the composed ``PeachPipeline`` at runtime."""

    protocol_lower: str
    protocol_upper: str
    protocol_name: str
    seed_dir: str
    state: PipelineState
    retriever: BaseRetriever
    agent_config: AgentConfig
    diagnosis_agent_graph: CompiledStateGraph
    datamodel_autofix_agent_graph: CompiledStateGraph

    call_agent: Callable[..., AgentResponse]
    save_state: Callable[[], None]
    fix_verify_loop: Callable[..., bool]

    _data_type_paths: Callable[[], tuple[Path, Path, Path]]
    _custom_data_element_context: Callable[[], str]


_DATAMODEL_MODELING_GUARDRAILS = """
DataModel token and generalization rules:
- `token="true"` is allowed when, and only when, the RFC requires one exact
  wire value and matching that value is needed to crack the correct model or
  Choice branch. Typical valid cases are protocol magic/literals, packet type
  or opcode discriminators, and reserved bits or fixed flags that the RFC says
  MUST have one value. A fixed protocol version may be a token only when this
  model intentionally supports exactly that version.
- Every token must have an explicit `value` justified by RFC evidence. Put it
  on the smallest discriminating scalar/string in the packet-specific model.
  Do not tokenize a container or a larger byte region merely because it happens
  to distinguish the supplied samples.
- Do NOT use `token="true"` for lengths/counts, identifiers, sequence numbers,
  timestamps, checksums, payload data, optional content, variable flags, or an
  enum/version field with multiple valid values. If several exact alternatives
  require distinct cracking branches, model the alternatives explicitly and
  tokenize only each branch's true discriminator. A token is not a substitute
  for Relation, Optional, repetition, or a semantic constraint.
- The RFC defines the accepted wire-language; seeds are only examples and
  regression inputs. The DataModel must accept valid unseen packets, including
  other legal values, lengths, counts, option combinations, repetitions, and
  payload sizes for every requested packet type.
- Never copy a seed-observed value into `value`/`token`, infer a fixed size or
  occurrence bound from the largest sample, remove an RFC-defined optional or
  alternate branch because no seed exercises it, or replace known structure
  with Blob just to make current seeds crack.
- During repair, fix the general RFC-level structural rule causing the failure
  and preserve all valid variants. Passing the supplied seeds is necessary but
  not sufficient; reject any repair that merely special-cases seed bytes,
  filenames, observed lengths, or the current corpus distribution.
"""


def _env_float(key: str, default: float) -> float:
    val = os.environ.get(key)
    if val is None:
        return default
    try:
        return float(val)
    except ValueError:
        return default


def _env_int(key: str, default: int) -> int:
    val = os.environ.get(key)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        return default
