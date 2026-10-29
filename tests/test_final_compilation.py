from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.peach_sdk import compile_csharp, peach_sdk_dir
from pipeline.peach_steps.compilation import CompilationStep


_TARGET = "pipeline.peach_steps.compilation"


class _Harness(CompilationStep):
    protocol_lower = "demo"
    protocol_upper = "DEMO"

    def _data_type_paths(self) -> tuple[Path, Path, Path]:
        root = Path("llm/peach/demo")
        return (
            root / "data_type_analysis.json",
            root / "DataElements",
            root / "DataElements/out/DEMODataElements.dll",
        )


class FinalCompilationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        previous = Path.cwd()
        self.addCleanup(os.chdir, previous)
        os.chdir(temporary.name)
        self.root = Path("llm/peach/demo")
        self.custom = self.root / "DataElements"
        self.custom.mkdir(parents=True)
        self.source = self.custom / "DemoNumber.cs"
        self.source.write_text(
            "namespace Demo; public class DemoNumber : Peach.Core.Dom.Number {}",
            encoding="utf-8",
        )
        self.pipeline = _Harness()

    def test_final_dll_exports_custom_types_without_separate_plugin(self) -> None:
        if shutil.which("dotnet") is None or not (peach_sdk_dir() / "Peach.Core.dll").is_file():
            self.skipTest("Run ./setup.sh to install the local .NET 8 SDK")
        plugin = self.custom / "out/DEMODataElements.dll"
        result = compile_csharp([self.source], plugin)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        for combined in (False, True):
            with self.subTest(combined=combined):
                mutators = self.root / "Mutators"
                fixer = self.root / "Fixers/DemoFixer.cs"
                if combined:
                    mutators.mkdir()
                    (mutators / "Active.cs").write_text(
                        "public class Active { public Demo.DemoNumber Value = new(); }",
                        encoding="utf-8",
                    )
                    (mutators / "Orphan.cs").write_text("invalid C#", encoding="utf-8")
                    fixer.parent.mkdir()
                    fixer.write_text(
                        "public class DemoFixer { public Demo.DemoNumber Value = new(); }",
                        encoding="utf-8",
                    )
                    validations = fixer.parent / "Validations"
                    validations.mkdir()
                    (validations / "Test.cs").write_text("invalid C#", encoding="utf-8")
                with patch(f"{_TARGET}.manifest_sources", return_value=[mutators / "Active.cs"]):
                    self.pipeline.step_final_compile()

                # Compile a consumer with only the final DLL, after removing the
                # standalone plugin, to verify the custom type is defined inside it.
                plugin.rename(plugin.with_suffix(".hidden"))
                try:
                    consumer = Path("Consumer.cs")
                    consumer.write_text(
                        "public class Consumer { public Demo.DemoNumber Value = new(); }",
                        encoding="utf-8",
                    )
                    result = compile_csharp(
                        [consumer], "Consumer.dll",
                        additional_references=[self.root / "DEMO.dll"],
                    )
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                finally:
                    plugin.with_suffix(".hidden").rename(plugin)

    def test_protocol_without_custom_sources_still_compiles(self) -> None:
        self.source.unlink()
        fixers = self.root / "Fixers"
        fixers.mkdir()
        source = fixers / "DemoFixer.cs"
        source.write_text("public class DemoFixer {}", encoding="utf-8")
        with patch(f"{_TARGET}.compile_csharp", return_value=subprocess.CompletedProcess([], 0, "", "")) as compiler:
            self.pipeline.step_final_compile()
        self.assertEqual([Path(path) for path in compiler.call_args.args[0]], [source])
        self.assertFalse(compiler.call_args.kwargs.get("additional_references"))

    def test_invalid_custom_source_fails_final_compilation(self) -> None:
        failure = subprocess.CompletedProcess([], 1, "custom type error", "")
        with patch(f"{_TARGET}.compile_csharp", return_value=failure):
            with self.assertRaisesRegex(RuntimeError, "custom type error"):
                self.pipeline.step_final_compile()


if __name__ == "__main__":
    unittest.main()
