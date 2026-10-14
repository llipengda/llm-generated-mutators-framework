"""Validate generated DSL modules and compile them to a Peach Pit XML file."""

from __future__ import annotations

import argparse
import ast
from collections.abc import Sequence
import importlib.abc
import importlib.machinery
import importlib.util
import json
import linecache
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import traceback
from threading import RLock
from types import CodeType, ModuleType
from typing import Any, cast
import xml.etree.ElementTree as ET

from .sdk import Schema, to_peach_artifacts


PEACH_NAMESPACE = "http://peachfuzzer.com/2012/Peach"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DSL_IMPORT_LOCK = RLock()


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


def _run_pyright(path: Path, dependencies: Sequence[Path] = ()) -> None:
    """Run one strict Pyright check over the entry and its local dependencies."""

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
                    *(str(dependency) for dependency in dependencies),
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
        payload = cast(dict[str, Any], json.loads(result.stdout))
        diagnostics = payload.get("generalDiagnostics", [])
        messages: list[str] = []
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
        detail = "Pyright is not installed; run `uv sync` from the repository root"
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
    """Type-check dependencies and render every local Schema without writing files."""

    path = path.resolve()
    validate_dsl_dependencies(path)
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
            to_peach_artifacts(schema)
        except Exception as error:
            failures.append(
                f"{path}: Schema {schema_name} failed to evaluate: "
                f"{_format_compile_error(error, path)}"
            )
    if failures:
        raise DSLValidationError("\n".join(failures))
    return tuple(schema_names)


def _local_import_paths(
    node: ast.Import | ast.ImportFrom, source: Path, directory: Path
) -> tuple[Path, ...]:
    base = directory
    if isinstance(node, ast.ImportFrom) and node.level:
        base = source.parent
        for _ in range(node.level - 1):
            base = base.parent
        if not base.is_relative_to(directory):
            return ()
    if isinstance(node, ast.Import):
        modules = [alias.name for alias in node.names]
    else:
        module = node.module or ""
        modules = [module] if module else []
        modules.extend(
            f"{module}.{alias.name}" if module else alias.name
            for alias in node.names if alias.name != "*"
        )
        if not module and (base / "__init__.py").is_file():
            modules.insert(0, "")

    paths: set[Path] = set()
    for module in modules:
        if module.split(".")[0] == "peach_dsl":
            continue
        current = base
        parts = module.split(".") if module else []
        if not parts:
            paths.add(current / "__init__.py")
        for index, part in enumerate(parts):
            current = current / part
            package = current / "__init__.py"
            if package.is_file():
                paths.add(package)
            elif index == len(parts) - 1 and current.with_suffix(".py").is_file():
                paths.add(current.with_suffix(".py"))
            elif not current.is_dir():
                break
    return tuple(sorted(paths))


def validate_dsl_dependencies(entry: Path) -> dict[Path, ast.Module]:
    """Type-check an entry and validate its local imported syntax trees."""

    entry = entry.resolve()
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
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                pending.extend(_local_import_paths(node, path, entry.parent))
    _run_pyright(entry, tuple(sorted(path for path in trees if path != entry)))
    return trees


class _FreshDSLLoader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname: str) -> CodeType:
        # DSL repairs may preserve both size and timestamp; pyc validation is
        # insufficient to guarantee that we execute the current source.
        linecache.cache.pop(self.path, None)
        return self.source_to_code(self.get_data(self.path), self.path)


class _DSLSourceFinder(importlib.abc.MetaPathFinder):
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def find_spec(
        self, fullname: str, path: Sequence[str] | None = None,
        target: ModuleType | None = None,
    ) -> importlib.machinery.ModuleSpec | None:
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if (
            spec is not None
            and spec.origin is not None
            and isinstance(spec.loader, importlib.machinery.SourceFileLoader)
            and Path(spec.origin).resolve().is_relative_to(self.directory)
        ):
            spec.loader = _FreshDSLLoader(fullname, spec.origin)
            return spec
        return None


def _load_entry(entry: Path) -> ModuleType:
    # DSL import and compilation run locally; Pyright validates types rather
    # than isolating execution.
    entry = entry.resolve()
    module_name = f"peach_generated_{os.getpid()}_{entry.stat().st_mtime_ns}"
    spec = importlib.util.spec_from_file_location(
        module_name, entry, loader=_FreshDSLLoader(module_name, str(entry))
    )
    if spec is None or spec.loader is None:
        raise DSLValidationError(f"cannot load DSL entry module {entry}")
    module = importlib.util.module_from_spec(spec)
    local_names = {
        path.stem if path.is_file() else path.name
        for path in entry.parent.iterdir()
        if (path.suffix == ".py" or path.is_dir())
    } - {"peach_dsl", "__pycache__"}
    local_names.add(module_name)
    finder = _DSLSourceFinder(entry.parent)
    with _DSL_IMPORT_LOCK:
        saved_modules = {
            name: value for name, value in tuple(sys.modules.items())
            if name.partition(".")[0] in local_names
        }
        for name in saved_modules:
            del sys.modules[name]
        saved_path = sys.path[:]
        sys.path.insert(0, str(entry.parent))
        sys.meta_path.insert(0, finder)
        sys.modules[module_name] = module
        importlib.invalidate_caches()
        try:
            spec.loader.exec_module(module)
        finally:
            sys.meta_path.remove(finder)
            sys.path[:] = saved_path
            for name in tuple(sys.modules):
                if name.partition(".")[0] in local_names:
                    del sys.modules[name]
            sys.modules.update(saved_modules)
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

    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    artifacts = to_peach_artifacts(root_schema, name=root_schema.__name__)
    xml_text = _remove_compiler_only_union_alternative(artifacts.xml)

    if artifacts.python_fixup is not None:
        script_output = output.with_name("python_fixup.py")
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=script_output.parent,
            prefix=script_output.name + ".",
            delete=False,
        ) as script_temporary:
            script_temporary.write(artifacts.python_fixup)
            script_temporary_path = Path(script_temporary.name)
        os.replace(script_temporary_path, script_output)

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
