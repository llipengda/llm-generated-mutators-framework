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

from core.tool_result import (
    DirectoryListData,
    DslModuleValidationData,
    DslValidationData,
    FileReadData,
    FileSearchData,
    FileWriteData,
    PatchData,
    ReadData,
    RfcSearchData,
    SearchContextLine,
    SearchMatch,
    ToolFailure,
    ToolResult,
    XmlValidationData,
    tool_error,
    tool_success,
)


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_MAX_SEARCH_FILE_BYTES = 1_000_000
_MAX_SEARCH_FILES = 2_000
def _filesystem_error(
    operation: str,
    filepath: str,
    error: Exception,
) -> ToolFailure:
    if isinstance(error, PermissionError):
        code = "permission_denied"
    elif isinstance(error, FileNotFoundError):
        code = "not_found"
    elif isinstance(error, UnicodeError):
        code = "not_text"
    else:
        code = "filesystem_error"
    return tool_error(
        code,
        f"Could not {operation} {filepath}: {error}",
    )


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


def _scoped_read_file(
    filepath: str,
    *,
    line_count: int = -1,
    start_line: int = 1,
    allowed_roots: tuple[Path, ...],
    allowed_files: tuple[Path, ...],
) -> ToolResult[ReadData]:
    """Read an allowlisted text file with stable 1-based line numbers."""
    try:
        safe_path = _resolve_tool_path(
            filepath,
            allowed_roots=allowed_roots,
            allowed_files=allowed_files,
            operation="Read",
        )
        if safe_path.is_dir():
            files = sorted(os.listdir(safe_path))
            content = "".join(
                f"{line_number:6d}: {name}\n"
                for line_number, name in enumerate(files, start=1)
            )
            response = tool_success(
                "directory_listed",
                f"Listed {len(files)} entries in {filepath}.",
                DirectoryListData(
                    kind="directory",
                    path=str(safe_path),
                    entry_count=len(files),
                    content=content,
                ),
            )
            return response
        with safe_path.open("r", encoding="utf-8") as source:
            lines = source.readlines()
        first = max(1, start_line)
        selected = lines[first - 1 :] if line_count == -1 else lines[
            first - 1 : first - 1 + max(0, line_count)
        ]
        content = "".join(
            f"{line_number:6d}: {line}"
            for line_number, line in enumerate(selected, start=first)
        )
        response = tool_success(
            "file_read",
            f"Read {len(selected)} line(s) from {filepath}.",
            FileReadData(
                kind="file",
                path=str(safe_path),
                start_line=first,
                end_line=first + len(selected) - 1 if selected else None,
                total_lines=len(lines),
                content=content,
            ),
        )
        return response
    except Exception as error:
        return _filesystem_error("read", filepath, error)


def _scoped_search_files(
    query: str,
    paths: list[str],
    *,
    regex: bool,
    case_sensitive: bool,
    context_lines: int,
    max_results: int,
    allowed_roots: tuple[Path, ...],
    allowed_files: tuple[Path, ...],
) -> ToolResult[FileSearchData]:
    """Search allowlisted text files and return bounded, line-numbered matches."""
    if not query:
        return tool_error("invalid_query", "query must not be empty.")
    if not paths:
        return tool_error("invalid_paths", "paths must contain at least one path.")
    if not 0 <= context_lines <= 5:
        return tool_error(
            "invalid_context_lines",
            "context_lines must be between 0 and 5.",
        )
    if not 1 <= max_results <= 200:
        return tool_error(
            "invalid_max_results",
            "max_results must be between 1 and 200.",
        )

    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        pattern = re.compile(query if regex else re.escape(query), flags)
    except re.error as error:
        return tool_error("invalid_regex", f"Invalid regular expression: {error}")

    candidates: dict[Path, None] = {}
    try:
        for requested_path in paths:
            safe_path = _resolve_tool_path(
                requested_path,
                allowed_roots=allowed_roots,
                allowed_files=allowed_files,
                operation="Search",
            )
            if safe_path.is_file():
                candidates[safe_path] = None
                continue
            if not safe_path.is_dir():
                return tool_error(
                    "not_found",
                    f"Search path does not exist: {requested_path}",
                )
            for candidate in sorted(safe_path.rglob("*")):
                if len(candidates) >= _MAX_SEARCH_FILES:
                    break
                if not candidate.is_file():
                    continue
                resolved_candidate = candidate.resolve()
                try:
                    resolved_candidate.relative_to(safe_path)
                except ValueError as error:
                    raise PermissionError(
                        f"Search path escapes allowed directory: {candidate}"
                    ) from error
                candidates[resolved_candidate] = None
    except Exception as error:
        return _filesystem_error("search", ", ".join(paths), error)

    matches: list[SearchMatch] = []
    searched_files = 0
    skipped_files = 0
    truncated = len(candidates) >= _MAX_SEARCH_FILES
    for candidate in candidates:
        try:
            if candidate.stat().st_size > _MAX_SEARCH_FILE_BYTES:
                skipped_files += 1
                continue
            lines = candidate.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            skipped_files += 1
            continue
        searched_files += 1
        for index, line in enumerate(lines):
            if pattern.search(line) is None:
                continue
            first = max(0, index - context_lines)
            last = min(len(lines), index + context_lines + 1)
            matches.append(
                SearchMatch(
                    path=str(candidate),
                    line=index + 1,
                    text=line,
                    context=[
                        SearchContextLine(
                            line=line_number + 1,
                            text=lines[line_number],
                        )
                        for line_number in range(first, last)
                    ],
                )
            )
            if len(matches) >= max_results:
                truncated = True
                break
        if len(matches) >= max_results:
            break

    response = tool_success(
        "search_complete",
        f"Found {len(matches)} matching line(s) in {searched_files} file(s).",
        FileSearchData(
            query=query,
            regex=regex,
            case_sensitive=case_sensitive,
            match_count=len(matches),
            searched_files=searched_files,
            skipped_files=skipped_files,
            truncated=truncated,
            matches=matches,
        ),
    )
    return response


def _write_file(
    filepath: str,
    content: str,
    *,
    allowed_roots: tuple[Path, ...],
    allowed_files: tuple[Path, ...],
) -> ToolResult[FileWriteData]:
    """Write content after enforcing the supplied allowlist."""
    try:
        safe_path = _resolve_tool_path(
            filepath,
            allowed_roots=allowed_roots,
            allowed_files=allowed_files,
            operation="Write",
        )
        safe_path.parent.mkdir(parents=True, exist_ok=True)

        with safe_path.open("w", encoding="utf-8") as f:
            f.write(content)

        response = tool_success(
            "file_written",
            f"Content written to {filepath}.",
            FileWriteData(
                path=str(safe_path),
                bytes_written=len(content.encode("utf-8")),
            ),
        )
        return response

    except Exception as error:
        return _filesystem_error("write", filepath, error)


def _apply_exact_patch(
    filepath: str,
    old_text: str,
    new_text: str,
    *,
    allowed_roots: tuple[Path, ...],
    allowed_files: tuple[Path, ...],
    expected_sha256: str = "",
) -> ToolResult[PatchData]:
    """Replace one exact text occurrence and atomically commit the result."""
    temporary_path: Path | None = None
    try:
        safe_path = _resolve_tool_path(
            filepath,
            allowed_roots=allowed_roots,
            allowed_files=allowed_files,
            operation="Patch",
        )
        if not safe_path.is_file():
            return tool_error(
                "not_file",
                f"Cannot patch missing or non-file path {filepath}.",
            )
        if not old_text:
            return tool_error("invalid_patch", "old_text must not be empty.")

        content = safe_path.read_text(encoding="utf-8")
        current_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if expected_sha256 and expected_sha256.lower() != current_sha256:
            return tool_error(
                "hash_mismatch",
                f"{filepath} changed since it was read: expected SHA-256 "
                f"{expected_sha256}, current SHA-256 {current_sha256}. Read it "
                "again before patching.",
            )

        occurrences = content.count(old_text)
        if occurrences == 0:
            return tool_error(
                "patch_context_not_found",
                f"Patch context was not found in {filepath}.",
            )
        if occurrences > 1:
            return tool_error(
                "ambiguous_patch_context",
                f"Patch context occurs {occurrences} times in {filepath}.",
            )

        patched = content.replace(old_text, new_text, 1)
        if patched == content:
            return tool_error(
                "no_change",
                f"Patch would not change {filepath}.",
            )

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
        return tool_success(
            "patch_applied",
            f"Patch applied to {filepath}.",
            PatchData(
                path=str(safe_path),
                previous_sha256=current_sha256,
                sha256=new_sha256,
            ),
        )
    except (OSError, UnicodeError, ValueError, PermissionError) as error:
        return _filesystem_error("patch", filepath, error)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


@tool("Validate_Peach_XML")
def validate_peach_xml(xml_path: str) -> ToolResult[XmlValidationData]:
    """Validate a generated Peach XML document against peach/peach.xsd."""
    document = Path(xml_path).resolve()
    schema = _PROJECT_ROOT / "peach" / "peach.xsd"
    if not document.is_file():
        response = tool_error(
            "not_found",
            f"XML file does not exist: {document}",
        )
    elif not schema.is_file():
        response = tool_error(
            "schema_not_found",
            f"Peach XSD file does not exist: {schema}",
        )
    elif shutil.which("xmllint") is None:
        response = tool_error(
            "dependency_missing",
            "xmllint is required for Peach XSD validation but was not found on "
            "PATH. Install libxml2/xmllint before generating a DataModel.",
        )
    else:
        custom_names: set[str] = set()
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
            response = tool_error(
                "xml_not_well_formed",
                f"XML is not well formed: {error}",
            )
            used_custom = set()
        else:
            response = None
        if response is None and used_custom:
            response = tool_success(
                "validation_deferred",
                "XML is well formed. Static XSD validation is deferred for "
                "generated plugin elements; runtime validation must load the plugin DLL.",
                XmlValidationData(
                    path=str(document),
                    schema=str(schema),
                    deferred=True,
                    custom_elements=sorted(used_custom),
                ),
            )
        elif response is None:
            result = subprocess.run(
                ["xmllint", "--noout", "--schema", str(schema), str(document)],
                capture_output=True,
                text=True,
            )
            diagnostics = (result.stdout + result.stderr).strip()
            if result.returncode == 0:
                response = tool_success(
                    "xml_valid",
                    f"{document} conforms to {schema}.",
                    XmlValidationData(
                        path=str(document),
                        schema=str(schema),
                        deferred=False,
                        custom_elements=[],
                    ),
                )
            else:
                response = tool_error(
                    "xml_schema_validation_failed",
                    f"{document} does not conform to {schema}.\n"
                    f"{diagnostics[-8000:]}",
                )
    return response


def make_rfc_search(retriever: BaseRetriever):
    @tool("RFC_Search")
    def rfc_search(query: str) -> ToolResult[RfcSearchData]:
        """Search RFC documents using RAG for protocol definitions, fields, and constraints."""
        if not retriever:
            return tool_error(
                "retriever_unavailable",
                "RFC document is not loaded.",
            )
        try:
            docs = retriever.invoke(query)
        except Exception as error:
            return tool_error(
                "retrieval_failed",
                f"RFC search failed: {error}",
            )
        content = "\n\n".join(d.page_content for d in docs)
        response = tool_success(
            "search_complete",
            f"Retrieved {len(docs)} RFC passage(s).",
            RfcSearchData(
                query=query,
                result_count=len(docs),
                content=content,
            ),
        )

        return response

    return rfc_search


from core.dotnet_tools import build_dotnet_dll, search_class


def get_tools(
    protocol: str,
    *,
    read_files: tuple[Path | str, ...] | None = None,
    write_files: tuple[Path | str, ...] | None = None,
    write_roots: tuple[Path | str, ...] | None = None,
) -> list[BaseTool]:
    """Build Peach tools with protocol-specific path policies."""
    output_root = _peach_output_dir(protocol)
    default_read_roots = (output_root,)
    default_read_files = (
        _PROJECT_ROOT / "docs" / "peach-dsl.md",
        _PROJECT_ROOT / "peach" / "README.md",
        _PROJECT_ROOT / "peach" / "peach.txt",
        _PROJECT_ROOT / "examples" / "ExampleEscapedUInt.cs",
        _PROJECT_ROOT / "tests" / "peach_fixer" / "example.cs",
    )
    if read_files is None:
        scoped_read_roots = default_read_roots
        scoped_read_files = default_read_files
    else:
        scoped_read_roots = ()
        scoped_read_files = tuple(Path(path).resolve() for path in read_files)
    if write_files is None and write_roots is None:
        scoped_write_roots = (output_root,)
        scoped_write_files: tuple[Path, ...] = ()
    else:
        scoped_write_roots = tuple(
            Path(path).resolve() for path in (write_roots or ())
        )
        scoped_write_files = tuple(
            Path(path).resolve() for path in (write_files or ())
        )
    dsl_root = output_root / "datamodel_dsl"
    derived_paths = {
        (output_root / "datamodel.xml").resolve(),
        # Legacy source maps are no longer generated. Keep the path blocked
        # so an agent cannot recreate this obsolete artifact.
        (output_root / "datamodel.map.json").resolve(),
        (output_root / "datamodel_error_report.txt").resolve(),
        (output_root / "datamodel_error_report.json").resolve(),
        (dsl_root / "root.py").resolve(),
    }

    def derived_artifact_error(
        destination: Path, filepath: str
    ) -> ToolFailure | None:
        if destination.suffix.lower() == ".xml" or destination in derived_paths or (
            destination.parent == dsl_root.resolve()
            and destination.name.startswith("_family_root_")
        ):
            return tool_error(
                "derived_artifact_read_only",
                f"{filepath} is a derived DataModel artifact. Edit shared_model.py "
                "or family_<id>.py and recompile instead.",
            )
        return None

    @tool("Read_File")
    def scoped_read_file(
        filepath: str, *, line_count: int = -1, start_line: int = 1
    ) -> ToolResult[ReadData]:
        """Read an allowed file with stable 1-based line numbers."""
        return _scoped_read_file(
            filepath,
            line_count=line_count,
            start_line=start_line,
            allowed_roots=scoped_read_roots,
            allowed_files=scoped_read_files,
        )

    @tool("Search_Files")
    def search_files(
        query: str,
        paths: list[str],
        regex: bool = False,
        case_sensitive: bool = False,
        context_lines: int = 0,
        max_results: int = 50,
    ) -> ToolResult[FileSearchData]:
        """Search allowed files and directories, returning line-numbered matches."""
        return _scoped_search_files(
            query,
            paths,
            regex=regex,
            case_sensitive=case_sensitive,
            context_lines=context_lines,
            max_results=max_results,
            allowed_roots=scoped_read_roots,
            allowed_files=scoped_read_files,
        )

    @tool("Write_File")
    def write_file(filepath: str, content: str) -> ToolResult[FileWriteData]:
        """Write a file to a path allowed for the current pipeline step."""
        try:
            destination = _resolve_tool_path(
                filepath,
                allowed_roots=scoped_write_roots,
                allowed_files=scoped_write_files,
                operation="Write",
            )
        except (OSError, ValueError, PermissionError) as error:
            return _filesystem_error("write", filepath, error)
        denial = derived_artifact_error(destination, filepath)
        if denial:
            return denial
        return _write_file(
            filepath,
            content,
            allowed_roots=scoped_write_roots,
            allowed_files=scoped_write_files,
        )

    @tool("Apply_Patch")
    def apply_patch(
        filepath: str,
        old_text: str,
        new_text: str,
        expected_sha256: str = "",
    ) -> ToolResult[PatchData]:
        """Replace exactly one old_text occurrence in an existing Peach output file.

        Copy old_text exactly from Read_File without the displayed line-number
        prefixes. Include enough surrounding context to make the match unique. If
        expected_sha256 is supplied, the patch is rejected when the file has changed
        since that hash was calculated.
        """
        try:
            destination = _resolve_tool_path(
                filepath,
                allowed_roots=scoped_write_roots,
                allowed_files=scoped_write_files,
                operation="Patch",
            )
        except (OSError, ValueError, PermissionError) as error:
            return _filesystem_error("patch", filepath, error)
        denial = derived_artifact_error(destination, filepath)
        if denial:
            return denial
        return _apply_exact_patch(
            filepath,
            old_text,
            new_text,
            allowed_roots=scoped_write_roots,
            allowed_files=scoped_write_files,
            expected_sha256=expected_sha256,
        )

    @tool("Validate_Peach_DSL_Module")
    def validate_peach_dsl_module(
        filepath: str,
    ) -> ToolResult[DslModuleValidationData]:
        """Type-check a Peach DSL module and evaluate every Schema it defines."""
        from peach_dsl.compiler import DSLValidationError, validate_dsl_module

        try:
            safe_path = _resolve_tool_path(
                filepath, allowed_roots=(output_root,), operation="Read"
            )
            if safe_path.suffix != ".py":
                return tool_error(
                    "invalid_dsl_module",
                    "Peach DSL modules must use the .py suffix.",
                )
            schemas = validate_dsl_module(safe_path)
            return tool_success(
                "dsl_module_valid",
                f"Validated DSL module {safe_path}.",
                DslModuleValidationData(
                    path=str(safe_path),
                    schema_count=len(schemas),
                    schemas=list(schemas),
                ),
            )
        except (DSLValidationError, OSError, ValueError) as error:
            return tool_error(
                "dsl_module_invalid",
                f"DSL module validation failed for {filepath}: {error}",
            )

    @tool("Validate_Peach_DSL")
    def validate_peach_dsl(entry_path: str) -> ToolResult[DslValidationData]:
        """Compile the complete generated DSL and validate its derived Pit XML."""
        from core.datamodel_dsl import compile_dsl_subprocess

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
                return tool_error(
                    "dsl_compile_failed",
                    "Peach DSL compilation failed for "
                    f"{entry}.\n{(result.stdout + result.stderr).strip()}",
                )
            xsd_result = validate_peach_xml.invoke({"xml_path": str(output)})
            if not xsd_result.get("ok", False):
                return tool_error(
                    "dsl_xml_invalid",
                    "Peach DSL compiled, but the generated XML failed validation: "
                    f"{xsd_result['message']}",
                )
            return tool_success(
                "dsl_valid",
                f"Peach DSL compiled and validated as {output}.",
                DslValidationData(
                    path=str(entry),
                    output=str(output),
                    validation=xsd_result,
                ),
            )
        except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as error:
            return tool_error(
                "dsl_validation_failed",
                f"Peach DSL validation failed for {entry_path}: {error}",
            )

    return [
        scoped_read_file,
        search_files,
        write_file,
        apply_patch,
        validate_peach_dsl_module,
        validate_peach_dsl,
        validate_peach_xml,
        search_class,
        build_dotnet_dll,
    ]
