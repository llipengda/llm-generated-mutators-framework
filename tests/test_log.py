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

    def test_parallel_task_context_and_detached_callbacks(self) -> None:
        from threading import Barrier
        from core.log import run_logged_task, task_scope

        barrier = Barrier(2)
        with tempfile.TemporaryDirectory() as directory:
            with log_session("demo", log_root=Path(directory)) as logger:
                def worker(name: str) -> None:
                    with task_scope("agent", reuse=True) as task:
                        self.assertEqual(task.task_name, name)
                        callback = logger.for_task(task)
                        barrier.wait(timeout=5)
                        UI.error(name)
                        # Callbacks can run on another executor without inherited context.
                        with ThreadPoolExecutor(max_workers=1) as callbacks:
                            callbacks.submit(callback.on_tool_start, {"name": name}, "", run_id=uuid4()).result()
                        logging.getLogger(__name__).info("after callback %s", name)

                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [pool.submit(run_logged_task, name, worker, name) for name in ("family_a", "family_b")]
                    for future in futures:
                        future.result()
                UI.warn("outside task")

            records = [json.loads(line) for line in logger.path.read_text().splitlines()]
            for name in ("family_a", "family_b"):
                own = [r for r in records if r.get("task_name") == name]
                self.assertEqual(len(own), 5)
                self.assertEqual(len({r["task_id"] for r in own}), 1)
                self.assertEqual(next(r["tool"] for r in own if r["event"] == "tool_start"), name)
            ids = {r["task_id"] for r in records if r.get("task_id")}
            self.assertEqual(len(ids), 2)
            self.assertIsNone(next(r for r in records if r.get("message") == "outside task")["task_id"])

    def test_failed_tasks_reset_context_and_retries_get_new_ids(self) -> None:
        from core.log import run_logged_task

        def fail() -> None:
            raise ValueError("postcheck failed")

        with tempfile.TemporaryDirectory() as directory:
            with log_session("demo", log_root=Path(directory)) as logger:
                for _ in range(2):
                    with self.assertRaises(ValueError):
                        run_logged_task("same family", fail)
                UI.warn("after failures")
            records = [json.loads(line) for line in logger.path.read_text().splitlines()]
            failures = [r for r in records if r["event"] == "task_error"]
            self.assertEqual(len({r["task_id"] for r in failures}), 2)
            self.assertTrue(all("ValueError: postcheck failed" in r["traceback"] for r in failures))
            self.assertIsNone(next(r for r in records if r.get("message") == "after failures")["task_id"])

    def test_agent_call_binds_callbacks_and_runtime_logs_to_worker(self) -> None:
        from threading import Lock
        from langchain_core.messages import AIMessage
        from langchain_core.runnables import RunnableConfig
        from core.agent_types import AgentResponse
        from core.log import PipelineLogger, run_logged_task
        from core.state import new_usage_bucket

        class Agent:
            def invoke(self, input: object, config: RunnableConfig | None = None) -> AgentResponse:
                assert config is not None
                callbacks = config.get("callbacks")
                assert isinstance(callbacks, list)
                callback = next(c for c in callbacks if isinstance(c, PipelineLogger))
                run_id = uuid4()
                callback.on_tool_start({"name": "RFC_Search"}, "query", run_id=run_id)
                logging.getLogger(__name__).warning("inside agent")
                callback.on_tool_end("result", run_id=run_id)
                return {"messages": [AIMessage(content="done")]}

        class Pipeline(BasePipeline):
            def __init__(self, callback: PipelineLogger) -> None:
                self.tool_usage_logger = callback
                self.config = {}
                self.agent_graph = Agent()
                self._state_lock = Lock()
                self.state = {
                    "packet_types": [], "data_type_analysis": {}, "constraints": "",
                    "token_usage_total": new_usage_bucket(), "token_usage_by_step": {},
                    "current_step_index": 0,
                }

            def save_state(self) -> None:
                logging.getLogger(__name__).info("saved agent state")

        with tempfile.TemporaryDirectory() as directory:
            with log_session("demo", log_root=Path(directory)) as logger:
                pipeline = Pipeline(logger)
                run_logged_task("DSL publish", pipeline.call_agent, "prompt", "Generate family")
                pipeline.call_agent("prompt", "Standalone retry")
            records = [json.loads(line) for line in logger.path.read_text().splitlines()]
            worker = [r for r in records if r.get("task_name") == "DSL publish"]
            self.assertEqual(len({r["task_id"] for r in worker}), 1)
            self.assertTrue({"task_start", "agent_start", "tool_start", "tool_end", "agent_end", "task_end"}.issubset({r["event"] for r in worker}))
            self.assertTrue(any(r.get("message") == "inside agent" for r in worker))
            retry = [r for r in records if r.get("task_name") == "Standalone retry"]
            self.assertTrue(retry)
            self.assertNotEqual(retry[0]["task_id"], worker[0]["task_id"])
