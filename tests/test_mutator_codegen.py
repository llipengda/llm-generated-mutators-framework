"""DSL mutator targets, generated interfaces, and managed source ownership."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from core.mutator_codegen import (
    MANIFEST_NAME,
    MutatorPlan,
    PacketMutatorPlan,
    build_mutator_plan,
    compile_packet,
    disable_mutators,
    load_mutator_plan,
    manifest_packet_types,
    manifest_sources,
    packet_for_class,
    packet_outputs_exist,
    prepare_packet,
    render_generated,
    render_plan_yaml,
    render_semantic_stub,
    set_active_packets,
)


FAMILY = '''from peach_dsl import *
from shared_model import Header

class FirstPacket(Schema):
    tag = Int8(fixed(1))
    flags = Int8()
    optional = Optional[Int8]()
    conditional = Optional[Blob[2]](when=(flags & 4) != 0)
    items = Array[Header, Occurs(0, 4)]()
    exact = Array[Int8, 2]()
    immutable = Int8(0, mutable=False)
    choice = Optional[Union(empty=Int8(), optional_empty=Int16())]()
    matrix = Array[Array[Int8, 2](), 3]()

class SecondPacket(Schema):
    header = Header(value=String[2]("hi"))
'''


class MutatorCodegenTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="mutator-codegen-test-")
        self.root = Path(self.temporary.name)
        self.dsl = self.root / "datamodel_dsl"
        self.output = self.root / "Mutators"
        self.sdk = self.root / "sdk"
        self.dsl.mkdir()
        self.sdk.mkdir()
        (self.sdk / "Peach.LLM.dll").write_bytes(b"SDK-v1")
        (self.dsl / "shared_model.py").write_text('''from peach_dsl import *
class Header(Schema):
    tag = Int8(fixed(2))
    value = Int8()
''', encoding="utf-8")
        (self.dsl / "family_packets.py").write_text(FAMILY, encoding="utf-8")
        (self.dsl / "root.py").write_text('''from peach_dsl import *
from family_packets import FirstPacket, SecondPacket
@PacketUnion
class Packets(Schema):
    packet_union = Union(first=FirstPacket, second=SecondPacket)
class PacketArray(Schema):
    packets = Array[Packets, Occurs(1, 100)]()
ROOT = PacketArray
''', encoding="utf-8")
        manifest = {
            "protocol": "demo",
            "packet_groups": [{"packet_models": [
                {"packet_type": "first", "symbol": "FirstPacket"},
                {"packet_type": "second", "symbol": "SecondPacket"},
            ]}],
        }
        (self.dsl / "schema_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        self.sdk_patch = patch("core.mutator_codegen.peach_sdk_dir", return_value=self.sdk)
        self.sdk_patch.start()
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(self.sdk_patch.stop)

    def plan(self) -> MutatorPlan:
        return build_mutator_plan("demo", dsl_dir=self.dsl, output_dir=self.output)

    def fake_build(self, plan: MutatorPlan, packet: PacketMutatorPlan) -> None:
        prepare_packet(plan, packet)
        packet.semantic_file.write_text("// verified semantic implementation\n", encoding="utf-8")
        packet.output_dll.parent.mkdir(exist_ok=True)
        packet.output_dll.write_bytes(b"compiled-artifact")

    def test_combined_properties_and_rooted_targets(self) -> None:
        plan = self.plan()
        packet = plan.packets[0]
        targets: dict[str, set[str]] = {}
        for spec in packet.mutators:
            targets.setdefault(spec.path.rsplit(".packet_union.first.", 1)[-1], set()).add(spec.operation)
        self.assertEqual(targets["items"], {"Add", "Remove", "Repeat", "Mutate"})
        self.assertEqual(targets["exact"], {"Repeat", "Mutate"})
        self.assertEqual(targets["optional"], {"Add", "Remove", "Mutate"})
        self.assertEqual(targets["items[].value"], {"Mutate"})
        self.assertNotIn("tag", targets)
        self.assertNotIn("immutable", targets)
        self.assertNotIn("items[].tag", targets)
        self.assertNotIn("optional.empty", targets)
        self.assertNotIn("matrix[]", targets)
        self.assertIn("matrix[].item", targets)
        self.assertTrue(all(".packets[].packet_union.first." in spec.path for spec in packet.mutators))
        self.assertTrue(all(".packet_union.second." in spec.path for spec in plan.packets[1].mutators))
        self.assertEqual([spec.runtime_type for spec in plan.packets[1].mutators if spec.path.endswith(".value")], ["String"])

    def test_derived_fields_are_skipped_but_content_and_condition_sources_remain(self) -> None:
        family = self.dsl / "family_packets.py"
        family.write_text(FAMILY.replace(
            "    flags = Int8()",
            "    length = Int16()\n    content = Blob[length]()\n"
            "    count = Int8()\n    counted = Array[Int8, count]()\n"
            "    flags = Int8()",
        ), encoding="utf-8")
        packet = self.plan().packets[0]
        targets = {spec.path.rsplit(".packet_union.first.", 1)[-1] for spec in packet.mutators}
        self.assertNotIn("length", targets)
        self.assertNotIn("count", targets)
        self.assertTrue({"content", "counted", "counted[]", "flags", "conditional"} <= targets)
        self.assertEqual(
            {spec.operation for spec in packet.mutators if spec.path.endswith(".counted")},
            {"Add", "Remove", "Repeat", "Mutate"},
        )

    def test_structural_selectors_use_compiler_empty_branch_and_sdk_checks(self) -> None:
        plan = self.plan()
        packet = plan.packets[0]
        choice_add = next(spec for spec in packet.mutators if spec.operation == "Add" and spec.path.endswith(".choice"))
        self.assertEqual(choice_add.empty_branch_name, "optional_optional_empty")
        self.assertIn('LLMAddMutator.CanApply(obj, @"optional_optional_empty")', choice_add.expression)
        emitted = render_generated(packet)
        self.assertIn('base(obj, @"optional_optional_empty")', emitted)
        conditional_add = next(spec for spec in packet.mutators if spec.operation == "Add" and spec.path.endswith(".conditional"))
        self.assertEqual(conditional_add.runtime_type, "Optional")
        self.assertIsNotNone(conditional_add.condition)
        self.assertTrue(conditional_add.source_path and conditional_add.source_path.endswith(".flags"))
        self.assertIn("LLMAddMutator.CanApply(obj)", conditional_add.expression)
        self.assertIn("protected override partial void UpdatePresenceSource", emitted)

    def test_emission_is_deterministic_and_requires_semantic_implementation(self) -> None:
        first = self.plan()
        second = self.plan()
        self.assertEqual(first, second)
        classes = [spec.class_name for packet in first.packets for spec in packet.mutators]
        self.assertEqual(len(classes), len(set(classes)))
        for packet in first.packets:
            source = render_generated(packet)
            stub = render_semantic_stub(packet)
            self.assertIn("=> MutateCore(obj);", source)
            self.assertNotIn("partial void MutateCore", source)
            self.assertIn("// REQUIRED: private void MutateCore(DataElement obj)", stub)
            self.assertNotIn("throw new NotImplementedException", stub)
            self.assertNotIn("private void MutateCore(DataElement obj) {", stub)
            for spec in packet.mutators:
                self.assertIn(f"public partial class {spec.class_name} : {spec.base_class}", source)
                self.assertEqual(packet_for_class(first, spec.class_name), packet)
        with self.assertRaisesRegex(ValueError, "absent"):
            packet_for_class(first, "UnplannedClass")

    def test_unknown_packet_rejected_and_selection_preserves_actual_names(self) -> None:
        selected = build_mutator_plan("demo", dsl_dir=self.dsl, output_dir=self.output, packet_types=["SECOND"])
        self.assertEqual([packet.packet_type for packet in selected.packets], ["second"])
        with self.assertRaisesRegex(ValueError, "absent"):
            build_mutator_plan("demo", dsl_dir=self.dsl, output_dir=self.output, packet_types=["missing"])

    def test_plan_preview_groups_identical_relative_targets_and_keeps_presence_sources(self) -> None:
        plan = self.plan()
        first = plan.packets[0]
        copied = replace(first, packet_type="copy", mutators=tuple(
            replace(spec, path=spec.path.replace(".first.", ".copy."),
                    source_path=spec.source_path.replace(".first.", ".copy.") if spec.source_path else None)
            for spec in first.mutators
        ))
        preview = render_plan_yaml(replace(plan, packets=(first, copied, plan.packets[1])))
        self.assertIn('packets: ["first", "copy"]', preview)
        self.assertIn('packets: ["second"]', preview)
        self.assertEqual(preview.count('"items": [Add, Remove, Repeat, Mutate]'), 1)
        self.assertIn('presence_sources:\n      "conditional": "flags"', preview)
        self.assertNotIn(str(self.root), preview)
        self.assertNotIn("MatchesPath", preview)
        self.assertNotIn("class_name", preview)
        self.assertFalse(self.output.exists())

    def test_readable_names_and_required_structural_hooks(self) -> None:
        packet = self.plan().packets[0]
        add = next(spec for spec in packet.mutators if spec.operation == "Add" and spec.path.endswith(".optional"))
        self.assertEqual(add.class_name, "DEMOFirstAddOptional")
        expected = {
            "Add": ["InitializeAddedContent"],
            "Remove": [],
            "Repeat": [],
            "Mutate": ["MutateCore"],
        }
        for spec in packet.mutators:
            names = expected[spec.operation].copy()
            if spec.operation in ("Add", "Remove") and spec.runtime_type == "Optional":
                names.append("UpdatePresenceSource")
            self.assertEqual([hook.name for hook in spec.hooks], names)
        stub = render_semantic_stub(packet)
        self.assertIn("// REQUIRED: protected override void InitializeAddedContent", stub)
        self.assertIn("// REQUIRED: protected override partial void UpdatePresenceSource", stub)

    def test_generated_mutate_requires_optional_target_presence(self) -> None:
        packet = self.plan().packets[0]
        for operation in ("Add", "Remove", "Mutate"):
            spec = next(spec for spec in packet.mutators
                        if spec.operation == operation and spec.path.endswith(".conditional"))
            generated = render_generated(replace(packet, mutators=(spec,)))
            self.assertEqual("obj is not Peach.LLM.Core.Dom.Optional optional || optional.Exists" in generated,
                             operation == "Mutate")

    def test_normalized_name_collisions_use_readable_numbers(self) -> None:
        family = self.dsl / "family_packets.py"
        family.write_text(FAMILY.replace("    flags = Int8()", "    flags = Int8()\n    foo_bar = Int8()\n    fooBar = Int8()\n    foobar = Int8()"), encoding="utf-8")
        packet = self.plan().packets[0]
        names = [spec.class_name for spec in packet.mutators if spec.path.endswith((".foo_bar", ".fooBar", ".foobar"))]
        self.assertEqual(names, ["DEMOFirstMutateFooBar", "DEMOFirstMutateFooBar_2", "DEMOFirstMutateFoobar_3"])
        self.assertEqual(packet, self.plan().packets[0])

    def test_reuse_requires_manifest_sources_and_dll_but_not_content_checks(self) -> None:
        plan = self.plan()
        packet = plan.packets[0]
        self.assertFalse(packet_outputs_exist(plan, packet))
        prepare_packet(plan, packet)
        self.assertFalse(packet_outputs_exist(plan, packet))
        packet.output_dll.parent.mkdir(exist_ok=True)
        packet.output_dll.write_bytes(b"unrecorded-existing-dll")
        self.assertTrue(packet_outputs_exist(plan, packet))
        packet.output_dll.write_bytes(b"wrong-dll")
        packet.semantic_file.write_text("// semantic changed", encoding="utf-8")
        packet.generated_file.write_text(render_generated(packet) + "// forbidden edit", encoding="utf-8")
        self.assertTrue(packet_outputs_exist(plan, packet))
        self.assertEqual(len(manifest_sources(self.output)), 2)
        for source in (packet.generated_file, packet.semantic_file, packet.output_dll):
            original = source.read_bytes()
            source.unlink()
            self.assertFalse(packet_outputs_exist(plan, packet))
            source.write_bytes(original)

    def test_dsl_sdk_and_custom_reference_changes_allow_reuse(self) -> None:
        plan = self.plan()
        packet = plan.packets[0]
        self.fake_build(plan, packet)
        shared = self.dsl / "shared_model.py"
        original = shared.read_text(encoding="utf-8")
        shared.write_text(original + "\n# new DSL revision\n", encoding="utf-8")
        changed = self.plan()
        self.assertTrue(packet_outputs_exist(changed, changed.packets[0]))
        shared.write_text(original, encoding="utf-8")
        (self.sdk / "Peach.LLM.dll").write_bytes(b"SDK-v2")
        changed = self.plan()
        self.assertTrue(packet_outputs_exist(changed, changed.packets[0]))
        (self.sdk / "Peach.LLM.dll").write_bytes(b"SDK-v1")
        self.assertEqual(len(manifest_sources(self.output)), 2)
        custom_dir = self.output.parent / "DataElements" / "out"
        custom_dir.mkdir(parents=True)
        (custom_dir / "DEMODataElements.dll").write_bytes(b"custom-runtime")
        changed = self.plan()
        self.assertTrue(packet_outputs_exist(changed, changed.packets[0]))

    def test_copied_fixer_dll_is_excluded_from_mutator_compilation(self) -> None:
        plan = self.plan()
        packet = plan.packets[0]
        self.fake_build(plan, packet)
        (self.sdk / "DEMOFixers.dll").write_bytes(b"Step 8 copies this into the SDK")
        (self.sdk / "UnrelatedMutators.dll").write_bytes(b"another protocol's artifact")
        changed = self.plan()
        self.assertTrue(packet_outputs_exist(changed, changed.packets[0]))
        with patch("core.mutator_codegen.compile_csharp", return_value=subprocess.CompletedProcess([], 1, "", "probe")) as compiler:
            compile_packet(changed, changed.packets[0])
        self.assertEqual(compiler.call_args.kwargs["excluded_reference_names"], ["DEMOFixers.dll", "UnrelatedMutators.dll"])

    def test_parallel_prepare_preserves_other_packet_entries_and_user_files(self) -> None:
        plan = self.plan()
        self.output.mkdir()
        user_source = self.output / "MyCustomMutator.cs"
        user_source.write_text("// user source", encoding="utf-8")
        legacy = self.output / "DEMOFirstMutators.cs"
        legacy.write_text("// old framework source", encoding="utf-8")
        def prepare(packet: PacketMutatorPlan) -> None:
            prepare_packet(plan, packet)

        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(prepare, plan.packets))
        sources = manifest_sources(self.output)
        self.assertEqual(len(sources), 4)
        self.assertEqual(user_source.read_text(), "// user source")
        self.assertFalse(legacy.exists())
        self.assertEqual(len(list((self.output / ".archive").glob("DEMOFirstMutators.cs.*.bak"))), 1)
        semantic = plan.packets[0].semantic_file
        semantic.write_text("// retained implementation", encoding="utf-8")
        prepare_packet(plan, plan.packets[0])
        self.assertEqual(semantic.read_text(), "// retained implementation")
        self.assertEqual(len(manifest_sources(self.output)), 4)

    def test_explicit_prepare_changed_class_set_archives_semantics(self) -> None:
        plan = self.plan()
        for packet in plan.packets:
            self.fake_build(plan, packet)
        self.assertEqual(len(manifest_sources(self.output)), 4)
        family = self.dsl / "family_packets.py"
        family.write_text(FAMILY.replace("    flags = Int8()", "    flags = Int8()\n    added = Int8()"), encoding="utf-8")
        changed = self.plan()
        packet = changed.packets[0]
        self.assertTrue(packet_outputs_exist(changed, packet))
        loaded = load_mutator_plan(self.output)
        self.assertEqual([spec.class_name for spec in loaded.packets[0].mutators], [spec.class_name for spec in plan.packets[0].mutators])
        self.assertTrue(any(spec.path.endswith(".added") for spec in packet.mutators))
        prepare_packet(changed, packet)
        self.assertIn("// REQUIRED:", packet.semantic_file.read_text())
        self.assertEqual(len(list((self.output / ".archive").glob("DEMOFirstMutators.semantic.cs.*.bak"))), 1)
        self.assertEqual(len(manifest_sources(self.output)), 4)
        packet.output_dll.unlink()
        self.assertFalse(packet_outputs_exist(changed, packet))
        self.assertEqual(len(manifest_sources(self.output)), 4)

    def test_manifest_cannot_reference_outside_or_duplicate_sources(self) -> None:
        plan = self.plan()
        for packet in plan.packets:
            prepare_packet(plan, packet)
        manifest_path = self.output / MANIFEST_NAME
        text = manifest_path.read_text(encoding="utf-8")
        manifest_path.write_text(text.replace("DEMOSecondMutators.g.cs", "DEMOFirstMutators.g.cs"), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            manifest_sources(self.output)
        manifest_path.write_text(text.replace('"DEMOFirstMutators.g.cs"', '"../Escape.g.cs"'), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "invalid managed"):
            manifest_sources(self.output)
        manifest_path.write_text(text, encoding="utf-8")
        outside = self.root / "Outside.g.cs"
        outside.write_text("private content", encoding="utf-8")
        plan.packets[0].generated_file.unlink()
        plan.packets[0].generated_file.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "escapes"):
            prepare_packet(plan, plan.packets[0])
        self.assertEqual(outside.read_text(), "private content")

    def test_active_selection_excludes_inactive_packets(self) -> None:
        plan = self.plan()
        for packet in plan.packets:
            self.fake_build(plan, packet)
        plan.packets[1].semantic_file.write_text("// stale inactive source", encoding="utf-8")
        set_active_packets(plan, ["FIRST"])
        self.assertEqual(manifest_packet_types(self.output), ["first"])
        self.assertEqual(set(manifest_sources(self.output)), {plan.packets[0].generated_file, plan.packets[0].semantic_file})
        self.assertTrue(plan.packets[1].semantic_file.exists())
        set_active_packets(plan, ["second", "first"])
        self.assertEqual(len(manifest_sources(self.output)), 4)
        set_active_packets(plan, [])
        with self.assertRaisesRegex(ValueError, "active"):
            manifest_packet_types(self.output)
        with self.assertRaisesRegex(ValueError, "absent"):
            set_active_packets(plan, ["missing"])

    def test_source_listing_does_not_read_dsl(self) -> None:
        plan = self.plan()
        prepare_packet(plan, plan.packets[0])
        self.assertEqual(len(manifest_sources(self.output)), 2)
        (self.dsl / "schema_manifest.json").unlink()
        (self.dsl / "root.py").unlink()
        self.assertEqual(len(manifest_sources(self.output)), 2)
        self.assertEqual(load_mutator_plan(self.output).packets[0].packet_type, "first")

    def test_disable_persists_per_class_and_preserves_other_mutators_and_semantics(self) -> None:
        plan = self.plan()
        for packet in plan.packets:
            self.fake_build(plan, packet)
        packet = plan.packets[0]
        name = packet.mutators[0].class_name
        semantic = packet.semantic_file.read_text(encoding="utf-8")
        other_generated = plan.packets[1].generated_file.read_text(encoding="utf-8")
        disabled = disable_mutators(plan, [name])
        loaded = load_mutator_plan(self.output)
        self.assertEqual(loaded.packets[0].mutators[0].enabled, False)
        self.assertTrue(all(spec.enabled for spec in loaded.packets[0].mutators[1:]))
        self.assertTrue(all(spec.enabled for spec in loaded.packets[1].mutators))
        self.assertEqual(packet.semantic_file.read_text(encoding="utf-8"), semantic)
        self.assertEqual(plan.packets[1].generated_file.read_text(encoding="utf-8"), other_generated)
        self.assertIn("supportedDataElement(DataElement obj) => false;", packet.generated_file.read_text(encoding="utf-8"))
        self.assertEqual(render_generated(disabled.packets[0]), packet.generated_file.read_text(encoding="utf-8"))
        # Packet selection and aggregate compilation retain disabled selectors.
        set_active_packets(loaded, [packet.packet_type])
        self.assertFalse(load_mutator_plan(self.output).packets[0].mutators[0].enabled)
        self.assertEqual(manifest_sources(self.output), sorted([packet.generated_file, packet.semantic_file]))
        with patch("core.mutator_codegen.compile_csharp", return_value=subprocess.CompletedProcess([], 1, "", "build failed")) as compiler:
            self.assertEqual(compile_packet(disabled, disabled.packets[0]).returncode, 1)
        compiler.assert_called_once()

    def test_disable_rejects_unknown_classes_and_modified_generated_sources(self) -> None:
        plan = self.plan()
        prepare_packet(plan, plan.packets[0])
        before = (self.output / MANIFEST_NAME).read_text(encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "absent from plan"):
            disable_mutators(plan, ["UnknownMutator"])
        packet = plan.packets[0]
        packet.generated_file.write_text("// edited contract", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "contract changed"):
            disable_mutators(plan, [packet.mutators[0].class_name])
        self.assertEqual((self.output / MANIFEST_NAME).read_text(encoding="utf-8"), before)
        self.assertEqual(packet.generated_file.read_text(encoding="utf-8"), "// edited contract")

    def test_prepare_rejects_manifest_owned_by_other_protocol(self) -> None:
        plan = self.plan()
        prepare_packet(plan, plan.packets[0])
        manifest_path = self.output / MANIFEST_NAME
        text = manifest_path.read_text(encoding="utf-8")
        manifest_path.write_text(text.replace('"protocol": "demo"', '"protocol": "other"'), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "protocol"):
            prepare_packet(plan, plan.packets[0])

    def test_staged_active_packets_have_no_sources_until_prepared(self) -> None:
        plan = self.plan()
        set_active_packets(plan, ["first", "second"])
        self.assertEqual(manifest_packet_types(self.output), ["first", "second"])
        prepare_packet(plan, plan.packets[0])
        with self.assertRaisesRegex(ValueError, "not been prepared"):
            manifest_sources(self.output)
        prepare_packet(plan, plan.packets[1])
        self.assertEqual(len(manifest_sources(self.output)), 4)

    def test_stored_plan_preserves_generated_contract_and_active_order(self) -> None:
        plan = self.plan()
        for packet in plan.packets:
            prepare_packet(plan, packet)
        set_active_packets(plan, ["second", "first"])
        loaded = load_mutator_plan(self.output)
        self.assertEqual([packet.packet_type for packet in loaded.packets], ["second", "first"])
        for packet in loaded.packets:
            self.assertEqual(render_generated(packet), packet.generated_file.read_text(encoding="utf-8"))
            original = next(original for original in plan.packets if original.packet_type == packet.packet_type)
            self.assertEqual([spec.hooks for spec in packet.mutators], [spec.hooks for spec in original.mutators])
        manifest = json.loads((self.output / MANIFEST_NAME).read_text(encoding="utf-8"))
        self.assertNotIn("fingerprint", manifest)
        self.assertTrue(all("built" not in record and "plan_fingerprint" not in record for record in manifest["packets"].values()))

    def test_original_v1_fingerprint_fields_are_ignored_and_dropped_on_write(self) -> None:
        plan = self.plan()
        self.fake_build(plan, plan.packets[0])
        manifest_path = self.output / MANIFEST_NAME
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        entry = manifest["packets"]["first"]
        entry["plan_fingerprint"] = "old-fingerprint"
        entry["built"] = {"malformed old build record": True}
        for cls in entry["classes"]:
            for key in list(cls):
                if key not in ("class_name", "operation", "path", "base_class", "hooks"):
                    del cls[key]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        self.assertTrue(packet_outputs_exist(plan, plan.packets[0]))
        loaded = load_mutator_plan(self.output)
        self.assertEqual(render_generated(loaded.packets[0]), plan.packets[0].generated_file.read_text(encoding="utf-8"))
        set_active_packets(plan, ["first"])
        entry = json.loads(manifest_path.read_text(encoding="utf-8"))["packets"]["first"]
        self.assertNotIn("plan_fingerprint", entry)
        self.assertNotIn("built", entry)

    def test_missing_active_source_is_rejected_without_requiring_packet_dll(self) -> None:
        plan = self.plan()
        packet = plan.packets[0]
        prepare_packet(plan, packet)
        self.assertEqual(len(manifest_sources(self.output)), 2)
        packet.semantic_file.unlink()
        with self.assertRaisesRegex(ValueError, "missing sources"):
            manifest_sources(self.output)
        with self.assertRaisesRegex(ValueError, "missing sources"):
            load_mutator_plan(self.output)

    def test_compile_uses_exact_partial_inputs_and_preserves_previous_dll_on_failure(self) -> None:
        plan = self.plan()
        packet = plan.packets[0]
        prepare_packet(plan, packet)
        user_source = self.output / "Unrelated.cs"
        user_source.write_text("invalid C#", encoding="utf-8")

        def build(sources: list[Path], output: Path, *, additional_references: list[Path], excluded_reference_names: list[str]) -> subprocess.CompletedProcess[str]:
            self.assertEqual(sources, [packet.generated_file, packet.semantic_file])
            self.assertEqual(additional_references, [])
            self.assertEqual(excluded_reference_names, [])
            output.parent.mkdir(exist_ok=True)
            output.write_bytes(b"actual-output")
            return subprocess.CompletedProcess(["compiler"], 0, "built", "")

        with patch("core.mutator_codegen.compile_csharp", side_effect=build):
            result = compile_packet(plan, packet)
        self.assertEqual(result.returncode, 0)
        self.assertTrue(packet_outputs_exist(plan, packet))
        packet.semantic_file.write_text("// new source", encoding="utf-8")
        with patch("core.mutator_codegen.compile_csharp", return_value=subprocess.CompletedProcess([], 1, "", "compile error")):
            result = compile_packet(plan, packet)
        self.assertEqual(result.returncode, 1)
        self.assertTrue(packet_outputs_exist(plan, packet))
        self.assertEqual(packet.output_dll.read_bytes(), b"actual-output")

    def test_compile_rejects_generated_contract_edits_and_missing_output(self) -> None:
        plan = self.plan()
        packet = plan.packets[0]
        prepare_packet(plan, packet)
        packet.generated_file.write_text("// edited", encoding="utf-8")
        with patch("core.mutator_codegen.compile_csharp") as compiler:
            result = compile_packet(plan, packet)
        self.assertEqual(result.returncode, 2)
        compiler.assert_not_called()
        packet.generated_file.unlink()
        prepare_packet(plan, packet)

        def build(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess([], 0, "", "")

        with patch("core.mutator_codegen.compile_csharp", side_effect=build):
            result = compile_packet(plan, packet)
        self.assertEqual(result.returncode, 1)
        self.assertIn("did not produce", result.stderr)
        self.assertFalse(packet_outputs_exist(plan, packet))


if __name__ == "__main__":
    unittest.main()
