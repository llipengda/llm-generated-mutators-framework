"""Configuration and CLI contract for the local .NET pipeline."""

from __future__ import annotations

from contextlib import nullcontext
import os
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

from click.testing import CliRunner

import core.config as config_module
from main import main


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config_patch = patch.object(config_module, "config", None)
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)

    def test_builds_protocol_config_without_backend_environment_changes(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            config_module.build_config_from_args(
                "mqtt", "seeds/mqtt", ["rfc/one.pdf", "rfc/two.pdf"], fixer=True,
            )
            self.assertEqual(dict(os.environ), {})
        self.assertEqual(config_module.get_protocol_name(), "mqtt")
        self.assertEqual(config_module.get_seed_dir(), "seeds/mqtt")
        self.assertEqual(config_module.get_rfc_paths(), ["rfc/one.pdf", "rfc/two.pdf"])
        self.assertTrue(config_module.get_fixer_enabled())
        self.assertEqual(config_module.get_language(), "en")
        self.assertEqual(set(config_module.config or {}), {
            "protocol_name", "seed_dir", "rfc_paths", "fixer", "language",
        })

    def test_config_getters_require_initialization(self) -> None:
        for getter in (
            config_module.get_protocol_name, config_module.get_seed_dir,
            config_module.get_rfc_paths, config_module.get_fixer_enabled,
            config_module.get_language,
        ):
            with self.subTest(getter=getter.__name__):
                with self.assertRaisesRegex(ValueError, "Config not built"):
                    getter()

    def test_cli_uses_local_pipeline_and_preserves_multiple_rfcs(self) -> None:
        pipeline = Mock()
        pipeline_factory = Mock(return_value=pipeline)
        pipeline_module = ModuleType("pipeline.peach")
        setattr(pipeline_module, "PeachPipeline", pipeline_factory)
        with patch.dict("sys.modules", {"pipeline.peach": pipeline_module}), \
            patch("main.load_env"), patch("main.log_session", return_value=nullcontext()):
            result = CliRunner().invoke(main, [
                "--protocol", "mqtt", "--seed-dir", "seeds/mqtt",
                "--rfc-path", "rfc/one.pdf", "--rfc-path", "rfc/two.pdf", "--fixer",
                "--language", "zh-CN",
            ])
        self.assertEqual(result.exit_code, 0, result.output)
        pipeline_factory.assert_called_once_with()
        pipeline.assert_called_once_with()
        self.assertEqual(config_module.get_rfc_paths(), ["rfc/one.pdf", "rfc/two.pdf"])
        self.assertTrue(config_module.get_fixer_enabled())
        self.assertEqual(config_module.get_language(), "zh-CN")

    def test_language_accepts_names_and_rejects_empty_values(self) -> None:
        config_module.build_config_from_args(
            "mqtt", "seeds/mqtt", ["rfc/one.pdf"], language=" Japanese ",
        )
        self.assertEqual(config_module.get_language(), "Japanese")
        with self.assertRaisesRegex(ValueError, "Language must not be empty"):
            config_module.build_config_from_args(
                "mqtt", "seeds/mqtt", ["rfc/one.pdf"], language="  ",
            )
        with patch("main.load_env") as load_env:
            result = CliRunner().invoke(main, [
                "--protocol", "mqtt", "--seed-dir", "seeds/mqtt",
                "--rfc-path", "rfc/one.pdf", "--language", "  ",
            ])
        self.assertEqual(result.exit_code, 2, result.output)
        self.assertIn("Invalid value for '--language'", result.output)
        load_env.assert_not_called()

    def test_cli_has_no_backend_option(self) -> None:
        result = CliRunner().invoke(main, ["--help"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn("--modern-sdk", result.output)
        result = CliRunner().invoke(main, ["--modern-sdk"])
        self.assertEqual(result.exit_code, 2)
        self.assertIn("No such option", result.output)


if __name__ == "__main__":
    unittest.main()
