"""Prompt loading and the contract between templates and their callers."""

import ast
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.prompts import PROMPT_ROOT, load_prompt, render_prompt


class PromptTests(unittest.TestCase):
    def test_substitution_preserves_code_and_does_not_expand_inserted_values(self):
        with patch("core.prompts.load_prompt", return_value='{"code": "{ x; }", "text": "{{text}}"}'):
            self.assertEqual(
                render_prompt("example.txt", text="中文 {{other}}", other="unwanted"),
                '{"code": "{ x; }", "text": "中文 {{other}}"}',
            )

    def test_missing_parameter_identifies_template_and_name(self):
        with patch("core.prompts.load_prompt", return_value="{{required}}"):
            with self.assertRaisesRegex(ValueError, "example.txt: required"):
                render_prompt("example.txt")

    def test_loading_is_independent_of_working_directory(self):
        expected = load_prompt("system/peach.txt")
        previous = Path.cwd()
        try:
            with tempfile.TemporaryDirectory() as directory:
                os.chdir(directory)
                self.assertEqual(load_prompt("system/peach.txt"), expected)
        finally:
            os.chdir(previous)

    def test_path_cannot_escape_prompt_directory(self):
        with self.assertRaises(ValueError):
            load_prompt("../README.md")

    def test_all_templates_are_used_and_callers_supply_their_parameters(self):
        root = PROMPT_ROOT.parent
        referenced: set[str] = set()
        for folder in ("core", "pipeline"):
            for source in (root / folder).rglob("*.py"):
                for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
                    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                        continue
                    if node.func.id not in {"render_prompt", "load_prompt"} or not node.args:
                        continue
                    name = node.args[0]
                    if not isinstance(name, ast.Constant) or not isinstance(name.value, str):
                        continue
                    referenced.add(name.value)
                    with self.subTest(source=source, template=name.value):
                        if node.func.id == "render_prompt":
                            values = {keyword.arg: "test" for keyword in node.keywords if keyword.arg}
                            render_prompt(name.value, **values)
                        else:
                            load_prompt(name.value)
        available = {
            str(path.relative_to(PROMPT_ROOT))
            for path in PROMPT_ROOT.rglob("*")
            if path.suffix in {".txt", ".json"}
        }
        self.assertEqual(referenced, available)
