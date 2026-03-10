import json
import os
from typing import TypedDict

from console import console


class PipelineState(TypedDict):
    packet_types: list[str]
    constraints: str

def _pipeline_state_path() -> str:
    # Store next to repo root to make it easy to find/reuse across runs.
    repo_root = os.path.join(os.path.dirname(__file__), "..")
    return os.path.join(repo_root, ".pipeline_state.json")


def load_pipeline_state() -> PipelineState:
    path = _pipeline_state_path()
    if not os.path.exists(path):
        return {"packet_types": [], "constraints": ""}

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "packet_types" in data and "constraints" in data:
            return data # type: ignore
    except Exception as e:
        console.print(
            f"[yellow]Warning: failed to load pipeline state from {path}: {e}[/yellow]"
        )

    return {"packet_types": [], "constraints": ""}


def save_pipeline_state(state: PipelineState) -> None:
    path = _pipeline_state_path()
    tmp_path = f"{path}.tmp"

    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp_path, path)
    except Exception as e:
        console.print(
            f"[yellow]Warning: failed to save pipeline state to {path}: {e}[/yellow]"
        )
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
