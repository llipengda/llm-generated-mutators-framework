import os

from ui import UI
from pipeline.peach_steps.common import PeachStepMixin


class CompilationStep(PeachStepMixin):
    def step_final_compile(self):
        UI.title("Final Compilation")

        import glob
        import subprocess

        cs_files = []
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

        reference_dir = "./peach/sdk/"
        refs = [
            f"-r:{os.path.join(reference_dir, f)}"
            for f in os.listdir(reference_dir)
            if f.endswith(".dll")
        ]
        _, _, custom_dll = self._data_type_paths()
        if custom_dll.is_file():
            refs.append(f"-r:{custom_dll}")

        UI.dim(f"Compiling {len(cs_files)} .cs files into {output_dll}...")

        cmd = (
            ["mcs", "-sdk:4.5", "-target:library", "-out:" + output_dll]
            + refs
            + cs_files
        )
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            UI.success(f"Successfully compiled: {output_dll}")
        else:
            UI.error(f"Compilation failed:\n{result.stderr}")
