import asyncio
import questionary

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
            "Retry previous step", disabled=None if has_previous else "No previous step"
        ),
        "Skip the step",
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
    if choice == "Retry previous step":
        return "retry_prev"
    if choice == "Skip the step":
        return "skip"
    return "exit"


def run_agent_step(*, agent_graph: CompiledStateGraph, prompt_text: str, config: RunnableConfig, step_title: str):
    """Run the agent with a loading spinner and formatted output."""

    UI.title(step_title)

    with UI.status(f"LLM is thinking & coding for {step_title}...", spinner="dots"):
        response = agent_graph.invoke(
            {"messages": [{"role": "user", "content": prompt_text}]},
            config=config,
        )
        final_response = response["messages"][-1].content

    UI.result_markdown(step_title, final_response)

    return response
    
