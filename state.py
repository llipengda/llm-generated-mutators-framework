import json
import os
from typing import TypedDict

from ui import UI


class PipelineState(TypedDict):
    packet_types: list[str]
    constraints: str
    token_usage_total: dict[str, int]
    token_usage_by_step: dict[str, dict[str, int]]


def new_usage_bucket() -> dict[str, int]:
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cached_tokens": 0,
        "total_tokens": 0,
        "calls": 0,
    }


def add_step_usage(
    state: PipelineState,
    *,
    step_title: str,
    usage: dict[str, int],
) -> None:
    total = state.setdefault("token_usage_total", new_usage_bucket())
    by_step = state.setdefault("token_usage_by_step", {})
    step_bucket = by_step.setdefault(step_title, new_usage_bucket())

    for key in ("prompt_tokens", "completion_tokens", "cached_tokens", "total_tokens", "calls"):
        val = int(usage.get(key, 0))
        total[key] += val
        step_bucket[key] += val

REPO_ROOT = os.path.dirname(__file__)

def _pipeline_state_path(protocol_name: str) -> str:
    state_dir = os.path.join(REPO_ROOT, ".pipeline_state")
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, f"{protocol_name}.json")


# ---------------------------------------------------------------------------
# Session-scoped pipeline state — lives inside the session folder
# under data/uploads/sessions/<session_id>/pipeline_state.json.
# When the session folder is deleted, the state is cleaned up automatically.
# ---------------------------------------------------------------------------

SESSION_STATE_FILENAME = "pipeline_state.json"


def _session_dir(session_id: str) -> str:
    return os.path.join(REPO_ROOT, "data", "uploads", "sessions", session_id)


def _session_state_path(session_id: str) -> str:
    return os.path.join(_session_dir(session_id), SESSION_STATE_FILENAME)


def load_session_state(session_id: str) -> PipelineState:
    """Load pipeline state for a specific API session."""
    path = _session_state_path(session_id)
    if not os.path.exists(path):
        return {
            "packet_types": [],
            "constraints": "",
            "token_usage_total": new_usage_bucket(),
            "token_usage_by_step": {},
        }
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "packet_types" in data and "constraints" in data:
            data.setdefault("token_usage_total", new_usage_bucket())
            data.setdefault("token_usage_by_step", {})
            data.setdefault("_session_meta", {})
            return data  # type: ignore
    except Exception as e:
        UI.warn(f"Warning: failed to load session state from {path}: {e}")
    return {
        "packet_types": [],
        "constraints": "",
        "token_usage_total": new_usage_bucket(),
        "token_usage_by_step": {},
    }


def save_session_state(state: PipelineState, session_id: str) -> None:
    """Persist pipeline state for a specific API session."""
    path = _session_state_path(session_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.tmp"
    UI.dim(f"Saving session state to {path}...")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp_path, path)
    except Exception as e:
        UI.warn(f"Warning: failed to save session state to {path}: {e}")
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass


def delete_session_state(session_id: str) -> None:
    """Remove the pipeline state file for a session (and the session dir if empty)."""
    path = _session_state_path(session_id)
    if os.path.exists(path):
        os.remove(path)
    # Remove session dir if it's now empty.
    sdir = _session_dir(session_id)
    try:
        if os.path.isdir(sdir) and not os.listdir(sdir):
            os.rmdir(sdir)
    except OSError:
        pass


def list_session_state_ids() -> list[str]:
    """Return session IDs by scanning for pipeline_state.json inside session dirs."""
    sessions_root = os.path.join(REPO_ROOT, "data", "uploads", "sessions")
    if not os.path.isdir(sessions_root):
        return []
    ids: list[str] = []
    for name in os.listdir(sessions_root):
        state_file = os.path.join(sessions_root, name, SESSION_STATE_FILENAME)
        if os.path.isfile(state_file):
            ids.append(name)
    return ids


def load_pipeline_state(protocol_name: str) -> PipelineState:
    path = _pipeline_state_path(protocol_name)
    if not os.path.exists(path):
        return {
            "packet_types": [],
            "constraints": "",
            "token_usage_total": new_usage_bucket(),
            "token_usage_by_step": {},
        }

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "packet_types" in data and "constraints" in data:
            data.setdefault("token_usage_total", new_usage_bucket())
            data.setdefault("token_usage_by_step", {})
            return data # type: ignore
    except Exception as e:
        UI.warn(
            f"Warning: failed to load pipeline state from {path}: {e}"
        )

    return {
        "packet_types": [],
        "constraints": "",
        "token_usage_total": new_usage_bucket(),
        "token_usage_by_step": {},
    }


def save_pipeline_state(state: PipelineState, protocol_name: str) -> None:
    path = _pipeline_state_path(protocol_name)
    tmp_path = f"{path}.tmp"

    UI.dim(f"Saving pipeline state to {path}...")

    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp_path, path)
    except Exception as e:
        UI.warn(
            f"Warning: failed to save pipeline state to {path}: {e}"
        )
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
