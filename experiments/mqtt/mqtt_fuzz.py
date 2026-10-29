"""Launch isolated MQTT Random / LLM two-phase campaigns and collect coverage."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from typing import TextIO
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[2]
NS = "http://peachfuzzer.com/2012/Peach"
ET.register_namespace("", NS)


@dataclass
class Campaign:
    mode: str
    directory: Path
    container: str | None = None
    process: subprocess.Popen[str] | None = None
    console: TextIO | None = None
    returncode: int | None = None
    coverage_error: str | None = None


def command(args: list[str], *, timeout: int = 120) -> str:
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{' '.join(args)}\n{result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip()


def child(parent: ET.Element, tag: str, **attrs: str) -> ET.Element:
    return ET.SubElement(parent, f"{{{NS}}}{tag}", attrs)


def write_pit(directory: Path, seeds: Path, mode: str, mutations: int) -> None:
    peach = ET.Element(f"{{{NS}}}Peach")
    child(peach, "Include", ns="dm", src="file:datamodel.xml")
    model = child(peach, "StateModel", name="State", initialState="Initial")
    action = child(child(model, "State", name="Initial"), "Action", type="outputPackets")
    # Peach otherwise uses the namespaced ref as the root name, defeating MatchesPath.
    child(action, "DataModel", name="mqtt_packet_array", ref="dm:mqtt_packet_array")
    child(action, "Data", fileName=str(seeds / "*.raw"))
    test = child(peach, "Test", name="Default")
    child(test, "StateModel", ref="State")
    publisher = child(test, "Publisher", **{"class": "TcpClient"})
    for name, value in (("Host", "##HOST##"), ("Port", "##PORT##"), ("Timeout", "200")):
        child(publisher, "Param", name=name, value=value)
    strategy = child(test, "Strategy", **{"class": "Random" if mode == "random" else "TwoPhaseRandom"})
    if mode == "llm":
        child(strategy, "Param", name="TwoPhaseMutation", value="True")
        child(strategy, "Param", name="MultipleMutationsPerElement", value=str(mutations))
    logger = child(test, "Logger", **{"class": "File"})
    child(logger, "Param", name="Path", value=str(directory / "peach_logs"))
    ET.indent(peach)
    ET.ElementTree(peach).write(directory / "pit.xml", encoding="utf-8", xml_declaration=True)


def positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def parse_args() -> argparse.Namespace:
    default_runtime = Path(os.environ.get("PEACH_DIR", str(Path.home() / "peach")))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", nargs="?", choices=("random", "llm", "both"), default="both")
    parser.add_argument("--peach-dir", type=Path, default=default_runtime)
    parser.add_argument("--seed-dir", type=Path, default=ROOT / "seeds/mqtt")
    parser.add_argument("--generated-dir", type=Path, default=ROOT / "llm/peach/mqtt")
    parser.add_argument("--output-dir", type=Path, help="new directory; defaults to logs/mqtt/campaign-*")
    parser.add_argument("--seed", type=int, default=123, help="same Peach RNG seed for both strategies")
    budget = parser.add_mutually_exclusive_group()
    budget.add_argument("--duration", default="00:10:00", help="Peach duration (default: 00:10:00)")
    budget.add_argument("--iterations", type=positive, help="iteration limit instead of duration")
    parser.add_argument("--mutations", type=positive, default=3, help="LLM strategy mutations per element (default: 3)")
    parser.add_argument("--image", default="pdli/mosquitto:latest", help="coverage-enabled Mosquitto Docker image")
    parser.add_argument("--host", help="use existing target(s), skipping Docker and coverage collection")
    parser.add_argument("--port", type=positive, help="existing target port (random or a single strategy)")
    parser.add_argument("--llm-port", type=positive, help="separate LLM target port when using both with --host")
    parser.add_argument("--prepare-only", action="store_true", help="write isolated plugins and Pits without starting processes")
    args = parser.parse_args()
    if args.seed < 0 or args.seed > 4294967295:
        parser.error("--seed must be between 0 and 4294967295")
    if args.host and not args.port:
        parser.error("--host requires --port")
    if not args.host and (args.port or args.llm_port):
        parser.error("--port/--llm-port require --host; Docker ports are allocated automatically")
    if args.host and args.mode == "both" and (not args.llm_port or args.llm_port == args.port):
        parser.error("both with --host requires distinct --port and --llm-port targets")
    if args.mode != "both" and args.llm_port:
        parser.error("--llm-port is only used with both")
    if any(port and port > 65535 for port in (args.port, args.llm_port)):
        parser.error("ports must be at most 65535")
    return args


def prepare(args: argparse.Namespace) -> list[Campaign]:
    runtime = args.peach_dir.expanduser().resolve()
    generated = args.generated_dir.expanduser().resolve()
    seeds = args.seed_dir.expanduser().resolve()
    modes = ["random", "llm"] if args.mode == "both" else [args.mode]
    required = [runtime / "Peach.dll", runtime / "Peach.runtimeconfig.json",
                runtime / "Peach.LLM.dll", runtime / "Peach.LLM.Validations.Common.dll",
                generated / "datamodel.xml", generated / "DataElements/out/MQTTDataElements.dll"]
    if "llm" in modes:
        required.append(generated / "Mutators/out/MQTTMutators.dll")
    for path in required:
        if not path.is_file():
            raise RuntimeError(f"Missing file: {path}")
    if not seeds.is_dir() or not any(seeds.glob("*.raw")):
        raise RuntimeError(f"No .raw seeds in {seeds}")
    if not args.prepare_only:
        if not shutil.which("dotnet"):
            raise RuntimeError("dotnet is not on PATH")
        if not args.host:
            command(["docker", "info", "--format", "{{.ServerVersion}}"])
            command(["docker", "image", "inspect", args.image])
    if args.output_dir:
        output = args.output_dir.expanduser().resolve()
        output.mkdir(parents=True, exist_ok=False)
    else:
        parent = ROOT / "logs/mqtt"
        parent.mkdir(parents=True, exist_ok=True)
        output = Path(tempfile.mkdtemp(prefix=f"campaign-{time.strftime('%Y%m%d-%H%M%S')}-", dir=parent))
    args.peach_dir, args.seed_dir, args.output_dir = runtime, seeds, output
    campaigns: list[Campaign] = []
    for mode in modes:
        directory = output / mode
        plugins = directory / "plugins"
        plugins.mkdir(parents=True)
        # Never load ~/peach/Plugins: its combined MQTT.dll would contaminate Random.
        for filename in ("Peach.LLM.dll", "Peach.LLM.Validations.Common.dll"):
            shutil.copy2(runtime / filename, plugins / filename)
        shutil.copy2(generated / "DataElements/out/MQTTDataElements.dll", plugins)
        if mode == "llm":
            shutil.copy2(generated / "Mutators/out/MQTTMutators.dll", plugins)
        shutil.copy2(generated / "datamodel.xml", directory)
        write_pit(directory, seeds, mode, args.mutations)
        campaigns.append(Campaign(mode, directory))
    inputs = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in required}
    metadata = {key: str(value) if isinstance(value, Path) else value for key, value in vars(args).items()}
    metadata["input_sha256"] = inputs
    (output / "config.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(f"Output: {output}", flush=True)
    return campaigns


def start_target(campaign: Campaign, args: argparse.Namespace) -> tuple[str, int]:
    if args.host:
        port = args.llm_port if args.mode == "both" and campaign.mode == "llm" else args.port
        return args.host, port
    coverage = campaign.directory / "coverage"
    coverage.mkdir()
    campaign.container = f"mqtt-fuzz-{uuid.uuid4().hex[:12]}-{campaign.mode}"
    command(["docker", "run", "-d", "--name", campaign.container,
             "-p", "127.0.0.1::1883", "-v", f"{coverage}:/coverage_data",
             "-e", f"NAME={campaign.mode}", "-e", "INTERVAL=60",
             "-e", "LOG=/coverage_data/broker.log", "-e", "LOG_ERR=/coverage_data/broker.err",
             args.image])
    # Wait for the coverage wrapper's initial snapshot, without connecting to the broker.
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        running = command(["docker", "inspect", "--format", "{{.State.Running}}", campaign.container])
        if running != "true":
            raise RuntimeError(f"Target exited: {command(['docker', 'logs', campaign.container])}")
        if any(coverage.glob("*/coverage.csv")):
            csv = next(coverage.glob("*/coverage.csv"))
            if len(csv.read_text().splitlines()) > 1:
                address = command(["docker", "port", campaign.container, "1883/tcp"])
                return "127.0.0.1", int(address.rsplit(":", 1)[1])
        time.sleep(0.2)
    raise RuntimeError(f"Mosquitto coverage startup timed out: {campaign.container}")


def collect_coverage(campaign: Campaign) -> None:
    if not campaign.container:
        return
    # Fuzzer is stopped first; flush and capture before deleting the target container.
    command(["docker", "exec", campaign.container, "sh", "-c",
             "kill -USR1 $(pidof mosquitto) && sleep 1 && "
             "lcov --capture --directory . --rc branch_coverage=1 "
             "--output-file /coverage_data/final.info -q"])
    summary = command(["docker", "exec", campaign.container, "lcov", "--summary",
                       "/coverage_data/final.info", "--rc", "branch_coverage=1"])
    (campaign.directory / "coverage/summary.txt").write_text(summary + "\n")
    print(f"[{campaign.mode}] {summary}", flush=True)


def log_metrics(campaign: Campaign) -> dict[str, int | str | None]:
    mutations = llm_mutations = last_iteration = 0
    error = None
    console = campaign.directory / "console.log"
    if console.is_file():
        with console.open(errors="replace") as lines:
            for line in lines:
                match = re.search(r"\bMutator:\s+(\S+)", line)
                if match:
                    mutations += 1
                    llm_mutations += match[1].startswith("MQTT")
                iteration = re.match(r"\[(\d+),", line)
                if iteration:
                    last_iteration = max(last_iteration, int(iteration[1]))
                if "Test 'Default' error:" in line:
                    error = line.strip()
    return {"mutations": mutations, "llm_mutations": llm_mutations,
            "last_iteration": last_iteration, "test_error": error}


def run(args: argparse.Namespace, campaigns: list[Campaign]) -> int:
    interrupted = False
    failed = False
    previous_sigint = signal.getsignal(signal.SIGINT)
    previous_sigterm = signal.getsignal(signal.SIGTERM)
    try:
        # Prepare both brokers before starting either campaign.
        targets = [(campaign, start_target(campaign, args)) for campaign in campaigns]
        for campaign, (host, port) in targets:
            budget = [f"--range=1,{args.iterations}"] if args.iterations else [f"--duration={args.duration}"]
            argv = ["dotnet", str(args.peach_dir / "Peach.dll"), "--noweb", "--polite",
                    f"--plugins={campaign.directory / 'plugins'}", f"-DHOST={host}", f"-DPORT={port}",
                    f"--seed={args.seed}", *budget, "pit.xml"]
            (campaign.directory / "command.json").write_text(json.dumps(argv, indent=2) + "\n")
            campaign.console = (campaign.directory / "console.log").open("w")
            campaign.process = subprocess.Popen(argv, cwd=campaign.directory, stdout=campaign.console,
                                                stderr=subprocess.STDOUT, text=True, start_new_session=True)
            print(f"[{campaign.mode}] Started PID {campaign.process.pid} → {host}:{port}; "
                  f"log: {campaign.directory / 'console.log'}", flush=True)
        pending = list(campaigns)
        while pending:
            for campaign in pending[:]:
                assert campaign.process is not None
                code = campaign.process.poll()
                if code is not None:
                    campaign.returncode = code
                    failed |= code != 0
                    print(f"[{campaign.mode}] Peach exited: {code}", flush=True)
                    pending.remove(campaign)
            time.sleep(0.2)
    except KeyboardInterrupt:
        interrupted = True
        print("Stopping campaigns and collecting coverage…", flush=True)
    finally:
        # Restore handlers so cleanup survives a second Ctrl-C / SIGTERM.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        for campaign in campaigns:
            process = campaign.process
            if process is not None and process.poll() is None:
                os.killpg(process.pid, signal.SIGINT)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                campaign.returncode = process.returncode
            if campaign.console:
                campaign.console.close()
            try:
                collect_coverage(campaign)
            except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
                failed = True
                campaign.coverage_error = str(exc)
                print(f"[{campaign.mode}] Coverage collection failed: {exc}", file=sys.stderr)
            finally:
                if campaign.container:
                    try:
                        command(["docker", "rm", "-f", campaign.container])
                    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
                        failed = True
                        print(f"Container cleanup failed: {exc}", file=sys.stderr)
        summary = []
        for campaign in campaigns:
            metrics = log_metrics(campaign)
            summary.append({"mode": campaign.mode, "returncode": campaign.returncode,
                            "console": str(campaign.directory / "console.log"),
                            "coverage_error": campaign.coverage_error, **metrics})
            print(f"[{campaign.mode}] Last iteration: {metrics['last_iteration']}; "
                  f"LLM mutator calls: {metrics['llm_mutations']}", flush=True)
            if metrics["test_error"]:
                failed = True
                print(f"[{campaign.mode}] {metrics['test_error']}", file=sys.stderr)
        (args.output_dir / "results.json").write_text(json.dumps(summary, indent=2) + "\n")
        signal.signal(signal.SIGINT, previous_sigint)
        signal.signal(signal.SIGTERM, previous_sigterm)
    return 130 if interrupted else int(failed)


def main() -> int:
    args = parse_args()
    # Treat SIGTERM like Ctrl-C so target coverage is still flushed.
    def stop(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    try:
        campaigns = prepare(args)
        if args.prepare_only:
            print("Prepared Pits and plugin snapshots; no processes started.")
            return 0
        return run(args, campaigns)
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
