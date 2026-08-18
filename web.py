"""Start the local Peach API and Pit Studio development server together."""

from __future__ import annotations

import signal
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def main() -> int:
    venv_python = ROOT / ".venv/bin/python"
    python = str(venv_python) if venv_python.exists() else sys.executable
    api = subprocess.Popen([python, "-m", "uvicorn", "web_server:app", "--host", "127.0.0.1", "--port", "8000"], cwd=ROOT)
    frontend = subprocess.Popen(["npm", "run", "dev"], cwd=ROOT / "pit-visualizer")

    def stop(*_args: object) -> None:
        for process in (frontend, api):
            if process.poll() is None:
                process.terminate()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        return frontend.wait()
    finally:
        stop()
        api.wait(timeout=10)


if __name__ == "__main__":
    raise SystemExit(main())
