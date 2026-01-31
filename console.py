from rich.console import Console

console = Console()

import os
if os.path.exists("tool_usage.log"):
    os.remove("tool_usage.log")
file_logger = Console(file=open("tool_usage.log", "a"))
