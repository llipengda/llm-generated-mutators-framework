import os

from agent import build_agent_graph
from pipeline.peach_steps.common import PeachStepMixin
from ui import UI, ask_regenerate, ask_select_types, ask_skip_verification


class MutatorSteps(PeachStepMixin):
    def step_4_mutator_generation(self):
        UI.title("Step 4: Mutator Generation")
        packet_types = self.state.get("packet_types") or []
        if not packet_types:
            UI.warn(
                "Warning: packet_types is empty. Step 4 will not generate any mutators."
            )
            return

        import os

        out_dir = f"./llm/peach/{self.protocol_lower}/Mutators/out"
        types_to_generate = []
        for pkt_type in packet_types:
            dll_name = (
                f"{self.protocol_upper}{pkt_type.capitalize()}Mutators.dll"
            )
            dll_path = os.path.join(out_dir, dll_name)
            if os.path.exists(dll_path):
                if not ask_regenerate(
                    f"mutator DLL for {pkt_type}", self.protocol_lower
                ):
                    UI.dim(f"Skipping mutator generation for {pkt_type}.")
                    continue
            types_to_generate.append(pkt_type)

        if not types_to_generate:
            UI.success("All mutator DLLs already exist and were skipped.")
            return

        types_to_generate = ask_select_types(types_to_generate, self.protocol_lower)
        if not types_to_generate:
            UI.warn("No packet types selected. Skipping mutator generation.")
            return

        def run_one(pkt_type: str, index: int):
            mutator_prompt = f"""
            List ALL fields for the {self.protocol_lower} {pkt_type} packet.

            For EACH field <field_name> in the {self.protocol_lower} {pkt_type} packet:
            1. Fixed value? If the field is fixed per the spec, output exactly: not mutable and stop. Do not generate any mutator functions.
            2. Otherwise (the field is mutable):
            a. If the field is optional, implement:
            class {self.protocol_upper}{pkt_type.capitalize()}Add<field_name.capitalize()>
            class {self.protocol_upper}{pkt_type.capitalize()}Remove<field_name.capitalize()>
            b. If the field may appear multiple times, also implement:
            class {self.protocol_upper}{pkt_type.capitalize()}Repeat<field_name.capitalize()>
            c. Mutate. Design semantic-aware mutators for this field by covering the following field-local semantic categories:
                A. Canonical form
                B. Boundaries
                C. Equivalence-class alternatives
                D. Allowed bitfield/enum/range
                E. Encoding-shape variant
                F. Padding/alignment
                G. prefix/suffix
                H. Random valid mix
            Add randomized perturbations mixing shallow and deep changes to preserve long-term diversity and avoid collapse into a single pattern.
            class {self.protocol_upper}{pkt_type.capitalize()}Mutate<field_name.capitalize()>

            Write in C# using the llm-peach sdk in namespace Peach.LLM.Generated.Mutators.{self.protocol_upper}.{pkt_type.capitalize()}. Use C# 5.0.
            ```csharp
            using System;
            using System.ComponentModel;
            using Peach.Core;
            using Peach.Core.Dom;
            using Peach.LLM.Core;
            using Peach.LLM.Core.Mutators;
            
            [Mutator("<mutator_class_name>")]
            [Description("Description of the mutator")]
            public class <mutator_class_name> : LLMMutator
            {{
                public <mutator_class_name>(DataElement obj) : base(obj) {{ }}
                public new static bool supportedDataElement(DataElement obj) 
                {{
                    // Return true if this mutator supports the given DataElement based on its name, type, or other characteristics. This is used to determine which mutators can be applied to which fields.
                    // Hint: A data element with maxOccurs and/or minOccurs is type `Array`.
                    // Hint: obj.IsIn(...) can be used to check if the DataElement is part of a specific packet type or field.
                }}

                protected override void PerformMutation(DataElement obj)
                {{
                    // Implement the mutation logic here
                    // Hint: obj.Bytes() gives you the raw bytes of the field.
                    // Hint: obj.MutatedValue = new Variant(...) can be used to set the mutated value.
                    // Hint: obj.parent.Remove(obj) can be used to remove the field.
                }}
            }}
            ```

            **You must not stop until you have generated mutators for ALL fields of the {self.protocol_lower} {pkt_type} packet, and built the DLL successfully without syntax errors.**

            Use "Read_File" to list "./llm/peach/{self.protocol_lower}/datamodel_dsl" and read shared_model.py plus the family module that defines this packet. Treat those DSL files as the DataModel source of truth; do not edit or rely on derived datamodel.xml.
            Use the "Read_File" tool to read the README of llm-peach SDK in "./peach/README.md".
            Use the "Search_Class" tool to check existing classes and class members in the SDK to understand how to implement the mutators.
            Use the "Write_File" tool to save the generated mutator code to "./llm/peach/{self.protocol_lower}/Mutators/{self.protocol_upper}{pkt_type.capitalize()}Mutators.cs".
            Use the "Build_DotNet_DLL" tool to compile the generated mutators into a DLL "./llm/peach/{self.protocol_lower}/Mutators/out/{self.protocol_upper}{pkt_type.capitalize()}Mutators.dll" and verify there are no syntax errors.
            Use the "RFC_Search" tool to look up protocol details in the RFC as needed.
            """

            agent = build_agent_graph(
                retriever=self.retriever, target="peach", config=self.agent_config
            )

            self.call_agent(
                mutator_prompt,
                f"Step 4.{index + 1}: Mutator Generation for {pkt_type}",
                agent_graph=agent,
            )

        from concurrent.futures import ThreadPoolExecutor, as_completed

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(run_one, pkt_type, idx) for idx, pkt_type in enumerate(types_to_generate)]
            for future in as_completed(futures):
                future.result()

    def step_5_mutator_validation_and_fix(self):
        UI.title("Step 5: Mutator Validation & Fix")

        import os
        import glob

        skip_first = ask_skip_verification("Mutator Validation")
        _skip_this_verify = [skip_first]  # mutable, flipped after first use
        _failing_mutators: list[str] = []  # names of currently-failing mutators

        def fix_fn(output: str, hint: str | None) -> None:
            error_log_dir = (
                f"./llm/peach/{self.protocol_lower}/mutator_test_logs/error"
            )
            error_logs = glob.glob(os.path.join(error_log_dir, "*.log"))

            if not error_logs:
                UI.success("No mutator errors to fix.")
                return

            UI.warn(
                f"Found {len(error_logs)} mutators with ERRORs. Attempting to fix..."
            )

            def fix_one(log_file: str) -> None:
                with open(log_file, "r", encoding="utf-8") as f:
                    test_output = f.read()

                mutator_name = os.path.basename(log_file).replace(".log", "")

                prompt = f"""
        We ran a verification test against the generated mutators. The test failed with an ERROR for the mutator `{mutator_name}`.
        Here is the test error output for this mutator:

        ```
        {test_output}
        ```

        The test logs indicate there are issues with the generated C# code for `{mutator_name}`.

        You need to:
        1. Find the C# file containing the `{mutator_name}` class in `./llm/peach/{self.protocol_lower}/Mutators/`. The file name should be {self.protocol_upper}<pkt_type>Mutators.cs where <pkt_type> is the packet type this mutator is associated with.
        2. Analyze the traceback and error message to understand the logic flaw or runtime exception.
        3. Use the "Read_File" tool to read the corresponding mutator file.
        4. Fix the bug in the C# code. 
        5. Use "Apply_Patch" for a localized fix. Use "Write_File" only if the
           complete file genuinely needs to be replaced.
        6. Use the "Build_DotNet_DLL" tool to recompile the mutators and ensure there are no syntax errors. The DLL should be at "./llm/peach/{self.protocol_lower}/Mutators/out/{self.protocol_upper}<pkt_type>Mutators.dll".
        
        Hint for common errors:
        - InternalValueToBitStream called on DataElement where InternalValue is not a BitStream. Type is String. 
          Do not set a Variant(String) to DataElement that is not of type String. Instead, set the a Variant of bytes using System.Text.Encoding.XX.GetBytes(...).
        - Detected duplicate child name of 'xx'.
          When copying a DataElement, use elem.Clone(newName) to avoid duplicate names in the same parent.

        Be thorough and ensure the C# code will successfully compile.
        """
                if hint:
                    prompt += (
                        f"\n\nAdditional guidance from the user:\n{hint}\n"
                    )

                agent = build_agent_graph(
                    retriever=self.retriever, target="peach", config=self.agent_config
                )

                self.call_agent(
                    prompt,
                    f"Step 5: Fix Mutator {mutator_name}",
                    agent_graph=agent,
                )

            from concurrent.futures import ThreadPoolExecutor, as_completed

            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = [executor.submit(fix_one, log_file) for log_file in error_logs]
                for future in as_completed(futures):
                    future.result()

        def verify_fn() -> tuple[bool, str]:
            if not _skip_this_verify[0]:
                cmd = [
                    "./tests/peach_mutator/run_peach_mutator_test.sh",
                    self.protocol_lower,
                    self.seed_dir,
                ]
                if _failing_mutators:
                    cmd.append(",".join(_failing_mutators))
                    UI.dim(
                        "Filtering to previously-failing mutators: "
                        + ", ".join(_failing_mutators)
                    )
                result = UI.run_with_live_output(cmd, title="Running Mutator Tests")

                # Save results to file
                results_dir = os.path.join("llm", "peach", self.protocol_lower, "mutator_test_logs")
                os.makedirs(results_dir, exist_ok=True)
                results_path = os.path.join(results_dir, "results.txt")
                with open(results_path, "w", encoding="utf-8") as f:
                    f.write(result.stdout)
                UI.dim(f"  Mutator test results saved to: {results_path}")
            _skip_this_verify[0] = False

            error_log_dir = (
                f"./llm/peach/{self.protocol_lower}/mutator_test_logs/error"
            )
            error_logs = glob.glob(os.path.join(error_log_dir, "*.log"))

            _failing_mutators[:] = [
                os.path.basename(l).replace(".log", "")
                for l in error_logs
            ]

            if not error_logs:
                return True, ""

            parts = []
            for log_file in error_logs:
                with open(log_file, "r", encoding="utf-8") as f:
                    parts.append(
                        f"--- {os.path.basename(log_file)} ---\n{f.read()}"
                    )
            return False, "\n\n".join(parts)

        if not self.fix_verify_loop(
            "Step 5: Mutator Validation & Fix", verify_fn, fix_fn
        ):
            return
