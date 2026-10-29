"""Check language propagation without model requests or SDK initialization."""

from contextlib import ExitStack
import json
import unittest
from unittest.mock import Mock, patch

import httpx
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

import core.config as config_module
from core.agent import AgentConfig, build_agent_graph
from core.datamodel_diagnoser import add_llm_judgment, build_parser, prepare_llm_report
from core.language import language_instructions
from pipeline.peach import PeachPipeline


class AgentLanguageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(config_module, "config", None))
        config_module.build_config_from_args("mqtt", "seeds/mqtt", [], language="zh-CN")

    def test_default_and_custom_agents_receive_language_system_requirement(self) -> None:
        create_agent = self.stack.enter_context(patch("core.agent._langchain_agents.create_agent"))
        self.stack.enter_context(patch("core.agent.ChatOpenAI"))
        self.stack.enter_context(patch("core.agent.make_rfc_search", return_value=Mock(name="RFC_Search")))
        self.stack.enter_context(patch("core.agent.get_tools", return_value=[]))
        for config, language in (
            (None, "zh-CN"),
            (AgentConfig(system_prompt="Diagnose failures.", language="Japanese"), "Japanese"),
        ):
            with self.subTest(language=language):
                build_agent_graph(retriever=Mock(), config=config)
                prompt = create_agent.call_args.kwargs["system_prompt"]
                self.assertIn(language_instructions(language), prompt)
                self.assertIn("JSON keys", prompt)
                if config is not None:
                    self.assertTrue(prompt.startswith(config.system_prompt))
                    self.assertEqual(config.system_prompt, "Diagnose failures.")

    def test_outbound_requests_include_native_reminder_across_tool_calls(self) -> None:
        payloads: list[dict[str, object]] = []

        def respond(request: httpx.Request) -> httpx.Response:
            payloads.append(json.loads(request.content))
            message = (
                {"role": "assistant", "content": "", "reasoning_content": "Inspect the packet.",
                 "tool_calls": [{"id": "call_rfc", "type": "function", "function": {
                     "name": "RFC_Search", "arguments": '{"query":"packet length"}',
                 }}]}
                if len(payloads) == 1 else
                {"role": "assistant", "content": "检查完成。", "reasoning_content": "已检查长度规则。"}
            )
            return httpx.Response(200, json={
                "id": f"reply_{len(payloads)}", "object": "chat.completion",
                "created": 0, "model": "test-model",
                "choices": [{"index": 0, "message": message,
                             "finish_reason": "tool_calls" if len(payloads) == 1 else "stop"}],
            })

        @tool
        def RFC_Search(query: str) -> str:
            """Look up a protocol rule."""
            return "The packet length is an unsigned two-byte big-endian field."

        client = self.stack.enter_context(httpx.Client(transport=httpx.MockTransport(respond)))
        llm = ChatOpenAI(model="test-model", api_key="test-key", http_client=client, max_retries=0)
        self.stack.enter_context(patch("core.agent.ChatOpenAI", return_value=llm))
        self.stack.enter_context(patch("core.agent.make_rfc_search", return_value=RFC_Search))
        self.stack.enter_context(patch("core.agent.get_tools", return_value=[]))
        graph = build_agent_graph(retriever=Mock())
        original = HumanMessage(content="Check the packet length rule using RFC_Search.")
        result = graph.invoke(
            {"messages": [original]}, {"configurable": {"thread_id": "language-test"}},
        )
        self.assertEqual(len(payloads), 2)
        for payload in payloads:
            messages = payload["messages"]
            self.assertIsInstance(messages, list)
            system = next(message for message in messages if message["role"] == "system")
            user = next(message for message in messages if message["role"] == "user")
            self.assertIn("从第一个推理词元开始", system["content"])
            self.assertTrue(user["content"].endswith(language_instructions("zh-CN")))
            self.assertEqual(user["content"].count("语言要求："), 1)
        self.assertEqual(original.content, "Check the packet length rule using RFC_Search.")
        self.assertEqual(result["messages"][0].content, original.content)
        assistant = next(message for message in payloads[1]["messages"] if message["role"] == "assistant")
        self.assertEqual(assistant["reasoning_content"], "Inspect the packet.")
        self.assertEqual(payloads[1]["messages"][-1]["role"], "tool")

    def test_pipeline_applies_language_to_generation_diagnosis_and_repair(self) -> None:
        def initialize(pipeline: PeachPipeline) -> None:
            pipeline.state = {"peach_step_layout": "combined-dsl-mutators-v3", "current_step_index": 0}
            pipeline.retriever = Mock()

        self.stack.enter_context(patch("pipeline.peach.BasePipeline.__init__", initialize))
        self.stack.enter_context(patch("pipeline.peach.build_agent_graph"))
        pipeline = PeachPipeline()
        for config in (
            pipeline.agent_config,
            pipeline.diagnosis_agent_config,
            pipeline.datamodel_autofix_agent_config,
        ):
            self.assertEqual(config.language, "zh-CN")

    def test_standalone_diagnoser_accepts_language_alias_and_preserves_selection(self) -> None:
        args = build_parser().parse_args(["logs", "--language", "Japanese"])
        self.assertEqual(args.llm_language, "Japanese")
        invoker = Mock()
        invoker.invoke.return_value.content = (
            '{"summary": "完了", "root_causes": [], "priority_order": [], "uncertainties": []}'
        )
        invoker.invoke.return_value.usage_metadata = None
        add_llm_judgment(
            prepare_llm_report([], None), None, language=args.llm_language, llm=invoker,
        )
        messages = invoker.invoke.call_args.args[0]
        self.assertIn("Think and reason in Japanese", messages[0][1])
        self.assertIn("Write all explanatory strings in Japanese", messages[1][1])
        legacy_args = build_parser().parse_args(["logs", "--llm-language", "en"])
        self.assertEqual(legacy_args.llm_language, "en")


if __name__ == "__main__":
    unittest.main()
