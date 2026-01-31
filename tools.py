import os
import subprocess

from langchain_core.tools import tool

from console import console, file_logger


@tool("Save_And_Verify_Code")
def save_and_verify_code(filename: str, complete_c_code: str) -> str:
    """Save COMPLETE C code to filename and check syntax with GCC."""
    console.log(f"[dim]Tool: Saving to {filename}...[/dim]")
    file_logger.log(
        f"""
                    TOOL CALL: save_and_verify_code
                        filename: {filename}
                        complete_c_code: {complete_c_code[:10]}...
                    """
    )

    os.makedirs(os.path.dirname(filename), exist_ok=True)

    try:
        with open(filename, "w", encoding="utf-8") as f:
            clean_code = complete_c_code.replace("```c", "").replace("```", "")
            f.write(clean_code)

        cmd = ["gcc", "-fsyntax-only", filename]
        result = subprocess.run(cmd, capture_output=True, text=True)

        console.log(f"[dim]Tool: Running GCC check on {filename}...[/dim]")

        if result.returncode == 0:
            response = f"SUCCESS: Code saved to {filename}. GCC syntax check passed."
        else:
            response = (
                f"WARNING: Saved to {filename}, but GCC found errors:\n{result.stderr}"
            )

        file_logger.log(f"""\
                        TOOL RESPONSE:
                        {response}
                        """)
        return response

    except Exception as e:
        return f"ERROR: Failed to write or check file. {str(e)}"


@tool("Read_File")
def read_file(filepath: str, *, line_count: int = -1, start_line: int = 1) -> str:
    """Read a file and return its content."""
    console.log(f"[dim]Tool: Reading file {filepath} (lines {start_line}+)[/dim]")

    file_logger.log(
        f"""
                    TOOL CALL: read_file
                        filepath: {filepath}
                        line_count: {line_count}
                        start_line: {start_line}
                    """
    )
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            if line_count == -1:
                return f.read()

            lines = f.readlines()
            selected_lines = lines[start_line - 1 : start_line - 1 + line_count]
            return "".join(selected_lines)
    except Exception as e:
        return f"ERROR: Could not read file {filepath}. {str(e)}"


@tool("Append_And_Verify_Code")
def append_and_verify_code(filepath: str, content_to_append: str) -> str:
    """Append content to a file and run a GCC syntax check."""
    console.log(f"[dim]Tool: Appending to {filepath}...[/dim]")
    file_logger.log(
        f"""
                    TOOL CALL: append_and_verify_code
                        filepath: {filepath}
                        content_to_append: {content_to_append[:10]}...
                    """
    )
    try:
        os.makedirs(os.path.dirname(filepath), exist_ok=True)

        with open(filepath, "a", encoding="utf-8") as f:
            f.write(content_to_append)

        cmd = ["gcc", "-fsyntax-only", filepath]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            response = f"SUCCESS: Appended to {filepath}. GCC syntax check passed."
        else:
            response = (
                f"WARNING: Appended to {filepath}, but GCC found errors:\n{result.stderr}"
            )

        file_logger.log(f"""\
                        TOOL RESPONSE:
                        {response}
                        """)
        return response

    except Exception as e:
        return f"ERROR: Could not append to file {filepath}. {str(e)}"


def make_rfc_search(retriever):
    @tool("RFC_Search")
    def rfc_search(query: str) -> str:
        """Search RFC documents for protocol definitions, fields, and constraints."""
        console.log(f"[dim]Tool: Searching RFC for '{query}'...[/dim]")
        file_logger.log(
            f"""
                    TOOL CALL: rfc_search
                        query: {query}
                    """
        )
        if not retriever:
            return "Error: RFC document not loaded."
        docs = retriever.invoke(query)
        response = "\n\n".join(d.page_content for d in docs)

        file_logger.log(
            f"""
                    TOOL RESPONSE:
                    {response}
                    """
        )
        return response

    return rfc_search
