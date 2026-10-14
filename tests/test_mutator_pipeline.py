from __future__ import annotations

import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, cast
from unittest.mock import Mock, patch

from core.agent_types import AgentGraph, AgentResponse
from core.mutator_codegen import MutatorPlan, PacketMutatorPlan
from core.state import new_usage_bucket
from core.tool_result import DotNetBuildData, ToolResult, tool_success
from core.tools import get_tools
from pipeline.peach_steps.mutators import MutatorSteps


_TARGET = "pipeline.peach_steps.mutators"


class _Harness(MutatorSteps):
    def __init__(self, plan: MutatorPlan) -> None:
        self.protocol_lower = "demo"
        self.protocol_upper = "DEMO"
        self.seed_dir = "seeds/demo"
        self.state = {
            "packet_types": [packet.packet_type for packet in plan.packets],
            "constraints": "", "data_type_analysis": {}, "current_step_index": 0,
            "token_usage_total": new_usage_bucket(), "token_usage_by_step": {},
        }
        self.retriever = Mock()
        self.agent_config = Mock()
        self.plan = plan
        self.prompts: list[str] = []

    def _mutator_plan(self, *, active: bool = False) -> MutatorPlan:
        return self.plan

    def call_agent(self, prompt: str, title: str, *, agent_graph: AgentGraph) -> AgentResponse:
        self.prompts.append(prompt)
        return {"messages": []}

    def fix_verify_loop(
        self, title: str, verify_fn: Callable[[], tuple[bool, str]],
        fix_fn: Callable[[str, str | None], None],
    ) -> bool:
        success, output = verify_fn()
        if not success:
            fix_fn(output, None)
            success, _ = verify_fn()
        return success


class MutatorPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.output = self.root / "Mutators"
        self.output.mkdir()
        self.dsl = self.root / "datamodel_dsl"
        self.dsl.mkdir()
        (self.dsl / "shared_model.py").write_text("# schema", encoding="utf-8")
        self.packet = self.make_packet("request", ("DemoMutateValue", "DemoMutateOther"))
        self.plan = cast(MutatorPlan, SimpleNamespace(
            protocol="demo", output_dir=self.output, dsl_dir=self.dsl,
            packets=(self.packet,), sdk="modern",
        ))
        self.pipeline = _Harness(self.plan)

    def make_packet(self, packet_type: str, class_names: tuple[str, ...]) -> PacketMutatorPlan:
        specs = tuple(SimpleNamespace(
            class_name=name, packet_type=packet_type,
            path=f"demo_packet_array.packets[].{packet_type}.value", dsl_path="value",
            namespace="Peach.LLM.Generated.Mutators.DEMO.Request",
            operation="Mutate", runtime_type="Number", description="Mutable field",
            hooks=(SimpleNamespace(name="MutateCore", signature="private void MutateCore(DataElement obj)"),),
        ) for name in class_names)
        packet = cast(PacketMutatorPlan, SimpleNamespace(
            packet_type=packet_type, generated_file=self.output / f"{packet_type}.g.cs",
            semantic_file=self.output / f"{packet_type}.semantic.cs",
            output_dll=self.output / "out" / f"{packet_type}.dll", mutators=specs,
        ))
        packet.generated_file.write_text("// generated", encoding="utf-8")
        packet.semantic_file.write_text("// semantic", encoding="utf-8")
        packet.output_dll.parent.mkdir(exist_ok=True)
        packet.output_dll.write_bytes(b"dll")
        return packet

    def patched(self) -> ExitStack:
        def select(items: list[str], protocol: str) -> list[str]:
            return items

        stack = ExitStack()
        stack.enter_context(patch(f"{_TARGET}.ensure_mutator_sdk"))
        stack.enter_context(patch(f"{_TARGET}.UI.title"))
        stack.enter_context(patch(f"{_TARGET}.UI.warn"))
        stack.enter_context(patch(f"{_TARGET}.UI.success"))
        stack.enter_context(patch(f"{_TARGET}.UI.dim"))
        stack.enter_context(patch(f"{_TARGET}.ask_select_types", side_effect=select))
        stack.enter_context(patch(f"{_TARGET}.ask_regenerate", return_value=False))
        stack.enter_context(patch(f"{_TARGET}.ask_skip_verification", return_value=False))
        stack.enter_context(patch(f"{_TARGET}.build_agent_graph", return_value=Mock()))
        stack.enter_context(patch(f"{_TARGET}.prepare_packet"))
        stack.enter_context(patch(f"{_TARGET}.set_active_packets"))
        return stack

    def test_generation_passes_semantic_scope_and_exact_required_signatures(self) -> None:
        with self.patched(), patch(f"{_TARGET}.packet_outputs_exist", return_value=False), \
            patch(f"{_TARGET}.compile_packet", return_value=subprocess.CompletedProcess([], 0, "", "")), \
            patch(f"{_TARGET}.build_agent_graph", return_value=Mock()) as agent:
            self.pipeline.step_4_mutator_generation()
        scope = agent.call_args.kwargs
        self.assertEqual(scope["write_files"], (self.packet.semantic_file,))
        self.assertIn(self.packet.generated_file, scope["read_files"])
        self.assertIn("Build_Mutator_DLL", scope["tool_names"])
        self.assertNotIn("Build_DotNet_DLL", scope["tool_names"])
        self.assertTrue(callable(scope["mutator_build"]))
        prompt = self.pipeline.prompts[0]
        self.assertIn("private void MutateCore(DataElement obj)", prompt)
        self.assertIn("A. Canonical form", prompt)
        self.assertIn("H. Random valid mix", prompt)
        self.assertIn("partial classes", prompt)
        self.assertNotIn("List ALL fields", prompt)

    def test_host_compilation_rejects_agent_claimed_success(self) -> None:
        with self.patched(), patch(f"{_TARGET}.packet_outputs_exist", return_value=False), \
            patch(f"{_TARGET}.compile_packet", return_value=subprocess.CompletedProcess([], 1, f"{self.packet.semantic_file}: error CS1002", "")):
            with self.assertRaisesRegex(RuntimeError, "three semantic repair attempts"):
                self.pipeline.step_4_mutator_generation()
        self.assertEqual(len(self.pipeline.prompts), 4)

    def test_missing_sdk_references_or_restore_failures_do_not_trigger_semantic_repair(self) -> None:
        for diagnostics in (
            "CSC : error CS0006: Metadata file 'Peach.Core.dll' could not be found",
            f"{self.packet.semantic_file}(2,2): error CS0009: Metadata file could not be opened",
            "Mutators.csproj : error NU1301: Unable to load package service index",
            "MSBUILD : error MSB1009: Project file does not exist",
            "The compiler executable is unavailable",
        ):
            with self.subTest(diagnostics=diagnostics), self.patched(), \
                patch(f"{_TARGET}.packet_outputs_exist", return_value=False), \
                patch(f"{_TARGET}.compile_packet", return_value=subprocess.CompletedProcess([], 1, diagnostics, "")):
                self.pipeline.prompts.clear()
                with self.assertRaisesRegex(RuntimeError, "generator or SDK"):
                    self.pipeline.step_4_mutator_generation()
                self.assertEqual(len(self.pipeline.prompts), 1)

    def test_missing_required_hook_can_be_repaired_at_generated_caller(self) -> None:
        diagnostics = f"{self.packet.generated_file}(20,9): error CS0103: The name 'MutateCore' does not exist"
        with self.patched(), patch(f"{_TARGET}.packet_outputs_exist", return_value=False), \
            patch(f"{_TARGET}.compile_packet", side_effect=[
                subprocess.CompletedProcess([], 1, diagnostics, ""),
                subprocess.CompletedProcess([], 0, "", ""),
            ]):
            self.pipeline.step_4_mutator_generation()
        self.assertEqual(len(self.pipeline.prompts), 2)

    def test_generated_compile_error_does_not_trigger_semantic_repair(self) -> None:
        with self.patched(), patch(f"{_TARGET}.packet_outputs_exist", return_value=False), \
            patch(f"{_TARGET}.compile_packet", return_value=subprocess.CompletedProcess(
                [], 1, f"{self.packet.generated_file}: error CS1061: bad SDK API", "")):
            with self.assertRaisesRegex(RuntimeError, "generator or SDK"):
                self.pipeline.step_4_mutator_generation()
        self.assertEqual(len(self.pipeline.prompts), 1)

    def test_fresh_subset_selection_records_only_selected_packets_as_active(self) -> None:
        other = self.make_packet("response", ("DemoMutateResponse",))
        self.plan = cast(MutatorPlan, SimpleNamespace(
            **{**vars(self.plan), "packets": (self.packet, other)},
        ))
        self.pipeline = _Harness(self.plan)
        with self.patched(), patch(f"{_TARGET}.packet_outputs_exist", return_value=False), \
            patch(f"{_TARGET}.ask_select_types", return_value=["request"]), \
            patch(f"{_TARGET}.set_active_packets") as active, \
            patch(f"{_TARGET}.prepare_packet") as prepare, \
            patch(f"{_TARGET}.compile_packet", return_value=subprocess.CompletedProcess([], 0, "", "")):
            self.pipeline.step_4_mutator_generation()
        active.assert_called_once_with(self.plan, ["request"])
        prepare.assert_called_once_with(self.plan, self.packet)

    def test_active_selection_keeps_existing_packets_skipped_by_user(self) -> None:
        reused = self.make_packet("response", ("DemoMutateResponse",))
        unselected = self.make_packet("notice", ("DemoMutateNotice",))
        self.plan = cast(MutatorPlan, SimpleNamespace(
            **{**vars(self.plan), "packets": (self.packet, reused, unselected)},
        ))
        self.pipeline = _Harness(self.plan)

        def exists(plan: MutatorPlan, packet: PacketMutatorPlan) -> bool:
            return packet.packet_type == "response"

        with self.patched(), patch(f"{_TARGET}.packet_outputs_exist", side_effect=exists), \
            patch(f"{_TARGET}.ask_select_types", return_value=["request"]), \
            patch(f"{_TARGET}.set_active_packets") as active, \
            patch(f"{_TARGET}.prepare_packet") as prepare, \
            patch(f"{_TARGET}.compile_packet", return_value=subprocess.CompletedProcess([], 0, "", "")):
            self.pipeline.step_4_mutator_generation()
        active.assert_called_once_with(self.plan, ["request", "response"])
        prepare.assert_called_once_with(self.plan, self.packet)

    def test_existing_outputs_remain_reusable_when_user_skips_regeneration(self) -> None:
        self.packet.semantic_file.write_text("// modified semantic code", encoding="utf-8")
        (self.dsl / "shared_model.py").write_text("# modified schema", encoding="utf-8")
        with self.patched(), patch(f"{_TARGET}.packet_outputs_exist", return_value=True), \
            patch(f"{_TARGET}.ask_regenerate", return_value=False) as regenerate, \
            patch(f"{_TARGET}.compile_packet") as compile_packet, \
            patch(f"{_TARGET}.set_active_packets") as active:
            self.pipeline.step_4_mutator_generation()
        regenerate.assert_called_once_with("mutators for request", "demo")
        compile_packet.assert_not_called()
        active.assert_called_once_with(self.plan, ["request"])
        self.assertFalse(self.pipeline.prompts)

    def test_validation_loads_active_manifest_contract_without_rebuilding_dsl(self) -> None:
        other = self.make_packet("response", ("DemoMutateResponse",))
        self.plan = cast(MutatorPlan, SimpleNamespace(
            **{**vars(self.plan), "packets": (self.packet, other)},
        ))
        self.pipeline = _Harness(self.plan)
        active_plan = cast(MutatorPlan, SimpleNamespace(
            **{**vars(self.plan), "packets": (self.packet,)},
        ))
        output = "Mutator Test Results:\n[PASS][100/100] DemoMutateValue\n[PASS][100/100] DemoMutateOther\n"
        with self.patched(), patch.object(_Harness, "_mutator_plan", getattr(MutatorSteps, "_mutator_plan")), \
            patch(f"{_TARGET}.load_mutator_plan", return_value=active_plan) as load, \
            patch(f"{_TARGET}.build_mutator_plan") as build, \
            patch(f"{_TARGET}.UI.run_with_live_output", return_value=subprocess.CompletedProcess([], 0, output, "")):
            self.pipeline.step_5_mutator_validation_and_fix()
        load.assert_called_once()
        build.assert_not_called()

    def test_validation_rejects_active_packet_absent_from_pipeline_state(self) -> None:
        orphan = self.make_packet("orphan", ("OrphanMutator",))
        active_plan = cast(MutatorPlan, SimpleNamespace(
            **{**vars(self.plan), "packets": (orphan,)},
        ))
        with self.patched(), patch.object(_Harness, "_mutator_plan", getattr(MutatorSteps, "_mutator_plan")), \
            patch(f"{_TARGET}.load_mutator_plan", return_value=active_plan), \
            patch(f"{_TARGET}.build_mutator_plan") as build:
            with self.assertRaisesRegex(RuntimeError, "absent from pipeline state"):
                self.pipeline.step_5_mutator_validation_and_fix()
        build.assert_not_called()

    def test_stopped_fix_loop_cannot_advance_to_next_step(self) -> None:
        with self.patched(), \
            patch.object(self.pipeline, "fix_verify_loop", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "stopped before"):
                self.pipeline.step_5_mutator_validation_and_fix()

    def test_compiler_or_docker_failure_cannot_pass_without_error_logs(self) -> None:
        with self.patched(), \
            patch(f"{_TARGET}.UI.run_with_live_output", return_value=subprocess.CompletedProcess([], 1, "Docker failed", "")):
            with self.assertRaisesRegex(RuntimeError, "validator command failed"):
                self.pipeline.step_5_mutator_validation_and_fix()
        self.assertFalse(self.pipeline.prompts)

    def test_incomplete_successful_process_is_an_infrastructure_failure(self) -> None:
        with self.patched(), \
            patch(f"{_TARGET}.UI.run_with_live_output", return_value=subprocess.CompletedProcess([], 0, "Found 0 mutators", "")):
            with self.assertRaisesRegex(RuntimeError, "did not complete"):
                self.pipeline.step_5_mutator_validation_and_fix()

    def test_all_skipped_mutators_cannot_pass_validation(self) -> None:
        output = "Unable to parse data: bad seed\nMutator Test Results:\n[SKIP][000/000] DemoMutateValue\n[SKIP][000/000] DemoMutateOther\n"
        with self.patched(), \
            patch(f"{_TARGET}.UI.run_with_live_output", return_value=subprocess.CompletedProcess([], 0, output, "")):
            with self.assertRaisesRegex(RuntimeError, "skipped every planned class"):
                self.pipeline.step_5_mutator_validation_and_fix()

    def test_partial_skips_are_accepted_but_unplanned_classes_are_rejected(self) -> None:
        output = "Mutator Test Results:\n[PASS][100/100] DemoMutateValue\n[SKIP][000/000] DemoMutateOther\n"
        with self.patched(), \
            patch(f"{_TARGET}.UI.run_with_live_output", return_value=subprocess.CompletedProcess([], 0, output, "")):
            self.pipeline.step_5_mutator_validation_and_fix()
        output += "[PASS][100/100] UnplannedMutator\n"
        with self.patched(), \
            patch(f"{_TARGET}.UI.run_with_live_output", return_value=subprocess.CompletedProcess([], 0, output, "")):
            with self.assertRaisesRegex(RuntimeError, "unplanned classes"):
                self.pipeline.step_5_mutator_validation_and_fix()

    def test_duplicate_validator_class_results_are_rejected(self) -> None:
        output = "Mutator Test Results:\n[ERROR] DemoMutateValue\n[PASS][100/100] DemoMutateValue\n[PASS][100/100] DemoMutateOther\n"
        with self.patched(), \
            patch(f"{_TARGET}.UI.run_with_live_output", return_value=subprocess.CompletedProcess([], 0, output, "")):
            with self.assertRaisesRegex(RuntimeError, "duplicate class names"):
                self.pipeline.step_5_mutator_validation_and_fix()

    def test_recrack_fail_is_reported_without_semantic_repair(self) -> None:
        output = "Mutator Test Results:\n[FAIL][001/100] DemoMutateValue\n[PASS][100/100] DemoMutateOther\n"
        with self.patched(), \
            patch(f"{_TARGET}.UI.run_with_live_output", return_value=subprocess.CompletedProcess([], 0, output, "")):
            self.pipeline.step_5_mutator_validation_and_fix()
        self.assertFalse(self.pipeline.prompts)

    def test_error_logs_are_grouped_per_semantic_file_and_retest_whole_packet(self) -> None:
        error_dir = self.root / "mutator_test_logs" / "error"
        error_dir.mkdir(parents=True)
        for name in ("DemoMutateValue", "DemoMutateOther"):
            (error_dir / f"{name}.log").write_text(f"at {name}.MutateCore(DataElement obj): overflow", encoding="utf-8")
        calls: list[list[str]] = []

        def run(cmd: list[str], *, title: str) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            if len(calls) == 1:
                output = "Mutator Test Results:\n[ERROR] DemoMutateValue\n[ERROR] DemoMutateOther\n"
            else:
                for path in error_dir.glob("*.log"):
                    path.unlink()
                output = "Mutator Test Results:\n[PASS][100/100] DemoMutateValue\n[PASS][100/100] DemoMutateOther\n"
            return subprocess.CompletedProcess(cmd, 0, output, "")

        with self.patched(), \
            patch(f"{_TARGET}.packet_for_class", return_value=self.packet), \
            patch(f"{_TARGET}.UI.run_with_live_output", side_effect=run), \
            patch(f"{_TARGET}.compile_packet", return_value=subprocess.CompletedProcess([], 0, "", "")):
            self.pipeline.step_5_mutator_validation_and_fix()
        self.assertEqual(len(self.pipeline.prompts), 1)
        self.assertIn("DemoMutateValue", self.pipeline.prompts[0])
        self.assertIn("DemoMutateOther", self.pipeline.prompts[0])
        self.assertEqual(calls[1][-1], "DemoMutateOther,DemoMutateValue")

    def test_unknown_error_log_is_rejected(self) -> None:
        error_dir = self.root / "mutator_test_logs" / "error"
        error_dir.mkdir(parents=True)
        (error_dir / "UnknownMutator.log").write_text("error", encoding="utf-8")
        output = "Mutator Test Results:\n[PASS][100/100] DemoMutateValue\n[PASS][100/100] DemoMutateOther\n"
        with self.patched(), \
            patch(f"{_TARGET}.packet_for_class", side_effect=KeyError("unknown")), \
            patch(f"{_TARGET}.UI.run_with_live_output", return_value=subprocess.CompletedProcess([], 0, output, "")):
            with self.assertRaisesRegex(RuntimeError, "Cannot route"):
                self.pipeline.step_5_mutator_validation_and_fix()

    def test_generated_constructor_exception_is_not_sent_to_semantic_agent(self) -> None:
        error_dir = self.root / "mutator_test_logs" / "error"
        error_dir.mkdir(parents=True)
        (error_dir / "DemoMutateValue.log").write_text(
            "at DemoMutateValue..ctor (Peach.Core.Dom.DataElement obj): invalid argument", encoding="utf-8",
        )
        output = "Mutator Test Results:\n[ERROR] DemoMutateValue\n[PASS][100/100] DemoMutateOther\n"
        with self.patched(), \
            patch(f"{_TARGET}.packet_for_class", return_value=self.packet), \
            patch(f"{_TARGET}.UI.run_with_live_output", return_value=subprocess.CompletedProcess([], 0, output, "")):
            with self.assertRaisesRegex(RuntimeError, "generator/SDK"):
                self.pipeline.step_5_mutator_validation_and_fix()
        self.assertFalse(self.pipeline.prompts)

    def test_skip_verification_reuses_saved_results_without_source_checks(self) -> None:
        output = "Mutator Test Results:\n[PASS][100/100] DemoMutateValue\n[PASS][100/100] DemoMutateOther\n"
        results_dir = self.root / "mutator_test_logs"
        results_dir.mkdir()
        (results_dir / "results.txt").write_text(output, encoding="utf-8")
        self.packet.semantic_file.write_text("// edited after test", encoding="utf-8")
        self.packet.generated_file.write_text("// changed contract", encoding="utf-8")
        self.packet.output_dll.write_bytes(b"changed dll")
        (self.dsl / "shared_model.py").write_text("# changed schema", encoding="utf-8")
        with self.patched(), patch(f"{_TARGET}.ask_skip_verification", return_value=True), \
            patch(f"{_TARGET}.UI.run_with_live_output") as run:
            self.pipeline.step_5_mutator_validation_and_fix()
        run.assert_not_called()
        self.assertFalse((results_dir / "verification.json").exists())

    def test_skip_verification_requires_saved_complete_report(self) -> None:
        with self.patched(), patch(f"{_TARGET}.ask_skip_verification", return_value=True), \
            patch(f"{_TARGET}.UI.run_with_live_output") as run:
            with self.assertRaisesRegex(RuntimeError, "No saved mutator verification report"):
                self.pipeline.step_5_mutator_validation_and_fix()
        run.assert_not_called()
        results_dir = self.root / "mutator_test_logs"
        results_dir.mkdir()
        (results_dir / "results.txt").write_text("Docker failed", encoding="utf-8")
        with self.patched(), patch(f"{_TARGET}.ask_skip_verification", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "did not complete"):
                self.pipeline.step_5_mutator_validation_and_fix()

    def test_failed_command_report_cannot_pass_when_verification_is_skipped(self) -> None:
        output = "Mutator Test Results:\n[PASS][100/100] DemoMutateValue\n[PASS][100/100] DemoMutateOther\n"
        with self.patched(), \
            patch(f"{_TARGET}.UI.run_with_live_output", return_value=subprocess.CompletedProcess([], 1, output, "")):
            with self.assertRaisesRegex(RuntimeError, "validator command failed"):
                self.pipeline.step_5_mutator_validation_and_fix()
        with self.patched(), patch(f"{_TARGET}.ask_skip_verification", return_value=True), \
            patch(f"{_TARGET}.UI.run_with_live_output") as run:
            with self.assertRaisesRegex(RuntimeError, "failed validator command"):
                self.pipeline.step_5_mutator_validation_and_fix()
        run.assert_not_called()

    def test_saved_filtered_retest_cannot_replace_full_report_on_next_run(self) -> None:
        other = self.make_packet("response", ("DemoMutateResponse",))
        self.plan = cast(MutatorPlan, SimpleNamespace(
            **{**vars(self.plan), "packets": (self.packet, other)},
        ))
        self.pipeline = _Harness(self.plan)
        error_dir = self.root / "mutator_test_logs" / "error"
        error_dir.mkdir(parents=True)
        (error_dir / "DemoMutateValue.log").write_text(
            "at DemoMutateValue.MutateCore(DataElement obj): overflow", encoding="utf-8",
        )
        calls = [0]

        def run(cmd: list[str], *, title: str) -> subprocess.CompletedProcess[str]:
            calls[0] += 1
            if calls[0] == 1:
                output = "Mutator Test Results:\n[ERROR] DemoMutateValue\n[PASS][100/100] DemoMutateOther\n[PASS][100/100] DemoMutateResponse\n"
            else:
                (error_dir / "DemoMutateValue.log").unlink()
                output = "Mutator Test Results:\n[PASS][100/100] DemoMutateValue\n[PASS][100/100] DemoMutateOther\n"
            return subprocess.CompletedProcess(cmd, 0, output, "")

        with self.patched(), \
            patch(f"{_TARGET}.packet_for_class", return_value=self.packet), \
            patch(f"{_TARGET}.UI.run_with_live_output", side_effect=run), \
            patch(f"{_TARGET}.compile_packet", return_value=subprocess.CompletedProcess([], 0, "", "")):
            self.pipeline.step_5_mutator_validation_and_fix()
        with self.patched(), \
            patch(f"{_TARGET}.ask_skip_verification", return_value=True), \
            patch(f"{_TARGET}.UI.run_with_live_output") as process:
            with self.assertRaisesRegex(RuntimeError, "omitted planned classes: DemoMutateResponse"):
                self.pipeline.step_5_mutator_validation_and_fix()
        process.assert_not_called()

    def test_scoped_build_tool_has_no_path_parameters_or_generic_compiler(self) -> None:
        result = tool_success("compiled", "compiled", DotNetBuildData(
            source=str(self.packet.semantic_file), output=str(self.packet.output_dll), source_count=2,
        ))
        build = Mock(return_value=result)
        tools = {item.name: item for item in get_tools(
            "demo", read_files=(self.packet.generated_file, self.packet.semantic_file),
            write_files=(self.packet.semantic_file,), mutator_build=build,
        )}
        self.assertNotIn("Build_DotNet_DLL", tools)
        build_invoke = cast(Callable[[object], ToolResult[object]], getattr(tools["Build_Mutator_DLL"], "invoke"))
        write_invoke = cast(Callable[[object], ToolResult[object]], getattr(tools["Write_File"], "invoke"))
        patch_invoke = cast(Callable[[object], ToolResult[object]], getattr(tools["Apply_Patch"], "invoke"))
        self.assertEqual(build_invoke({}), result)
        build.assert_called_once_with()
        denied = write_invoke({"filepath": str(self.packet.generated_file), "content": "changed"})
        self.assertFalse(denied["ok"])
        denied_patch = patch_invoke({
            "filepath": str(self.packet.generated_file), "old_text": "// generated", "new_text": "changed",
        })
        self.assertFalse(denied_patch["ok"])
        self.assertEqual(self.packet.generated_file.read_text(), "// generated")


if __name__ == "__main__":
    unittest.main()
