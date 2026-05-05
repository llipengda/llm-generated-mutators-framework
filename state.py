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

def _pipeline_state_path(protocol_name: str) -> str:
    repo_root = os.path.dirname(__file__)
    state_dir = os.path.join(repo_root, ".pipeline_state")
    os.makedirs(state_dir, exist_ok=True)
    return os.path.join(state_dir, f"{protocol_name}.json")


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
