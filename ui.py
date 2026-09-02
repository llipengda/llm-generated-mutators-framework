import subprocess
import threading

import questionary
from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.styles import Style

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
            while True:
                with lock:
                    tail = lines[-max_lines:]
                padded = tail + [""] * (max_lines - len(tail))
                clipped = [ln[:content_width] for ln in padded]
                live.update(
                    Panel("\n".join(clipped), title=title_str, border_style="grey50")
                )
                if not (reader_thread.is_alive() or proc.poll() is None):
                    break
                reader_thread.join(timeout=0.15)

        proc.wait()
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=proc.returncode,
            stdout="\n".join(lines),
        )


def ask_before_step(step_name: str, *, has_previous: bool, timeout_s: float = 60.0) -> tuple[str, str | None]:
    """Ask what to do BEFORE starting a step.

    Returns (action, extra_prompt) where action is one of: 'continue', 'retry_prev', 'skip', 'exit'.
    extra_prompt carries the user's additional instructions when the user types text
    on the "Continue with extra prompt" line and presses Enter.

    Uses a custom prompt_toolkit select menu with inline text editing.
    Use ↑/↓ to navigate, Enter to confirm. When "Continue with extra prompt"
    is selected, typing edits the text directly on that line.
    Defaults to 'continue' after timeout.
    """
    # ---------- mutable state ----------
    options = [
        ("continue", "Continue", False),
        ("continue", "Continue with extra prompt:", True),
        ("retry_prev", "Go to previous step", False),
        ("skip", "Go to next step", False),
        ("exit", "Exit", False),
    ]
    selected = [0]
    cursor_on = [True]   # set False on Enter to hide █ while keeping ▶
    confirmed = [False]  # set True on Enter: collapse display to selected option only
    extra_buffer = Buffer(multiline=False)
    result: list = [None]  # will hold (action, extra_text) or None

    # ---------- styles ----------
    style = Style(
        [
            ("message", "fg:ansiblue italic"),
            ("pointer", "fg:ansipurple bold"),
            ("selected", "bold"),
            ("extra", "fg:ansigreen"),
            ("dimmed", "fg:ansibrightblack"),
        ]
    )

    # ---------- display ----------
    def _render_line(i: int, label: str, editable: bool) -> list:
        """Render a single option line. Returns list of (style, text) tuples (no trailing newline)."""
        is_sel = i == selected[0]
        pointer = "▶" if is_sel else " "

        if i == 2 and not has_previous:
            return [("class:dimmed", f"  {pointer} {label}  (no previous step)")]

        if editable:
            text = extra_buffer.text
            cursor = "█" if (is_sel and cursor_on[0]) else ""
            if is_sel:
                return [
                    ("class:pointer", f"  {pointer} "),
                    ("class:selected", label),
                    ("", " "),
                    ("class:extra", text),
                    ("class:pointer", cursor),
                ]
            else:
                return [
                    ("", f"  {pointer} "),
                    ("", label),
                    ("", " "),
                    ("class:extra", text),
                ]
        else:
            if is_sel:
                return [
                    ("class:pointer", f"  {pointer} "),
                    ("class:selected", label),
                ]
            else:
                return [("", f"  {pointer} {label}")]

    def _render():
        if confirmed[0]:
            _, label, editable = options[selected[0]]
            lines = [("class:message", f"About to start: {step_name}"),
                     ("", "\n")]
            lines.extend(_render_line(selected[0], label, editable))
            return lines

        lines = [
            ("class:message", f"About to start: {step_name} (auto-continue in {timeout_s:.0f}s)"),
            ("", "\n"),
        ]
        for idx, (_, label, editable) in enumerate(options):
            if idx > 0:
                lines.append(("", "\n"))
            lines.extend(_render_line(idx, label, editable))
        return lines

    display_control = FormattedTextControl(_render)
    display_window = Window(content=display_control, always_hide_cursor=True)

    # Hidden input that captures text editing (height=0, invisible cursor)
    input_window = Window(
        content=BufferControl(buffer=extra_buffer),
        height=0,
        always_hide_cursor=True,
    )

    # ---------- key bindings ----------
    kb = KeyBindings()

    @kb.add("up")
    def _up(event):
        selected[0] = (selected[0] - 1) % len(options)
        # Skip disabled "previous step" option
        if selected[0] == 2 and not has_previous:
            selected[0] = (selected[0] - 1) % len(options)

    @kb.add("down")
    def _down(event):
        selected[0] = (selected[0] + 1) % len(options)
        # Skip disabled "previous step" option
        if selected[0] == 2 and not has_previous:
            selected[0] = (selected[0] + 1) % len(options)

    @kb.add("enter")
    def _enter(event):
        val, _, editable = options[selected[0]]
        extra = extra_buffer.text if editable else None
        result[0] = (val, extra)
        cursor_on[0] = False
        confirmed[0] = True  # collapse to selected option only
        event.app.exit()

    @kb.add("c-c")
    def _ctrl_c(event):
        result[0] = ("exit", None)
        event.app.exit()

    # ---------- layout ----------
    root = HSplit([display_window, input_window])
    layout = Layout(root)

    app = Application(
        layout=layout,
        key_bindings=kb,
        style=style,
        full_screen=False,
    )

    # Always focus the hidden input so typing works immediately
    layout.focus(input_window)

    # ---------- timeout ----------
    timer = threading.Timer(timeout_s, lambda: _timeout(app, result))
    timer.daemon = True
    timer.start()

    try:
        app.run()
    finally:
        timer.cancel()

    if result[0] is None:
        UI.dim("Timeout reached. Defaulting to: Continue")
        return "continue", None

    action, extra = result[0]
    if extra:
        UI.dim(f"  Continuing with extra prompt: {extra}")
    return action, extra


def _timeout(app: Application, result: list) -> None:
    """Called by the timer thread when the auto-continue timeout fires."""
    if result[0] is None:
        result[0] = ("continue", None)
        try:
            app.exit()
        except Exception:
            pass


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


def ask_reuse_generated_component(component_name: str, path: str) -> bool:
    """Ask whether a validated generated component should be reused."""
    console.print()
    console.print(
        f"[bold blue]Found validated generated component: {component_name}[/bold blue]"
    )
    console.print(f"[dim]{path}[/dim]")

    choice = questionary.select(
        "Would you like to reuse this component?",
        choices=[
            "Reuse existing component",
            "Regenerate component",
        ],
        style=QUESTIONARY_BASE_STYLE,
    ).ask()

    return choice == "Reuse existing component"


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


def ask_reuse_diagnosis(protocol_name: str) -> bool:
    """Ask whether to reuse an existing DataModel diagnosis report."""
    console.print()
    console.print(
        f"[bold blue]Found existing DataModel diagnosis for: {protocol_name}[/bold blue]"
    )
    choice = questionary.select(
        "Would you like to reuse it and skip diagnosis?",
        choices=[
            "Reuse existing diagnosis (skip diagnosis)",
            "Run diagnosis again",
        ],
        style=QUESTIONARY_BASE_STYLE,
    ).ask()
    return choice == "Reuse existing diagnosis (skip diagnosis)"


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


def ask_select_types(packet_types: list[str], protocol: str) -> list[str]:
    """Ask user to select packet types for mutator generation.

    All types are pre-selected by default. Returns the selected types.
    """
    console.print()
    console.print(
        f"[bold blue]Select packet types to generate mutators for protocol: {protocol}[/bold blue]"
    )

    choices = [
        questionary.Choice(title=t, value=t, checked=True) for t in packet_types
    ]
    selected = questionary.checkbox(
        "Packet types:",
        choices=choices,
        style=QUESTIONARY_BASE_STYLE,
    ).ask()

    if selected is None:
        UI.dim("Selection cancelled. Using all types.")
        return list(packet_types)
    return selected


def ask_generate_custom_data_elements(protocol: str, element_names: list[str]) -> bool:
    """Require approval before implementing protocol-specific DSL scalar types."""
    console.print()
    console.print(
        f"[bold yellow]The {protocol} schema requires these custom DSL scalar "
        f"types: {', '.join(element_names)}[/bold yellow]"
    )
    choice = questionary.select(
        "Generate and compile runtime implementations for DSL ExtendedType declarations?",
        choices=[
            "Generate custom elements",
            "Stop without generating",
        ],
        style=QUESTIONARY_BASE_STYLE,
    ).ask()
    return choice == "Generate custom elements"


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
