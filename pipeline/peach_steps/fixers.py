import os

from agent import build_agent_graph
from pipeline.peach_steps.common import PeachStepMixin
from ui import UI


class FixerSteps(PeachStepMixin):
    def step_6_constraint_extraction(self):
        UI.title("Step 6: Constraint Extraction")

        prompt = f"""
        Extract all constraints related to REQUEST(client->server) message format from the {self.protocol_name} RFC.
        For example, in MQTT there are the following constraints:
            - [MQTT-2.2.1-2] A PUBLISH packet MUST NOT contain a Packet Identifier if its QoS value is set to 0.

            - [MQTT-3.1.2-11] If the Will Flag is set to 0, then the Will QoS MUST be set to 0 (0x00).

            - ...
        Use the "RFC_Search" tool to look up the relevant sections in the RFC.
        Split the constraints into separate blocks with double newlines (\\n\\n) between them.
        Add a tag [<ConstraintID>] at the beginning of each constraint.
        Output the constraints ONLY, nothing else.
        """

        response = self.call_agent(prompt, "Step 6: Constraint Extraction")
        constraints = response["messages"][-1].content
        self.state["constraints"] = constraints
        self.save_state()
        UI.success("Constraints extracted successfully.")

    def step_6_1_constraint_filtering(self):
        UI.title("Step 6.1: Constraint Filtering")
        constraints = self.state.get("constraints") or ""
        if not constraints:
            UI.warn(
                "Warning: constraints is empty (Step 6 may have been skipped). Step 6.1 will still run."
            )
            return
        prompt = f"""
        For each constraint extracted of {self.protocol_name}, you need to:

        1. List "./llm/peach/{self.protocol_lower}/datamodel_dsl" and read its shared_model.py and family modules as the DataModel source of truth.
        2. Check if the constraint is already guaranteed by the structure of the datamodel. 
            Hint: Check the Relation and Optional elements. 
            A note for `Optional` DataElement If the expression evaluates to true, the Optional field must be present. However, the presence of the field does not imply that the expression is true.
        3. If the constraint is already guaranteed, write [GUARANTEED][<ConstraintID>]<ConstraintText>//<explanation of why it's guaranteed in a sentence>. Otherwise, write [NOT GUARANTEED][<ConstraintID>]<ConstraintText>.
        4. Write the output to "./llm/peach/{self.protocol_lower}/constraint_analysis.txt", separated by double newlines (\\n\\n) between constraints.

        constraints:
        {constraints}
        """

        self.call_agent(prompt, "Step 6.1: Constraint Filtering")

    def step_7_fixer_generation(self):
        UI.title("Step 7: Fixer Generation")

        constraints = ""
        with open(f"./llm/peach/{self.protocol_lower}/constraint_analysis.txt", "r", encoding="utf-8") as f:
            constraints = f.read()

        constraint_blocks = list(map(lambda x: x.replace("[NOT GUARANTEED]", ""), 
                                filter(lambda c: c.startswith("[NOT GUARANTEED]"), 
                                       [c.strip() for c in constraints.split("\n\n") if c.strip()])))
        
        # Group constraints into chunks of 2
        chunk_size = 3
        chunks = [constraint_blocks[i:i + chunk_size] for i in range(0, len(constraint_blocks), chunk_size)]

        def run_fixer_chunk(chunk: list[str], index: int):
            chunk_content = "\n\n".join(chunk)
            prompt = f"""
            For {self.protocol_lower}, write fixer functions for EACH constraint below.
            
            **CRITICAL:** 
            - You MUST implement a fixer function for EVERY SINGLE constraint provided here.
            - DO NOT leave placeholders like "// more constraints". 
            - DO NOT abbreviate or truncate the code. Output the complete implementation for all items.
            - When writing helper functions, add Part{index} to the function name. This is important to avoid naming conflicts across chunks.
            
            Write in C# 5.0.
            File: ./llm/peach/{self.protocol_lower}/Fixers/{self.protocol_upper}Fixers_part_{index}.cs
            ```csharp
            using System;
            using Peach.Core;
            using Peach.Core.Dom;
            using Peach.LLM.Core;
            using Encoding = System.Text.Encoding;
            
            namespace Peach.LLM.Generated.Fixups.{self.protocol_upper} 
            {{
                public partial class {self.protocol_upper}Fixers 
                {{
                    // Add the constraint content as a comment above each fixer function for clarity.
                    public static void Fix<ConstraintID>(DataElement obj) 
                    {{
                        // The input is a single {self.protocol_lower}_<pkt_type>_packet_t. Fix in place.
                    }}
                }}
            }}
            ```
            The input to each fixer function is a single packet (e.g., mqtt_connect_packet_t). The function should fix the packet in place to make it compliant with the constraint. You need to:
            1. Check if the constraint is related to the packet type. If not, do nothing and return.
            2. Check if the packet violates the constraint. If not, do nothing and return.
            3. Modify the fields of the packet to fix the violation according to the constraint.

            When fixing, follow these principles:
            - Preserve original values as much as possible, and only modify the minimal set of fields necessary to satisfy the constraint.
            - Avoid unnecessary overwrites or resetting fields to default values.
            - Prefer small, local adjustments over drastic changes.
            - When multiple valid fixes exist, introduce reasonable diversity in the fix strategy instead of always applying the same pattern.

            Useful hints:
            - Find a field: obj.find("<field_name>") or obj.find("a")?.find("b") if you want to find "a.b";
            - Modify a field: <field>.SetValue(new Variant(...));
            - Delete a field: <field>.parent.Remove(<field>);
            - Make a Optional filed present: <field>.SetValue(new Variant(...)); (if the field is in a Optional wrapper, setting a value will make all the fields in the Optional present).


            Constraints for this task:
            {chunk_content}

            You must ensure there are NO syntax errors and the code compiles successfully.

            Use "Read_File" to list "./llm/peach/{self.protocol_lower}/datamodel_dsl" and read the relevant shared_model.py and family modules. Do not edit the derived datamodel.xml.
            Use the "Read_File" tool to read the README of llm-peach SDK in "./peach/README.md".
            Use the "Search_Class" tool to check existing classes and class members in the SDK to understand how to implement the fixers.
            Use the "Write_File" tool to save the generated fixer code to "./llm/peach/{self.protocol_lower}/Fixers/{self.protocol_upper}Fixers_part_{index}.cs".
            Use the "Build_DotNet_DLL" tool to compile the generated fixers into a DLL "./llm/peach/{self.protocol_lower}/Fixers/out/{self.protocol_upper}Fixers_part_{index}.dll" and verify there are no syntax errors.
            Use the "RFC_Search" tool to look up protocol details in the RFC as needed.
            """

            agent = build_agent_graph(
                retriever=self.retriever, config=self.agent_config
            )

            self.call_agent(
                prompt,
                f"Step 7.1.{index}: Fixer Generation Part {index}",
                agent_graph=agent,
            )

        from concurrent.futures import ThreadPoolExecutor, as_completed

        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = [executor.submit(run_fixer_chunk, chunk, idx) for idx, chunk in enumerate(chunks)]
            for future in as_completed(futures):
                future.result()

        UI.title("Step 7.2: Fixup Class Generation")

        fixup_prompt = f"""
        Now that we have generated the individual fixer functions in partial classes, we need to generate the main Fixup class that calls them.

        Write the main Fixup class in C# 5.0.
        File: ./llm/peach/{self.protocol_lower}/Fixers/{self.protocol_upper}Fixup.cs
        ```csharp
        using System;
        using System.Collections.Generic;
        using System.ComponentModel;
        using NLog;
        using Peach.Core;
        using Peach.Core.Dom;
        using Peach.LLM.Core;
        using Peach.LLM.Core.Fixups;

        namespace Peach.LLM.Generated.Fixups.{self.protocol_upper} 
        {{
            [Description("{self.protocol_upper} Fixup.")]
            [Fixup("{self.protocol_upper}Fixup", true)]
            [Parameter("ref", typeof(DataElement), "Reference to data element")]
            [Serializable]
            public class {self.protocol_upper}Fixup : LLMFixup
            {{
                public DataElement _ref {{ get; protected set; }}
                [NonSerialized]
                private static readonly NLog.Logger _logger = LogManager.GetCurrentClassLogger();
                public {self.protocol_upper}Fixup(DataElement parent, Dictionary<string, Variant> args) : base(parent, args, "ref") {{ ParameterParser.Parse(this, args); }}

                protected override Variant fixupImpl()
                {{
                    if (!ShouldFixup)
                        return elements["ref"].InternalValue;
                    var elem = elements["ref"].Clone();
                    var packets = elem.find("packets") as Peach.Core.Dom.Array;
                    var before = elem.Bytes();
                    try
                    {{
                        for (int i = 0; i < packets.Count; i++)
                        {{
                            var p = (packets[i].find("packet_union") as Choice).SelectedElement;
                            // For each constraint, call the corresponding fixer function on the packet. 
                            // You can identify the packet type by checking which field in the choice is populated. For example, if p.Name == "connect", then it's a <proto>_connect_packet_t.
                        }}
                    }}
                    catch (NullReferenceException ex)
                    {{
                        _logger.Error(ex, "{self.protocol_upper} Fixup failed due to missing expected elements. Skipping fixup.");
                        return new Variant(before);
                    }}
                    catch (Exception ex)
                    {{
                        _logger.Error(ex, "{self.protocol_upper} Fixup failed. Skipping fixup.");
                        return new Variant(before);
                    }}
                    return elem.InternalValue;
                }}
            }}
        }}

        YOU MUST ensure there are NO syntax errors and the code compiles successfully. Fix syntax errors by reading the error messages, fixing the code, and rebuilding until there are no syntax errors.
        This class should call ALL the fixer functions generated.
        
        Use the "Read_File" tool to read the generated partial classes in "./llm/peach/{self.protocol_lower}/Fixers/" to see the exact names of the static Fix methods to call.
        Use the "Write_File" tool to save the generated fixer code to "./llm/peach/{self.protocol_lower}/Fixers/{self.protocol_upper}Fixup.cs".
        Use the "Build_DotNet_DLL" tool to compile ALL the generated fixers (.cs files in "./llm/peach/{self.protocol_lower}/Fixers/") into a DLL "./llm/peach/{self.protocol_lower}/Fixers/out/{self.protocol_upper}Fixers.dll" and verify there are no syntax errors.
        """

        self.call_agent(fixup_prompt, "Step 7.2: Fixup Class Generation")

    def step_7_5_fixer_constraint_mapping(self):
        UI.title("Step 7.5: Fixer-Constraint Mapping")

        mapping_prompt = f"""
        Create a mapping document that clearly maps each constraint to the corresponding fixer function that address it.

        The mapping should be in a txt format and saved to "./llm/peach/{self.protocol_lower}/Fixers/fixer_constraint_mapping.txt".

        For each constraint, list:
        - The exact text of the constraint (copy-paste from the original constraints).
        - The name of the fixer function that are designed to fix this constraint.
        If the constraint is guaranteed by the datamodel and does not have a corresponding fixer, ignore it and do not include it in the mapping.

        Format:
Constraint: [Exact constraint text]
Fixer Function: [C# static method name, e.g., FixMQTT2212]
\\n\\n

        OUTPUT ONLY THE MAPPING, NOTHING ELSE. DO NOT OUTPUT ANY EXPLANATION OR EXTRA TEXT.

        This mapping is critical for traceability and future maintenance, so be thorough and accurate.

        Use the "Read_File" tool to read the generated fixers in "./llm/peach/{self.protocol_lower}/Fixers/" to identify which functions correspond to which constraints.
        Use the "Write_File" tool to save the generated mapping document to "./llm/peach/{self.protocol_lower}/Fixers/fixer_constraint_mapping.txt".
        """

        self.call_agent(mapping_prompt, "Step 7.5: Fixer-Constraint Mapping")

    def step_8_fixer_test_generation(self):
        UI.title("Step 8: Fixer Test Generation")

        import os
        dll_source = f"./llm/peach/{self.protocol_lower}/Fixers/out/{self.protocol_upper}Fixers.dll"
        dll_destination = f"./peach/sdk/{self.protocol_upper}Fixers.dll"
        if os.path.exists(dll_source):
            import shutil
            shutil.copy(dll_source, dll_destination)
            UI.success(f"Copied {dll_source} to {dll_destination} for test compilation.")
        else:
            UI.warn(f"Expected DLL not found at {dll_source}. Make sure Step 7 completed successfully. Step 8 may fail to compile tests without the fixers DLL.")

        # constraints = self.state.get("constraints") or ""
        constraints = ''
        with open(f"./llm/peach/{self.protocol_lower}/Fixers/fixer_constraint_mapping.txt", "r", encoding="utf-8") as f:
            constraints = f.read()
        if not constraints:
            UI.warn("Warning: constraints is empty. Step 8 will not generate any tests.")
            return

        constraint_blocks = [c.strip() for c in constraints.split("\n\n") if c.strip()]
        
        # Group constraints into chunks of 2
        chunk_size = 3
        chunks = [constraint_blocks[i:i + chunk_size] for i in range(0, len(constraint_blocks), chunk_size)]

        def run_test_chunk(chunk: list[str], index: int):
            chunk_content = "\n\n".join(chunk)
            prompt = f"""
            For {self.protocol_lower}, write NUnit test functions for validating EACH fixer constraint below.

            For EACH constraint and its corresponding fixer function:
            1. Generate a Peach DataElement that **violates** the constraint. Base its structure on the DSL modules in "./llm/peach/{self.protocol_lower}/datamodel_dsl", and make it a packet_array containing one violating packet.
            2. Apply the fixer function to the violating DataElement.
            3. Assert that after the fixer is applied, the DataElement now **complies** with the constraint.
            You should generate at least one test function per constraint, but you can generate more if there are multiple ways to violate the constraint or if the constraint has multiple components.

            Here is an example structure for the test in file "./tests/peach_fixer/example.cs".

            Write in C# 5.0.
            File: ./llm/peach/{self.protocol_lower}/Fixers/Validations/{self.protocol_upper}FixerTest_part_{index}.cs

            Constraints for this task:
            {chunk_content}

            IMPORTANT:
            1. You MUST implement one or more test function(s) for EVERY SINGLE constraint provided here.
            2. DO NOT leave placeholders.
            3. Add Part{index} to helper function names to avoid naming conflicts across chunks.
            4. You must ensure there are NO syntax errors and the code compiles successfully.
            5. You must NOT read the Fixer functions. You should treat the Fixers as a black box and only focus on testing the constraints. 

            Use "Read_File" to list "./llm/peach/{self.protocol_lower}/datamodel_dsl" and read the relevant shared_model.py and family modules. Do not edit the derived datamodel.xml.
            Use the "Write_File" tool to save the generated test code to "./llm/peach/{self.protocol_lower}/Fixers/Validations/{self.protocol_upper}FixerTest_part_{index}.cs".
            Use the "Build_DotNet_DLL" tool to compile the test file. Ensure it compiles successfully without syntax errors. The DLL should be at "./llm/peach/{self.protocol_lower}/Fixers/Validations/out/{self.protocol_upper}FixerTest_part_{index}.dll".
            """

            agent = build_agent_graph(
                retriever=self.retriever, config=self.agent_config
            )

            self.call_agent(
                prompt,
                f"Step 8.{index}: Fixer Validation Generation Part {index}",
                agent_graph=agent,
            )

        from concurrent.futures import ThreadPoolExecutor, as_completed

        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = [executor.submit(run_test_chunk, chunk, idx) for idx, chunk in enumerate(chunks)]
            for future in as_completed(futures):
                future.result()

    def step_9_fixer_validation_and_fix(self):
        UI.title("Step 9: Fixer Validation & Fix")

        import os
        import glob

        def verify_fn() -> tuple[bool, str]:
            cmd = [
                "./tests/peach_fixer/run_peach_fixer_test.sh",
                self.protocol_lower,
                self.seed_dir,
            ]
            result = UI.run_with_live_output(
                cmd, title="Running Fixer Tests"
            )

            log_dir = (
                f"./llm/peach/{self.protocol_lower}/fixer_test_logs"
            )
            all_logs = glob.glob(os.path.join(log_dir, "*.log"))
            fail_logs = [
                l for l in all_logs if os.path.basename(l) != "fixer.log"
            ]

            if not fail_logs:
                return True, ""

            parts = []
            for log_file in fail_logs:
                with open(log_file, "r", encoding="utf-8") as f:
                    parts.append(
                        f"--- {os.path.basename(log_file)} ---\n{f.read()}"
                    )
            return False, "\n\n".join(parts)

        def fix_fn(output: str, hint: str | None) -> None:
            log_dir = (
                f"./llm/peach/{self.protocol_lower}/fixer_test_logs"
            )
            all_logs = glob.glob(os.path.join(log_dir, "*.log"))
            fail_logs = [
                l for l in all_logs if os.path.basename(l) != "fixer.log"
            ]

            if not fail_logs:
                UI.success(
                    "No fixer test failures to fix (already resolved)."
                )
                return

            UI.warn(
                f"Found {len(fail_logs)} fixer tests with failures. Attempting to fix..."
            )

            for log_file in fail_logs:
                with open(log_file, "r", encoding="utf-8") as f:
                    test_output = f.read()

                test_name = os.path.basename(log_file).replace(".log", "")

                prompt = f"""
        We ran a verification test against the generated fixers. The test failed for the fixer/test `{test_name}`.
        Here is the test error output:

        ```
        {test_output}
        ```

        The test logs indicate there are issues with either the generated C# code for the fixer or the test itself.

        You need to:
        1. Find the C# file containing the fixer function in `./llm/peach/{self.protocol_lower}/Fixers/` and the test in `./llm/peach/{self.protocol_lower}/Fixers/Validations/`.
        2. Analyze the traceback and error message to understand the logic flaw or runtime exception.
        3. Use the "Read_File" tool to read the corresponding file(s).
        4. Fix the bug in the C# code. Make sure to handle potential nulls, index out of bounds, etc., that might occur at runtime.
        5. Use "Apply_Patch" for localized fixes. Use "Write_File" only if a
           complete file genuinely needs to be replaced.
        6. Use the "Build_DotNet_DLL" tool to recompile:
           - The fixers DLL at "./llm/peach/{self.protocol_lower}/Fixers/out/{self.protocol_upper}Fixers.dll"
           - The test DLL at "./llm/peach/{self.protocol_lower}/Fixers/Validations/out/{self.protocol_upper}FixerTests.dll"
           Ensure there are no syntax errors.

        Be thorough and ensure the C# code will successfully compile and pass the tests.
        """
                if hint:
                    prompt += (
                        f"\n\nAdditional guidance from the user:\n{hint}\n"
                    )

                self.call_agent(
                    prompt, f"Step 9: Fix Fixer Test {test_name}"
                )

        if not self.fix_verify_loop(
            "Step 9: Fixer Validation & Fix", verify_fn, fix_fn
        ):
            return
