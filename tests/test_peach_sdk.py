import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.peach_sdk import (
    compile_csharp,
    csharp_language_guidance,
    csharp_language_version,
    ensure_mutator_sdk,
    peach_sdk_dir,
)


class PeachSdkTests(unittest.TestCase):
    def test_default_install_is_absolute_repository_path(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(peach_sdk_dir(), Path(__file__).resolve().parents[1] / "peach" / "sdk")

    def test_install_directory_can_be_overridden(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"PEACH_SDK_DIR": directory}):
                self.assertEqual(peach_sdk_dir(), Path(directory).resolve())

    def test_language_guidance_matches_net8_compiler(self) -> None:
        self.assertEqual(csharp_language_version(), "12.0")
        self.assertIn("C# 12", csharp_language_guidance())
        self.assertIn(".NET 8", csharp_language_guidance())

    def test_compiles_csharp12_against_installed_peach(self) -> None:
        if shutil.which("dotnet") is None:
            self.skipTest("dotnet is not installed")
        if not (peach_sdk_dir() / "Peach.runtimeconfig.json").is_file():
            self.skipTest("Run ./setup.sh to install the local .NET 8 SDK")
        with tempfile.TemporaryDirectory(prefix="peach-language-test-") as directory:
            source = Path(directory) / "LanguageProbe.cs"
            output = Path(directory) / "LanguageProbe.dll"
            source.write_text(
                """using Peach.Core.Dom;
namespace GeneratedLanguageProbe;
public static class LanguageProbe
{
    public static int[] Values() => [1, 2, 3];
    public static string Describe(DataElement element) => element.Name;
}
""", encoding="utf-8",
            )
            result = compile_csharp([source], output)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(output.is_file())

    def test_missing_compiler_has_actionable_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sdk = Path(directory)
            source = sdk / "Probe.cs"
            source.write_text("public class Probe {}", encoding="utf-8")
            (sdk / "Peach.Core.dll").write_bytes(b"reference")
            with (
                patch("core.peach_sdk.peach_sdk_dir", return_value=sdk),
                patch("core.peach_sdk.shutil.which", return_value=None),
            ):
                result = compile_csharp([source], sdk / "Probe.dll")
            self.assertEqual(result.returncode, 127)
            self.assertIn("dotnet was not found", result.stderr)

    def test_missing_structural_api_stops_before_agent_generation(self) -> None:
        failure = subprocess.CompletedProcess(["dotnet"], 1, "", "LLMAddMutator could not be found")
        with patch("core.peach_sdk.compile_csharp", return_value=failure):
            with self.assertRaisesRegex(RuntimeError, "Update Peach.LLM.dll"):
                ensure_mutator_sdk()

    def test_capability_probe_reports_missing_compiler(self) -> None:
        failure = subprocess.CompletedProcess(["dotnet"], 127, "", "dotnet was not found")
        with patch("core.peach_sdk.compile_csharp", return_value=failure):
            with self.assertRaisesRegex(RuntimeError, "dotnet was not found"):
                ensure_mutator_sdk()

    def test_supported_sdk_probe_completes(self) -> None:
        result = subprocess.CompletedProcess(["dotnet"], 0, "", "")
        with patch("core.peach_sdk.compile_csharp", return_value=result):
            ensure_mutator_sdk()

    def test_probe_ignores_generated_protocol_assemblies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sdk = Path(directory)
            (sdk / "Peach.LLM.dll").write_bytes(b"sdk")
            (sdk / "DEMOFixers.dll").write_bytes(b"protocol-artifact")
            result = subprocess.CompletedProcess(["dotnet"], 0, "", "")
            with (
                patch("core.peach_sdk.peach_sdk_dir", return_value=sdk),
                patch("core.peach_sdk.compile_csharp", return_value=result) as compiler,
            ):
                ensure_mutator_sdk()
            self.assertEqual(compiler.call_args.kwargs["excluded_reference_names"], ["DEMOFixers.dll"])


if __name__ == "__main__":
    unittest.main()
