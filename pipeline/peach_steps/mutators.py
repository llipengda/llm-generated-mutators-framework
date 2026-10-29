from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from core.prompts import render_prompt
from core.agent import build_agent_graph
from core.agent_types import AgentGraph
from core.log import run_logged_task
from core.mutator_assessment import (
    RANDOM_BASELINE_DOCUMENT,
    PacketAssessment,
    assessment_notes,
    assessment_prompt,
    filter_packet,
    parse_assessment,
    save_assessment,
)
from core.mutator_codegen import (
    MutatorPlan,
    PacketMutatorPlan,
    build_mutator_plan,
    compile_packet,
    disable_mutators,
    load_mutator_plan,
    packet_for_class,
    packet_outputs_exist,
    prepare_packet,
    set_active_packets,
)
from core.mutator_validation import (
    COMMAND_FAILURE_PREFIX,
    MutatorIssue,
    collect_issues,
    parse_report,
    require_semantic_repair,
    validate_report,
)
from core.peach_sdk import ensure_mutator_sdk
from core.tool_result import DotNetBuildData, ToolResult, tool_error, tool_success
from core.ui import UI, ask_mutator_failure_action, ask_regenerate, ask_select_types, ask_skip_verification
from pipeline.peach_steps.common import PeachStepMixin


_MUTATOR_TOOLS = (
    "Read_File", "Search_Files", "Write_File", "Apply_Patch", "Search_Class",
    "RFC_Search", "Build_Mutator_DLL",
)
_ASSESSMENT_TOOLS = ("Read_File", "Search_Files", "Search_Class", "RFC_Search")
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


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
            packet.generated_file.name in line and any(code in line for code in ("error CS0103", "error CS0534", "error CS8795"))
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
            render_prompt(
                "mutators/required_methods.txt",
                spec_class_name=spec.class_name,
                spec_namespace=spec.namespace,
                spec_operation=spec.operation,
                spec_path=spec.path,
                spec_dsl_path=spec.dsl_path,
                spec_runtime_type=spec.runtime_type,
                spec_description=spec.description,
                spec_source_path=spec.source_path,
                hooks=hooks,
            )
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

    def _assess_mutator_packet(
        self, plan: MutatorPlan, packet: PacketMutatorPlan, index: int,
    ) -> PacketMutatorPlan:
        if not packet.mutators:
            save_assessment(plan, packet, PacketAssessment(packet.packet_type, ()))
            return packet
        agent = build_agent_graph(
            retriever=self.retriever,
            config=self.agent_config,
            tool_names=_ASSESSMENT_TOOLS,
            read_files=(
                RANDOM_BASELINE_DOCUMENT,
                _PROJECT_ROOT / "docs" / "llm-peach-sdk.md",
                _PROJECT_ROOT / "docs" / "peach-dsl.md",
                *sorted(plan.dsl_dir.glob("*.py")),
                *sorted((plan.output_dir.parent / "DataElements").glob("*.cs")),
            ),
            write_files=(),
        )
        prompt = assessment_prompt(plan, packet)
        diagnostics = ""
        for attempt in range(2):
            response = self.call_agent(
                prompt if attempt == 0 else diagnostics,
                f"Step 4.{index + 1}: Semantic Value Assessment for {packet.packet_type} ({attempt + 1})",
                agent_graph=agent,
            )
            try:
                if not response["messages"]:
                    raise ValueError("assessment agent returned no final message")
                assessment = parse_assessment(str(response["messages"][-1].text), packet)
            except ValueError as error:
                if attempt == 1:
                    raise RuntimeError(
                        f"Semantic value assessment for {packet.packet_type} failed before code generation: {error}"
                    ) from error
                diagnostics = render_prompt("mutators/assessment_retry.txt", error=error)
                continue
            save_assessment(plan, packet, assessment)
            filtered = filter_packet(packet, assessment)
            UI.dim(
                f"{packet.packet_type}: retained {sum(field.generate for field in assessment.fields)}"
                f"/{len(assessment.fields)} fields, {len(filtered.mutators)}/{len(packet.mutators)} mutators."
            )
            return filtered
        raise AssertionError("unreachable assessment retry state")

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
        notes = assessment_notes(packet)
        assessment_guidance = (
            render_prompt("mutators/accepted_reasons.txt")
            + notes + "\n"
        ) if notes else ""
        return render_prompt(
            "mutators/implementation.txt",
            protocol_lower=self.protocol_lower,
            packet_packet_type=packet.packet_type,
            packet_semantic_file=packet.semantic_file,
            packet_generated_file=packet.generated_file,
            assessment_guidance=assessment_guidance,
            semantic_tasks_packet=_semantic_tasks(packet),
        )

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
                + render_prompt("mutators/compile_repair.txt", diagnostics=diagnostics),
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

        # Dispatch packet-local workers before assessment. Each worker first
        # reviews its candidate fields, then creates only the accepted contracts.
        packets = [packet for packet in plan.packets if packet.packet_type in selected]

        def run_one(packet: PacketMutatorPlan, index: int) -> None:
            filtered = self._assess_mutator_packet(plan, packet, index)
            scoped_plan = plan if filtered is packet else MutatorPlan(
                plan.protocol, plan.dsl_dir, plan.output_dir,
                tuple(filtered if item.packet_type == packet.packet_type else item for item in plan.packets),
            )
            packet = filtered
            prepare_packet(scoped_plan, packet)
            if any(spec.hooks for spec in packet.mutators):
                self.call_agent(
                    self._mutator_prompt(packet),
                    f"Step 4.{index + 1}: Mutator Generation for {packet.packet_type}",
                    agent_graph=self._mutator_agent(scoped_plan, packet),
                )
            self._compile_mutator_with_repairs(scoped_plan, packet)

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [
                executor.submit(run_logged_task, f"Mutator {packet.packet_type}", run_one, packet, index)
                for index, packet in enumerate(packets)
            ]
            for future in as_completed(futures):
                future.result()

    def _run_mutator_validation(
        self, plan: MutatorPlan, packets: tuple[PacketMutatorPlan, ...],
        *, reuse: bool = False, filtered: bool = False,
    ) -> tuple[MutatorIssue, ...]:
        directory = plan.output_dir.parent / "mutator_test_logs"
        saved = directory / "results.txt"
        if reuse:
            # Standalone test runs save summary.log; pipeline runs additionally
            # save the process output, including failed-command diagnostics.
            source = saved if saved.is_file() else directory / "summary.log"
            try:
                output = source.read_text(encoding="utf-8")
            except OSError as error:
                raise RuntimeError(
                    "No saved mutator verification report is available. Rerun Step 5 without skipping verification."
                ) from error
        else:
            command = ["./tests/peach_mutator/run_peach_mutator_test.sh", self.protocol_lower, self.seed_dir]
            if filtered:
                names = sorted(spec.class_name for packet in packets for spec in packet.mutators)
                command.append(",".join(names))
                UI.dim("Retesting affected packets: " + ", ".join(packet.packet_type for packet in packets))
            process = UI.run_with_live_output(command, title="Running Mutator Tests")
            output = (process.stdout or "") + (process.stderr or "")
            directory.mkdir(parents=True, exist_ok=True)
            if process.returncode != 0:
                output = f"{COMMAND_FAILURE_PREFIX} (exit {process.returncode}).\n{output}"
            saved.write_text(output, encoding="utf-8")
            if process.returncode != 0:
                raise RuntimeError(output)
        results = parse_report(output)
        validate_report(results, packets)
        return collect_issues(results, directory)

    def _repair_mutator_packets(
        self, plan: MutatorPlan, packets: tuple[PacketMutatorPlan, ...], issues: tuple[MutatorIssue, ...],
    ) -> None:
        by_name = {issue.result.name: issue for issue in issues}
        # Check every issue before starting agents; generated contracts and SDK
        # failures cannot be repaired by editing a semantic partial class.
        for packet in packets:
            for spec in packet.mutators:
                if spec.class_name in by_name:
                    require_semantic_repair(spec, by_name[spec.class_name])

        def repair(packet: PacketMutatorPlan) -> None:
            evidence = "\n\n".join(
                f"Mutator {spec.class_name}:\n{by_name[spec.class_name].evidence}"
                for spec in packet.mutators if spec.class_name in by_name
            )
            prompt = self._mutator_prompt(packet) + render_prompt(
                "mutators/validation_repair.txt",
                packet_semantic_file=packet.semantic_file,
                evidence=evidence,
            )
            self.call_agent(prompt, f"Step 5: Fix Mutators for {packet.packet_type}",
                            agent_graph=self._mutator_agent(plan, packet))
            self._compile_mutator_with_repairs(plan, packet, step_title="Step 5")

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(run_logged_task, f"Mutator fix {packet.packet_type}", repair, packet)
                       for packet in packets]
            for future in as_completed(futures):
                future.result()

    def step_5_mutator_validation_and_fix(self) -> None:
        UI.title("Step 5: Mutator Validation & Fix")
        if not self.state.get("packet_types"):
            UI.warn("packet_types is empty. Step 5 has no mutators to validate.")
            return
        ensure_mutator_sdk()
        plan = self._mutator_plan(active=True)
        if not any(packet.mutators for packet in plan.packets):
            UI.dim("No mutators retained in the active manifest; skipping validation.")
            return
        packets = plan.packets
        reuse = ask_skip_verification("Mutator Validation")
        filtered = False
        while True:
            issues = self._run_mutator_validation(plan, packets, reuse=reuse, filtered=filtered)
            reuse = False
            if not issues:
                UI.success("Mutator validation complete: no [N] results.")
                return
            UI.warn("Unacceptable mutator results:\n" + "\n".join(issue.result.summary() for issue in issues))
            action = ask_mutator_failure_action()
            if action == "exit":
                raise RuntimeError("Mutator validation was stopped before [N] mutators were repaired or disabled.")
            names = sorted(issue.result.name for issue in issues)
            affected = {packet_for_class(plan, name).packet_type for name in names}
            packets = tuple(packet for packet in plan.packets if packet.packet_type in affected)
            if action == "repair":
                self._repair_mutator_packets(plan, packets, issues)
            elif action == "disable":
                plan = disable_mutators(plan, names)
                packets = tuple(packet for packet in plan.packets if packet.packet_type in affected)
                for packet in packets:
                    result = _build_packet(plan, packet)
                    if not result["ok"]:
                        raise RuntimeError(result["message"])
                UI.success("Disabled mutators: " + ", ".join(names))
            else:
                raise RuntimeError(f"Unknown mutator failure action: {action}")
            filtered = True
