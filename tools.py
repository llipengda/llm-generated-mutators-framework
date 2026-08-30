import os
import json
import re
import shutil
import subprocess
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from langchain_core.tools import BaseTool, tool
from langchain_core.retrievers import BaseRetriever

from log import console, file_logger


_PROJECT_ROOT = Path(__file__).resolve().parent


def _resolve_tool_path(
    filepath: str,
    *,
    allowed_roots: tuple[Path, ...] = (),
    allowed_files: tuple[Path, ...] = (),
    operation: str,
) -> Path:
    """Resolve filepath and enforce a symlink-safe allowlist."""
    candidate = Path(filepath)
    if not candidate.is_absolute():
        candidate = _PROJECT_ROOT / candidate
    resolved = candidate.resolve()

    # Exact-file entries must themselves be regular paths, not symlinks that
    # redirect an allowlisted filename to an arbitrary target.
    if any(resolved == allowed.absolute() for allowed in allowed_files):
        return resolved
    for root in allowed_roots:
        try:
            resolved.relative_to(root.resolve())
            return resolved
        except ValueError:
            continue

    allowed = [str(path.resolve()) for path in (*allowed_roots, *allowed_files)]
    raise PermissionError(
        f"{operation} access denied for {filepath!r}; allowed paths: "
        + ", ".join(allowed)
    )


def _peach_output_dir(protocol: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", protocol):
        raise ValueError(f"Unsafe protocol name: {protocol!r}")
    return _PROJECT_ROOT / "llm" / "peach" / protocol.lower()


_family_validation_lock = threading.Lock()
_family_validation_runs: dict[str, dict[str, object]] = {}


_VALIDATION_SUMMARY_RE = re.compile(
    r"^\[(PASS|FAIL)\]\s+\d+/\d+\s+tests passed\.?\s*$"
)


def _datamodel_validation_passed(returncode: int, stdout: str) -> tuple[bool, str]:
    """Read the validator summary from stdout, ignoring Docker stderr warnings."""
    summaries = [
        (match.group(1), line.strip())
        for line in stdout.splitlines()
        if (match := _VALIDATION_SUMMARY_RE.match(line.strip()))
    ]
    if not summaries:
        return False, "validator produced no [PASS]/[FAIL] summary"
    marker, summary = summaries[-1]
    return returncode == 0 and marker == "PASS", summary


def reset_family_validation_session(fragment_path: str) -> None:
    """Reset the hard validation budget before starting one family agent."""
    with _family_validation_lock:
        _family_validation_runs[str(Path(fragment_path).resolve())] = {
            "validations": 0,
            "status": "NOT_RUN",
        }


def get_family_validation_result(fragment_path: str) -> dict[str, object]:
    with _family_validation_lock:
        return dict(
            _family_validation_runs.get(
                str(Path(fragment_path).resolve()),
                {"validations": 0, "status": "NOT_RUN"},
            )
        )


@tool("Save_And_Verify_Code")
def save_and_verify_code(filename: str, complete_c_code: str) -> str:
    """Save COMPLETE C code to filename and check syntax with GCC."""
    console.log(f"[dim]Tool: Saving to {filename}...[/dim]")
    file_logger.log(
f"""
TOOL CALL: save_and_verify_code
    filename: {filename}
    c_code: {complete_c_code[:10]}...
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

        file_logger.log(
f"""
TOOL RESPONSE:
    {response}
""")
        return response

    except Exception as e:
        return f"ERROR: Failed to write or check file. {str(e)}"


def _scoped_read_file(
    filepath: str,
    *,
    line_count: int = -1,
    start_line: int = 1,
    allowed_roots: tuple[Path, ...],
    allowed_files: tuple[Path, ...],
) -> str:
    """Read a file after enforcing the supplied allowlist."""
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
        safe_path = _resolve_tool_path(
            filepath,
            allowed_roots=allowed_roots,
            allowed_files=allowed_files,
            operation="Read",
        )
        if safe_path.is_dir():
            files = os.listdir(safe_path)
            response = f"Directory listing for {filepath}:\n" + "\n".join(files)
            file_logger.log(
f"""
TOOL RESPONSE:
{response}
""")
            return response
        with safe_path.open("r", encoding="utf-8") as f:
            if line_count == -1:
                return f.read()

            lines = f.readlines()
            selected_lines = lines[start_line - 1 : start_line - 1 + line_count]
            return "".join(selected_lines)
    except Exception as e:
        return f"ERROR: Could not read file {filepath}. {str(e)}"


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
        if os.path.isdir(filepath):
            files = os.listdir(filepath)
            response = f"Directory listing for {filepath}:\n" + "\n".join(files)
            file_logger.log(f"\nTOOL RESPONSE:\n{response}\n")
            return response
        with open(filepath, "r", encoding="utf-8") as source:
            if line_count == -1:
                return source.read()
            lines = source.readlines()
            return "".join(lines[start_line - 1 : start_line - 1 + line_count])
    except Exception as error:
        return f"ERROR: Could not read file {filepath}. {error}"


def _read_file_with_line_numbers(
    filepath: str,
    *,
    line_count: int = -1,
    start_line: int = 1,
    allowed_roots: tuple[Path, ...],
    allowed_files: tuple[Path, ...],
) -> str:
    """Read an allowlisted text file with stable 1-based line numbers."""
    console.log(
        f"[dim]Tool: Reading numbered file {filepath} "
        f"(lines {start_line}+)[/dim]"
    )
    try:
        safe_path = _resolve_tool_path(
            filepath,
            allowed_roots=allowed_roots,
            allowed_files=allowed_files,
            operation="Read",
        )
        with safe_path.open("r", encoding="utf-8") as source:
            lines = source.readlines()
        first = max(1, start_line)
        selected = lines[first - 1 :] if line_count == -1 else lines[
            first - 1 : first - 1 + max(0, line_count)
        ]
        return "".join(
            f"{line_number:6d}: {line}"
            for line_number, line in enumerate(selected, start=first)
        )
    except Exception as error:
        return f"ERROR: Could not read numbered file {filepath}. {error}"


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

        file_logger.log(
f"""
TOOL RESPONSE:
    {response}
""")
        return response

    except Exception as e:
        return f"ERROR: Could not append to file {filepath}. {str(e)}"
    
def _write_file(filepath: str, content: str, *, output_root: Path) -> str:
    """Write content to a file inside output_root."""
    console.log(f"[dim]Tool: Writing to file {filepath}...[/dim]")
    file_logger.log(
f"""
TOOL CALL: write_file
    filepath: {filepath}
    content: {content[:10]}...
"""
    )
    try:
        safe_path = _resolve_tool_path(
            filepath, allowed_roots=(output_root,), operation="Write"
        )
        safe_path.parent.mkdir(parents=True, exist_ok=True)

        with safe_path.open("w", encoding="utf-8") as f:
            f.write(content)

        response = f"SUCCESS: Content written to {filepath}."
        file_logger.log(
f"""
TOOL RESPONSE:
    {response}
""")
        return response

    except Exception as e:
        return f"ERROR: Could not write to file {filepath}. {str(e)}"


@tool("Validate_Peach_XML")
def validate_peach_xml(xml_path: str) -> str:
    """Validate a generated Peach XML document against peach/peach.xsd."""
    document = Path(xml_path).resolve()
    schema = Path(__file__).resolve().parent / "peach" / "peach.xsd"
    console.log(f"[cyan]Peach XSD validator:[/cyan] validating {document}")
    file_logger.log(
        f"\nTOOL CALL: validate_peach_xml\n"
        f"    xml_path: {document}\n"
        f"    xsd_path: {schema}\n"
    )
    if not document.is_file():
        response = f"FAIL: XML file does not exist: {document}"
    elif not schema.is_file():
        response = f"ERROR: Peach XSD file does not exist: {schema}"
    elif shutil.which("xmllint") is None:
        response = (
            "ERROR: xmllint is required for Peach XSD validation but was not found "
            "on PATH. Install libxml2/xmllint before generating a DataModel."
        )
    else:
        custom_names: set[str] = set()
        response = ""
        for parent in (document.parent, *document.parents):
            manifest = parent / "DataElements" / "manifest.json"
            if not manifest.is_file():
                continue
            try:
                entries = json.loads(manifest.read_text(encoding="utf-8"))
                custom_names = {
                    item["element_name"]
                    for item in entries
                    if isinstance(item, dict)
                    and isinstance(item.get("element_name"), str)
                }
            except (OSError, json.JSONDecodeError):
                custom_names = set()
            break
        try:
            parsed_root = ET.parse(document).getroot()
            used_custom = {
                node.tag.rsplit("}", 1)[-1]
                for node in parsed_root.iter()
                if node.tag.rsplit("}", 1)[-1] in custom_names
            }
        except ET.ParseError as error:
            response = f"FAIL: XML is not well formed: {error}"
            used_custom = set()
        if response.startswith("FAIL:"):
            pass
        elif used_custom:
            response = (
                "PASS: XML is well formed. Static XSD validation is deferred for "
                "generated plugin element(s): " + ", ".join(sorted(used_custom)) +
                ". Runtime DataModel validation must load the custom plugin DLL."
            )
        else:
            result = subprocess.run(
                ["xmllint", "--noout", "--schema", str(schema), str(document)],
                capture_output=True,
                text=True,
            )
            diagnostics = (result.stdout + result.stderr).strip()
            if result.returncode == 0:
                response = f"PASS: {document} conforms to {schema}."
            else:
                response = (
                    f"FAIL: {document} does not conform to {schema}.\n"
                    f"{diagnostics[-8000:]}"
                )
    console.log(
        f"[{'green' if response.startswith('PASS:') else 'yellow'}]"
        f"Peach XSD validator: {response.splitlines()[0]}[/]"
    )
    file_logger.log(f"\nTOOL RESPONSE:\n{response}\n")
    return response


@tool("Inspect_Seed_Directory")
def inspect_seed_directory(directory: str) -> str:
    """Return a binary-safe JSON inventory with hex bytes for protocol seed files."""
    root = os.path.abspath(directory)
    if not os.path.isdir(root):
        return json.dumps({"error": f"Seed directory does not exist: {directory}"})

    max_files = 256
    max_bytes_per_file = 65536
    max_total_bytes = 1024 * 1024
    used = 0
    seeds = []
    paths = []
    for current_root, _, filenames in os.walk(root):
        for filename in filenames:
            paths.append(os.path.join(current_root, filename))
    for path in sorted(paths)[:max_files]:
        size = os.path.getsize(path)
        allowance = min(max_bytes_per_file, max(0, max_total_bytes - used))
        with open(path, "rb") as seed_file:
            data = seed_file.read(allowance)
        used += len(data)
        seeds.append(
            {
                "file": os.path.relpath(path, root),
                "size": size,
                "hex": data.hex(),
                "truncated": len(data) < size,
            }
        )
    return json.dumps(
        {
            "directory": root,
            "seeds": seeds,
            "truncated_file_list": len(paths) > max_files,
        },
        ensure_ascii=False,
    )


@tool("Validate_DataModel_Family")
def validate_datamodel_family(
    protocol: str,
    group_id: str,
    seed_files: list[str],
    fragment_dir: str,
    output_dir: str,
) -> str:
    """Assemble and validate one family against classified single-packet seeds.

    Waits for shared.xml before validation. The initial validation plus at most
    three post-repair validations are allowed for each family fragment.
    """
    from datamodel_split import assemble_datamodel, validate_manifest

    fragment_root = Path(fragment_dir).resolve()
    output_root = Path(output_dir).resolve()
    shared_path = fragment_root / "shared.xml"
    shared_ready_path = fragment_root / "shared.xml.ready"
    manifest_path = fragment_root / "schema_manifest.json"
    fragment_path = fragment_root / f"packet_{group_id}.xml"
    key = str(fragment_path)

    console.log(f"[cyan]Family validator:[/cyan] preparing {group_id}")
    deadline = time.monotonic() + 60
    next_log = time.monotonic() + 5
    if not shared_ready_path.is_file():
        console.log(
            f"[yellow]Family validator {group_id}:[/yellow] shared.xml is not "
            f"ready at {shared_path}; waiting up to 60 seconds"
        )
    while not shared_ready_path.is_file():
        if time.monotonic() >= deadline:
            response = (
                "WAITING: shared.xml is not ready after 60 seconds. "
                "Do not edit the family or count this as a repair; call this tool again."
            )
            with _family_validation_lock:
                _family_validation_runs.setdefault(key, {"validations": 0})[
                    "status"
                ] = "WAITING"
            console.log(f"[yellow]Family validator {group_id}:[/yellow] {response}")
            return response
        if time.monotonic() >= next_log:
            console.log(
                f"[dim]Family validator {group_id}: shared.xml still unavailable; waiting...[/dim]"
            )
            next_log = time.monotonic() + 5
        time.sleep(0.25)

    if not shared_path.is_file():
        with _family_validation_lock:
            _family_validation_runs.setdefault(key, {"validations": 0})[
                "status"
            ] = "BLOCKED_SHARED"
        return "BLOCKED_SHARED: shared generation completed without shared.xml."
    try:
        ET.parse(shared_path)
    except (OSError, ET.ParseError) as error:
        with _family_validation_lock:
            _family_validation_runs.setdefault(key, {"validations": 0})[
                "status"
            ] = "BLOCKED_SHARED"
        return f"BLOCKED_SHARED: shared.xml is not valid XML: {error}"

    try:
        raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        raw_packet_types = [
            str(packet)
            for group in raw_manifest.get("packet_groups", [])
            for packet in group.get("packet_types", [])
        ]
        manifest = validate_manifest(raw_manifest, protocol, raw_packet_types)
        group = next(
            item for item in manifest["packet_groups"] if item["id"] == group_id
        )
    except (OSError, ValueError, KeyError, StopIteration, json.JSONDecodeError) as error:
        with _family_validation_lock:
            _family_validation_runs.setdefault(key, {"validations": 0})[
                "status"
            ] = "CONTRACT_ERROR"
        return f"ERROR: Cannot load family contract: {error}"

    with _family_validation_lock:
        state = _family_validation_runs.setdefault(
            key, {"validations": 0, "status": "NOT_RUN"}
        )
        if state.get("status") == "PASS":
            return "PASS: this family already passed; no additional validation is needed."
        completed_validations = state.get("validations")
        if type(completed_validations) is not int:
            state["status"] = "CONTRACT_ERROR"
            return "ERROR: family validation state has an invalid validation count."
        validation_number = completed_validations + 1
        if validation_number > 4:
            state["status"] = "REPAIR_LIMIT_REACHED"
            return (
                "REPAIR_LIMIT_REACHED: three repair attempts have already been "
                "validated. Stop editing this fragment and defer to final validation."
            )
        state["validations"] = validation_number
        state["status"] = "RUNNING"

    family_dir = output_root / "datamodel_family_validation" / group_id
    family_datamodel = family_dir / "datamodel.xml"
    log_dir = family_dir / "logs"
    console.log(
        f"[cyan]Family validator {group_id}:[/cyan] validation "
        f"{validation_number}/4 with {len(seed_files)} seed(s)"
    )
    try:
        assemble_datamodel(
            protocol=protocol,
            packet_types=[str(item) for item in group["packet_types"]],
            shared_fragment=shared_path,
            packet_fragments=[fragment_path],
            output_path=family_datamodel,
            expected_shared_models=[
                str(model["name"]) for model in manifest["shared_models"]
            ],
        )
        with tempfile.TemporaryDirectory(prefix=f"{protocol}-{group_id}-") as temp:
            seed_dir = Path(temp) / "seeds"
            seed_dir.mkdir()
            for index, source_name in enumerate(seed_files):
                source = Path(source_name).resolve()
                if not source.is_file():
                    raise ValueError(f"seed does not exist: {source}")
                shutil.copy2(source, seed_dir / f"{index:04d}_{source.name}")
            command = [
                "./tests/datamodel/run_datamodel_test.sh",
                protocol,
                str(seed_dir),
                str(family_datamodel),
                f"{protocol}_packet_array",
                str(log_dir),
            ]
            result = subprocess.run(command, capture_output=True, text=True)
        output = (result.stdout + result.stderr).strip()
        passed, summary = _datamodel_validation_passed(
            result.returncode, result.stdout
        )
        status = "PASS" if passed else "FAIL"
    except (OSError, ValueError) as error:
        output = str(error)
        summary = "validation could not be executed"
        status = "FAIL"

    with _family_validation_lock:
        state = _family_validation_runs[key]
        state["status"] = status
        state["output"] = output
        state["log_dir"] = str(log_dir)
    repairs_remaining = max(0, 4 - validation_number)
    console.log(
        f"[{'green' if status == 'PASS' else 'yellow'}]Family validator "
        f"{group_id}: {status}; repair attempts remaining: {repairs_remaining}[/]"
    )
    return (
        f"{status}: validation {validation_number}/4; repair attempts remaining: "
        f"{repairs_remaining}; summary: {summary}; logs: {log_dir}\n{output[-6000:]}"
    )


def make_rfc_search(retriever: BaseRetriever):
    @tool("RFC_Search")
    def rfc_search(query: str) -> str:
        """Search RFC documents using RAG for protocol definitions, fields, and constraints."""
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


from dotnet_tools import search_class, build_dotnet_dll, validate_data


def get_tools(target: str, protocol: str) -> list[BaseTool]:
    """Build file tools with target- and protocol-specific path policies."""
    if target == "aflnet":
        return [save_and_verify_code, read_file, append_and_verify_code]
    if target != "peach":
        raise ValueError(f"Unknown target: {target!r}")

    output_root = _peach_output_dir(protocol)
    read_roots = (output_root,)
    read_files = (
        _PROJECT_ROOT / "peach" / "README.md",
        _PROJECT_ROOT / "peach" / "peach.txt",
        _PROJECT_ROOT / "examples" / "peach_datamodel_example.xml",
        _PROJECT_ROOT / "examples" / "ExampleEscapedUInt.cs",
        _PROJECT_ROOT / "tests" / "peach_fixer" / "example.cs",
    )

    @tool("Read_File")
    def scoped_read_file(
        filepath: str, *, line_count: int = -1, start_line: int = 1
    ) -> str:
        """Read a file from the paths allowed for the current pipeline."""
        return _scoped_read_file(
            filepath,
            line_count=line_count,
            start_line=start_line,
            allowed_roots=read_roots,
            allowed_files=read_files,
        )

    @tool("Read_File_With_Line_Numbers")
    def read_file_with_line_numbers(
        filepath: str, *, line_count: int = -1, start_line: int = 1
    ) -> str:
        """Read an allowed file with stable 1-based source line numbers."""
        return _read_file_with_line_numbers(
            filepath,
            line_count=line_count,
            start_line=start_line,
            allowed_roots=read_roots,
            allowed_files=read_files,
        )

    @tool("Write_File")
    def write_file(filepath: str, content: str) -> str:
        """Write a file in the current Peach protocol output directory."""
        return _write_file(filepath, content, output_root=output_root)

    return [
        scoped_read_file,
        read_file_with_line_numbers,
        write_file,
        validate_peach_xml,
        inspect_seed_directory,
        validate_datamodel_family,
        search_class,
        build_dotnet_dll,
        # validate_data
    ]
