from __future__ import annotations

import subprocess
import json
import threading
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, cast
from unittest.mock import Mock, patch

from langchain_core.messages import AIMessage

from core.agent_types import AgentGraph, AgentResponse
from core.mutator_codegen import MutatorPlan, PacketMutatorPlan
from core.mutator_assessment import assessment_path
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
            "data_type_analysis": {}, "current_step_index": 0,
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
            packets=(self.packet,),
        ))
        self.pipeline = _Harness(self.plan)

    def make_packet(self, packet_type: str, class_names: tuple[str, ...]) -> PacketMutatorPlan:
        specs = tuple(SimpleNamespace(
            class_name=name, packet_type=packet_type,
            path=f"demo_packet_array.packets[].{packet_type}.value", dsl_path="value",
            namespace="Peach.LLM.Generated.Mutators.DEMO.Request",
            operation="Mutate", runtime_type="Number", description="Mutable field", source_path=None,
            enabled=True,
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

        def retain(plan: MutatorPlan, packet: PacketMutatorPlan, index: int) -> PacketMutatorPlan:
            return packet

        stack = ExitStack()
        stack.enter_context(patch(f"{_TARGET}.ensure_mutator_sdk"))
        stack.enter_context(patch(f"{_TARGET}.UI.title"))
        stack.enter_context(patch(f"{_TARGET}.UI.warn"))
        stack.enter_context(patch(f"{_TARGET}.UI.success"))
        stack.enter_context(patch(f"{_TARGET}.UI.dim"))
        stack.enter_context(patch(f"{_TARGET}.ask_select_types", side_effect=select))
        stack.enter_context(patch(f"{_TARGET}.ask_regenerate", return_value=False))
        stack.enter_context(patch(f"{_TARGET}.ask_skip_verification", return_value=False))
        stack.enter_context(patch(f"{_TARGET}.ask_mutator_failure_action", return_value="repair"))
        stack.enter_context(patch(f"{_TARGET}.build_agent_graph", return_value=Mock()))
        stack.enter_context(patch(f"{_TARGET}.prepare_packet"))
        stack.enter_context(patch.object(
            _Harness, "_assess_mutator_packet", side_effect=retain,
        ))
        stack.enter_context(patch(f"{_TARGET}.set_active_packets"))
        return stack

    def assessment_response(self, packet: PacketMutatorPlan, retained: set[str]) -> AgentResponse:
        return {"messages": [AIMessage(content=json.dumps({
            "decisions": [[index, path in retained,
                           "unique protocol case" if path in retained else "random baseline is sufficient"]
                          for index, path in enumerate(dict.fromkeys(spec.path for spec in packet.mutators))],
        }))]}

    def test_packet_assessment_precedes_sources_and_only_retained_fields_are_generated(self) -> None:
        other_spec = cast(SimpleNamespace, self.packet.mutators[1])
        other_spec.path += "_other"
        self.packet.generated_file.unlink()
        events: list[str] = []
        response = self.assessment_response(self.packet, {self.packet.mutators[0].path})

        def invoke(prompt: str, title: str, *, agent_graph: AgentGraph) -> AgentResponse:
            if "Semantic Value Assessment" in title:
                self.assertFalse(self.packet.generated_file.exists())
                events.append("assessment")
                return response
            self.assertTrue(assessment_path(self.packet).is_file())
            self.assertNotIn("Class: DemoMutateOther", prompt)
            self.assertIn("Class: DemoMutateValue", prompt)
            events.append("semantic")
            return {"messages": []}

        def prepare(plan: MutatorPlan, packet: PacketMutatorPlan) -> None:
            self.assertEqual(events, ["assessment"])
            self.assertTrue(assessment_path(packet).is_file())
            self.assertEqual([spec.class_name for spec in packet.mutators], ["DemoMutateValue"])
            self.assertIn(packet, plan.packets)
            packet.generated_file.write_text("// accepted contract")
            events.append("prepare")

        with self.patched(), \
            patch.object(_Harness, "_assess_mutator_packet", getattr(MutatorSteps, "_assess_mutator_packet")), \
            patch.object(self.pipeline, "call_agent", side_effect=invoke), \
            patch(f"{_TARGET}.packet_outputs_exist", return_value=False), \
            patch(f"{_TARGET}.prepare_packet", side_effect=prepare), \
            patch(f"{_TARGET}.compile_packet", return_value=subprocess.CompletedProcess([], 0, "", "")), \
            patch(f"{_TARGET}.build_agent_graph", return_value=Mock()) as agents:
            self.pipeline.step_4_mutator_generation()
        self.assertEqual(events, ["assessment", "prepare", "semantic"])
        scope = agents.call_args_list[0].kwargs
        self.assertEqual(scope["write_files"], ())
        self.assertNotIn("Write_File", scope["tool_names"])
        self.assertNotIn("Build_Mutator_DLL", scope["tool_names"])
        self.assertNotIn(self.packet.generated_file, scope["read_files"])
        self.assertNotIn(self.packet.semantic_file, scope["read_files"])
        self.assertNotIn(assessment_path(self.packet), agents.call_args.kwargs["read_files"])

    def test_invalid_assessment_cannot_modify_existing_sources_or_start_generation(self) -> None:
        original = self.packet.generated_file.read_bytes()
        invalid = {"messages": [AIMessage(content='{"decisions":[]}')]}
        with self.patched(), \
            patch.object(_Harness, "_assess_mutator_packet", getattr(MutatorSteps, "_assess_mutator_packet")), \
            patch.object(self.pipeline, "call_agent", return_value=invalid) as invoke, \
            patch(f"{_TARGET}.packet_outputs_exist", return_value=False), \
            patch(f"{_TARGET}.prepare_packet") as prepare, \
            patch(f"{_TARGET}.compile_packet") as compile_packet:
            with self.assertRaisesRegex(RuntimeError, "failed before code generation"):
                self.pipeline.step_4_mutator_generation()
        self.assertEqual(invoke.call_count, 2)
        prepare.assert_not_called()
        compile_packet.assert_not_called()
        self.assertEqual(self.packet.generated_file.read_bytes(), original)
        self.assertFalse(assessment_path(self.packet).exists())

    def test_all_skipped_assessment_produces_empty_contract_without_semantic_agent(self) -> None:
        response = self.assessment_response(self.packet, set())
        with self.patched(), \
            patch.object(_Harness, "_assess_mutator_packet", getattr(MutatorSteps, "_assess_mutator_packet")), \
            patch.object(self.pipeline, "call_agent", return_value=response) as invoke, \
            patch(f"{_TARGET}.packet_outputs_exist", return_value=False), \
            patch(f"{_TARGET}.prepare_packet") as prepare, \
            patch(f"{_TARGET}.compile_packet", return_value=subprocess.CompletedProcess([], 0, "", "")):
            self.pipeline.step_4_mutator_generation()
        self.assertEqual(invoke.call_count, 1)
        self.assertEqual(prepare.call_args.args[1].mutators, ())
        self.assertTrue(assessment_path(self.packet).exists())

    def test_packet_workers_are_dispatched_before_their_assessments(self) -> None:
        other = self.make_packet("response", ("DemoMutateResponse",))
        self.plan = cast(MutatorPlan, SimpleNamespace(**{**vars(self.plan), "packets": (self.packet, other)}))
        self.pipeline = _Harness(self.plan)
        barrier = threading.Barrier(2, timeout=5)
        completed: set[str] = set()
        lock = threading.Lock()

        def assess(plan: MutatorPlan, packet: PacketMutatorPlan, index: int) -> PacketMutatorPlan:
            barrier.wait()
            with lock:
                completed.add(packet.packet_type)
            return packet

        def prepare(plan: MutatorPlan, packet: PacketMutatorPlan) -> None:
            with lock:
                self.assertIn(packet.packet_type, completed)

        with self.patched(), \
            patch.object(_Harness, "_assess_mutator_packet", side_effect=assess) as assess_packet, \
            patch(f"{_TARGET}.packet_outputs_exist", return_value=False), \
            patch(f"{_TARGET}.prepare_packet", side_effect=prepare), \
            patch(f"{_TARGET}.compile_packet", return_value=subprocess.CompletedProcess([], 0, "", "")):
            self.pipeline.step_4_mutator_generation()
        self.assertEqual(assess_packet.call_count, 2)
        self.assertEqual(completed, {"request", "response"})

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

    def test_missing_structural_hook_is_routed_to_semantic_repair(self) -> None:
        for code, name in (("CS0534", "InitializeAddedContent"), ("CS8795", "UpdatePresenceSource")):
            with self.subTest(code=code):
                spec = cast(SimpleNamespace, self.packet.mutators[0])
                original = spec.hooks
                spec.hooks = (SimpleNamespace(name=name, signature=f"protected override void {name}(DataElement obj)"),)
                diagnostics = f"{self.packet.generated_file}(20,9): error {code}: Missing implementation of '{name}'"
                self.pipeline.prompts.clear()
                with self.patched(), patch(f"{_TARGET}.packet_outputs_exist", return_value=False), \
                    patch(f"{_TARGET}.compile_packet", side_effect=[
                        subprocess.CompletedProcess([], 1, diagnostics, ""),
                        subprocess.CompletedProcess([], 0, "", ""),
                    ]):
                    self.pipeline.step_4_mutator_generation()
                self.assertEqual(len(self.pipeline.prompts), 2)
                spec.hooks = original

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

    def report(self, *rows: str) -> str:
        return "Mutator Test Results:\n" + "\n".join(rows) + "\n"

    def passed_report(self) -> str:
        return self.report(
            "[P] DemoMutateValue TOTAL=100 PASS=1 FAIL=49 ERROR=0 UNCHANGED=50",
            "[P] DemoMutateOther TOTAL=100 PASS=100 FAIL=0 ERROR=0 UNCHANGED=0",
        )

    def issue_report(self, *, errors: int = 0) -> str:
        return self.report(
            f"[N] DemoMutateValue TOTAL=100 PASS=0 FAIL=0 ERROR={errors} UNCHANGED={100-errors}",
            "[P] DemoMutateOther TOTAL=100 PASS=100 FAIL=0 ERROR=0 UNCHANGED=0",
        )

    def issue_log(self, category: str, name: str = "DemoMutateValue", text: str = "issue evidence") -> Path:
        directory = self.root / "mutator_test_logs" / category
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{name}.log"
        path.write_text(text, encoding="utf-8")
        return path

    def test_validation_loads_active_manifest_without_rebuilding_dsl(self) -> None:
        with self.patched(), patch.object(_Harness, "_mutator_plan", getattr(MutatorSteps, "_mutator_plan")), \
            patch(f"{_TARGET}.load_mutator_plan", return_value=self.plan) as load, \
            patch(f"{_TARGET}.build_mutator_plan") as build, \
            patch(f"{_TARGET}.UI.run_with_live_output", return_value=subprocess.CompletedProcess([], 0, self.passed_report(), "")):
            self.pipeline.step_5_mutator_validation_and_fix()
        load.assert_called_once()
        build.assert_not_called()

    def test_validation_rejects_active_packet_absent_from_pipeline_state(self) -> None:
        self.pipeline.state["packet_types"] = ["other"]
        with self.patched(), patch.object(_Harness, "_mutator_plan", getattr(MutatorSteps, "_mutator_plan")), \
            patch(f"{_TARGET}.load_mutator_plan", return_value=self.plan):
            with self.assertRaisesRegex(RuntimeError, "absent from pipeline state"):
                self.pipeline.step_5_mutator_validation_and_fix()

    def test_p_and_partial_skips_need_no_action(self) -> None:
        for output in (self.passed_report(), self.passed_report().replace(
            "[P] DemoMutateOther TOTAL=100 PASS=100 FAIL=0 ERROR=0 UNCHANGED=0", "[-] DemoMutateOther")):
            with self.subTest(output=output), self.patched(), \
                patch(f"{_TARGET}.ask_mutator_failure_action") as choose, \
                patch(f"{_TARGET}.UI.run_with_live_output", return_value=subprocess.CompletedProcess([], 0, output, "")):
                self.pipeline.step_5_mutator_validation_and_fix()
                choose.assert_not_called()
                self.assertFalse(self.pipeline.prompts)

    def test_n_cancellation_stops_for_error_and_zero_pass(self) -> None:
        for errors in (0, 100):
            with self.subTest(errors=errors):
                if errors:
                    self.issue_log("error")
                with self.patched(), patch(f"{_TARGET}.ask_mutator_failure_action", return_value="exit"), \
                    patch(f"{_TARGET}.UI.run_with_live_output", return_value=subprocess.CompletedProcess([], 0, self.issue_report(errors=errors), "")):
                    with self.assertRaisesRegex(RuntimeError, r"\[N\] mutators"):
                        self.pipeline.step_5_mutator_validation_and_fix()

    def test_failed_command_is_saved_and_cannot_be_reused(self) -> None:
        with self.patched(), patch(f"{_TARGET}.UI.run_with_live_output", return_value=subprocess.CompletedProcess([], 1, self.passed_report(), "runtime failed")):
            with self.assertRaisesRegex(RuntimeError, "validator command failed"):
                self.pipeline.step_5_mutator_validation_and_fix()
        with self.patched(), patch(f"{_TARGET}.ask_skip_verification", return_value=True), \
            patch(f"{_TARGET}.UI.run_with_live_output") as run:
            with self.assertRaisesRegex(RuntimeError, "failed validator command"):
                self.pipeline.step_5_mutator_validation_and_fix()
            run.assert_not_called()

    def test_repair_groups_n_classes_and_retests_whole_packet(self) -> None:
        error = self.issue_log("error", text="at DemoMutateValue.MutateCore(DataElement obj): overflow")
        self.issue_log("unchanged", "DemoMutateOther", "unchanged evidence")
        output = self.report(
            "[N] DemoMutateValue TOTAL=100 PASS=1 FAIL=0 ERROR=99 UNCHANGED=0",
            "[N] DemoMutateOther TOTAL=100 PASS=0 FAIL=0 ERROR=0 UNCHANGED=100",
        )
        calls: list[list[str]] = []

        def run(command: list[str], *, title: str) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            if len(calls) == 1:
                return subprocess.CompletedProcess(command, 0, output, "")
            error.unlink()
            return subprocess.CompletedProcess(command, 0, self.passed_report(), "")

        with self.patched(), patch(f"{_TARGET}.UI.run_with_live_output", side_effect=run), \
            patch(f"{_TARGET}.compile_packet", return_value=subprocess.CompletedProcess([], 0, "", "")):
            self.pipeline.step_5_mutator_validation_and_fix()
        self.assertEqual(len(self.pipeline.prompts), 1)
        self.assertIn("overflow", self.pipeline.prompts[0])
        self.assertIn("unchanged evidence", self.pipeline.prompts[0])
        self.assertIn("SetValue(rawValue)", self.pipeline.prompts[0])
        self.assertEqual(calls[1][-1], "DemoMutateOther,DemoMutateValue")

    def test_zero_pass_repairs_receive_fail_and_unchanged_logs(self) -> None:
        self.issue_log("fail", text="fail evidence")
        self.issue_log("unchanged", text="unchanged evidence")
        self.issue_log("fail", "DemoMutateOther", "accepted class evidence")
        output = self.issue_report().replace("FAIL=0 ERROR=0 UNCHANGED=100", "FAIL=50 ERROR=0 UNCHANGED=50")
        with self.patched(), patch(f"{_TARGET}.UI.run_with_live_output", side_effect=[
            subprocess.CompletedProcess([], 0, output, ""), subprocess.CompletedProcess([], 0, self.passed_report(), ""),
        ]), patch(f"{_TARGET}.compile_packet", return_value=subprocess.CompletedProcess([], 0, "", "")):
            self.pipeline.step_5_mutator_validation_and_fix()
        self.assertIn("fail evidence", self.pipeline.prompts[0])
        self.assertIn("unchanged evidence", self.pipeline.prompts[0])
        self.assertNotIn("accepted class evidence", self.pipeline.prompts[0])

    def test_disable_persists_and_rebuilds_only_affected_packet(self) -> None:
        def disable(plan: MutatorPlan, names: list[str]) -> MutatorPlan:
            cast(SimpleNamespace, self.packet.mutators[0]).enabled = False
            return plan

        output = self.issue_report()
        retest = output.replace("[N] DemoMutateValue TOTAL=100 PASS=0 FAIL=0 ERROR=0 UNCHANGED=100", "[-] DemoMutateValue")
        with self.patched(), patch(f"{_TARGET}.ask_mutator_failure_action", return_value="disable"), \
            patch(f"{_TARGET}.disable_mutators", side_effect=disable) as disabled, \
            patch(f"{_TARGET}.compile_packet", return_value=subprocess.CompletedProcess([], 0, "", "")) as build, \
            patch(f"{_TARGET}.UI.run_with_live_output", side_effect=[
                subprocess.CompletedProcess([], 0, output, ""), subprocess.CompletedProcess([], 0, retest, ""),
            ]):
            self.pipeline.step_5_mutator_validation_and_fix()
        disabled.assert_called_once_with(self.plan, ["DemoMutateValue"])
        build.assert_called_once_with(self.plan, self.packet)
        self.assertTrue(self.packet.mutators[1].enabled)
        self.assertFalse(self.pipeline.prompts)

    def test_generated_and_structural_errors_are_not_sent_to_semantic_agent(self) -> None:
        for structural in (False, True):
            with self.subTest(structural=structural):
                if structural:
                    cast(SimpleNamespace, self.packet.mutators[0]).hooks = ()
                self.issue_log("error", text="at DemoMutateValue..ctor (DataElement obj): failure")
                with self.patched(), patch(f"{_TARGET}.UI.run_with_live_output", return_value=subprocess.CompletedProcess([], 0, self.issue_report(errors=100), "")):
                    with self.assertRaisesRegex(RuntimeError, "generator/SDK"):
                        self.pipeline.step_5_mutator_validation_and_fix()
                self.assertFalse(self.pipeline.prompts)

    def test_saved_reports_and_standalone_summary_can_be_reused(self) -> None:
        directory = self.root / "mutator_test_logs"
        directory.mkdir()
        for filename in ("summary.log", "results.txt"):
            with self.subTest(filename=filename):
                path = directory / filename
                path.write_text(self.passed_report(), encoding="utf-8")
                with self.patched(), patch(f"{_TARGET}.ask_skip_verification", return_value=True), \
                    patch(f"{_TARGET}.UI.run_with_live_output") as run:
                    self.pipeline.step_5_mutator_validation_and_fix()
                    run.assert_not_called()
                path.unlink()

    def test_missing_saved_report_requires_verification(self) -> None:
        with self.patched(), patch(f"{_TARGET}.ask_skip_verification", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "No saved mutator verification report"):
                self.pipeline.step_5_mutator_validation_and_fix()

    def test_saved_filtered_report_cannot_replace_full_report(self) -> None:
        other = self.make_packet("response", ("DemoMutateResponse",))
        self.plan = cast(MutatorPlan, SimpleNamespace(**{**vars(self.plan), "packets": (self.packet, other)}))
        self.pipeline = _Harness(self.plan)
        first = self.issue_report() + "[P] DemoMutateResponse TOTAL=100 PASS=100 FAIL=0 ERROR=0 UNCHANGED=0\n"
        with self.patched(), patch(f"{_TARGET}.UI.run_with_live_output", side_effect=[
            subprocess.CompletedProcess([], 0, first, ""), subprocess.CompletedProcess([], 0, self.passed_report(), ""),
        ]) as run, patch(f"{_TARGET}.compile_packet", return_value=subprocess.CompletedProcess([], 0, "", "")):
            self.pipeline.step_5_mutator_validation_and_fix()
            self.assertEqual(run.call_args.args[0][-1], "DemoMutateOther,DemoMutateValue")
        with self.patched(), patch(f"{_TARGET}.ask_skip_verification", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "omitted planned classes: DemoMutateResponse"):
                self.pipeline.step_5_mutator_validation_and_fix()

    def test_all_fields_filtered_out_skips_validator(self) -> None:
        cast(SimpleNamespace, self.packet).mutators = ()
        with self.patched(), patch(f"{_TARGET}.UI.run_with_live_output") as run, \
            patch(f"{_TARGET}.ask_skip_verification") as choose:
            self.pipeline.step_5_mutator_validation_and_fix()
            run.assert_not_called()
            choose.assert_not_called()

    def test_all_skipped_rows_are_accepted_without_treating_them_as_n(self) -> None:
        output = self.report("[-] DemoMutateValue", "[-] DemoMutateOther")
        with self.patched(), patch(f"{_TARGET}.ask_mutator_failure_action") as choose, \
            patch(f"{_TARGET}.UI.run_with_live_output", return_value=subprocess.CompletedProcess([], 0, output, "")):
            self.pipeline.step_5_mutator_validation_and_fix()
            choose.assert_not_called()

    def test_repair_that_still_returns_n_requires_another_action(self) -> None:
        with self.patched(), patch(f"{_TARGET}.ask_mutator_failure_action", side_effect=["repair", "exit"]) as choose, \
            patch(f"{_TARGET}.UI.run_with_live_output", return_value=subprocess.CompletedProcess([], 0, self.issue_report(), "")) as run, \
            patch(f"{_TARGET}.compile_packet", return_value=subprocess.CompletedProcess([], 0, "", "")):
            with self.assertRaisesRegex(RuntimeError, r"\[N\] mutators"):
                self.pipeline.step_5_mutator_validation_and_fix()
        self.assertEqual(choose.call_count, 2)
        self.assertEqual(run.call_count, 2)

    def test_disable_build_failure_stops_before_retest(self) -> None:
        with self.patched(), patch(f"{_TARGET}.ask_mutator_failure_action", return_value="disable"), \
            patch(f"{_TARGET}.disable_mutators", return_value=self.plan), \
            patch(f"{_TARGET}.compile_packet", return_value=subprocess.CompletedProcess([], 1, "build failed", "")), \
            patch(f"{_TARGET}.UI.run_with_live_output", return_value=subprocess.CompletedProcess([], 0, self.issue_report(), "")) as run:
            with self.assertRaisesRegex(RuntimeError, "Compilation failed"):
                self.pipeline.step_5_mutator_validation_and_fix()
        run.assert_called_once()

    def test_action_menu_supports_choices_and_cancellation(self) -> None:
        from core.ui import ask_mutator_failure_action
        for response, expected in (("repair", "repair"), ("disable", "disable"), (None, "exit")):
            with self.subTest(response=response), patch("core.ui.questionary.select") as select:
                select.return_value.ask.return_value = response
                self.assertEqual(ask_mutator_failure_action(), expected)

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
