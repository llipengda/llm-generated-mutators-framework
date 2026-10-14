import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.peach_sdk import (
    PeachSdk,
    compile_csharp,
    csharp_language_guidance,
    csharp_language_version,
    ensure_mutator_sdk,
    peach_image,
    peach_sdk_dir,
    peach_sdk_from_environment,
)


class PeachSdkSelectionTests(unittest.TestCase):
    def test_defaults_to_legacy(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(peach_sdk_from_environment(), "legacy")
            self.assertEqual(peach_sdk_dir().as_posix(), "peach/sdk")
            self.assertEqual(peach_image(), "pdli/llm-peach:sdk")
            self.assertEqual(csharp_language_version(), "7.2")
            self.assertIn("C# 7.2", csharp_language_guidance())
            self.assertIn(".NET Framework 4.5", csharp_language_guidance())

    def test_accepts_modern_sdk_alias(self) -> None:
        with patch.dict(os.environ, {"PEACH_SDK": "modern-sdk"}, clear=True):
            self.assertEqual(peach_sdk_from_environment(), "modern")
            self.assertEqual(peach_sdk_dir().as_posix(), "peach/modern-sdk")
            self.assertEqual(peach_image(), "pdli/llm-peach:modern-sdk")
            self.assertEqual(csharp_language_version(), "12.0")
            self.assertIn("C# 12", csharp_language_guidance())
            self.assertIn(".NET 8", csharp_language_guidance())

    def test_rejects_unknown_variant(self) -> None:
        with patch.dict(os.environ, {"PEACH_SDK": "future"}, clear=True):
            with self.assertRaisesRegex(ValueError, "PEACH_SDK"):
                peach_sdk_from_environment()


class PeachCSharpCompilationTests(unittest.TestCase):
    def require_compiler_and_sdk(self, compiler: str, sdk: PeachSdk) -> None:
        if shutil.which(compiler) is None:
            self.skipTest(f"{compiler} is not installed")
        sdk_dir = peach_sdk_dir(sdk)
        if not all(
            (sdk_dir / name).is_file()
            for name in ("Peach.Core.dll", "Peach.LLM.dll")
        ):
            self.skipTest(f"Peach {sdk} SDK DLLs are not installed in {sdk_dir}")

    def assert_compiles(self, source: str, sdk: PeachSdk) -> None:
        with tempfile.TemporaryDirectory(prefix="peach-language-test-") as directory:
            source_path = Path(directory) / "LanguageProbe.cs"
            output_path = Path(directory) / "LanguageProbe.dll"
            source_path.write_text(source, encoding="utf-8")
            result = compile_csharp([source_path], output_path, sdk=sdk)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(output_path.is_file(), "Compiler did not produce a DLL")

    def test_legacy_compiles_csharp_72_for_framework_45(self) -> None:
        self.require_compiler_and_sdk("mcs", "legacy")
        self.assert_compiles(
            """using Peach.Core.Dom;

public readonly struct LegacyLanguageProbe
{
    public string Describe(object value)
    {
        if (value is DataElement element)
            return $"{nameof(element)}:{element.Name}";
        return nameof(value);
    }
}
""",
            "legacy",
        )

    def test_modern_compiles_csharp_12_collection_expression(self) -> None:
        self.require_compiler_and_sdk("dotnet", "modern")
        installed_sdks = subprocess.run(
            ["dotnet", "--list-sdks"], text=True, capture_output=True, check=True
        )
        if not any(
            line.partition(".")[0].isdigit()
            and int(line.partition(".")[0]) >= 8
            for line in installed_sdks.stdout.splitlines()
        ):
            self.skipTest("A .NET SDK supporting C# 12 is not installed")
        self.assert_compiles(
            """using Peach.Core.Dom;

namespace GeneratedLanguageProbe;

public static class ModernLanguageProbe
{
    public static int[] Values() => [1, 2, 3];
    public static string Describe(DataElement element) =>
        $"{nameof(element)}:{element.Name}";
}
""",
            "modern",
        )


class MutatorSdkCapabilityTests(unittest.TestCase):
    def test_stale_sdk_fails_before_agent_generation(self) -> None:
        failure = subprocess.CompletedProcess(
            ["dotnet"], 1, "", "LLMAddMutator could not be found"
        )
        with patch("core.peach_sdk.compile_csharp", return_value=failure):
            with self.assertRaisesRegex(RuntimeError, "Update Peach.LLM.dll"):
                ensure_mutator_sdk("modern")

    def test_missing_compiler_is_reported_as_sdk_failure(self) -> None:
        failure = subprocess.CompletedProcess(["mcs"], 127, "", "mcs was not found")
        with patch("core.peach_sdk.compile_csharp", return_value=failure):
            with self.assertRaisesRegex(RuntimeError, "mcs was not found"):
                ensure_mutator_sdk("legacy")

    def test_supported_sdk_probe_completes(self) -> None:
        result = subprocess.CompletedProcess(["dotnet"], 0, "", "")
        with patch("core.peach_sdk.compile_csharp", return_value=result):
            ensure_mutator_sdk("modern")

    def test_probe_ignores_generated_protocol_assemblies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sdk_dir = Path(directory)
            (sdk_dir / "Peach.LLM.dll").write_bytes(b"sdk")
            (sdk_dir / "DEMOFixers.dll").write_bytes(b"protocol-artifact")
            result = subprocess.CompletedProcess(["dotnet"], 0, "", "")
            with (
                patch("core.peach_sdk.peach_sdk_dir", return_value=sdk_dir),
                patch("core.peach_sdk.compile_csharp", return_value=result) as compiler,
            ):
                ensure_mutator_sdk("modern")
            self.assertEqual(
                compiler.call_args.kwargs["excluded_reference_names"],
                ["DEMOFixers.dll"],
            )


if __name__ == "__main__":
    unittest.main()
