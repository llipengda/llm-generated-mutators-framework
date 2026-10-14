"""Local SDK installation and runnable fuzz bundle integration checks."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
PROJECTS = (
    "pro/Peach/Peach.csproj",
    "pro/PitTool/PitTool.csproj",
    "llm/Validations/DataModel/Peach.LLM.Validations.DataModel.csproj",
    "llm/Validations/Mutator/Peach.LLM.Validations.Mutator.csproj",
    "llm/Validations/Fixer/Peach.LLM.Validations.Fixer.csproj",
)


class NativeSetupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="peach-native-setup-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "framework"
        self.root.mkdir()
        self.source = self.root.parent / "llm-peach"
        for project in PROJECTS:
            path = self.source / project
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("<Project />", encoding="utf-8")
        for script in ("setup.sh", "peach_gen.sh"):
            shutil.copy2(ROOT / script, self.root / script)
        docs = self.root / "docs"
        docs.mkdir()
        (docs / "llm-peach-sdk.md").write_text("SDK reference", encoding="utf-8")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "commands.jsonl"
        self.environment = os.environ.copy()
        self.environment.pop("PEACH_SDK_DIR", None)
        self.environment.pop("PEACH_SOURCE_DIR", None)
        self.environment.update({
            "PATH": f"{self.bin}{os.pathsep}{self.environment['PATH']}",
            "COMMAND_LOG": str(self.log),
        })
        self.command("xmllint", "#!/usr/bin/env bash\nexit 0\n")
        self.command("dotnet", '''#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$COMMAND_LOG"
if [ "$1" = --list-sdks ]; then
  echo '10.0.401 [/sdk]'
elif [ "$1" = publish ]; then
  project="$2"
  if [ "${FAIL_PROJECT:-}" = "$(basename "$project")" ]; then exit 9; fi
  for ((i=1; i<=$#; i++)); do
    if [ "${!i}" = --output ]; then next=$((i+1)); output="${!next}"; fi
  done
  mkdir -p "$output/runtimes/test-native"
  name="$(basename "$project" .csproj)"
  for suffix in dll deps.json runtimeconfig.json; do touch "$output/$name.$suffix"; done
  touch "$output/Peach.LLM.dll" "$output/Peach.LLM.Validations.Common.dll"
  touch "$output/nunit.framework.dll" "$output/runtimes/test-native/asset"
elif [ "${2:-}" = --showenv ]; then
  echo 'Peach environment'
elif [ "${2:-}" = makexsd ]; then
  echo '<schema />' > peach.xsd
else
  printf '%s\\n' "$@" > "$COMMAND_LOG.args"
fi
''')

    def command(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)

    def run_script(self, name: str, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(self.root / name), *arguments],
            cwd=self.root.parent,
            env=self.environment,
            capture_output=True,
            text=True,
        )

    def test_setup_publishes_complete_net8_install_and_preserves_previous_sdk(self) -> None:
        sdk = self.root / "peach" / "sdk"
        sdk.mkdir(parents=True)
        (sdk / "OldProtocol.dll").write_bytes(b"old")
        result = self.run_script("setup.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        backups = list(sdk.parent.glob("sdk.backup.*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / "OldProtocol.dll").read_bytes(), b"old")
        self.assertFalse((sdk / "OldProtocol.dll").exists())
        self.assertTrue((sdk / "runtimes/test-native/asset").is_file())
        self.assertTrue((sdk / "Plugins/Peach.LLM.dll").is_file())
        self.assertTrue((sdk / "Plugins/Peach.LLM.Validations.Common.dll").is_file())
        self.assertEqual((self.root / "peach/README.md").read_text(), "SDK reference")
        commands = self.log.read_text().splitlines()
        published = [command for command in commands if command.startswith("publish ")]
        self.assertEqual(len(published), 5)
        self.assertTrue(all("--framework net8.0" in command for command in published))
        self.assertTrue(all("-p:PeachInstallTargetFramework=net8.0" in command for command in published))
        for project in PROJECTS:
            name = Path(project).stem
            self.assertTrue((sdk / f"{name}.runtimeconfig.json").is_file())
            self.assertTrue((sdk / f"{name}.deps.json").is_file())
        launcher = subprocess.run(
            [str(sdk / "pittool"), "crack", "a.xml"],
            cwd=self.root,
            env=self.environment,
            capture_output=True,
            text=True,
        )
        self.assertEqual(launcher.returncode, 0, launcher.stderr)
        self.assertIn(str(sdk / "PitTool.dll"), Path(f"{self.log}.args").read_text())

    def test_publish_failure_keeps_previous_install_and_diagnostic_stage(self) -> None:
        sdk = self.root / "peach" / "sdk"
        sdk.mkdir(parents=True)
        (sdk / "Existing.dll").write_bytes(b"preserve me")
        self.environment["FAIL_PROJECT"] = "PitTool.csproj"
        result = self.run_script("setup.sh", "--source", str(self.source))
        self.assertEqual(result.returncode, 9, result.stdout + result.stderr)
        self.assertEqual((sdk / "Existing.dll").read_bytes(), b"preserve me")
        self.assertEqual(list(sdk.parent.glob("sdk.backup.*")), [])
        self.assertEqual(len(list(sdk.parent.glob(".sdk-install.*"))), 1)
        self.assertIn("Incomplete setup files preserved", result.stderr)

    def test_sdk_directory_override_and_removed_backend_flag(self) -> None:
        self.environment["PEACH_SDK_DIR"] = str(self.root / "custom/sdk")
        result = self.run_script("setup.sh", "--source", str(self.source))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.root / "custom/sdk/Peach.runtimeconfig.json").is_file())
        result = self.run_script("setup.sh", "--modern-sdk")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Usage:", result.stderr)

    def test_native_bundle_uses_only_its_protocol_plugins_and_escaped_defines(self) -> None:
        setup = self.run_script("setup.sh")
        self.assertEqual(setup.returncode, 0, setup.stdout + setup.stderr)
        generated = self.root / "llm/peach/demo"
        generated.mkdir(parents=True)
        (generated / "datamodel.xml").write_text("<Peach />", encoding="utf-8")
        (generated / "python_fixup.py").write_text("value = 42", encoding="utf-8")
        sdk = self.root / "peach/sdk"
        for directory in (sdk, sdk / "Plugins"):
            (directory / "OtherMutators.dll").touch()
            (directory / "OtherFixerTests.dll").touch()
            (directory / "SharedExtension.dll").touch()
        elements = generated / "DataElements/out"
        elements.mkdir(parents=True)
        (elements / "DEMODataElements.dll").touch()
        (elements / "UnmanagedDataElements.dll").touch()
        fixers = generated / "Fixers/out"
        fixers.mkdir(parents=True)
        (fixers / "DEMOFixers.dll").touch()
        (fixers / "UnmanagedFixers.dll").touch()
        seeds = self.root / "seeds/demo"
        seeds.mkdir(parents=True)
        (seeds / "one.raw").write_bytes(b"one")
        self.command("uv", '''#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "${PEACH_SDK_DIR:-}" > "$COMMAND_LOG.sdk"
for ((i=1; i<=$#; i++)); do
  if [ "${!i}" = --output ]; then next=$((i+1)); output="${!next}"; fi
done
mkdir -p "$(dirname "$output")"
touch "$output"
''')
        # The caller's relative SDK path must keep its meaning after the script
        # changes to the framework directory and starts the compiler.
        self.environment["PEACH_SDK_DIR"] = str(sdk.relative_to(self.root.parent))
        result = self.run_script("peach_gen.sh", "DEMO")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        exported_sdk = Path(Path(f"{self.log}.sdk").read_text().strip())
        self.assertTrue(exported_sdk.is_absolute())
        self.assertEqual(exported_sdk.resolve(), sdk.resolve())
        bundles = generated / "native"
        self.assertTrue((bundles / "llm/ProtocolPlugins/DEMOMutators.dll").is_file())
        self.assertFalse((bundles / "peach/ProtocolPlugins/DEMOMutators.dll").exists())
        self.assertTrue((bundles / "peach/ProtocolPlugins/DEMODataElements.dll").is_file())
        for flavor in ("llm", "peach"):
            self.assertEqual((bundles / flavor / "python_fixup.py").read_text(), "value = 42")
            plugins = bundles / flavor / "ProtocolPlugins"
            self.assertTrue((plugins / "DEMOFixers.dll").is_file())
            self.assertFalse((plugins / "UnmanagedDataElements.dll").exists())
            self.assertFalse((plugins / "UnmanagedFixers.dll").exists())
            for directory in (bundles / flavor, bundles / flavor / "Plugins"):
                self.assertFalse((directory / "OtherMutators.dll").exists())
                self.assertFalse((directory / "OtherFixerTests.dll").exists())
                self.assertTrue((directory / "SharedExtension.dll").is_file())
        self.assertTrue((sdk / "OtherMutators.dll").is_file())
        self.assertEqual((bundles / "llm/seeds/one.raw").read_bytes(), b"one")
        self.assertIn("TwoPhaseRandom", (bundles / "llm/pit.xml").read_text())
        self.assertIn('Strategy class="Random"', (bundles / "peach/pit.xml").read_text())
        env = self.environment | {"HOST": "host&name", "PORT": "1883"}
        launched = subprocess.run(
            [str(bundles / "llm/run.sh"), "--range", "1,10"],
            cwd=self.root.parent,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(launched.returncode, 0, launched.stderr)
        arguments = Path(f"{self.log}.args").read_text().splitlines()
        self.assertIn(f"--plugins={bundles}/llm/ProtocolPlugins", arguments)
        self.assertIn("HOST=host&amp;name", arguments)
        self.assertIn("PORT=1883", arguments)
        self.assertEqual(arguments[-3:], ["--range", "1,10", str(bundles / "llm/pit.xml")])
        without_port = subprocess.run(
            [str(bundles / "llm/run.sh")],
            env=self.environment,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(without_port.returncode, 0)
        self.assertIn("Set PORT", without_port.stderr)


if __name__ == "__main__":
    unittest.main()
