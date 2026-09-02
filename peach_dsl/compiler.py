"""Validate generated DSL modules and compile them to a Peach Pit XML file."""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import traceback
from types import ModuleType
import xml.etree.ElementTree as ET

from .sdk import Schema, evaluate_schema, to_peach_data_model


PEACH_NAMESPACE = "http://peachfuzzer.com/2012/Peach"
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class DSLValidationError(ValueError):
    """Pyright or runtime DSL validation failed."""


def _validate_language_rules(path: Path, tree: ast.Module) -> None:
    """Reject DSL constructs that Python's subtype rules cannot express."""

    for node in ast.walk(tree):
        if not isinstance(node, ast.Subscript):
            continue
        target = node.value
        is_extended_type = (
            isinstance(target, ast.Name) and target.id == "ExtendedType"
        ) or (isinstance(target, ast.Attribute) and target.attr == "ExtendedType")
        if (
            is_extended_type
            and isinstance(node.slice, ast.Name)
            and node.slice.id == "bool"
        ):
            raise DSLValidationError(
                f"{path}:{node.lineno}:{node.col_offset + 1}: "
                "ExtendedType[bool] is forbidden; use a numeric value type "
                "and 0/1 values"
            )


def _format_compile_error(error: BaseException, entry: Path) -> str:
    """Format runtime failures with the generated DSL source traceback."""

    frames = traceback.extract_tb(error.__traceback__)
    dsl_directory = entry.resolve().parent
    dsl_frames: list[traceback.FrameSummary] = []
    for frame in frames:
        try:
            Path(frame.filename).resolve().relative_to(dsl_directory)
        except (OSError, ValueError):
            continue
        dsl_frames.append(frame)

    if not dsl_frames:
        return str(error)

    lines = [f"{type(error).__name__}: {error}", "DSL source traceback:"]
    for index, frame in enumerate(dsl_frames):
        label = "root cause" if index == len(dsl_frames) - 1 else "via"
        lines.append(f"  {label}: {Path(frame.filename).resolve()}:{frame.lineno}")
        if frame.line:
            lines.append(f"    {frame.line.strip()}")
    return "\n".join(lines)


def _run_pyright(path: Path) -> None:
    """Run strict Pyright checking against one generated DSL entry module."""

    path = path.resolve()
    config = {
        "extraPaths": [str(PROJECT_ROOT), str(path.parent)],
        "typeCheckingMode": "strict",
        "pythonVersion": f"{sys.version_info.major}.{sys.version_info.minor}",
    }
    with tempfile.TemporaryDirectory(prefix="peach-dsl-pyright-") as directory:
        config_path = Path(directory) / "pyrightconfig.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pyright",
                    "--project",
                    str(config_path),
                    "--outputjson",
                    str(path),
                ],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired as error:
            raise DSLValidationError(f"Pyright timed out while checking {path}") from error

    if result.returncode == 0:
        return
    try:
        payload = json.loads(result.stdout)
        diagnostics = payload.get("generalDiagnostics", [])
        messages = []
        for diagnostic in diagnostics:
            location = diagnostic.get("range", {}).get("start", {})
            line = int(location.get("line", 0)) + 1
            column = int(location.get("character", 0)) + 1
            rule = diagnostic.get("rule")
            suffix = f" ({rule})" if rule else ""
            messages.append(
                f"{diagnostic.get('file', path)}:{line}:{column}: "
                f"{diagnostic.get('message', 'Pyright error')}{suffix}"
            )
        detail = "\n".join(messages)
    except (json.JSONDecodeError, TypeError, ValueError):
        detail = (result.stdout + result.stderr).strip()
    if not detail and "No module named pyright" in result.stderr:
        detail = "Pyright is not installed; run `pip install -r requirements.txt`"
    raise DSLValidationError(detail or f"Pyright failed while checking {path}")


def validate_dsl_source(path: Path) -> ast.Module:
    """Type-check one DSL module with Pyright and return its parsed source tree."""

    path = path.resolve()
    _run_pyright(path)
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (OSError, SyntaxError, UnicodeError) as error:
        raise DSLValidationError(f"cannot parse {path}: {error}") from error
    _validate_language_rules(path, tree)
    return tree


def _module_schema_classes(module: ModuleType) -> tuple[type[Schema], ...]:
    """Return every Schema class defined by a loaded DSL module.

    Imported schemas are validated by their own module. Nested schema classes
    remain part of the module and are included even when they are not reachable
    from a top-level schema's evaluated field tree.
    """

    schemas: list[type[Schema]] = []
    seen: set[type[Schema]] = set()

    def collect(value: object) -> None:
        if (
            not isinstance(value, type)
            or not issubclass(value, Schema)
            or value is Schema
            or value.__module__ != module.__name__
            or value in seen
        ):
            return
        seen.add(value)
        schemas.append(value)
        for nested in vars(value).values():
            collect(nested)

    for value in vars(module).values():
        collect(value)
    return tuple(schemas)


def validate_dsl_module(path: Path) -> tuple[str, ...]:
    """Type-check, load, and evaluate every Schema defined in one DSL module."""

    path = path.resolve()
    validate_dsl_source(path)
    try:
        module = _load_entry(path)
    except Exception as error:
        raise DSLValidationError(
            f"failed to load {path}: {_format_compile_error(error, path)}"
        ) from error

    schema_names: list[str] = []
    failures: list[str] = []
    for schema in _module_schema_classes(module):
        schema_name = schema.__qualname__
        schema_names.append(schema_name)
        try:
            evaluate_schema(schema)
        except Exception as error:
            failures.append(
                f"{path}: Schema {schema_name} failed to evaluate: "
                f"{_format_compile_error(error, path)}"
            )
    if failures:
        raise DSLValidationError("\n".join(failures))
    return tuple(schema_names)


def validate_dsl_dependencies(entry: Path) -> dict[Path, ast.Module]:
    """Type-check an entry and collect its imported sibling syntax trees."""

    entry = entry.resolve()
    _run_pyright(entry)
    pending = [entry]
    trees: dict[Path, ast.Module] = {}
    while pending:
        path = pending.pop().resolve()
        if path in trees:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, UnicodeError) as error:
            raise DSLValidationError(f"cannot parse {path}: {error}") from error
        _validate_language_rules(path, tree)
        trees[path] = tree
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.level != 0:
                continue
            module = node.module or ""
            sibling = path.parent / f"{module}.py"
            if module != "peach_dsl" and sibling.is_file():
                pending.append(sibling)
    return trees


def _load_entry(entry: Path) -> ModuleType:
    # TODO(sandbox): Run DSL import and compilation in a hardened Docker
    # container. It is intentionally local for now; Pyright is not a sandbox.
    module_name = f"peach_generated_{os.getpid()}_{entry.stat().st_mtime_ns}"
    spec = importlib.util.spec_from_file_location(module_name, entry)
    if spec is None or spec.loader is None:
        raise DSLValidationError(f"cannot load DSL entry module {entry}")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(entry.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


def load_dsl_root(entry: Path) -> type[Schema]:
    """Validate an entry module and return its exported root Schema class."""

    entry = entry.resolve()
    validate_dsl_dependencies(entry)
    module = _load_entry(entry)
    root_schema = getattr(module, "ROOT", None)
    if not isinstance(root_schema, type) or not issubclass(root_schema, Schema):
        raise DSLValidationError(f"{entry} must export ROOT as a Schema class")
    return root_schema


def _remove_compiler_only_union_alternative(xml_text: str) -> str:
    """Remove the synthetic duplicate used to satisfy the public Union rule."""

    ET.register_namespace("", PEACH_NAMESPACE)
    ET.register_namespace("xsi", "http://www.w3.org/2001/XMLSchema-instance")
    root = ET.fromstring(xml_text)
    for parent in root.iter():
        if (
            parent.tag.rsplit("}", 1)[-1] != "Choice"
            or parent.get("name") != "packet_union"
        ):
            continue
        for child in list(parent):
            if child.get("name") == "compiler_single_packet_duplicate":
                parent.remove(child)
    ET.indent(root, space="  ")
    return ET.tostring(
        root,
        encoding="utf-8",
        xml_declaration=True,
        short_empty_elements=True,
    ).decode("utf-8")


def compile_entry(
    entry: Path,
    output: Path,
) -> Path:
    """Compile one validated DSL entry module and atomically publish its XML."""

    entry = entry.resolve()
    if not entry.is_file():
        raise DSLValidationError(f"DSL entry does not exist: {entry}")
    validate_dsl_dependencies(entry)
    module = _load_entry(entry)
    root_schema = getattr(module, "ROOT", None)
    if not isinstance(root_schema, type) or not issubclass(root_schema, Schema):
        raise DSLValidationError(f"{entry} must export ROOT as a Schema class")

    xml_text = to_peach_data_model(root_schema, name=root_schema.__name__)
    xml_text = _remove_compiler_only_union_alternative(xml_text)
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=output.parent, prefix=output.name + ".", delete=False
    ) as temporary:
        temporary.write(xml_text)
        temporary_path = Path(temporary.name)
    os.replace(temporary_path, output)
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compile a Peach DSL module to Pit XML")
    parser.add_argument("--entry", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        destination = compile_entry(args.entry, args.output)
        print(f"[PASS] DSL compiled to {destination}")
        return 0
    except (DSLValidationError, OSError, TypeError, ValueError, ET.ParseError) as error:
        print(f"[FAIL] {_format_compile_error(error, args.entry)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
