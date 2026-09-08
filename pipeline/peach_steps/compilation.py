import os

from core.peach_sdk import compile_csharp
from core.ui import UI
from pipeline.peach_steps.common import PeachStepMixin


class CompilationStep(PeachStepMixin):
    def step_final_compile(self) -> None:
        UI.title("Final Compilation")

        import glob

        cs_files: list[str] = []
        mutators_dir = f"./llm/peach/{self.protocol_lower}/Mutators/"
        fixers_dir = f"./llm/peach/{self.protocol_lower}/Fixers/"

        if os.path.isdir(mutators_dir):
            cs_files.extend(glob.glob(os.path.join(mutators_dir, "*.cs")))
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

        _, _, custom_dll = self._data_type_paths()
        extra_refs = [custom_dll] if custom_dll.is_file() else []

        UI.dim(f"Compiling {len(cs_files)} .cs files into {output_dll}...")

        result = compile_csharp(
            cs_files,
            output_dll,
            additional_references=extra_refs,
            warnings_as_errors=False,
        )
        if result.returncode == 0:
            UI.success(f"Successfully compiled: {output_dll}")
        else:
            UI.error(f"Compilation failed:\n{result.stdout}{result.stderr}")
