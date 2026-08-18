import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pipeline.base import BasePipeline
from pipeline.peach import PeachPipeline


class PeachPipelineStep3Tests(unittest.TestCase):
    @patch("pipeline.peach.UI.success")
    @patch("pipeline.peach.UI.panel")
    def test_reads_diagnosis_inputs_via_agent_tools_and_prints_result(
        self,
        panel,
        _success,
    ):
        pipeline = object.__new__(PeachPipeline)
        pipeline.protocol_lower = "mqtt"
        pipeline.protocol_name = "MQTT"
        pipeline.agent_config = SimpleNamespace(model="test-model")
        pipeline.diagnosis_agent_graph = object()
        analysis = {
            "logs_analyzed": 1,
            "summary": "根因概要",
            "root_causes": [{"id": "RC1"}],
            "priority_order": ["RC1"],
            "uncertainties": [],
        }
        report = {
            "diagnosis_mode": "llm",
            "datamodel": "llm/peach/mqtt/datamodel.xml",
            "logs_analyzed": 1,
            "log_files": ["llm/peach/mqtt/dm_test_logs/failure.log"],
            "cross_log_summary": [],
            "reports": [],
            "static_diagnostics": [],
            "llm_judgment": {
                "status": "ok",
                "model": "test-model",
                "analysis": analysis,
            },
        }
        report_json = json.dumps(report)
        pipeline.call_agent = Mock(return_value={"messages": [
            SimpleNamespace(content="", tool_calls=[{
                "name": "Read_File",
                "args": {"filepath": "llm/peach/mqtt/datamodel.xml"},
                "id": "read-datamodel",
            }, {
                "name": "Read_File",
                "args": {"filepath": "llm/peach/mqtt/dm_test_logs"},
                "id": "list-logs",
            }]),
            SimpleNamespace(
                content=(
                    "Directory listing for llm/peach/mqtt/dm_test_logs:\n"
                    "failure.log"
                ),
                tool_call_id="list-logs",
            ),
            SimpleNamespace(content="", tool_calls=[{
                "name": "Read_File",
                "args": {"filepath": "llm/peach/mqtt/dm_test_logs/failure.log"},
                "id": "read-log",
            }]),
            SimpleNamespace(content="", tool_calls=[{
                "name": "Write_File",
                "args": {
                    "filepath": "llm/peach/mqtt/datamodel_diagnosis.json",
                    "content": report_json,
                },
                "id": "write-diagnosis",
            }]),
            SimpleNamespace(
                content=(
                    "SUCCESS: Content written to "
                    "llm/peach/mqtt/datamodel_diagnosis.json."
                ),
                tool_call_id="write-diagnosis",
            ),
            SimpleNamespace(content=report_json, tool_calls=[]),
        ]})

        result = pipeline.diagnose_datamodel_failure("[FAIL] datamodel")

        prompt = pipeline.call_agent.call_args.args[0]
        self.assertIn('Call "Read_File" for "llm/peach/mqtt/datamodel.xml"', prompt)
        self.assertIn('Call "Read_File" for "llm/peach/mqtt/dm_test_logs"', prompt)
        self.assertIn('for EVERY .log file', prompt)
        self.assertIn('use "RFC_Search" as needed', prompt)
        self.assertIn('call "Write_File" exactly', prompt)
        self.assertIs(
            pipeline.call_agent.call_args.kwargs["agent_graph"],
            pipeline.diagnosis_agent_graph,
        )
        parsed = json.loads(result)
        self.assertEqual("RC1", parsed["llm_judgment"]["analysis"]["root_causes"][0]["id"])
        panel.assert_called_once_with(
            result,
            title="Datamodel LLM Diagnosis",
            border_style="cyan",
            expand=True,
        )

    def test_diagnoses_after_failed_validation_and_before_auto_fix(self):
        pipeline = object.__new__(PeachPipeline)
        pipeline.protocol_lower = "mqtt"
        pipeline.protocol_name = "MQTT"
        pipeline.datamodel_autofix_agent_graph = object()
        events = []
        verification_results = iter(
            [(False, "[FAIL] datamodel"), (True, "[PASS] datamodel")]
        )

        def verify():
            events.append("verify")
            return next(verification_results)

        def diagnose(_test_output):
            events.append("diagnose")
            return "RC1: remaining-length relation is unresolved"

        def call_agent(prompt, step_title, *, agent_graph=None):
            events.append("fix")
            self.assertNotIn("RC1: remaining-length relation is unresolved", prompt)
            self.assertNotIn("[FAIL] datamodel", prompt)
            self.assertNotIn("dm_test_logs", prompt)
            self.assertIn("diagnosis report is the sole source", prompt)
            self.assertIn(
                "llm_judgment.analysis.priority_order", prompt
            )
            self.assertEqual("Step 3: Datamodel Validation & Fix", step_title)
            self.assertIs(agent_graph, pipeline.datamodel_autofix_agent_graph)
            return {"messages": []}

        pipeline.verify_datamodel = verify
        pipeline.diagnose_datamodel_failure = diagnose
        pipeline.call_agent = call_agent
        pipeline.fix_verify_loop = BasePipeline.fix_verify_loop.__get__(
            pipeline, PeachPipeline
        )

        with patch("pipeline.peach.Path.exists", return_value=False):
            pipeline.step_3_datamodel_validation_and_fix()

        self.assertEqual(["verify", "diagnose", "fix", "verify"], events)

    def test_can_reuse_existing_diagnosis_and_skip_diagnosis_agent(self):
        pipeline = object.__new__(PeachPipeline)
        pipeline.protocol_lower = "mqtt"
        pipeline.protocol_name = "MQTT"
        pipeline.datamodel_autofix_agent_graph = object()
        verification_results = iter(
            [(False, "[FAIL] datamodel"), (True, "[PASS] datamodel")]
        )
        pipeline.verify_datamodel = lambda: next(verification_results)
        pipeline.diagnose_datamodel_failure = Mock()
        pipeline.call_agent = Mock(return_value={"messages": []})
        pipeline.fix_verify_loop = BasePipeline.fix_verify_loop.__get__(
            pipeline, PeachPipeline
        )

        with (
            patch("pipeline.peach.Path.exists", return_value=True),
            patch("pipeline.peach.ask_reuse_diagnosis", return_value=True) as ask,
        ):
            pipeline.step_3_datamodel_validation_and_fix()

        ask.assert_called_once_with("mqtt")
        pipeline.diagnose_datamodel_failure.assert_not_called()
        repair_prompt = pipeline.call_agent.call_args.args[0]
        self.assertIn("datamodel_diagnosis.json", repair_prompt)
        self.assertNotIn("[FAIL] datamodel", repair_prompt)


if __name__ == "__main__":
    unittest.main()
