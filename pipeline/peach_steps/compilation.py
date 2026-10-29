import os
from pathlib import Path

from core.peach_sdk import compile_csharp
from core.mutator_codegen import manifest_sources
from core.ui import UI
from pipeline.peach_steps.common import PeachStepMixin


class CompilationStep(PeachStepMixin):
    def step_final_compile(self) -> None:
        UI.title("Final Compilation")

        import glob

        cs_files: list[str] = []
        mutators_dir = f"./llm/peach/{self.protocol_lower}/Mutators/"
        fixers_dir = f"./llm/peach/{self.protocol_lower}/Fixers/"
        _, data_elements_dir, _ = self._data_type_paths()

        # References do not embed type definitions in the final protocol DLL.
        cs_files.extend(str(path) for path in sorted(data_elements_dir.glob("*.cs")) if path.is_file())
        if os.path.isdir(mutators_dir):
            cs_files.extend(str(path) for path in manifest_sources(Path(mutators_dir)))
        if os.path.isdir(fixers_dir):
            cs_files.extend(
                f for f in glob.glob(os.path.join(fixers_dir, "*.cs"))
                if "Validations" not in f
            )

        if not cs_files:
            UI.warn("No .cs files found to compile.")
            return

        output_dll = f"./llm/peach/{self.protocol_lower}/{self.protocol_upper}.dll"
        os.makedirs(os.path.dirname(output_dll), exist_ok=True)

        UI.dim(f"Compiling {len(cs_files)} .cs files into {output_dll}...")

        result = compile_csharp(
            cs_files,
            output_dll,
            warnings_as_errors=False,
        )
        if result.returncode == 0:
            UI.success(f"Successfully compiled: {output_dll}")
        else:
            raise RuntimeError(f"Compilation failed:\n{result.stdout}{result.stderr}")
