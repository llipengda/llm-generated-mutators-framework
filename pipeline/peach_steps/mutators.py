from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from core.agent import build_agent_graph
from core.agent_types import AgentGraph
from core.log import run_logged_task
from core.mutator_codegen import (
    MutatorPlan,
    PacketMutatorPlan,
    build_mutator_plan,
    compile_packet,
    load_mutator_plan,
    packet_for_class,
    packet_outputs_exist,
    prepare_packet,
    set_active_packets,
)
from core.peach_sdk import csharp_language_guidance, ensure_mutator_sdk
from core.tool_result import DotNetBuildData, ToolResult, tool_error, tool_success
from core.ui import UI, ask_regenerate, ask_select_types, ask_skip_verification
from pipeline.peach_steps.common import PeachStepMixin


_MUTATOR_TOOLS = (
    "Read_File", "Search_Files", "Write_File", "Apply_Patch", "Search_Class",
    "RFC_Search", "Build_Mutator_DLL",
)
_RESULT_LINE = re.compile(
    r"^\[(PASS|FAIL|ERROR|SKIP)\](?:\[\d+/\d+\])?\s+(\w+)\s*$",
    re.MULTILINE,
)
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_COMMAND_FAILURE_PREFIX = "[ERROR] Mutator validator command failed"


def _build_packet(plan: MutatorPlan, packet: PacketMutatorPlan) -> ToolResult[DotNetBuildData]:
    """The same scoped compiler is available to the agent and host."""
    try:
        result = compile_packet(plan, packet)
    except (OSError, ValueError, RuntimeError) as error:
        return tool_error("mutator_build_unavailable", str(error))
    if result.returncode != 0:
        return tool_error(
            "mutator_build_failed",
            f"Compilation failed for {packet.packet_type} (exit {result.returncode}).\n"
            + ((result.stdout or "") + (result.stderr or "")).strip(),
        )
    if not packet.output_dll.is_file():
        return tool_error("output_missing", f"Compiler did not produce {packet.output_dll}.")
    return tool_success(
        "mutator_build_succeeded",
        f"Compiled {packet.output_dll}.",
        DotNetBuildData(
            source=str(packet.semantic_file), output=str(packet.output_dll), source_count=2,
        ),
    )


def _owned_compiler_error(packet: PacketMutatorPlan, diagnostics: str) -> bool:
    """A missing required semantic method is reported at its generated caller."""
    error_lines = [
        line for line in diagnostics.splitlines()
        if re.search(r"\berror\s+(?:[A-Z]+\d+|:)", line)
    ]
    if not error_lines:
        return False
    hook_names = {hook.name for spec in packet.mutators for hook in spec.hooks}
    reference_errors = {"CS0006", "CS0009", "CS0012", "CS1704", "CS1705"}
    for line in error_lines:
        if any(code in line for code in reference_errors):
            return False
        if packet.semantic_file.name in line and re.search(r"\berror CS\d+", line):
            continue
        if (
            packet.generated_file.name in line and "error CS0103" in line
            and any(name in line for name in hook_names)
        ):
            continue
        return False
    return True


def _semantic_tasks(packet: PacketMutatorPlan) -> str:
    tasks: list[str] = []
    for spec in packet.mutators:
        if not spec.hooks:
            continue
        hooks = "\n".join(f"    {hook.signature}" for hook in spec.hooks)
        tasks.append(
            f"Class: {spec.class_name}\nNamespace: {spec.namespace}\n"
            f"Operation: {spec.operation}\nTarget path: {spec.path}\n"
            f"DSL path: {spec.dsl_path}\nRuntime type: {spec.runtime_type}\n"
            f"Field description: {spec.description}\n"
            f"Required methods (implement exactly these signatures):\n{hooks}"
        )
    return "\n\n".join(tasks)


class MutatorSteps(PeachStepMixin):
    def _mutator_plan(self, *, active: bool = False) -> MutatorPlan:
        packet_types = self.state.get("packet_types") or []
        if active:
            output_dir = _PROJECT_ROOT / "llm" / "peach" / self.protocol_lower / "Mutators"
            plan = load_mutator_plan(output_dir)
            packet_types = [packet.packet_type for packet in plan.packets]
            unknown = set(packet_types).difference(self.state.get("packet_types") or [])
            if unknown:
                raise RuntimeError(
                    "Active mutator packets are absent from pipeline state: "
                    + ", ".join(sorted(unknown)) + ". Run Step 4 again."
                )
            return plan
        return build_mutator_plan(
            self.protocol_lower, packet_types=packet_types,
        )

    def _mutator_agent(self, plan: MutatorPlan, packet: PacketMutatorPlan) -> AgentGraph:
        read_files = [
            packet.generated_file, packet.semantic_file,
            _PROJECT_ROOT / "docs" / "llm-peach-sdk.md",
            _PROJECT_ROOT / "docs" / "peach-dsl.md",
            *sorted(plan.dsl_dir.glob("*.py")),
            *sorted((plan.output_dir.parent / "DataElements").glob("*.cs")),
        ]
        return build_agent_graph(
            retriever=self.retriever,
            config=self.agent_config,
            tool_names=_MUTATOR_TOOLS,
            read_files=read_files,
            write_files=(packet.semantic_file,),
            mutator_build=lambda: _build_packet(plan, packet),
        )

    def _mutator_prompt(self, packet: PacketMutatorPlan) -> str:
        return f"""Implement the required protocol-aware mutation methods for the
{self.protocol_lower} {packet.packet_type} packet in {packet.semantic_file}.

The host has already planned every field, operation, class, exact rooted path,
constructor and static supportedDataElement method from the validated DSL.
Read {packet.generated_file} as the immutable interface contract. Write only
{packet.semantic_file}, using partial classes in the exact supplied namespaces.
Add, Remove and Repeat already inherit the SDK's structural mutator bases;
their PerformMutation implementations are sealed. Do not redeclare their
constructors, attributes, selectors, base classes or PerformMutation methods.
Do not add or remove mutator classes, invent fields, or change the DSL.

{_semantic_tasks(packet)}

For each MutateCore, cover all applicable field-local semantic categories:
A. Canonical form
B. Boundaries
C. Equivalence-class alternatives
D. Allowed bitfield/enum/range
E. Encoding-shape variant
F. Padding/alignment
G. prefix/suffix
H. Random valid mix
Use randomized shallow and deep perturbations for long-term diversity. Keep
mutations local to the planned target. Explain in a short code comment when a
category cannot apply to that field. Helpers belong in this semantic file.
Do not leave required methods empty, stubbed, or throwing NotImplementedException.
Use Next() from LLMMutator for randomness. Preserve the structural bases' default
presence-source policy unless the plan explicitly requires a synchronization hook.

{csharp_language_guidance()}
Read docs/llm-peach-sdk.md and the relevant shared_model.py/family modules with
Read_File. Treat the DSL as the DataModel source of truth. Use Search_Class for
SDK APIs and RFC_Search for protocol semantics. Write_File creates the semantic
file; Apply_Patch makes localized repairs. Call Build_Mutator_DLL with no arguments
until both partial source files compile together successfully. You must implement
ALL required methods before finishing. The host independently checks compilation.
"""

    def _compile_mutator_with_repairs(
        self, plan: MutatorPlan, packet: PacketMutatorPlan,
        *, step_title: str = "Step 4",
    ) -> None:
        for attempt in range(4):
            result = _build_packet(plan, packet)
            if result["ok"]:
                return
            diagnostics = result["message"]
            if (
                result["code"] != "mutator_build_failed"
                or not packet.semantic_file.is_file()
                or not _owned_compiler_error(packet, diagnostics)
            ):
                raise RuntimeError(
                    f"Mutator generation for {packet.packet_type} failed outside the "
                    f"semantic implementation. Fix the code generator or SDK.\n{diagnostics}"
                )
            if attempt == 3:
                raise RuntimeError(
                    f"Mutator compilation for {packet.packet_type} failed after "
                    f"three semantic repair attempts.\n{diagnostics}"
                )
            self.call_agent(
                self._mutator_prompt(packet)
                + f"\nHost compiler diagnostics requiring repair:\n{diagnostics}",
                f"{step_title}: Compile repair {packet.packet_type} ({attempt + 1})",
                agent_graph=self._mutator_agent(plan, packet),
            )

    def step_4_mutator_generation(self):
        UI.title("Step 4: Mutator Generation")
        if not self.state.get("packet_types"):
            UI.warn("packet_types is empty. Step 4 will not generate mutators.")
            return
        ensure_mutator_sdk()
        plan = self._mutator_plan()
        selected: list[str] = []
        existing_packets: set[str] = set()
        for packet in plan.packets:
            exists = packet_outputs_exist(plan, packet)
            if exists:
                existing_packets.add(packet.packet_type)
            if exists and not ask_regenerate(
                f"mutators for {packet.packet_type}", self.protocol_lower,
            ):
                UI.dim(f"Skipping existing mutators for {packet.packet_type}.")
                continue
            selected.append(packet.packet_type)
        if not selected:
            set_active_packets(plan, [packet.packet_type for packet in plan.packets if packet.packet_type in existing_packets])
            UI.success("All existing mutators were skipped.")
            return
        selected = ask_select_types(selected, self.protocol_lower)
        active_packets = existing_packets.union(selected)
        set_active_packets(plan, [packet.packet_type for packet in plan.packets if packet.packet_type in active_packets])
        if not selected:
            UI.warn("No packet types selected. Skipping mutator generation.")
            return

        # Host-owned sources and manifest entries are prepared before parallel agents.
        packets = [packet for packet in plan.packets if packet.packet_type in selected]
        for packet in packets:
            prepare_packet(plan, packet)

        def run_one(packet: PacketMutatorPlan, index: int) -> None:
            if any(spec.hooks for spec in packet.mutators):
                self.call_agent(
                    self._mutator_prompt(packet),
                    f"Step 4.{index + 1}: Mutator Generation for {packet.packet_type}",
                    agent_graph=self._mutator_agent(plan, packet),
                )
            self._compile_mutator_with_repairs(plan, packet)

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [
                executor.submit(run_logged_task, f"Mutator {packet.packet_type}", run_one, packet, index)
                for index, packet in enumerate(packets)
            ]
            for future in as_completed(futures):
                future.result()

    def step_5_mutator_validation_and_fix(self):
        UI.title("Step 5: Mutator Validation & Fix")
        if not self.state.get("packet_types"):
            UI.warn("packet_types is empty. Step 5 has no mutators to validate.")
            return
        ensure_mutator_sdk()
        plan = self._mutator_plan(active=True)
        results_dir = plan.output_dir.parent / "mutator_test_logs"
        error_dir = results_dir / "error"
        results_path = results_dir / "results.txt"
        skip_first = [ask_skip_verification("Mutator Validation")]
        affected_packets: set[str] = set()

        def error_groups() -> dict[str, list[tuple[str, str]]]:
            grouped: dict[str, list[tuple[str, str]]] = {}
            for log_file in sorted(error_dir.glob("*.log")):
                mutator_name = log_file.stem
                try:
                    packet = packet_for_class(plan, mutator_name)
                except (KeyError, ValueError) as error:
                    raise RuntimeError(f"Cannot route mutator error log {log_file}: {error}") from error
                spec = next(spec for spec in packet.mutators if spec.class_name == mutator_name)
                output = log_file.read_text(encoding="utf-8")
                # Structural templates have no semantic implementation to repair.
                # For semantic classes, exceptions inside a helper may omit the
                # MutateCore name, but their generated selector/constructor errors
                # must never be handed to the LLM.
                generated_frame = re.search(
                    re.escape(spec.class_name) + r"\.(?:supportedDataElement|\.ctor)\s*\(",
                    output,
                )
                if not spec.hooks or generated_frame:
                    raise RuntimeError(
                        f"{mutator_name} failed in generated code or the structural SDK. "
                        f"Fix the generator/SDK rather than editing partial sources.\n{output}"
                    )
                grouped.setdefault(packet.packet_type, []).append((mutator_name, output))
            return grouped

        def fix_fn(output: str, hint: str | None) -> None:
            groups = error_groups()
            if not groups:
                raise RuntimeError("Mutator validation failed without routable ERROR logs.\n" + output)
            affected_packets.clear()
            affected_packets.update(groups)

            def fix_one(packet: PacketMutatorPlan) -> None:
                errors = "\n\n".join(
                    f"Mutator {name}:\n{log}" for name, log in groups[packet.packet_type]
                )
                prompt = self._mutator_prompt(packet) + f"""
Repair runtime ERRORs in the semantic implementation for this packet. These
classes share {packet.semantic_file}; repair all reported errors together.
The generated file and SDK are read-only. Use Apply_Patch for localized fixes.
Re-crack FAILs can be intentional fuzz outcomes; this task repairs exceptions.

{errors}

Common fixes: use byte-backed Variant values for non-String DataElements;
clone elements with unique names; verify SDK APIs before using them.
"""
                if hint:
                    prompt += f"\nAdditional guidance from the user:\n{hint}\n"
                self.call_agent(
                    prompt, f"Step 5: Fix Mutators for {packet.packet_type}",
                    agent_graph=self._mutator_agent(plan, packet),
                )
                self._compile_mutator_with_repairs(plan, packet, step_title="Step 5")

            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = [
                    executor.submit(run_logged_task, f"Mutator fix {packet.packet_type}", fix_one, packet)
                    for packet in plan.packets if packet.packet_type in groups
                ]
                for future in as_completed(futures):
                    future.result()

        def verify_fn() -> tuple[bool, str]:
            expected = {
                spec.class_name for packet in plan.packets
                if not affected_packets or packet.packet_type in affected_packets
                for spec in packet.mutators
            }
            if skip_first[0]:
                skip_first[0] = False
                try:
                    output = results_path.read_text(encoding="utf-8")
                except OSError as error:
                    raise RuntimeError(
                        "No saved mutator verification report is available. "
                        "Rerun Step 5 without skipping verification."
                    ) from error
                if output.startswith(_COMMAND_FAILURE_PREFIX):
                    raise RuntimeError(
                        "Saved mutator verification records a failed validator command. "
                        "Rerun Step 5 without skipping verification.\n" + output
                    )
            else:
                cmd = [
                    "./tests/peach_mutator/run_peach_mutator_test.sh",
                    self.protocol_lower, self.seed_dir,
                ]
                if affected_packets:
                    cmd.append(",".join(sorted(expected)))
                    UI.dim("Retesting all mutators in affected packets: " + ", ".join(sorted(affected_packets)))
                result = UI.run_with_live_output(cmd, title="Running Mutator Tests")
                output = (result.stdout or "") + (result.stderr or "")
                results_dir.mkdir(parents=True, exist_ok=True)
                if result.returncode != 0:
                    output = f"{_COMMAND_FAILURE_PREFIX} (exit {result.returncode}).\n{output}"
                results_path.write_text(output, encoding="utf-8")
                if result.returncode != 0:
                    raise RuntimeError(
                        f"Mutator validator command failed (exit {result.returncode}). "
                        f"Check compiler, Docker and SDK diagnostics.\n{output}"
                    )
            if "Mutator Test Results:" not in output:
                raise RuntimeError("Mutator validator did not complete its results report.\n" + output)
            report = output.rsplit("Mutator Test Results:", 1)[1]
            result_rows = _RESULT_LINE.findall(report)
            statuses = {name: status for status, name in result_rows}
            if len(result_rows) != len(statuses):
                raise RuntimeError("Mutator validator reported duplicate class names; check generated classes and plugin assemblies.")
            missing = expected.difference(statuses)
            if missing:
                raise RuntimeError("Mutator validator omitted planned classes: " + ", ".join(sorted(missing)))
            unexpected = statuses.keys() - expected
            if unexpected:
                raise RuntimeError("Mutator validator reported unplanned classes: " + ", ".join(sorted(unexpected)))
            if expected and all(statuses[name] == "SKIP" for name in expected):
                raise RuntimeError(
                    "Mutator validator skipped every planned class; no mutations were tested. "
                    "Check that seeds parse and target paths match the model.\n" + output
                )
            failed = sorted(name for name, status in statuses.items() if status == "FAIL")
            if failed:
                UI.warn("Re-crack FAILs (reported without automatic repair): " + ", ".join(failed))
            groups = error_groups()
            reported_errors = {name for name, status in statuses.items() if status == "ERROR"}
            logged_errors = {name for items in groups.values() for name, _ in items}
            if reported_errors != logged_errors:
                raise RuntimeError("Mutator ERROR results and log files disagree; rerun validation.")
            if not groups:
                return True, ""
            return False, "\n\n".join(log for items in groups.values() for _, log in items)

        if not self.fix_verify_loop("Step 5: Mutator Validation & Fix", verify_fn, fix_fn):
            raise RuntimeError("Mutator validation was stopped before all runtime ERRORs were resolved.")
