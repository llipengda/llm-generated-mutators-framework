import tempfile
import unittest
from pathlib import Path
from unittest import mock

from datamodel_diagnoser import (
    LogAnalysis,
    XmlIndex,
    add_llm_judgment,
    analyze,
    prepare_llm_report,
)


PIT = """<?xml version="1.0"?>
<Peach>
  <DataModel name="body_t">
    <Optional name="payload_optional" src="../header/flags" expression="value != 0">
      <Blob name="payload"/>
    </Optional>
    <Number name="trailer" size="8"/>
  </DataModel>
  <DataModel name="packet_t">
    <Choice name="packet_union">
      <Block name="data" ref="body_t"/>
    </Choice>
    <Number name="submessage_length" size="16"/>
  </DataModel>
</Peach>
"""


ENDIAN_LOG = """ |-+ Choice 'packet_union', Bytes: 0/28, Bits: 0/224
 | |-+ DataModel 'data', Bytes: 0/28, Bits: 0/224
 | | |-- Number 'submessage_id', Bytes: 0/28, Bits: 0/224
 | | |   Size: 1 bytes | 8 bits (Has Length)
 | | |   Value: 21 (0x15)
 | | |-- Number 'submessage_length', Bytes: 2/28, Bits: 16/224
 | | |   Size: 2 bytes | 16 bits (Has Length)
 | | |   Value: 6144 (0x1800)
 | | |-+ DataModel 'submessage_body', Bytes: 4/28, Bits: 32/224
 | | |   Size: 6144 bytes | 49152 bits (Has Length)
 | | | X (Length is 49152 bits but buffer only has 192 bits left.)
Failed to parse file 'big.raw': failed
Bytes:
15 00 00 18 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
"""


REFERENCE_AND_DIFF_LOG = """ |-+ Optional 'payload_optional', Bytes: 0/4, Bits: 0/32
Error evaluating condition for Optional 'Optional 'root.data.payload_optional'': Optional 'root.data.payload_optional': Referenced element '../header/flags' not found
Parsed bytes do not match original file for 'roundtrip.raw'
Original Bytes:
01 02 03 04
Parsed   Bytes:
01 02
"""


EOF_LOG = """ |-+ Choice 'packet_union', Bytes: 0/20, Bits: 0/160
 | |-+ DataModel 'a', Bytes: 0/20, Bits: 0/160
 | | |-- Number 'submessage_id', Bytes: 20/20, Bits: 160/160
 | | |   Size: 1 bytes | 8 bits (Has Length)
 | | |   Failed: Length is 8 bits but buffer only has 0 bits left.
 | |-+ DataModel 'b', Bytes: 0/20, Bits: 0/160
 | | |-- Number 'submessage_id', Bytes: 20/20, Bits: 160/160
 | | |   Size: 1 bytes | 8 bits (Has Length)
 | | |   Failed: Length is 8 bits but buffer only has 0 bits left.
Failed to parse file 'empty.raw': no alternative
Bytes:
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
"""


BINDING_LOG = """ |-+ Optional 'payload_optional', Bytes: 2/8, Bits: 16/64
 | |-- Blob 'payload', Bytes: 2/8, Bits: 16/64
Unable to resolve binding '../payload_length' attached to 'root.body.payload_optional.payload'.
 | |   Size: ??? (Not Last Unsized)
 | |   Failed: Element is unsized.
Parsed bytes do not match original file for 'binding.raw'
Original Bytes:
01 02 03 04 05 06 07 08
Parsed   Bytes:
01 02
"""


class DiagnoserTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.pit = self.root / "datamodel.xml"
        self.pit.write_text(PIT)
        self.index = XmlIndex(self.pit)

    def tearDown(self):
        self.tempdir.cleanup()

    def write_log(self, name, content):
        path = self.root / name
        path.write_text(content)
        return path

    def codes(self, content):
        path = self.write_log("failure.log", content)
        return {item.code for item in LogAnalysis(path, self.index).diagnose()}

    def test_detects_endianness_and_matched_branch_failure(self):
        codes = self.codes(ENDIAN_LOG)
        self.assertIn("probable_endianness_mismatch", codes)
        self.assertIn("matched_choice_branch_failed", codes)

    def test_detects_reference_and_roundtrip_failure(self):
        path = self.write_log("failure.log", REFERENCE_AND_DIFF_LOG)
        diagnostics = LogAnalysis(path, self.index).diagnose()
        codes = {item.code for item in diagnostics}
        self.assertIn("unresolved_runtime_reference", codes)
        self.assertIn("roundtrip_bytes_mismatch", codes)
        reference = next(
            item for item in diagnostics if item.code == "unresolved_runtime_reference"
        )
        self.assertEqual(4, reference.xml_locations[0].line)

    def test_aggregates_choice_branches_that_all_hit_eof(self):
        codes = self.codes(EOF_LOG)
        self.assertIn("all_choice_branches_hit_eof", codes)
        # A token with no value was not successfully matched.
        self.assertNotIn("matched_choice_branch_failed", codes)

    def test_static_lint_finds_unbounded_wrapper_before_sibling(self):
        diagnostics = self.index.static_diagnostics()
        finding = next(
            item
            for item in diagnostics
            if item.code == "unbounded_element_before_sibling"
        )
        self.assertEqual("payload_optional", finding.xml_locations[0].name)

    def test_detects_unresolved_binding_and_unsized_cascade(self):
        codes = self.codes(BINDING_LOG)
        self.assertIn("unresolved_relation_binding", codes)
        self.assertIn("unsized_element", codes)

    def test_directory_analysis_and_json_shape(self):
        self.write_log("one.log", ENDIAN_LOG)
        report = analyze([self.root], self.pit, include_static=False)
        self.assertEqual(1, report["logs_analyzed"])
        self.assertEqual("big.raw", report["reports"][0]["seed"])

    def test_adds_structured_llm_root_cause_judgment(self):
        class FakeResponse:
            usage_metadata = {
                "input_tokens": 100,
                "output_tokens": 50,
                "total_tokens": 150,
            }
            content = """```json
{
  "summary": "长度字段使用了错误字节序。",
  "root_causes": [{
    "id": "RC1",
    "title": "长度字节序错误",
    "classification": "root_cause",
    "category": "endianness",
    "confidence": 0.99,
    "affected_seeds": ["big.raw"],
    "xml_locations": [{"line": 12, "element": "submessage_length"}],
    "reasoning": "交换字节后等于剩余长度。",
    "evidence": ["6144 -> 24"],
    "suggested_fix": "让长度字段服从 flags。",
    "verification": "重新解析大端种子。"
  }],
  "causal_relationships": [],
  "priority_order": ["RC1"],
  "uncertainties": []
}
```"""

        class FakeLlm:
            def __init__(self):
                self.messages = None

            def invoke(self, messages):
                self.messages = messages
                return FakeResponse()

        self.write_log("one.log", ENDIAN_LOG)
        # Even if a caller passes a legacy heuristic report, entering LLM mode
        # must discard those findings and diagnose from its raw log paths.
        report = analyze([self.root], self.pit, include_static=True)
        fake = FakeLlm()
        add_llm_judgment(report, self.pit, model_name="fake", llm=fake)

        judgment = report["llm_judgment"]
        self.assertEqual("ok", judgment["status"])
        self.assertEqual("RC1", judgment["analysis"]["root_causes"][0]["id"])
        self.assertEqual(150, judgment["usage"]["total_tokens"])
        self.assertEqual([], report["cross_log_summary"])
        self.assertEqual([], report["static_diagnostics"])
        self.assertEqual([], report["reports"])
        self.assertIn("Value: 6144", fake.messages[1][1])
        self.assertNotIn("probable_endianness_mismatch", fake.messages[1][1])
        self.assertIn("DATAMODEL XML", fake.messages[1][1])

    def test_llm_report_does_not_run_heuristic_diagnosis(self):
        self.write_log("one.log", ENDIAN_LOG)
        with mock.patch.object(
            LogAnalysis,
            "diagnose",
            side_effect=AssertionError("heuristics must not run"),
        ):
            report = prepare_llm_report([self.root], self.pit)

        self.assertEqual("llm", report["diagnosis_mode"])
        self.assertEqual([], report["cross_log_summary"])
        self.assertEqual([], report["static_diagnostics"])
        self.assertEqual([], report["reports"])


if __name__ == "__main__":
    unittest.main()
