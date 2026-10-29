"""Run Peach validators in isolated local .NET runtime directories."""

from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Literal
from xml.dom import minidom
from xml.sax.saxutils import quoteattr

from core.peach_sdk import MUTATOR_SDK_REFERENCE_NAMES, compile_csharp, peach_sdk_dir


ROOT = Path(__file__).resolve().parents[1]
Validator = Literal["DataModel", "Mutator", "Fixer"]
_PROTOCOL_PLUGIN_SUFFIXES = ("Mutators.dll", "Fixers.dll", "FixerTests.dll", "DataElements.dll")


def protocol_directory(protocol: str) -> Path:
    protocol = protocol.lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", protocol):
        raise ValueError(f"Invalid protocol name: {protocol!r}")
    return ROOT / "llm" / "peach" / protocol


def _require_file(path: Path, label: str) -> Path:
    if not path.is_file():
        raise FileNotFoundError(f"{label} '{path}' does not exist. Run ./setup.sh if the SDK is missing.")
    return path.resolve()


def _require_seeds(path: Path) -> Path:
    if not path.is_dir():
        raise FileNotFoundError(f"Seed directory '{path}' does not exist.")
    return path.resolve()


def _nlog_config(log_dir: Path) -> str:
    destination = quoteattr(str(log_dir / "${var:logFileName}"))
    return f'''<?xml version="1.0" encoding="utf-8"?>
<nlog xmlns="http://www.nlog-project.org/schemas/NLog.xsd"
      xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <variable name="logFileName" value="default.log"/>
  <targets>
    <target name="file" xsi:type="File" fileName={destination}
            layout="${{message}}" deleteOldFileOnStartup="true"/>
  </targets>
  <rules><logger name="*" minlevel="Debug" writeTo="file"/></rules>
</nlog>
'''


def _stage_datamodel(source: Path, runtime: Path) -> None:
    """Resolve each local Include beside its source, without editing source XML."""
    staged: dict[Path, Path] = {source.resolve(): runtime / "datamodel.xml"}
    complete: set[Path] = set()
    active: set[Path] = set()

    def stage(original: Path) -> Path:
        original = original.resolve()
        destination = staged.setdefault(original, runtime / "Included" / f"{len(staged)}_{original.name}")
        if original in active:
            raise ValueError(f"Cyclic XML Include involving '{original}'.")
        if original in complete:
            return destination
        active.add(original)
        document = minidom.parse(str(original))
        for element in document.getElementsByTagName("*"):
            tag = element.localName or element.tagName
            if tag == "Include" and element.hasAttribute("src"):
                include = Path(element.getAttribute("src").replace("file:", ""))
                if not include.is_absolute():
                    include = original.parent / include
                if include.is_file():
                    element.setAttribute("src", "file:" + str(stage(include)))
            elif tag == "PythonPath" and element.hasAttribute("path"):
                search_path = Path(element.getAttribute("path"))
                if not search_path.is_absolute():
                    element.setAttribute("path", str((original.parent / search_path).resolve()))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(document.toxml(), encoding="utf-8")
        document.unlink()
        active.remove(original)
        complete.add(original)
        return destination

    stage(source)


@contextmanager
def runtime_directory(
    sdk_dir: Path,
    log_dir: Path,
    *,
    plugins: Sequence[Path] = (),
    datamodel: Path | None = None,
) -> Generator[Path]:
    """Keep SDK assets intact and stage only this invocation's protocol plugins."""
    if not sdk_dir.is_dir():
        raise FileNotFoundError(f"Peach runtime '{sdk_dir}' is missing. Run ./setup.sh first.")
    log_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="peach-runtime-") as temporary:
        runtime = Path(temporary) / "sdk"
        shutil.copytree(sdk_dir, runtime)
        plugin_dir = runtime / "Plugins"
        plugin_dir.mkdir(exist_ok=True)
        # Older pipeline runs copied protocol assemblies into the shared SDK.
        # Do not let those assemblies leak into another protocol's validation.
        for directory in (runtime, plugin_dir):
            for assembly in directory.glob("*.dll"):
                if assembly.name.endswith(_PROTOCOL_PLUGIN_SUFFIXES):
                    assembly.unlink()
        for plugin in plugins:
            shutil.copy2(_require_file(plugin, "Protocol plugin"), plugin_dir / plugin.name)
        if datamodel is not None:
            _stage_datamodel(_require_file(datamodel, "DataModel"), runtime)
            fixup = datamodel.with_name("python_fixup.py")
            if fixup.is_file():
                shutil.copy2(fixup, runtime / fixup.name)
        (runtime / "NLog.config").write_text(_nlog_config(log_dir.resolve()), encoding="utf-8")
        for name in ("fail", "error", "unchanged"):
            target = log_dir / name
            target.mkdir(exist_ok=True)
            local = runtime / name
            if local.is_dir():
                shutil.rmtree(local)
            elif local.exists():
                local.unlink()
            local.symlink_to(target.resolve(), target_is_directory=True)
        yield runtime


def _protocol_plugins(directory: Path, *categories: str) -> list[Path]:
    name = directory.name.upper()
    paths = {"custom": directory / "DataElements" / "out" / f"{name}DataElements.dll",
             "mutator": directory / "Mutators" / "out" / f"{name}Mutators.dll",
             "fixer": directory / "Fixers" / "out" / f"{name}Fixers.dll",
             "fixer_tests": directory / "Fixers" / "Validations" / "out" / f"{name}FixerTests.dll"}
    return [paths[category] for category in categories if paths[category].is_file()]


def _mutator_summary(lines: Sequence[str]) -> str:
    """Keep the validator's result table with compact, local-only status tags."""
    rows = ["Mutator Test Results:"]
    for line in lines:
        if line.startswith(("[P] ", "[N] ", "[-] ")):
            rows.append(line)
        elif line.startswith("[RESULT] "):
            match = re.search(r"\bPASS=(\d+)\b", line)
            if match is None:
                raise ValueError(f"Mutator result is missing PASS count: {line}")
            rows.append(("[P]" if int(match.group(1)) > 0 else "[N]") + line[len("[RESULT]"):])
        elif line.startswith("[ERROR] ") or line.startswith("[FAIL] "):
            rows.append("[N]" + line[line.index(" "):])
        elif line.startswith("[SKIP]"):
            rows.append("[-]" + line[line.index(" "):])
        elif line.startswith("[PASS] "):
            rows.append("[P]" + line[len("[PASS]"):])
    return "\n".join(rows) + "\n"


def _run(
    validator: Validator,
    arguments: Sequence[str],
    *,
    protocol: str,
    datamodel: Path,
    logs: Path,
    plugins: Sequence[Path],
) -> int:
    if shutil.which("dotnet") is None:
        raise FileNotFoundError("dotnet was not found. Install the .NET 8 SDK first.")
    sdk = peach_sdk_dir().resolve()
    executable = f"Peach.LLM.Validations.{validator}.dll"
    for suffix in ("dll", "deps.json", "runtimeconfig.json"):
        _require_file(sdk / f"Peach.LLM.Validations.{validator}.{suffix}", "Validator runtime")
    logs.mkdir(parents=True, exist_ok=True)
    replay = logs / "replay"
    replay.mkdir(exist_ok=True)
    environment = os.environ.copy()
    environment.update(PEACH_LOG_DIR=str(logs.resolve()), TMPDIR=str(replay.resolve()),
                       TMP=str(replay.resolve()), TEMP=str(replay.resolve()))
    with runtime_directory(sdk, logs, plugins=plugins, datamodel=datamodel) as runtime:
        staged_arguments = [str(runtime / "datamodel.xml") if argument == str(datamodel) else argument
                            for argument in arguments]
        command = ["dotnet", str(runtime / executable), *staged_arguments]
        if validator == "Mutator":
            summary_lines: list[str] = []
            in_summary = False
            with subprocess.Popen(command, cwd=runtime, env=environment, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True, errors="replace", bufsize=1) as process:
                assert process.stdout is not None
                for line in process.stdout:
                    print(line, end="", flush=True)
                    if line.strip() == "Mutator Test Results:":
                        in_summary = True
                    elif in_summary:
                        summary_lines.append(line.rstrip("\r\n"))
                status = process.wait()
            if in_summary:
                summary = logs / "summary.log"
                summary.write_text(_mutator_summary(summary_lines), encoding="utf-8")
                print(f"Saved mutator summary: {summary}", flush=True)
        else:
            status = subprocess.run(command, cwd=runtime, env=environment).returncode
        for report in logs.rglob("*.log"):
            content = report.read_text(encoding="utf-8", errors="replace")
            report.write_text(content.replace(str(runtime / "datamodel.xml"), str(datamodel)), encoding="utf-8")
    if validator == "Mutator":
        # Make saved replay commands work after the temporary runtime is removed.
        replay_command = shlex.join(["uv", "run", "--project", str(ROOT), "--directory", str(ROOT), "python", "-m",
                                     "core.peach_runtime", "crack", protocol, "--verbose"])
        for report in logs.rglob("*.log"):
            content = report.read_text(encoding="utf-8", errors="replace")
            report.write_text(content.replace("./pittool crack -v", replay_command), encoding="utf-8")
    return status


def _print_compile(result: subprocess.CompletedProcess[str]) -> int:
    if result.stdout:
        print(result.stdout, end="", flush=True)
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr, flush=True)
    return result.returncode


def _clean_logs(logs: Path, *, all_files: bool) -> None:
    if all_files and logs.exists():
        shutil.rmtree(logs)
    logs.mkdir(parents=True, exist_ok=True)
    if not all_files:
        for report in logs.glob("*.log"):
            report.unlink()


def run_datamodel(protocol: str, seeds: Path | None = None, datamodel: Path | None = None,
                  name: str | None = None, logs: Path | None = None) -> int:
    directory = protocol_directory(protocol)
    datamodel = _require_file(datamodel or directory / "datamodel.xml", "DataModel")
    seeds = _require_seeds(seeds or ROOT / "seeds" / protocol.lower())
    logs = (logs or directory / "dm_test_logs").resolve()
    _clean_logs(logs, all_files=False)
    return _run("DataModel", [str(datamodel), name or f"{protocol.lower()}_packet_array", str(seeds)],
                protocol=protocol, datamodel=datamodel, logs=logs,
                plugins=_protocol_plugins(directory, "custom"))


def run_mutator(protocol: str, seeds: Path | None = None, mutator_filter: str = "") -> int:
    from core.mutator_codegen import manifest_sources

    directory = protocol_directory(protocol)
    datamodel = _require_file(directory / "datamodel.xml", "DataModel")
    seeds = _require_seeds(seeds or ROOT / "seeds" / protocol.lower())
    output = directory / "Mutators" / "out" / f"{protocol.upper()}Mutators.dll"
    result = compile_csharp(manifest_sources(directory / "Mutators"), output,
                            additional_references=_protocol_plugins(directory, "custom"),
                            excluded_reference_names=[path.name for path in peach_sdk_dir().glob("*.dll")
                                                      if path.name not in MUTATOR_SDK_REFERENCE_NAMES])
    if _print_compile(result):
        return result.returncode
    _require_file(output, "Compiled mutators")
    logs = directory / "mutator_test_logs"
    _clean_logs(logs, all_files=True)
    arguments = [str(datamodel), str(seeds), f"{protocol.lower()}_packet_array", "100"]
    if mutator_filter:
        arguments.append(mutator_filter)
    return _run("Mutator", arguments, protocol=protocol, datamodel=datamodel, logs=logs,
                plugins=_protocol_plugins(directory, "custom", "mutator"))


def run_fixer(protocol: str) -> int:
    directory = protocol_directory(protocol)
    datamodel = _require_file(directory / "datamodel.xml", "DataModel")
    fixers = directory / "Fixers"
    sources = [*sorted(fixers.glob("*.cs")), *sorted((fixers / "Validations").glob("*.cs"))]
    output = fixers / "Validations" / "out" / f"{protocol.upper()}FixerTests.dll"
    result = compile_csharp(sources, output,
                            additional_references=_protocol_plugins(directory, "custom"),
                            excluded_reference_names=[f"{protocol.upper()}Fixers.dll"])
    if _print_compile(result):
        return result.returncode
    _require_file(output, "Compiled fixer tests")
    logs = directory / "fixer_test_logs"
    _clean_logs(logs, all_files=True)
    nunit = _require_file(peach_sdk_dir() / "nunit.framework.dll", "Fixer test dependency")
    return _run("Fixer", [], protocol=protocol, datamodel=datamodel, logs=logs,
                plugins=[*_protocol_plugins(directory, "custom", "fixer_tests"), nunit])


def run_data(protocol: str, data: str) -> int:
    if len(data) % 2 or re.fullmatch(r"[0-9a-fA-F]*", data) is None:
        raise ValueError("Data must contain an even number of hexadecimal digits.")
    directory = protocol_directory(protocol)
    datamodel = _require_file(directory / "datamodel.xml", "DataModel")
    logs = directory / "data_test_logs"
    _clean_logs(logs, all_files=False)
    status = _run("Fixer", ["-d", str(datamodel), f"{protocol.lower()}_packet_array", data],
                  protocol=protocol, datamodel=datamodel, logs=logs,
                  plugins=_protocol_plugins(directory, "custom", "fixer"))
    failure_log = logs / "fixer.log"
    if status and failure_log.is_file():
        print(failure_log.read_text(encoding="utf-8", errors="replace"), end="")
    return status


def run_crack(protocol: str, datamodel: Path, name: str, sample: Path, *, verbose: bool = False) -> int:
    directory = protocol_directory(protocol)
    datamodel = _require_file(datamodel, "DataModel")
    sample = _require_file(sample, "Replay sample")
    sdk = peach_sdk_dir().resolve()
    _require_file(sdk / "PitTool.dll", "PitTool")
    logs = directory / "crack_logs"
    with runtime_directory(sdk, logs, plugins=_protocol_plugins(directory, "custom", "mutator", "fixer"),
                           datamodel=datamodel) as runtime:
        arguments = ["crack", *(["-v"] if verbose else []), str(runtime / "datamodel.xml"), name, str(sample)]
        return subprocess.run(["dotnet", str(runtime / "PitTool.dll"), *arguments], cwd=runtime).returncode


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the local .NET 8 Peach validators.")
    commands = parser.add_subparsers(dest="command", required=True)
    datamodel = commands.add_parser("datamodel")
    datamodel.add_argument("protocol")
    datamodel.add_argument("seeds", type=Path, nargs="?")
    datamodel.add_argument("datamodel", type=Path, nargs="?")
    datamodel.add_argument("name", nargs="?")
    datamodel.add_argument("logs", type=Path, nargs="?")
    mutator = commands.add_parser("mutator")
    mutator.add_argument("protocol")
    mutator.add_argument("seeds", type=Path, nargs="?")
    mutator.add_argument("filter", nargs="?", default="")
    fixer = commands.add_parser("fixer")
    fixer.add_argument("protocol")
    data = commands.add_parser("data")
    data.add_argument("protocol")
    data.add_argument("hex_data")
    crack = commands.add_parser("crack")
    crack.add_argument("protocol")
    crack.add_argument("--verbose", action="store_true")
    crack.add_argument("datamodel", type=Path)
    crack.add_argument("name")
    crack.add_argument("sample", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "datamodel":
            return run_datamodel(args.protocol, args.seeds, args.datamodel, args.name, args.logs)
        if args.command == "mutator":
            return run_mutator(args.protocol, args.seeds, args.filter)
        if args.command == "fixer":
            return run_fixer(args.protocol)
        if args.command == "data":
            return run_data(args.protocol, args.hex_data)
        return run_crack(args.protocol, args.datamodel, args.name, args.sample, verbose=args.verbose)
    except (OSError, ValueError, RuntimeError) as error:
        print(f"[ERROR] {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
