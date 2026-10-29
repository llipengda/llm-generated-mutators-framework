"""Load editable model instructions independently of the working directory."""

from pathlib import Path
import re


PROMPT_ROOT = Path(__file__).resolve().parents[1] / "prompts"
_PLACEHOLDER = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}")


def load_prompt(name: str) -> str:
    """Read a UTF-8 prompt or schema; preserve its whitespace exactly."""
    path = (PROMPT_ROOT / name).resolve()
    if not path.is_relative_to(PROMPT_ROOT.resolve()):
        raise ValueError(f"Prompt path must stay inside {PROMPT_ROOT}: {name}")
    return path.read_text(encoding="utf-8")


def render_prompt(name: str, **values: object) -> str:
    """Replace named {{placeholders}} once, leaving JSON and C# braces intact."""
    template = load_prompt(name)
    missing = set(_PLACEHOLDER.findall(template)).difference(values)
    if missing:
        raise ValueError(f"Missing prompt values for {name}: {', '.join(sorted(missing))}")
    return _PLACEHOLDER.sub(lambda match: str(values[match[1]]), template)
