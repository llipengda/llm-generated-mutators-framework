import os
import json
import hashlib
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from langchain_core.tools import BaseTool, tool
from langchain_core.retrievers import BaseRetriever

from log import file_logger


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


@tool("Save_And_Verify_Code")
def save_and_verify_code(filename: str, complete_c_code: str) -> str:
    """Save COMPLETE C code to filename and check syntax with GCC."""
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


def _apply_exact_patch(
    filepath: str,
    old_text: str,
    new_text: str,
    *,
    output_root: Path,
    expected_sha256: str = "",
) -> str:
    """Replace one exact text occurrence and atomically commit the result."""
    temporary_path: Path | None = None
    try:
        safe_path = _resolve_tool_path(
            filepath, allowed_roots=(output_root,), operation="Patch"
        )
        if not safe_path.is_file():
            return f"ERROR: Cannot patch missing or non-file path {filepath}."
        if not old_text:
            return "ERROR: old_text must not be empty."

        content = safe_path.read_text(encoding="utf-8")
        current_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if expected_sha256 and expected_sha256.lower() != current_sha256:
            return (
                f"CONFLICT: {filepath} changed since it was read. "
                f"Expected SHA-256 {expected_sha256}, current SHA-256 "
                f"{current_sha256}. Read the file again before patching."
            )

        occurrences = content.count(old_text)
        if occurrences == 0:
            return (
                f"ERROR: Patch context was not found in {filepath}. "
                "Read the current file and retry with exact old_text."
            )
        if occurrences > 1:
            return (
                f"ERROR: Patch context is ambiguous in {filepath}: old_text "
                f"occurs {occurrences} times. Include more surrounding context."
            )

        patched = content.replace(old_text, new_text, 1)
        if patched == content:
            return f"ERROR: Patch would not change {filepath}."

        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=safe_path.parent,
            prefix=f".{safe_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(patched)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)

        temporary_path.chmod(safe_path.stat().st_mode & 0o7777)
        os.replace(temporary_path, safe_path)
        temporary_path = None
        new_sha256 = hashlib.sha256(patched.encode("utf-8")).hexdigest()
        return (
            f"SUCCESS: Patch applied to {filepath}. "
            f"Previous SHA-256: {current_sha256}. New SHA-256: {new_sha256}."
        )
    except (OSError, UnicodeError, ValueError, PermissionError) as error:
        return f"ERROR: Could not patch file {filepath}. {error}"
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


@tool("Validate_Peach_XML")
def validate_peach_xml(xml_path: str) -> str:
    """Validate a generated Peach XML document against peach/peach.xsd."""
    document = Path(xml_path).resolve()
    schema = Path(__file__).resolve().parent / "peach" / "peach.xsd"
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
    file_logger.log(f"\nTOOL RESPONSE:\n{response}\n")
    return response


def make_rfc_search(retriever: BaseRetriever):
    @tool("RFC_Search")
    def rfc_search(query: str) -> str:
        """Search RFC documents using RAG for protocol definitions, fields, and constraints."""
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


def get_tools(
    target: str,
    protocol: str,
    *,
    read_files: tuple[Path | str, ...] | None = None,
) -> list[BaseTool]:
    """Build file tools with target- and protocol-specific path policies."""
    if target == "aflnet":
        return [save_and_verify_code, read_file, append_and_verify_code]
    if target != "peach":
        raise ValueError(f"Unknown target: {target!r}")

    output_root = _peach_output_dir(protocol)
    default_read_roots = (output_root,)
    default_read_files = (
        _PROJECT_ROOT / "docs" / "peach-dsl.md",
        _PROJECT_ROOT / "peach" / "README.md",
        _PROJECT_ROOT / "peach" / "peach.txt",
        _PROJECT_ROOT / "examples" / "peach_datamodel_example.xml",
        _PROJECT_ROOT / "examples" / "ExampleEscapedUInt.cs",
        _PROJECT_ROOT / "tests" / "peach_fixer" / "example.cs",
    )
    if read_files is None:
        scoped_read_roots = default_read_roots
        scoped_read_files = default_read_files
    else:
        scoped_read_roots = ()
        scoped_read_files = tuple(Path(path).resolve() for path in read_files)
    dsl_root = output_root / "datamodel_dsl"
    shared_dsl_path = dsl_root / "shared_model.py"
    shared_read_files = (
        _PROJECT_ROOT / "docs" / "peach-dsl.md",
        output_root / "data_type_analysis.json",
        dsl_root / "schema_manifest.json",
        shared_dsl_path,
    )
    derived_paths = {
        (output_root / "datamodel.xml").resolve(),
        # Legacy source maps are no longer generated. Keep the path blocked
        # so an agent cannot recreate this obsolete artifact.
        (output_root / "datamodel.map.json").resolve(),
        (output_root / "datamodel_error_report.txt").resolve(),
        (output_root / "datamodel_error_report.json").resolve(),
        (dsl_root / "root.py").resolve(),
    }

    def derived_artifact_error(destination: Path, filepath: str) -> str | None:
        if destination.suffix.lower() == ".xml" or destination in derived_paths or (
            destination.parent == dsl_root.resolve()
            and destination.name.startswith("_family_root_")
        ):
            return (
                f"ERROR: {filepath} is a derived DataModel artifact. "
                "Edit shared_model.py or family_<id>.py and recompile instead."
            )
        return None

    @tool("Read_File")
    def scoped_read_file(
        filepath: str, *, line_count: int = -1, start_line: int = 1
    ) -> str:
        """Read a file from the paths allowed for the current pipeline."""
        return _scoped_read_file(
            filepath,
            line_count=line_count,
            start_line=start_line,
            allowed_roots=scoped_read_roots,
            allowed_files=scoped_read_files,
        )

    @tool("Read_Shared_DSL_Context")
    def read_shared_dsl_context(
        filepath: str, *, line_count: int = -1, start_line: int = 1
    ) -> str:
        """Read only the inputs permitted while generating shared_model.py."""
        return _scoped_read_file(
            filepath,
            line_count=line_count,
            start_line=start_line,
            allowed_roots=(),
            allowed_files=shared_read_files,
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
            allowed_roots=scoped_read_roots,
            allowed_files=scoped_read_files,
        )

    @tool("Write_File")
    def write_file(filepath: str, content: str) -> str:
        """Write a file in the current Peach protocol output directory."""
        try:
            destination = _resolve_tool_path(
                filepath, allowed_roots=(output_root,), operation="Write"
            )
        except (OSError, ValueError, PermissionError) as error:
            return f"ERROR: {error}"
        denial = derived_artifact_error(destination, filepath)
        if denial:
            return denial
        return _write_file(filepath, content, output_root=output_root)

    @tool("Apply_Patch")
    def apply_patch(
        filepath: str,
        old_text: str,
        new_text: str,
        expected_sha256: str = "",
    ) -> str:
        """Replace exactly one old_text occurrence in an existing Peach output file.

        Copy old_text exactly from Read_File or Read_File_With_Line_Numbers (without
        the displayed line-number prefixes). Include enough surrounding context to
        make the match unique. If expected_sha256 is supplied, the patch is rejected
        when the file has changed since that hash was calculated.
        """
        try:
            destination = _resolve_tool_path(
                filepath, allowed_roots=(output_root,), operation="Patch"
            )
        except (OSError, ValueError, PermissionError) as error:
            return f"ERROR: {error}"
        denial = derived_artifact_error(destination, filepath)
        if denial:
            return denial
        return _apply_exact_patch(
            filepath,
            old_text,
            new_text,
            output_root=output_root,
            expected_sha256=expected_sha256,
        )

    @tool("Write_Shared_DSL")
    def write_shared_dsl(filepath: str, content: str) -> str:
        """Write only the shared Peach DSL module for the current protocol."""
        try:
            destination = _resolve_tool_path(
                filepath,
                allowed_files=(shared_dsl_path,),
                operation="Write",
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content, encoding="utf-8")
            return f"SUCCESS: Content written to {destination}."
        except (OSError, ValueError, PermissionError) as error:
            return f"ERROR: {error}"

    @tool("Validate_Peach_DSL_Module")
    def validate_peach_dsl_module(filepath: str) -> str:
        """Type-check a Peach DSL module and evaluate every Schema it defines."""
        from peach_dsl.compiler import DSLValidationError, validate_dsl_module

        try:
            safe_path = _resolve_tool_path(
                filepath, allowed_roots=(output_root,), operation="Read"
            )
            if safe_path.suffix != ".py":
                return "FAIL: Peach DSL modules must use the .py suffix."
            schemas = validate_dsl_module(safe_path)
            return (
                f"PASS: validated DSL module {safe_path}; "
                f"evaluated {len(schemas)} Schema(s)."
            )
        except (DSLValidationError, OSError, ValueError) as error:
            return f"FAIL: {error}"

    @tool("Validate_Peach_DSL")
    def validate_peach_dsl(entry_path: str) -> str:
        """Compile the complete generated DSL and validate its derived Pit XML."""
        from datamodel_dsl import compile_dsl_subprocess

        try:
            entry = _resolve_tool_path(
                entry_path, allowed_roots=(output_root,), operation="Read"
            )
            output = output_root / "datamodel.xml"
            result = compile_dsl_subprocess(
                entry,
                output,
            )
            if result.returncode != 0:
                return "FAIL: " + (result.stdout + result.stderr).strip()
            xsd_result = validate_peach_xml.invoke({"xml_path": str(output)})
            return f"PASS: DSL compiled to {output}.\n{xsd_result}" if str(xsd_result).startswith("PASS:") else str(xsd_result)
        except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as error:
            return f"FAIL: {error}"

    return [
        scoped_read_file,
        read_shared_dsl_context,
        read_file_with_line_numbers,
        write_file,
        apply_patch,
        write_shared_dsl,
        validate_peach_dsl_module,
        validate_peach_dsl,
        validate_peach_xml,
        search_class,
        build_dotnet_dll,
        # validate_data
    ]
