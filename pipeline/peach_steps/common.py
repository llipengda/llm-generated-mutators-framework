from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Protocol, TypedDict

from agent import AgentConfig
from langchain_core.callbacks.base import BaseCallbackHandler
from langchain_core.retrievers import BaseRetriever
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
    diagnosis_agent_config: AgentConfig
    datamodel_autofix_agent_config: AgentConfig
    tool_usage_logger: BaseCallbackHandler

    call_agent: Callable[..., AgentResponse]
    save_state: Callable[[], None]
    fix_verify_loop: Callable[..., bool]

    def _data_type_paths(self) -> tuple[Path, Path, Path]:
        raise NotImplementedError

    def _load_data_type_analysis(self, report_path: Path) -> dict:
        raise NotImplementedError

    def _finalize_data_type_support(self, report: dict) -> None:
        raise NotImplementedError

    def _custom_data_element_context(self) -> str:
        raise NotImplementedError

    def repair_datamodel_assembly(
        self, *, allow_packet_type_additions: bool = False
    ) -> None:
        raise NotImplementedError


_DATAMODEL_MODELING_GUARDRAILS = """
DataModel structural, fixed-value, and generalization rules:
- Model RFC-defined length and count relationships whenever the documented DSL
  can express them. Connect a length/count field to the data it governs with
  `Blob[length]`, `String[length]`, `Block[length]`, `Array[element, count]`, or
  a supported field expression as appropriate. Prefer these relationships over
  an unrelated scalar plus an unbounded `Blob` or `Array`; do not leave a known
  relationship implicit merely because the supplied seeds still crack.
- Small, deterministic, side-effect-free helper functions may be defined at
  module scope when they reduce repeated declarations or substantially shorten
  the generated DSL. Do not define them as members of a `Schema` class. Give
  their parameters and return values Pyright-compatible type annotations.
  Helpers may construct DSL declarations, but must not perform I/O, mutate global
  state, dynamically evaluate code, or import non-DSL modules.
- DSL `fixed(value)` is allowed when, and only when, the RFC requires one exact
  wire value and matching that value is needed to crack the correct model or
  Choice branch. Typical valid cases are protocol magic/literals, packet type
  or opcode discriminators, and reserved bits or fixed flags that the RFC says
  MUST have one value. A fixed protocol version may be a token only when this
  model intentionally supports exactly that version.
- Every fixed value must be justified by RFC evidence. Put it
  on the smallest discriminating scalar/string in the packet-specific model.
  Do not tokenize a container or a larger byte region merely because it happens
  to distinguish the supplied samples.
- Do NOT use `fixed(...)` for lengths/counts, identifiers, sequence numbers,
  timestamps, checksums, payload data, optional content, variable flags, or an
  enum/version field with multiple valid values. If several exact alternatives
  require distinct cracking branches, model the alternatives explicitly and
  fix only each branch's true discriminator. A fixed value is not a substitute
  for a length reference, Optional, repetition, or a semantic constraint.
- The RFC defines the accepted wire-language; seeds are only examples and
  regression inputs. The DataModel must accept valid unseen packets, including
  other legal values, lengths, counts, option combinations, repetitions, and
  payload sizes for every requested packet type.
- Never copy a seed-observed value into `fixed(...)`, infer a fixed size or
  occurrence bound from the largest sample, remove an RFC-defined optional or
  alternate branch because no seed exercises it, or replace known structure
  with Blob just to make current seeds crack.
- During repair, fix the general RFC-level structural rule causing the failure
  and preserve all valid variants. Passing the supplied seeds is necessary but
  not sufficient; reject any repair that merely special-cases seed bytes,
  filenames, observed lengths, or the current corpus distribution.
"""


_DATAMODEL_DSL_SOURCE_STYLE = """
DSL source style rules:
- Keep generated DSL modules compact and code-focused. Do not write module,
  class, or helper-function docstrings; section banners; separator lines; RFC
  summaries; or multi-line explanatory comment blocks.
- Omit comments for declarations whose meaning is already clear from the symbol,
  field name, DSL type, or fixed value.
- When a non-obvious field needs clarification, add at most one short end-of-line
  comment directly after that field declaration, using `field = DSL(...)  # ...`.
  Keep the comment on the same physical line as the field and state only the
  essential wire-format fact.
- Do not place standalone comments before or after fields. Prefer clear names and
  concise DSL declarations over prose.
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
