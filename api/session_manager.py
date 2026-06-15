"""Session lifecycle management & step execution engine.

Each session is backed by a per-session pipeline state file under
``.pipeline_state/session_<session_id>.json``.  The file stores the
standard PipelineState fields (packet_types, constraints, token_usage_*)
plus a ``_session_meta`` key with session metadata (protocol, fixer,
rfc_path, seed_dir, created_at, step_statuses).

This design:
- Isolates concurrent sessions for the same protocol.
- Survives server restarts (sessions can be recovered from disk).
"""

from __future__ import annotations

import os
import threading
import traceback
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Literal

from api.models import (
    CreateSessionResponse,
    RunStepRequest,
    RunStepResponse,
    SessionDetail,
    SessionSummary,
    StepStatus,
    TokenUsageSummary,
)
from agent import LlmOverrides
from config import build_config_from_args
from log import clear_api_session, set_api_session
from state import (
    list_session_state_ids,
    load_session_state,
    save_session_state,
)

# ---------------------------------------------------------------------------
# Step definitions
# ---------------------------------------------------------------------------

STEP_ORDER: list[str] = [
    "step_1",
    "step_2",
    "step_3",
    "step_4",
    "step_5",
    "step_6",
    "step_6_1",
    "step_7",
    "step_7_5",
    "step_8",
    "step_9",
    "step_final",
]

STEP_NAMES: dict[str, str] = {
    "step_1": "Packet Types Extraction",
    "step_2": "Datamodel Generation",
    "step_3": "Datamodel Validation & Fix",
    "step_4": "Mutator Generation",
    "step_5": "Mutator Validation & Fix",
    "step_6": "Constraint Extraction",
    "step_6_1": "Constraint Filtering",
    "step_7": "Fixer Generation",
    "step_7_5": "Fixer-Constraint Mapping",
    "step_8": "Fixer Test Generation",
    "step_9": "Fixer Validation & Fix",
    "step_final": "Final Compilation",
}

STEP_DEPENDENCIES: dict[str, list[str]] = {
    "step_1": [],
    "step_2": ["step_1"],
    "step_3": ["step_2"],
    "step_4": ["step_2"],
    "step_5": ["step_4"],
    "step_6": ["step_1"],
    "step_6_1": ["step_6"],
    "step_7": ["step_6_1"],
    "step_7_5": ["step_7"],
    "step_8": ["step_7_5"],
    "step_9": ["step_8"],
    "step_final": ["step_5", "step_8"],
}

FIXER_STEPS: set[str] = {
    "step_6",
    "step_6_1",
    "step_7",
    "step_7_5",
    "step_8",
    "step_9",
}

# ---------------------------------------------------------------------------
# Session metadata helpers
# ---------------------------------------------------------------------------

_SESSION_META_KEY = "_session_meta"


def _build_session_meta(ctx: "SessionContext") -> dict:
    """Serialize session metadata to a JSON-safe dict."""
    llm = None
    if ctx.llm_overrides is not None:
        llm = {
            "api_key": ctx.llm_overrides.api_key,
            "base_url": ctx.llm_overrides.base_url,
            "model": ctx.llm_overrides.model,
            "temperature": ctx.llm_overrides.temperature,
            "embedding_model": ctx.llm_overrides.embedding_model,
            "embedding_base_url": ctx.llm_overrides.embedding_base_url,
            "embedding_api_key": ctx.llm_overrides.embedding_api_key,
        }
    return {
        "session_id": ctx.session_id,
        "protocol": ctx.protocol,
        "fixer": ctx.fixer,
        "rfc_path": ctx.rfc_path,
        "seed_dir": ctx.seed_dir,
        "llm_overrides": llm,
        "created_at": ctx.created_at.isoformat(),
        "step_statuses": {
            sid: {
                "step_id": ss.step_id,
                "name": ss.name,
                "status": ss.status,
                "started_at": ss.started_at.isoformat() if ss.started_at else None,
                "finished_at": ss.finished_at.isoformat() if ss.finished_at else None,
                "error": ss.error,
            }
            for sid, ss in ctx.step_statuses.items()
        },
    }


def _restore_session_meta(meta: dict) -> tuple[
    str,  # protocol
    bool,  # fixer
    str,  # rfc_path
    str,  # seed_dir
    LlmOverrides | None,  # llm_overrides
    datetime,  # created_at
    dict[str, StepStatus],  # step_statuses
]:
    protocol = meta.get("protocol", "unknown")
    fixer = meta.get("fixer", False)
    rfc_path = meta.get("rfc_path", "")
    seed_dir = meta.get("seed_dir", "")

    llm_raw = meta.get("llm_overrides")
    llm_overrides: LlmOverrides | None = None
    if isinstance(llm_raw, dict):
        llm_overrides = LlmOverrides(
            api_key=llm_raw.get("api_key"),
            base_url=llm_raw.get("base_url"),
            model=llm_raw.get("model"),
            temperature=llm_raw.get("temperature"),
            embedding_model=llm_raw.get("embedding_model"),
            embedding_base_url=llm_raw.get("embedding_base_url"),
            embedding_api_key=llm_raw.get("embedding_api_key"),
        )

    created_at = datetime.fromisoformat(meta["created_at"]) if meta.get("created_at") else datetime.now(timezone.utc)
    step_statuses: dict[str, StepStatus] = {}
    for sid, sd in meta.get("step_statuses", {}).items():
        step_statuses[sid] = StepStatus(
            step_id=sd.get("step_id", sid),
            name=sd.get("name", STEP_NAMES.get(sid, sid)),
            status=sd.get("status", "pending"),
            started_at=datetime.fromisoformat(sd["started_at"]) if sd.get("started_at") else None,
            finished_at=datetime.fromisoformat(sd["finished_at"]) if sd.get("finished_at") else None,
            error=sd.get("error"),
            available=False,
        )
    return protocol, fixer, rfc_path, seed_dir, llm_overrides, created_at, step_statuses


# ---------------------------------------------------------------------------
# Session context
# ---------------------------------------------------------------------------


class SessionContext:
    def __init__(
        self,
        session_id: str,
        protocol: str,
        fixer: bool,
        rfc_path: str,
        seed_dir: str,
        *,
        llm_overrides: LlmOverrides | None = None,
        created_at: datetime | None = None,
        step_statuses: dict[str, StepStatus] | None = None,
    ):
        self.session_id = session_id
        self.protocol = protocol
        self.fixer = fixer
        self.rfc_path = rfc_path
        self.seed_dir = seed_dir
        self.llm_overrides = llm_overrides
        self.created_at = created_at or datetime.now(timezone.utc)

        # Step statuses.
        if step_statuses is not None:
            self.step_statuses = step_statuses
        else:
            self.step_statuses = {}
            for sid in STEP_ORDER:
                fixer_only = sid in FIXER_STEPS
                self.step_statuses[sid] = StepStatus(
                    step_id=sid,
                    name=STEP_NAMES[sid],
                    status="pending",
                    available=not fixer_only or fixer,
                )

        self._pipeline = None
        self._pipeline_lock = threading.Lock()
        self._current_future: Future | None = None
        self._dirty = False  # set when step statuses change → persist

    @property
    def pipeline(self):
        """Lazy-init the PeachPipeline on first access.

        Uses ``session_id`` as the state namespace so the pipeline state
        file is scoped to this session (no cross-session conflicts).
        """
        if self._pipeline is None:
            with self._pipeline_lock:
                if self._pipeline is None:
                    from pipeline.peach import PeachPipeline

                    self._pipeline = PeachPipeline(
                        interactive=False,
                        state_namespace=self.session_id,
                        llm_overrides=self.llm_overrides,
                    )
        return self._pipeline

    def _check_dependencies(self, step_id: str) -> bool:
        for dep in STEP_DEPENDENCIES.get(step_id, []):
            if self.step_statuses[dep].status != "completed":
                return False
        return True

    def _refresh_availability(self) -> None:
        for sid in STEP_ORDER:
            fixer_only = sid in FIXER_STEPS
            deps_met = self._check_dependencies(sid)
            self.step_statuses[sid].available = (
                deps_met and (not fixer_only or self.fixer)
            )

    def get_available_steps(self) -> list[str]:
        self._refresh_availability()
        return [sid for sid in STEP_ORDER if self.step_statuses[sid].available]

    def get_completed_steps(self) -> int:
        return sum(
            1 for s in self.step_statuses.values() if s.status == "completed"
        )

    def get_total_steps(self) -> int:
        enabled = set(STEP_ORDER)
        if not self.fixer:
            enabled -= FIXER_STEPS
        return len(enabled)

    def get_overall_status(self) -> Literal["running", "failed", "completed", "idle"]:
        all_enabled = [
            sid
            for sid in STEP_ORDER
            if not (sid in FIXER_STEPS and not self.fixer)
        ]
        if any(
            self.step_statuses[s].status == "running" for s in all_enabled
        ):
            return "running"
        failed = [
            s for s in all_enabled if self.step_statuses[s].status == "failed"
        ]
        if failed:
            return "failed"
        if all(
            self.step_statuses[s].status == "completed" for s in all_enabled
        ):
            return "completed"
        return "idle"

    def persist_meta(self) -> None:
        """Write session metadata into the pipeline state file.

        Preserves existing pipeline state fields (packet_types,
        constraints, token_usage) if the file already exists.
        """
        existing: dict[str, Any] = dict(load_session_state(self.session_id))
        existing[_SESSION_META_KEY] = _build_session_meta(self)
        # Merge back anything the pipeline may have written.
        if self._pipeline is not None:
            existing.update(self._pipeline.state)
        save_session_state(existing, self.session_id)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Session manager
# ---------------------------------------------------------------------------


class SessionManager:
    def __init__(self, max_workers: int = 2):
        self._sessions: dict[str, SessionContext] = {}
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._lock = threading.Lock()

    # ---- recovery ----

    def recover_sessions(self) -> int:
        """Restore sessions from persisted state files.  Returns count."""
        recovered = 0
        state_ids = list_session_state_ids()
        if state_ids:
            print(f"[INFO] Found {len(state_ids)} saved session(s) on disk.")
        for sid in state_ids:
            with self._lock:
                if sid in self._sessions:
                    continue  # already loaded
            state = load_session_state(sid)
            meta = state.get(_SESSION_META_KEY)
            if not meta:
                print(f"[WARN] Session {sid}: no _session_meta in state file, skipping.")
                continue
            try:
                protocol, fixer, rfc_path, seed_dir, llm_overrides, created_at, step_statuses = _restore_session_meta(meta)
            except Exception as e:
                print(f"[WARN] Session {sid}: failed to parse metadata: {e}")
                continue

            ctx = SessionContext(
                session_id=sid,
                protocol=protocol,
                fixer=fixer,
                rfc_path=rfc_path,
                seed_dir=seed_dir,
                llm_overrides=llm_overrides,
                created_at=created_at,
                step_statuses=step_statuses,
            )
            ctx._refresh_availability()
            with self._lock:
                self._sessions[sid] = ctx
            completed = ctx.get_completed_steps()
            print(f"[INFO] Recovered session {sid} (protocol={protocol}, {completed}/{ctx.get_total_steps()} steps done)")
            recovered += 1
        return recovered

    # ---- session CRUD ----

    def create_session(
        self,
        protocol: str,
        *,
        fixer: bool,
        rfc_path: str,
        seed_dir: str,
        llm_overrides: LlmOverrides | None = None,
    ) -> CreateSessionResponse:
        session_id = uuid.uuid4().hex[:12]
        ctx = SessionContext(
            session_id=session_id,
            protocol=protocol,
            fixer=fixer,
            rfc_path=rfc_path,
            seed_dir=seed_dir,
            llm_overrides=llm_overrides,
        )
        # Persist immediately so the session survives a restart.
        ctx.persist_meta()

        with self._lock:
            self._sessions[session_id] = ctx

        available = ctx.get_available_steps()
        return CreateSessionResponse(
            session_id=session_id,
            protocol=protocol,
            fixer_enabled=fixer,
            rfc_path=rfc_path,
            seed_dir=seed_dir,
            available_steps=available,
            created_at=ctx.created_at,
        )

    def get_session(self, session_id: str) -> SessionContext:
        with self._lock:
            ctx = self._sessions.get(session_id)
        if ctx is None:
            raise ValueError(f"Session not found: {session_id}")
        return ctx

    def list_sessions(self) -> list[SessionSummary]:
        with self._lock:
            ctxs = list(self._sessions.values())
        return [
            SessionSummary(
                session_id=c.session_id,
                protocol=c.protocol,
                fixer_enabled=c.fixer,
                created_at=c.created_at,
                completed_steps=c.get_completed_steps(),
                total_steps=c.get_total_steps(),
                status=c.get_overall_status(),
            )
            for c in ctxs
        ]

    def update_session_paths(
        self, session_id: str, rfc_path: str, seed_dir: str
    ) -> None:
        """Update the RFC path and seed directory for an existing session."""
        ctx = self.get_session(session_id)
        ctx.rfc_path = rfc_path
        ctx.seed_dir = seed_dir
        ctx.persist_meta()

    def delete_session(self, session_id: str) -> bool:
        with self._lock:
            ctx = self._sessions.pop(session_id, None)
        if ctx is None:
            return False
        # Cancel any running step.
        if ctx._current_future is not None:
            ctx._current_future.cancel()
        # Clean up session files (which includes pipeline_state.json).
        from api.file_store import delete_session_files

        delete_session_files(session_id)
        return True

    def get_session_detail(self, session_id: str) -> SessionDetail:
        ctx = self.get_session(session_id)
        ctx._refresh_availability()

        # Collect token usage.
        token_usage: dict[str, TokenUsageSummary] = {}
        if ctx._pipeline is not None:
            by_step = ctx._pipeline.state.get("token_usage_by_step", {})
            for step_key, usage in by_step.items():
                token_usage[step_key] = TokenUsageSummary(
                    prompt_tokens=usage.get("prompt_tokens", 0),
                    completion_tokens=usage.get("completion_tokens", 0),
                    cached_tokens=usage.get("cached_tokens", 0),
                    total_tokens=usage.get("total_tokens", 0),
                    calls=usage.get("calls", 0),
                )

        return SessionDetail(
            session_id=ctx.session_id,
            protocol=ctx.protocol,
            fixer_enabled=ctx.fixer,
            created_at=ctx.created_at,
            steps={
                sid: StepStatus(
                    step_id=ss.step_id,
                    name=ss.name,
                    status=ss.status,
                    started_at=ss.started_at,
                    finished_at=ss.finished_at,
                    error=ss.error,
                    available=ss.available,
                )
                for sid, ss in ctx.step_statuses.items()
            },
            packet_types=(
                ctx.pipeline.state.get("packet_types")
                if ctx._pipeline is not None
                else None
            ),
            token_usage=token_usage or None,
            rfc_path=ctx.rfc_path,
            seed_dir=ctx.seed_dir,
        )

    # ---- step execution ----

    def run_step(
        self, session_id: str, step_id: str, params: RunStepRequest
    ) -> RunStepResponse:
        ctx = self.get_session(session_id)

        # Validate.
        if step_id not in STEP_ORDER:
            raise ValueError(f"Unknown step: {step_id}")
        if step_id in FIXER_STEPS and not ctx.fixer:
            raise ValueError(
                f"Step {step_id} is fixer-only but fixer is not enabled."
            )
        ss = ctx.step_statuses[step_id]
        # Only block if genuinely running (future still active).
        if ss.status == "running":
            if ctx._current_future is not None and not ctx._current_future.done():
                raise ValueError(f"Step {step_id} is already running.")
            # Stale "running" status from a crashed run — allow re-run.

        # For re-runs: reset this step AND all subsequent steps.
        if ss.status in ("completed", "failed"):
            step_idx = STEP_ORDER.index(step_id)
            for sid in STEP_ORDER[step_idx:]:
                ctx.step_statuses[sid].status = "pending"
                ctx.step_statuses[sid].error = None
                ctx.step_statuses[sid].started_at = None
                ctx.step_statuses[sid].finished_at = None

        if not ctx._check_dependencies(step_id):
            missing = [
                d
                for d in STEP_DEPENDENCIES.get(step_id, [])
                if ctx.step_statuses[d].status != "completed"
            ]
            raise ValueError(
                f"Dependencies not met for {step_id}: {missing}"
            )

        # Mark running.
        ss.status = "running"
        ss.started_at = datetime.now(timezone.utc)
        ss.error = None
        ctx.persist_meta()

        # Execute in thread.
        def _execute():
            # Thread-local config.
            build_config_from_args(
                protocol=ctx.protocol,
                seed_dir=ctx.seed_dir,
                rfc_path=ctx.rfc_path,
                fixer=ctx.fixer,
            )

            # Mark current thread as API session for logging.
            set_api_session(session_id)
            try:
                pipeline = ctx.pipeline

                # Map step_id to method + kwargs.
                step_kwargs = self._build_step_kwargs(step_id, params)

                try:
                    method = getattr(pipeline, self._method_name(step_id))
                    method(**step_kwargs)
                    ss.status = "completed"
                except Exception:
                    ss.status = "failed"
                    ss.error = traceback.format_exc()

                ss.finished_at = datetime.now(timezone.utc)
                # Persist updated statuses + pipeline state in one shot.
                ctx.persist_meta()
            finally:
                clear_api_session()

        future = self._executor.submit(_execute)
        ctx._current_future = future
        try:
            future.result()  # block until done
        except Exception:
            ss.status = "failed"
            ss.error = traceback.format_exc()
            ss.finished_at = datetime.now(timezone.utc)
            ctx.persist_meta()

        ctx._refresh_availability()

        # Collect all LLM outputs produced during this step.
        llm_outputs: list[str] | None = None
        if ctx._pipeline is not None:
            outputs = ctx._pipeline._last_llm_outputs
            if outputs:
                llm_outputs = list(outputs)
            ctx._pipeline._last_llm_outputs.clear()  # reset for next step

        return RunStepResponse(
            session_id=session_id,
            step_id=step_id,
            status=ss.status,  # type: ignore[arg-type]
            output=ss.error if ss.status == "failed" else f"{STEP_NAMES[step_id]} completed.",
            llm_outputs=llm_outputs,
            token_usage=None,
            error=ss.error,
        )

    # ---- helpers ----

    @staticmethod
    def _method_name(step_id: str) -> str:
        mapping = {
            "step_1": "step_1_packet_types_extraction",
            "step_2": "step_2_datamodel_generation",
            "step_3": "step_3_datamodel_validation_and_fix",
            "step_4": "step_4_mutator_generation",
            "step_5": "step_5_mutator_validation_and_fix",
            "step_6": "step_6_constraint_extraction",
            "step_6_1": "step_6_1_constraint_filtering",
            "step_7": "step_7_fixer_generation",
            "step_7_5": "step_7_5_fixer_constraint_mapping",
            "step_8": "step_8_fixer_test_generation",
            "step_9": "step_9_fixer_validation_and_fix",
            "step_final": "step_final_compile",
        }
        return mapping[step_id]

    @staticmethod
    def _build_step_kwargs(
        step_id: str, params: RunStepRequest
    ) -> dict:
        if step_id == "step_4":
            return {
                "selected_types": params.selected_types,
                "regenerate": True,
            }
        if step_id in ("step_5", "step_9"):
            return {"skip_first_verification": params.skip_verification}
        return {}

    def get_log_path(self, session_id: str) -> str:
        from log import get_session_log_path

        path = get_session_log_path(session_id)
        if not os.path.exists(path):
            return ""
        return path
