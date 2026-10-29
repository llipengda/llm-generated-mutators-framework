"""Local .NET 8 Peach SDK and generated C# compilation helpers."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from collections.abc import Sequence
from xml.sax.saxutils import escape


_PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Mutator builds and their capability probe use only stable SDK assemblies.
MUTATOR_SDK_REFERENCE_NAMES = frozenset({
    "BouncyCastle.Crypto.dll", "NLog.dll", "Newtonsoft.Json.dll",
    "Peach.Core.dll", "Peach.LLM.dll", "Peach.Pro.dll",
    "Peach.LLM.Validations.Common.dll", "nunit.framework.dll",
})


def peach_sdk_dir() -> Path:
    """Return the installed runtime independently of the caller's directory."""
    configured = os.environ.get("PEACH_SDK_DIR")
    return Path(configured).expanduser().resolve() if configured else _PROJECT_ROOT / "peach" / "sdk"


def csharp_language_version() -> str:
    """Return the language version used to compile generated protocol code."""
    return "12.0"


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
    # Runtime assets such as IronPython's WPF adapter are not compilation
    # references. Extra libraries can be passed explicitly by the caller.
    return sorted(
        path for path in sdk_dir.glob("*.dll")
        if path.name in preferred_names
    )


def compile_csharp(
    sources: Sequence[str | Path],
    output: str | Path,
    *,
    additional_references: Sequence[str | Path] = (),
    excluded_reference_names: Sequence[str] = (),
    warnings_as_errors: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Compile generated Peach sources with the local .NET SDK."""
    source_paths = [Path(source).resolve() for source in sources]
    output_path = Path(output).resolve()
    sdk_dir = peach_sdk_dir()
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
    if shutil.which("dotnet") is None:
        return subprocess.CompletedProcess(
            ["dotnet"], 127, "", "dotnet was not found; install a .NET SDK supporting .NET 8."
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
    <LangVersion>{csharp_language_version()}</LangVersion>
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


def ensure_mutator_sdk() -> None:
    """Check required mutator APIs before spending agent calls."""
    with tempfile.TemporaryDirectory(prefix="peach-mutator-sdk-check-") as directory:
        source = Path(directory) / "MutatorSdkProbe.cs"
        source.write_text(
            """using Peach.Core.Dom;
using Peach.LLM.Core;
using Peach.LLM.Core.Mutators;

public class MutatorSdkProbe : LLMMutator
{
    public MutatorSdkProbe(DataElement obj) : base(obj) { }
    public DataElement Resolve(DataElement obj) => Find(obj, "Root.items");
    protected override void PerformMutation(DataElement obj)
    {
        Find("Root.items");
        obj.SetValue(new byte[] { 0x2a });
        obj.SetValue(42u);
        obj.SetValue("text");
        obj.SetValue(new Peach.Core.IO.BitStream(new byte[] { 0x2a }));
    }

    public static bool Supports(DataElement obj)
    {
        return obj.MatchesPath("Root.items") &&
            (LLMAddMutator.CanApply(obj, "empty") ||
             LLMRemoveMutator.CanApply(obj, "empty") ||
             LLMRepeatMutator.CanApply(obj));
    }
}
""",
            encoding="utf-8",
        )
        result = compile_csharp(
            [source], Path(directory) / "MutatorSdkProbe.dll",
            excluded_reference_names=[
                path.name for path in peach_sdk_dir().glob("*.dll")
                if path.name not in MUTATOR_SDK_REFERENCE_NAMES
            ],
        )
    if result.returncode != 0:
        details = (result.stdout + result.stderr).strip()
        raise RuntimeError(
            "The installed Peach SDK cannot compile structural mutators. "
            "Update Peach.LLM.dll to include LLMAddMutator, LLMRemoveMutator, "
            "LLMRepeatMutator, MatchesPath, SetValue and LLMMutator.Find, and use matching runtime assemblies. "
            "Run ./setup.sh with the updated llm-peach sources to install .NET 8 assemblies.\n"
            + details
        )


def _source_files(paths: list[str], directories: list[str]) -> list[Path]:
    files = [Path(path) for path in paths]
    for directory in directories:
        files.extend(sorted(Path(directory).glob("*.cs")))
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description="Compile C# for the local .NET 8 Peach SDK.")
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
