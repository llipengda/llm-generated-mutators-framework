import asyncio
import subprocess
import threading
import questionary

from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from log import console
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph


QUESTIONARY_BASE_STYLE = questionary.Style(
    [
        ("qmark", "fg:#673ab7 bold"),
        ("question", "bold"),
        ("answer", "fg:#f44336 bold"),
        ("pointer", "fg:#673ab7 bold"),
        ("highlighted", "fg:#673ab7 bold"),
        ("selected", "fg:#cc5454"),
        ("separator", "fg:#cc5454"),
        ("instruction", ""),
        ("text", ""),
        ("disabled", "fg:#858585 italic"),
    ]
)


class UI:
    @staticmethod
    def title(text: str):
        console.rule(f"[bold blue]{text}[/bold blue]")

    @staticmethod
    def warning_rule(text: str):
        console.rule(f"[yellow]{text}[/yellow]", style="yellow")

    @staticmethod
    def status(text: str, *, spinner: str = "dots"):
        return console.status(f"[bold cyan]{text}[/bold cyan]", spinner=spinner)

    @staticmethod
    def panel(content: str | Markdown, *, title: str | None = None, border_style: str = "blue", expand: bool = False, style: str | None = None):
        panel_kwargs = {
            "title": title,
            "border_style": border_style,
            "expand": expand,
        }
        if style is not None:
            panel_kwargs["style"] = style

        console.print(
            Panel(
                content,
                **panel_kwargs,
            )
        )

    @staticmethod
    def result_markdown(step_title: str, content: str):
        UI.panel(
            Markdown(content),
            title=f"Result: {step_title}",
            border_style="blue",
            expand=False,
        )

    @staticmethod
    def warn(message: str):
        console.print(f"[bold yellow]{message}[/bold yellow]")

    @staticmethod
    def error(message: str):
        console.print(f"[bold red]{message}[/bold red]")

    @staticmethod
    def success(message: str):
        console.print(f"[bold green]{message}[/bold green]")

    @staticmethod
    def dim(message: str):
        console.print(f"[dim]{message}[/dim]")

    print = staticmethod(console.print)

    @staticmethod
    def run_with_live_output(
        cmd: list[str], *, title: str = "", max_lines: int = 20
    ) -> subprocess.CompletedProcess:
        """Run a subprocess with live-scrolling output in a Rich panel.

        Returns the CompletedProcess (stdout contains all captured output).
        """
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        lines: list[str] = []
        lock = threading.Lock()

        def _reader() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                cleaned = line.rstrip("\r\n")
                if cleaned:
                    with lock:
                        lines.append(cleaned)

        reader_thread = threading.Thread(target=_reader, daemon=True)
        reader_thread.start()

        title_str = f"[bold cyan]{title}[/bold cyan]" if title else ""
        term_width = console.width
        content_width = max(term_width - 4, 40)

        # Start with a full-height placeholder so Live never resizes
        placeholder = "\n".join([""] * max_lines)

        with Live(
            Panel(placeholder, title=title_str, border_style="grey50"),
            console=console,
            refresh_per_second=8,
        ) as live:
            while reader_thread.is_alive() or proc.poll() is None:
                with lock:
                    tail = lines[-max_lines:]
                padded = tail + [""] * (max_lines - len(tail))
                clipped = [ln[:content_width] for ln in padded]
                live.update(
                    Panel("\n".join(clipped), title=title_str, border_style="grey50")
                )
                reader_thread.join(timeout=0.15)

        proc.wait()
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=proc.returncode,
            stdout="\n".join(lines),
        )


def ask_before_step(step_name: str, *, has_previous: bool, timeout_s: float = 60.0) -> str:
    """Ask what to do BEFORE starting a step.

    Returns one of: 'continue', 'retry_prev', 'skip', 'exit'.
    Defaults to 'continue' after timeout.
    """
    console.print()
    console.print(
        f"[blue italic]About to start: {step_name} (auto-continue in {timeout_s:.0f}s)[/blue italic]"
    )

    choices: list[str | questionary.Choice] = [
        "Continue",
        questionary.Choice(
            "Go to previous step", disabled=None if has_previous else "No previous step"
        ),
        "Go to next step",
        "Exit",
    ]

    question = questionary.select(
        "Choose an action:",
        choices=choices,
        style=QUESTIONARY_BASE_STYLE,
    )

    async def get_input_with_timeout():
        try:
            return await asyncio.wait_for(question.ask_async(), timeout=timeout_s)
        except asyncio.TimeoutError:
            return "TIMEOUT"

    choice = asyncio.run(get_input_with_timeout())
    if choice == "TIMEOUT" or choice is None:
        UI.dim("Timeout reached. Defaulting to: Continue")
        choice = "Continue"

    if choice == "Continue":
        return "continue"
    if choice == "Go to previous step":
        return "retry_prev"
    if choice == "Go to next step":
        return "skip"
    return "exit"


def ask_resume_state(protocol_name: str) -> bool:
    """Ask whether to resume from a saved pipeline state.

    Returns True to resume, False to start fresh.
    """
    console.print()
    console.print(
        f"[bold blue]Found saved pipeline state for protocol: {protocol_name}[/bold blue]"
    )

    choice = questionary.select(
        "Would you like to resume from the saved state?",
        choices=[
            "Resume from saved state",
            "Start fresh (discard saved state)",
        ],
        style=QUESTIONARY_BASE_STYLE,
    ).ask()

    if choice is None:
        return False
    return choice == "Resume from saved state"


def ask_after_fix_failure(step_title: str) -> str:
    """Ask user what to do after auto-fix retries are exhausted.

    Returns 'wait', 'hint', or 'exit'. No timeout.
    """
    console.print()
    console.print(
        f"[bold red]Auto-fix retries exhausted for: {step_title}[/bold red]"
    )

    choice = questionary.select(
        "What would you like to do?",
        choices=[
            "Wait for me to fix manually, then re-verify",
            "Provide a hint and retry the LLM fix",
            "Exit pipeline",
        ],
        style=QUESTIONARY_BASE_STYLE,
    ).ask()

    if choice is None:
        return "exit"
    if choice == "Provide a hint and retry the LLM fix":
        return "hint"
    if choice == "Wait for me to fix manually, then re-verify":
        return "wait"
    return "exit"


def ask_skip_verification(step_title: str) -> bool:
    """Ask whether to skip a time-consuming verification step.

    Returns True to skip, False to run the verification.
    """
    console.print()
    console.print(
        f"[bold yellow]Verification step: {step_title} (may take a long time)[/bold yellow]"
    )

    choice = questionary.select(
        "What would you like to do?",
        choices=[
            "Run verification",
            "Skip verification",
        ],
        style=QUESTIONARY_BASE_STYLE,
    ).ask()

    if choice is None:
        return False
    return choice == "Skip verification"


def ask_wait_for_fix(step_title: str) -> None:
    """Pause and wait for the user to manually fix files, then press Enter."""
    console.print()
    console.print(
        f"[bold yellow]Please fix the issue manually for: {step_title}[/bold yellow]"
    )
    console.print(
        "[dim]Press Enter when you are ready to re-verify...[/dim]"
    )
    input()


def ask_regenerate(what: str, protocol: str) -> bool:
    """Ask whether to regenerate existing generated code.

    Returns True to regenerate, False to skip.
    """
    console.print()
    console.print(
        f"[bold blue]Found existing {what} for protocol: {protocol}[/bold blue]"
    )

    choice = questionary.select(
        "Would you like to regenerate?",
        choices=[
            "Regenerate",
            "Use existing and skip",
        ],
        style=QUESTIONARY_BASE_STYLE,
    ).ask()

    if choice is None:
        return False
    return choice == "Regenerate"


def ask_for_hint(step_title: str) -> str:
    """Ask user for a free-text hint to guide the LLM fix. No timeout."""
    console.print()
    console.print(
        f"[bold yellow]Enter a hint to guide the LLM fix for: {step_title}[/bold yellow]"
    )

    hint = questionary.text(
        "Hint:",
        style=QUESTIONARY_BASE_STYLE,
    ).ask()

    return hint or ""


def run_agent_step(*, agent_graph: CompiledStateGraph, prompt_text: str, config: RunnableConfig, step_title: str):
    """Run the agent with a loading spinner and formatted output."""

    with UI.status(f"LLM is thinking & coding for {step_title}...", spinner="dots"):
        response = agent_graph.invoke(
            {"messages": [{"role": "user", "content": prompt_text}]},
            config=config,
        )
        final_response = response["messages"][-1].content

    UI.result_markdown(step_title, final_response)

    return response

