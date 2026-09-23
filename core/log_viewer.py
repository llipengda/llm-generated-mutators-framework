"""Build a self-contained, offline viewer for pipeline JSONL logs."""

from __future__ import annotations

import json
from pathlib import Path
import webbrowser

import click


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def render_viewer(source: str, name: str) -> str:
    """Embed logs as inert JSON; log text must never become HTML or JavaScript."""
    payload = json.dumps({"name": name, "source": source}, ensure_ascii=True)
    payload = payload.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    template = Path(__file__).with_name("log_viewer.html").read_text(encoding="utf-8")
    return template.replace("__LOG_PAYLOAD__", payload)


def resolve_source(source: str) -> Path:
    path = Path(source).expanduser()
    if path.is_file():
        return path.resolve()
    # A protocol name is a single directory component, not an arbitrary path.
    if path.name == source and source not in {".", ".."}:
        candidate = PROJECT_ROOT / "logs" / source / "log.jsonl"
        if candidate.is_file():
            return candidate
    raise click.ClickException(f"Log not found: {source} (expected a JSONL file or protocol name)")


@click.command()
@click.argument("source", required=False)
@click.option("--output", type=click.Path(path_type=Path), help="Output HTML path (default: tmp/log-viewer.html).")
@click.option("--open/--no-open", "open_browser", default=True, help="Open the generated viewer in your browser.")
def main(source: str | None, output: Path | None, open_browser: bool) -> None:
    """View a protocol's log or a JSONL file in an offline browser page.

    SOURCE is a protocol name (e.g. mqtt) or a file path. Omit it to open an
    empty viewer and choose/drop a file. The page is a snapshot, not a live tail.
    """
    path = resolve_source(source) if source else None
    destination = (output or PROJECT_ROOT / "tmp" / "log-viewer.html").expanduser().resolve()
    if path == destination:
        raise click.ClickException("Output must not overwrite the source log.")
    try:
        content = path.read_text(encoding="utf-8", errors="replace") if path else ""
        html = render_viewer(content, str(path) if path else "")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(html, encoding="utf-8")
    except OSError as error:
        raise click.ClickException(str(error)) from error
    click.echo(str(destination))
    if open_browser:
        webbrowser.open(destination.as_uri())


if __name__ == "__main__":
    main()
