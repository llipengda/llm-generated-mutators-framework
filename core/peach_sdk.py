"""Peach SDK selection and generated C# compilation helpers."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Literal, Sequence, cast
from xml.sax.saxutils import escape


PeachSdk = Literal["legacy", "modern"]


def peach_sdk_from_environment() -> PeachSdk:
    value = os.environ.get("PEACH_SDK", "legacy").strip().lower()
    aliases = {
        "legacy": "legacy",
        "sdk": "legacy",
        "modern": "modern",
        "modern-sdk": "modern",
    }
    try:
        return cast(PeachSdk, aliases[value])
    except KeyError as error:
        raise ValueError(
            "PEACH_SDK must be 'legacy' or 'modern' "
            f"(received {value!r})."
        ) from error


def peach_sdk_dir(sdk: PeachSdk | None = None) -> Path:
    selected = sdk or peach_sdk_from_environment()
    return Path("peach") / ("modern-sdk" if selected == "modern" else "sdk")


def peach_image(sdk: PeachSdk | None = None) -> str:
    selected = sdk or peach_sdk_from_environment()
    return f"pdli/llm-peach:{'modern-sdk' if selected == 'modern' else 'sdk'}"


def _sdk_references(sdk_dir: Path) -> list[Path]:
    preferred_names = {
        "BouncyCastle.Crypto.dll",
        "NLog.dll",
        "Newtonsoft.Json.dll",
        "Peach.Core.dll",
        "Peach.LLM.dll",
        "Peach.Pro.dll",
        "Peach.LLM.Validations.Common.dll",
        "nunit.framework.dll",
    }
    references = [path for path in sdk_dir.glob("*.dll") if path.name in preferred_names]
    # Protocol-specific assemblies are copied here while fixer tests are generated.
    references.extend(
        path
        for path in sdk_dir.glob("*.dll")
        if path.name not in preferred_names
        and not path.name.startswith(("System.", "Microsoft."))
    )
    return sorted(set(references))


def compile_csharp(
    sources: Sequence[str | Path],
    output: str | Path,
    *,
    additional_references: Sequence[str | Path] = (),
    excluded_reference_names: Sequence[str] = (),
    warnings_as_errors: bool = True,
    sdk: PeachSdk | None = None,
) -> subprocess.CompletedProcess[str]:
    """Compile generated Peach sources for the selected SDK."""
    selected = sdk or peach_sdk_from_environment()
    source_paths = [Path(source).resolve() for source in sources]
    output_path = Path(output).resolve()
    sdk_dir = peach_sdk_dir(selected).resolve()
    references = _sdk_references(sdk_dir)
    references.extend(Path(reference).resolve() for reference in additional_references)
    excluded_names = set(excluded_reference_names) | {output_path.name}
    references = sorted(
        reference for reference in set(references) if reference.name not in excluded_names
    )

    if not source_paths:
        return subprocess.CompletedProcess([], 2, "", "No C# source files were provided.")
    missing_sources = [str(path) for path in source_paths if not path.is_file()]
    if missing_sources:
        return subprocess.CompletedProcess(
            [], 2, "", "Missing C# source files: " + ", ".join(missing_sources)
        )
    if not references:
        return subprocess.CompletedProcess(
            [], 2, "", f"No reference DLLs found in {sdk_dir}. Run ./setup.sh first."
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if selected == "legacy":
        command = [
            "mcs",
            "-sdk:4.5",
            "-target:library",
            f"-out:{output_path}",
        ]
        if warnings_as_errors:
            command.append("-warnaserror")
        command.extend(f"-r:{reference}" for reference in references)
        command.extend(str(path) for path in source_paths)
        try:
            return subprocess.run(command, text=True, capture_output=True)
        except FileNotFoundError:
            return subprocess.CompletedProcess(
                command, 127, "", "mcs was not found; install Mono or use --modern-sdk."
            )

    if shutil.which("dotnet") is None:
        return subprocess.CompletedProcess(
            ["dotnet"], 127, "", "dotnet was not found; the modern SDK requires .NET 8."
        )

    compile_items = "\n".join(
        f'    <Compile Include="{escape(str(path))}" />' for path in source_paths
    )
    reference_items = "\n".join(
        "    <Reference Include=\"{}\"><HintPath>{}</HintPath>"
        "<Private>false</Private></Reference>".format(
            escape(reference.stem), escape(str(reference))
        )
        for reference in references
    )
    warn = "true" if warnings_as_errors else "false"
    project_text = f"""<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net8.0</TargetFramework>
    <AssemblyName>{escape(output_path.stem)}</AssemblyName>
    <EnableDefaultCompileItems>false</EnableDefaultCompileItems>
    <TreatWarningsAsErrors>{warn}</TreatWarningsAsErrors>
    <Nullable>disable</Nullable>
    <ImplicitUsings>disable</ImplicitUsings>
  </PropertyGroup>
  <ItemGroup>
{compile_items}
{reference_items}
  </ItemGroup>
</Project>
"""
    with tempfile.TemporaryDirectory(prefix="peach-dotnet-build-") as temp_dir:
        project_path = Path(temp_dir) / "Generated.csproj"
        build_dir = Path(temp_dir) / "out"
        project_path.write_text(project_text, encoding="utf-8")
        command = [
            "dotnet",
            "build",
            str(project_path),
            "--configuration",
            "Release",
            "--output",
            str(build_dir),
            "--nologo",
        ]
        result = subprocess.run(command, text=True, capture_output=True)
        built_dll = build_dir / f"{output_path.stem}.dll"
        if result.returncode == 0 and built_dll.is_file():
            shutil.copy2(built_dll, output_path)
        elif result.returncode == 0:
            return subprocess.CompletedProcess(
                command,
                1,
                result.stdout,
                result.stderr + f"\nBuild succeeded but {built_dll} was not produced.",
            )
        return result


def _source_files(paths: list[str], directories: list[str]) -> list[Path]:
    files = [Path(path) for path in paths]
    for directory in directories:
        files.extend(sorted(Path(directory).glob("*.cs")))
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description="Compile C# for the selected Peach SDK.")
    parser.add_argument("--output", required=True)
    parser.add_argument("--source", action="append", default=[])
    parser.add_argument("--source-dir", action="append", default=[])
    parser.add_argument("--reference", action="append", default=[])
    parser.add_argument("--exclude-reference-name", action="append", default=[])
    parser.add_argument("--no-warnings-as-errors", action="store_true")
    args = parser.parse_args()
    result = compile_csharp(
        _source_files(args.source, args.source_dir),
        args.output,
        additional_references=args.reference,
        excluded_reference_names=args.exclude_reference_name,
        warnings_as_errors=not args.no_warnings_as_errors,
    )
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=__import__("sys").stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
