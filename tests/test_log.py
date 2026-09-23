import json
import logging
import tempfile
import unittest
from unittest.mock import Mock
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

from core.log import get_pipeline_logger, log_session
from core.ui import UI
from pipeline.base import BasePipeline


class PipelineLogTests(unittest.TestCase):
    def test_errors_and_worker_events_share_one_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with log_session("Demo", log_root=root) as logger:
                self.assertIs(get_pipeline_logger("demo"), logger)
                UI.error("compiler failed")
                UI.warn("retrying validation")

                def tool_call(index: int) -> None:
                    run_id = uuid4()
                    logger.on_tool_start({"name": "test"}, str(index), run_id=run_id)
                    logger.on_tool_end({"index": index}, run_id=run_id)

                with ThreadPoolExecutor(max_workers=4) as pool:
                    list(pool.map(tool_call, range(12)))
                try:
                    raise ValueError("API failed")
                except ValueError as error:
                    logger.on_llm_error(error, run_id=uuid4())
                    logger.on_tool_error(error, run_id=uuid4())
                    logger.on_chain_error(error, run_id=uuid4())

            records = [json.loads(line) for line in logger.path.read_text().splitlines()]
            self.assertEqual(logger.path, root / "demo" / "log.jsonl")
            self.assertFalse((root / "demo" / "tool_usage.jsonl").exists())
            self.assertEqual(sum(r["event"] == "tool_end" for r in records), 12)
            self.assertTrue(any(r.get("message") == "compiler failed" and r["level"] == "ERROR" for r in records))
            for event in ("llm_error", "tool_error", "chain_error"):
                record = next(r for r in records if r["event"] == event)
                self.assertIn("ValueError: API failed", record["traceback"])
                self.assertEqual(record["level"], "ERROR")
            self.assertEqual({r["session_id"] for r in records}, {logger.session_id})

    def test_startup_failure_is_logged_and_handler_is_removed(self) -> None:
        root_logger = logging.getLogger()
        previous_handlers = root_logger.handlers[:]
        previous_level = root_logger.level
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "demo" / "log.jsonl"
            with self.assertRaisesRegex(RuntimeError, "initialization failed"):
                with log_session("demo", log_root=Path(directory)):
                    raise RuntimeError("initialization failed")
            records = [json.loads(line) for line in log_path.read_text().splitlines()]
            error = next(r for r in records if r["event"] == "pipeline_error")
            self.assertIn("RuntimeError: initialization failed", error["traceback"])
            self.assertEqual(records[-1]["status"], "error")
        self.assertEqual(root_logger.handlers, previous_handlers)
        self.assertEqual(root_logger.level, previous_level)

    def test_failed_validation_output_survives_successful_retry(self) -> None:
        pipeline = BasePipeline.__new__(BasePipeline)
        verify = Mock(side_effect=[(False, "seed.raw: parse failed"), (True, "all passed")])
        fix = Mock()
        with tempfile.TemporaryDirectory() as directory:
            with log_session("demo", log_root=Path(directory)) as logger:
                self.assertTrue(pipeline.fix_verify_loop("DataModel", verify, fix))
            records = [json.loads(line) for line in logger.path.read_text().splitlines()]
            results = [r for r in records if r["event"] == "verification_result"]
            self.assertEqual([r["level"] for r in results], ["ERROR", "INFO"])
            self.assertIn("seed.raw: parse failed", results[0]["message"])
            fix.assert_called_once_with("seed.raw: parse failed", None)
