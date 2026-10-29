from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from core.mutator_assessment import (
    SEMANTIC_VALUE_CRITERION, assessment_notes, assessment_path,
    assessment_prompt, filter_packet, parse_assessment, save_assessment,
)
from core.mutator_codegen import (
    MutatorPlan, MutatorSpec, PacketMutatorPlan, load_mutator_plan, prepare_packet, render_generated,
)


class MutatorAssessmentTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        specs = tuple(MutatorSpec(
            packet_type="request", path=f"Root.packet_union.request.{field}",
            operation=operation, class_name=f"Demo{operation}{field}", namespace="Demo",
            base_class="LLMMutator", expression="true", empty_branch_name=None,
            description="candidate", hooks=(), runtime_type="Number", kind="int",
            dsl_path=field, condition=None, source_path=None,
        ) for field, operation in (("a", "Add"), ("a", "Mutate"), ("b", "Mutate")))
        self.packet = PacketMutatorPlan("request", root / "Demo.g.cs", root / "Demo.semantic.cs",
                                        root / "out/Demo.dll", specs)
        self.plan = MutatorPlan("demo", root / "dsl", root, (self.packet,))

    def report(self, *, generate_a: bool = True, generate_b: bool = False) -> dict[str, object]:
        return {"decisions": [[index, generate,
                               "specific protocol relationship" if generate else "baseline already covers this"]
                              for index, generate in enumerate((generate_a, generate_b))]}

    def test_field_decision_filters_all_operations_and_persists_before_sources(self) -> None:
        assessment = parse_assessment(json.dumps(self.report()), self.packet)
        packet = filter_packet(self.packet, assessment)
        self.assertEqual([spec.class_name for spec in packet.mutators], ["DemoAdda", "DemoMutatea"])
        path = save_assessment(self.plan, self.packet, assessment)
        data = json.loads(path.read_text())
        self.assertEqual(path, assessment_path(self.packet))
        self.assertEqual(data["criterion"], SEMANTIC_VALUE_CRITERION)
        self.assertNotIn("budget_seconds", data)
        self.assertEqual(data["fields"][0]["operations"], ["Add", "Mutate"])
        self.assertFalse(data["fields"][1]["generate"])
        self.assertIn("request.a: specific protocol relationship", assessment_notes(packet))
        self.assertNotIn("request.b", assessment_notes(packet))
        self.assertFalse(self.packet.generated_file.exists())
        self.assertFalse(self.packet.semantic_file.exists())
        self.assertEqual(len(self.packet.mutators), 3)

    def test_all_skipped_and_all_retained_are_valid(self) -> None:
        rejected = parse_assessment(json.dumps(self.report(generate_a=False)), self.packet)
        self.assertEqual(filter_packet(self.packet, rejected).mutators, ())
        retained = parse_assessment(json.dumps(self.report(generate_b=True)), self.packet)
        self.assertIs(filter_packet(self.packet, retained), self.packet)

    def test_filtered_and_empty_contracts_round_trip_through_managed_manifest(self) -> None:
        for keep_a in (True, False):
            with self.subTest(keep_a=keep_a):
                assessment = parse_assessment(json.dumps(self.report(generate_a=keep_a)), self.packet)
                packet = filter_packet(self.packet, assessment)
                plan = MutatorPlan(self.plan.protocol, self.plan.dsl_dir, self.plan.output_dir, (packet,))
                prepare_packet(plan, packet)
                loaded = load_mutator_plan(plan.output_dir).packets[0]
                self.assertEqual([spec.class_name for spec in loaded.mutators],
                                 [spec.class_name for spec in packet.mutators])
                self.assertEqual(packet.generated_file.read_text(), render_generated(loaded))

    def test_missing_duplicate_or_invalid_ids_are_rejected(self) -> None:
        for case in ("missing", "duplicate", "foreign", "negative", "bool", "string"):
            with self.subTest(case=case):
                data = self.report()
                fields = data["decisions"]
                assert isinstance(fields, list)
                if case == "missing": fields.pop()
                elif case == "duplicate": fields.append(fields[0])
                elif case == "foreign": fields[0][0] = 100
                elif case == "negative": fields[0][0] = -1
                elif case == "bool": fields[0][0] = True
                else: fields[0][0] = "0"
                with self.assertRaises(ValueError):
                    parse_assessment(json.dumps(data), self.packet)

    def test_decisions_require_boolean_and_nonempty_reason(self) -> None:
        for index, value in ((1, "true"), (1, 1), (2, ""), (2, None)):
            with self.subTest(index=index, value=value):
                data = self.report()
                fields = data["decisions"]
                assert isinstance(fields, list)
                fields[0][index] = value
                with self.assertRaises(ValueError):
                    parse_assessment(json.dumps(data), self.packet)

    def test_fenced_json_is_accepted_but_trailing_text_is_not(self) -> None:
        text = json.dumps(self.report())
        self.assertEqual(len(parse_assessment(f"```json\n{text}\n```", self.packet).fields), 2)
        with self.assertRaises(ValueError):
            parse_assessment(text + " All done", self.packet)

    def test_prompt_is_compact_and_contains_criterion_and_baseline(self) -> None:
        prompt = assessment_prompt(self.plan, self.packet)
        self.assertIn(SEMANTIC_VALUE_CRITERION, prompt)
        self.assertIn("numeric random/variance/edge cases", prompt)
        self.assertIn("candidate id exactly once", prompt)
        self.assertIn("ONE brief", prompt)
        self.assertIn("0|a|Add,Mutate", prompt)
        self.assertIn("1|b|Mutate", prompt)
        self.assertNotIn("budget_seconds", prompt)


if __name__ == "__main__":
    unittest.main()
