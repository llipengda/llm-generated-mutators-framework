"""Pydantic models for the API."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# LLM Configuration
# ---------------------------------------------------------------------------


class LlmConfig(BaseModel):
    """Per-session LLM configuration.  All fields optional — fall back to env."""

    api_key: str | None = None
    base_url: str | None = None
    model: str | None = None
    temperature: float | None = None
    embedding_model: str | None = None
    embedding_base_url: str | None = None
    embedding_api_key: str | None = None

    def to_overrides(self):
        """Convert to the internal dataclass used by agent/rag."""
        from agent import LlmOverrides

        return LlmOverrides(
            api_key=self.api_key,
            base_url=self.base_url,
            model=self.model,
            temperature=self.temperature,
            embedding_model=self.embedding_model,
            embedding_base_url=self.embedding_base_url,
            embedding_api_key=self.embedding_api_key,
        )


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


class CreateSessionResponse(BaseModel):
    session_id: str
    protocol: str
    fixer_enabled: bool
    rfc_path: str
    seed_dir: str
    available_steps: list[str]
    created_at: datetime


class SessionSummary(BaseModel):
    session_id: str
    protocol: str
    fixer_enabled: bool
    created_at: datetime
    completed_steps: int
    total_steps: int
    status: Literal["idle", "running", "completed", "failed"]


class StepStatus(BaseModel):
    step_id: str
    name: str
    status: Literal["pending", "running", "completed", "failed", "skipped"]
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    available: bool = False


class TokenUsageSummary(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    total_tokens: int = 0
    calls: int = 0


class SessionDetail(BaseModel):
    session_id: str
    protocol: str
    fixer_enabled: bool
    created_at: datetime
    steps: dict[str, StepStatus]
    packet_types: list[str] | None = None
    token_usage: dict[str, TokenUsageSummary] | None = None
    rfc_path: str
    seed_dir: str


# ---------------------------------------------------------------------------
# RFC
# ---------------------------------------------------------------------------


class RfcInfo(BaseModel):
    rfc_id: str
    filename: str
    size_bytes: int
    uploaded_at: datetime


# ---------------------------------------------------------------------------
# Seeds
# ---------------------------------------------------------------------------


class SeedsInfo(BaseModel):
    seeds_id: str
    file_count: int
    filenames: list[str]
    size_bytes: int
    uploaded_at: datetime


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


class RunStepRequest(BaseModel):
    selected_types: list[str] | None = Field(
        default=None,
        description="For step_4: filter which packet types to generate mutators for.",
    )
    skip_verification: bool = Field(
        default=False,
        description="For step_5/step_9: skip the first verification pass.",
    )


class RunStepResponse(BaseModel):
    session_id: str
    step_id: str
    status: Literal["completed", "failed"]
    output: str | None = None
    llm_outputs: list[str] | None = Field(
        default=None,
        description="LLM response contents from every call_agent invocation in this step.",
    )
    token_usage: dict[str, int] | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    sdk_available: bool
    api_mode: bool
