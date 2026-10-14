"""Native runtime isolation, plugin selection and validator failure handling."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import subprocess
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree

from core import peach_runtime


class NativeRuntimeTests(unittest.TestCase):
    def test_runtime_copies_dependencies_and_stages_exact_plugins(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            sdk = root / "sdk"
            sdk.mkdir()
            plugins = sdk / "Plugins"
            plugins.mkdir()
            (sdk / "Peach.Core.dll").write_bytes(b"core")
            (plugins / "Peach.LLM.dll").write_bytes(b"sdk plugin")
            (sdk / "OTHERFixers.dll").write_bytes(b"old copied fixer")
            (plugins / "OTHERMutators.dll").write_bytes(b"unrelated mutators")
            assets = sdk / "runtimes" / "osx-arm64" / "native"
            assets.mkdir(parents=True)
            (assets / "library.dylib").write_bytes(b"native dependency")
            custom = root / "TFTPDataElements.dll"
            custom.write_bytes(b"current plugin")
            datamodel = root / "protocol" / "datamodel.xml"
            datamodel.parent.mkdir()
            datamodel.write_text("<Peach/>")
            datamodel.with_name("python_fixup.py").write_text("value = 42")
            logs = root / 'logs & reports'
            with peach_runtime.runtime_directory(sdk, logs, plugins=[custom], datamodel=datamodel) as runtime:
                self.assertEqual((runtime / assets.relative_to(sdk) / "library.dylib").read_bytes(), b"native dependency")
                self.assertEqual((runtime / "Plugins" / custom.name).read_bytes(), b"current plugin")
                self.assertEqual((runtime / "Plugins" / "Peach.LLM.dll").read_bytes(), b"sdk plugin")
                self.assertFalse((runtime / "OTHERFixers.dll").exists())
                self.assertFalse((runtime / "Plugins" / "OTHERMutators.dll").exists())
                self.assertEqual((runtime / "python_fixup.py").read_text(), "value = 42")
                self.assertEqual(ElementTree.parse(runtime / "datamodel.xml").getroot().tag, "Peach")
                target = ElementTree.parse(runtime / "NLog.config").find("{*}targets/{*}target")
                assert target is not None
                self.assertEqual(target.attrib["fileName"], str(logs / "${var:logFileName}"))
                (runtime / "error" / "Current.log").write_text("current error")
            self.assertFalse(runtime.exists())
            self.assertEqual((logs / "error" / "Current.log").read_text(), "current error")
            self.assertEqual((sdk / "OTHERFixers.dll").read_bytes(), b"old copied fixer")
            self.assertFalse((sdk / "python_fixup.py").exists())
            self.assertFalse((plugins / custom.name).exists())

    def test_native_failure_is_forwarded_and_replay_artifacts_are_retained(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            sdk = root / "sdk"
            sdk.mkdir()
            for suffix in ("dll", "deps.json", "runtimeconfig.json"):
                (sdk / f"Peach.LLM.Validations.Mutator.{suffix}").touch()
            datamodel = root / "datamodel.xml"
            datamodel.write_text("<Peach/>")
            logs = root / "logs"

            def execute(command: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
                self.assertEqual(command, ["dotnet", str(cwd / "Peach.LLM.Validations.Mutator.dll"), "test-arg"])
                self.assertEqual(env["PEACH_LOG_DIR"], str(logs))
                self.assertEqual(env["TMPDIR"], str(logs / "replay"))
                (Path(env["TMPDIR"]) / "mutated.raw").write_bytes(b"replay")
                (logs / "error" / "Current.log").write_text('OriginalCrackCommand: ./pittool crack -v "pit.xml" "root" "seed.raw"\n')
                return subprocess.CompletedProcess(command, 23)

            with patch.object(peach_runtime, "peach_sdk_dir", return_value=sdk), \
                 patch.object(peach_runtime.shutil, "which", return_value="dotnet"), \
                 patch.object(peach_runtime.subprocess, "run", side_effect=execute):
                status = peach_runtime._run("Mutator", ["test-arg"], protocol="tftp", datamodel=datamodel, logs=logs, plugins=[])
            self.assertEqual(status, 23)
            self.assertEqual((logs / "replay" / "mutated.raw").read_bytes(), b"replay")
            self.assertIn("core.peach_runtime crack tftp --verbose", (logs / "error" / "Current.log").read_text())

    def test_include_paths_are_staged_without_wrapper_self_reference(self) -> None:
        with tempfile.TemporaryDirectory(prefix="peach include test ") as temporary:
            root = Path(temporary).resolve()
            sdk = root / "sdk"
            sdk.mkdir()
            models = root / "models"
            models.mkdir()
            wrapper = models / "wrapper.xml"
            main = models / "datamodel.xml"
            nested = models / "nested" / "fields.xml"
            nested.parent.mkdir()
            wrapper.write_text('<Peach><Include ns="packet" src="file:datamodel.xml"/></Peach>')
            main.write_text('<Peach><Include ns="fields" src="file:nested/fields.xml"/></Peach>')
            nested.write_text('<Peach><PythonPath path="."/><DataModel name="Fields"/></Peach>')
            original_contents = {path: path.read_bytes() for path in (wrapper, main, nested)}
            with peach_runtime.runtime_directory(sdk, root / "logs", datamodel=wrapper) as runtime:
                top_include = ElementTree.parse(runtime / "datamodel.xml").find("Include")
                assert top_include is not None
                staged_main = Path(top_include.attrib["src"].removeprefix("file:"))
                self.assertTrue(staged_main.is_relative_to(runtime))
                self.assertNotEqual(staged_main, runtime / "datamodel.xml")
                inner_include = ElementTree.parse(staged_main).find("Include")
                assert inner_include is not None
                staged_nested = Path(inner_include.attrib["src"].removeprefix("file:"))
                fields = ElementTree.parse(staged_nested).getroot()
                self.assertIsNotNone(fields.find("DataModel"))
                python_path = fields.find("PythonPath")
                assert python_path is not None
                self.assertEqual(python_path.attrib["path"], str(nested.parent))
            self.assertEqual({path: path.read_bytes() for path in original_contents}, original_contents)

    def test_include_cycles_fail_before_running_native_parser(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sdk = root / "sdk"
            sdk.mkdir()
            first = root / "first.xml"
            second = root / "second.xml"
            first.write_text('<Peach><Include ns="second" src="file:second.xml"/></Peach>')
            second.write_text('<Peach><Include ns="first" src="file:first.xml"/></Peach>')
            with self.assertRaisesRegex(ValueError, "Cyclic XML Include"):
                with peach_runtime.runtime_directory(sdk, root / "logs", datamodel=first):
                    self.fail("Cyclic Include should not reach the runtime")

    def test_missing_runtime_metadata_prevents_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            sdk = Path(temporary)
            (sdk / "Peach.LLM.Validations.DataModel.dll").touch()
            with patch.object(peach_runtime, "peach_sdk_dir", return_value=sdk), \
                 patch.object(peach_runtime.shutil, "which", return_value="dotnet"), \
                 patch.object(peach_runtime.subprocess, "run") as execute:
                with self.assertRaisesRegex(FileNotFoundError, "deps.json"):
                    peach_runtime._run("DataModel", [], protocol="tftp", datamodel=sdk / "datamodel.xml", logs=sdk / "logs", plugins=[])
                execute.assert_not_called()

    def test_fixer_test_plugin_does_not_load_production_fixers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "tftp"
            files = (directory / "DataElements" / "out" / "TFTPDataElements.dll",
                     directory / "Fixers" / "out" / "TFTPFixers.dll",
                     directory / "Fixers" / "Validations" / "out" / "TFTPFixerTests.dll")
            for path in files:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            self.assertEqual(peach_runtime._protocol_plugins(directory, "custom", "fixer_tests"), [files[0], files[2]])

    def test_native_fixer_resolves_nunit_and_removes_passing_case_log(self) -> None:
        sdk = peach_runtime.peach_sdk_dir()
        if shutil.which("dotnet") is None or not all((sdk / name).is_file() for name in (
            "Peach.LLM.Validations.Fixer.runtimeconfig.json", "nunit.framework.dll",
        )):
            self.skipTest("Run ./setup.sh to install the local fixer runtime")
        with tempfile.TemporaryDirectory(prefix="native-fixer-test-") as temporary:
            root = Path(temporary)
            directory = root / "llm" / "peach" / "smoke"
            validations = directory / "Fixers" / "Validations"
            validations.mkdir(parents=True)
            (directory / "datamodel.xml").write_text('<Peach xmlns="http://peachfuzzer.com/2012/Peach"/>')
            (validations / "NativeTests.cs").write_text('''
using NUnit.Framework;
using Peach.Core.Dom;
using Peach.LLM.Validations.Common;
public static class NativeTests {
    [FixerTest("native-nunit-dependency")]
    public static void Verify() {
        var value = DataElementMaker.Make<Number>("value", (ulong)7);
        Assert.That((ulong)value.DefaultValue, Is.EqualTo((ulong)7));
    }
}
''')
            with patch.object(peach_runtime, "ROOT", root):
                self.assertEqual(peach_runtime.run_fixer("smoke"), 0)
            logs = directory / "fixer_test_logs"
            self.assertIn("[PASS] 001/001 tests passed.", (logs / "fixer.log").read_text())
            self.assertFalse((logs / "native-nunit-dependency.log").exists())

    def test_data_rejects_invalid_hex_before_running_validator(self) -> None:
        with patch.object(peach_runtime, "_run") as execute:
            for value in ("1", "0x01", "zz"):
                with self.assertRaises(ValueError):
                    peach_runtime.run_data("tftp", value)
            execute.assert_not_called()

    def test_invalid_protocol_cannot_escape_project_directory(self) -> None:
        for protocol in ("../mqtt", "a/b", ""):
            with self.assertRaises(ValueError):
                peach_runtime.protocol_directory(protocol)

    def test_shell_frontends_work_outside_repository_with_spaced_paths(self) -> None:
        with tempfile.TemporaryDirectory(prefix="peach wrapper test ") as temporary:
            caller = Path(temporary).resolve()
            seeds = caller / "seed files"
            seeds.mkdir()
            datamodel = caller / "input model.xml"
            datamodel.write_text("<Peach/>")
            commands = caller / "commands"
            commands.mkdir()
            output = caller / "arguments"
            uv = commands / "uv"
            uv.write_text('#!/usr/bin/env bash\nprintf \'%s\\0\' "$@" > "$PEACH_TEST_ARGUMENTS"\n')
            uv.chmod(0o755)
            environment = os.environ.copy()
            environment["PATH"] = str(commands) + os.pathsep + environment.get("PATH", "")
            environment["PEACH_TEST_ARGUMENTS"] = str(output)
            cases = [
                ("tests/peach_mutator/run_peach_mutator_test.sh", ["TFTP", seeds.name, "OnlyMutator"],
                 ["mutator", "tftp", str(seeds), "OnlyMutator"]),
                ("tests/datamodel/run_datamodel_test.sh", ["TFTP", seeds.name, datamodel.name, "model", "log files"],
                 ["datamodel", "tftp", str(seeds), str(datamodel), "model", str(caller / "log files")]),
                ("tests/peach_fixer/run_peach_fixer_test.sh", ["tftp"], ["fixer", "tftp"]),
                ("tests/peach_fixer/run_data_test.sh", ["tftp", "0001"], ["data", "tftp", "0001"]),
            ]
            for script, arguments, expected in cases:
                with self.subTest(script=script):
                    result = subprocess.run([str(peach_runtime.ROOT / script), *arguments], cwd=caller,
                                            env=environment, text=True, capture_output=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    actual = output.read_bytes().decode().rstrip("\0").split("\0")
                    self.assertEqual(actual, ["run", "--project", str(peach_runtime.ROOT), "--directory",
                                              str(peach_runtime.ROOT), "python", "-m", "core.peach_runtime", *expected])


if __name__ == "__main__":
    unittest.main()
