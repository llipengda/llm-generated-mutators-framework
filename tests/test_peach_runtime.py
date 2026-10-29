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
from core.mutator_codegen import (
    MutatorPlan, MutatorSpec, PacketMutatorPlan, SemanticHook,
    disable_mutators, prepare_packet,
)


class NativeRuntimeTests(unittest.TestCase):
    def test_mutator_summary_uses_compact_status_tags(self) -> None:
        summary = peach_runtime._mutator_summary([
            "[RESULT] Passing TOTAL=100 PASS=1 FAIL=99 UNCHANGED=0",
            "[RESULT] NoPass TOTAL=100 PASS=0 FAIL=0 UNCHANGED=100",
            "[ERROR] Throwing",
            "[SKIP][000/000] Unmatched",
        ])
        self.assertEqual(summary, "Mutator Test Results:\n"
                         "[P] Passing TOTAL=100 PASS=1 FAIL=99 UNCHANGED=0\n"
                         "[N] NoPass TOTAL=100 PASS=0 FAIL=0 UNCHANGED=100\n"
                         "[N] Throwing\n"
                         "[-] Unmatched\n")
        self.assertEqual(peach_runtime._mutator_summary([
            "[P] Changed TOTAL=4 PASS=4 FAIL=0 ERROR=0 UNCHANGED=0",
            "[N] Throws TOTAL=4 PASS=0 FAIL=0 ERROR=4 UNCHANGED=0",
            "[-] Unmatched",
        ]), "Mutator Test Results:\n"
            "[P] Changed TOTAL=4 PASS=4 FAIL=0 ERROR=0 UNCHANGED=0\n"
            "[N] Throws TOTAL=4 PASS=0 FAIL=0 ERROR=4 UNCHANGED=0\n"
            "[-] Unmatched\n")

    def test_native_mutator_classifies_unchanged_and_partial_cracks_with_replay(self) -> None:
        sdk = peach_runtime.peach_sdk_dir()
        if shutil.which("dotnet") is None or not (sdk / "Peach.LLM.Validations.Mutator.runtimeconfig.json").is_file():
            self.skipTest("Run ./setup.sh to install the local mutator runtime")
        with tempfile.TemporaryDirectory(prefix="mutator-outcomes-test-") as temporary:
            root = Path(temporary).resolve()
            # A fixed marker lets truncated/invalid input fail, while a changed
            # value remains valid. A third byte parses as an unconsumed suffix.
            datamodel = root / "datamodel.xml"
            datamodel.write_text('''
<Peach xmlns="http://peachfuzzer.com/2012/Peach">
  <DataModel name="Root">
    <Number name="marker" size="8" value="171" token="true"/>
    <Number name="value" size="8"/>
  </DataModel>
</Peach>
''', encoding="utf-8")
            seeds = root / "seeds"
            seeds.mkdir()
            seed = b"\xab\x07"
            (seeds / "value.raw").write_bytes(seed)
            mutations = {
                "NoOp": "",
                "SameValue": "obj.MutatedValue = new Variant((ulong)7);",
                "Changed": "obj.MutatedValue = new Variant((ulong)9);",
                "TrailingBytes": "obj.parent.MutatedValue = new Variant(new byte[] { 171, 7, 99 });",
                "Truncated": "obj.parent.MutatedValue = new Variant(new byte[] { 171 });",
                "Throws": 'throw new System.InvalidOperationException("test mutation error");',
                "MixedPass": "if (System.Threading.Interlocked.Increment(ref calls) % 2 == 0) "
                             "obj.MutatedValue = new Variant((ulong)9);",
                "MixedFail": "if (System.Threading.Interlocked.Increment(ref calls) % 2 == 0) "
                             "obj.parent.MutatedValue = new Variant(new byte[] { 171, 7, 99 });",
                "MixedError": "if (System.Threading.Interlocked.Increment(ref calls) % 2 == 0) "
                              'throw new System.InvalidOperationException("mixed mutation error");',
                "MixedPassError": "if (System.Threading.Interlocked.Increment(ref calls) % 2 == 0) "
                                  'throw new System.InvalidOperationException("mixed mutation error"); '
                                  "obj.MutatedValue = new Variant((ulong)9);",
                "MixedAll": "int outcome = System.Threading.Interlocked.Increment(ref calls) % 4; "
                            "if (outcome == 1) obj.MutatedValue = new Variant((ulong)9); "
                            "if (outcome == 2) obj.parent.MutatedValue = new Variant(new byte[] { 171, 7, 99 });",
            }
            source = root / "Mutators.cs"
            source.write_text(
                "using Peach.Core; using Peach.Core.Dom; using Peach.LLM.Core.Mutators;\n"
                + "\n".join(
                    f"public class {name} : LLMMutator {{\n"
                    + ("private static int calls;\n" if name.startswith("Mixed") else "")
                    + f"public {name}(DataElement obj) : base(obj) {{ }}\n"
                    + 'public new static bool supportedDataElement(DataElement obj) { return obj.Name == "value"; }\n'
                    + f"protected override void PerformMutation(DataElement obj) {{ {body} }}\n}}"
                    for name, body in mutations.items()
                ), encoding="utf-8",
            )
            plugin = root / "OutcomeMutators.dll"
            build = peach_runtime.compile_csharp([source], plugin)
            self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
            logs = root / "logs"
            environment = os.environ.copy()
            replay = root / "replay"
            replay.mkdir()
            environment.update(PEACH_LOG_DIR=str(logs), TMPDIR=str(replay), TMP=str(replay), TEMP=str(replay))
            with peach_runtime.runtime_directory(sdk, logs, plugins=[plugin], datamodel=datamodel) as runtime:
                result = subprocess.run(
                    ["dotnet", str(runtime / "Peach.LLM.Validations.Mutator.dll"),
                     str(runtime / "datamodel.xml"), str(seeds), "Root", "4"],
                    cwd=runtime, env=environment, capture_output=True, text=True, timeout=120,
                )
            report = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, report)
            summary = report.rsplit("Mutator Test Results:", 1)[1]
            for name, counts in {
                "NoOp": (0, 0, 4), "SameValue": (0, 0, 4), "Changed": (4, 0, 0),
                "TrailingBytes": (0, 4, 0), "Truncated": (0, 4, 0),
                "MixedPass": (2, 0, 2), "MixedFail": (0, 2, 2), "MixedAll": (1, 1, 2),
            }.items():
                passed, failed, unchanged = counts
                tag = "[P]" if passed else "[N]"
                self.assertIn(f"{tag} {name} TOTAL=4 PASS={passed} FAIL={failed} ERROR=0 UNCHANGED={unchanged}", summary)
            for name in ("Throws", "MixedError", "MixedPassError"):
                self.assertRegex(summary, rf"\[N\] {name} TOTAL=4 PASS=\d+ FAIL=\d+ ERROR=[1-4] UNCHANGED=\d+")
            self.assertEqual(len(summary.strip().splitlines()), len(mutations))
            for name in ("NoOp", "SameValue", "MixedPass", "MixedFail", "MixedError", "MixedAll"):
                log = (logs / "unchanged" / f"{name}.log").read_text()
                self.assertIn("Reproduced: True", log)
                self.assertIn("Serialized packet is unchanged", log)
                sample = next(line.removeprefix("MutatedFile: ") for line in log.splitlines()
                              if line.startswith("MutatedFile: "))
                self.assertEqual(Path(sample).read_bytes(), seed)
            for name in ("TrailingBytes", "MixedFail", "MixedAll"):
                log = (logs / "fail" / f"{name}.log").read_text()
                self.assertIn("Reproduced: True", log)
                self.assertIn("8 bits remain (1 whole bytes); consumed 16/24 bits", log)
                sample = next(line.removeprefix("MutatedFile: ") for line in log.splitlines()
                              if line.startswith("MutatedFile: "))
                self.assertEqual(Path(sample).read_bytes(), seed + b"\x63")
            self.assertTrue(any(path.read_bytes() == seed for path in replay.rglob("NoOp_*.raw")))
            self.assertTrue(any(path.read_bytes() == seed + b"\x63" for path in replay.rglob("TrailingBytes_*.raw")))

    def test_native_mutator_skips_persistently_disabled_throwing_implementation(self) -> None:
        sdk = peach_runtime.peach_sdk_dir()
        if shutil.which("dotnet") is None or not (sdk / "Peach.LLM.Validations.Mutator.runtimeconfig.json").is_file():
            self.skipTest("Run ./setup.sh to install the local mutator runtime")
        with tempfile.TemporaryDirectory(prefix="disabled-mutator-test-") as temporary:
            root = Path(temporary).resolve()
            directory = root / "llm" / "peach" / "smoke"
            output = directory / "Mutators"
            specs = tuple(MutatorSpec(
                packet_type="request", path="smoke_packet_array.value", operation="Mutate",
                class_name=name, namespace="Smoke", base_class="LLMMutator",
                expression='obj.MatchesPath(@"smoke_packet_array.value")',
                empty_branch_name=None, description="Smoke test value",
                hooks=(SemanticHook("MutateCore", "private void MutateCore(DataElement obj)"),),
                runtime_type="Number", kind="int", dsl_path="value", condition=None, source_path=None,
            ) for name in ("SmokeDisabled", "SmokeEnabled"))
            packet = PacketMutatorPlan(
                "request", output / "Smoke.g.cs", output / "Smoke.semantic.cs",
                output / "out" / "SmokeRequest.dll", specs,
            )
            plan = MutatorPlan("smoke", directory / "datamodel_dsl", output, (packet,))
            prepare_packet(plan, packet)
            packet.semantic_file.write_text('''
using Peach.Core;
using Peach.Core.Dom;
namespace Smoke {
    public partial class SmokeDisabled {
        private void MutateCore(DataElement obj) { throw new System.InvalidOperationException("must never execute"); }
    }
    public partial class SmokeEnabled {
        private void MutateCore(DataElement obj) { obj.MutatedValue = new Variant((ulong)9); }
    }
}
''', encoding="utf-8")
            disable_mutators(plan, ["SmokeDisabled"])
            (directory / "datamodel.xml").write_text('''
<Peach xmlns="http://peachfuzzer.com/2012/Peach">
  <DataModel name="smoke_packet_array"><Number name="value" size="8"/></DataModel>
</Peach>
''', encoding="utf-8")
            seeds = root / "seeds"
            seeds.mkdir()
            (seeds / "value.raw").write_bytes(b"\x07")
            result = subprocess.run([
                "uv", "run", "python", "-c",
                "import sys; from pathlib import Path; from core import peach_runtime; "
                "peach_runtime.ROOT = Path(sys.argv[1]); "
                "raise SystemExit(peach_runtime.run_mutator('smoke', Path(sys.argv[2])))",
                str(root), str(seeds),
            ], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
            report = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, report)
            self.assertRegex(report, r"\[-\]\s+SmokeDisabled")
            self.assertIn("[P] SmokeEnabled TOTAL=100 PASS=100 FAIL=0 ERROR=0 UNCHANGED=0", report)

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
                (runtime / "unchanged" / "NoOp.log").write_text("unchanged packet")
            self.assertFalse(runtime.exists())
            self.assertEqual((logs / "error" / "Current.log").read_text(), "current error")
            self.assertEqual((logs / "unchanged" / "NoOp.log").read_text(), "unchanged packet")
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

            class FakeProcess:
                def __init__(self) -> None:
                    self.stdout = iter(["Mutator Test Results:\n", "[ERROR] Current\n"])

                def __enter__(self) -> FakeProcess:
                    return self

                def __exit__(self, *_args: object) -> None:
                    pass

                def wait(self) -> int:
                    return 23

            def execute(command: list[str], *, cwd: Path, env: dict[str, str],
                        **_kwargs: object) -> FakeProcess:
                self.assertEqual(command, ["dotnet", str(cwd / "Peach.LLM.Validations.Mutator.dll"), "test-arg"])
                self.assertEqual(env["PEACH_LOG_DIR"], str(logs))
                self.assertEqual(env["TMPDIR"], str(logs / "replay"))
                (Path(env["TMPDIR"]) / "mutated.raw").write_bytes(b"replay")
                (logs / "error" / "Current.log").write_text('OriginalCrackCommand: ./pittool crack -v "pit.xml" "root" "seed.raw"\n')
                return FakeProcess()

            with patch.object(peach_runtime, "peach_sdk_dir", return_value=sdk), \
                 patch.object(peach_runtime.shutil, "which", return_value="dotnet"), \
                 patch.object(peach_runtime.subprocess, "Popen", side_effect=execute):
                status = peach_runtime._run("Mutator", ["test-arg"], protocol="tftp", datamodel=datamodel, logs=logs, plugins=[])
            self.assertEqual(status, 23)
            self.assertEqual((logs / "summary.log").read_text(), "Mutator Test Results:\n[N] Current\n")
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
