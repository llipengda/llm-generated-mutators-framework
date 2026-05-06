import os
from typing import override

from agent import AgentConfig, build_agent_graph
from pipeline.base import BasePipeline
from ui import UI, ask_regenerate, ask_skip_verification


def _env_float(key: str, default: float) -> float:
    val = os.environ.get(key)
    if val is None:
        return default
    try:
        return float(val)
    except ValueError:
        return default


class PeachPipeline(BasePipeline):
    def __init__(self):
        super().__init__()
        peach_model = os.environ.get("LLM_PEACH_MODEL") or os.environ.get("LLM_MODEL") or "gpt-5.4"
        self.agent_config = AgentConfig(
            temperature=_env_float("LLM_PEACH_TEMPERATURE", _env_float("LLM_TEMPERATURE", 0.7)),
            model=peach_model,
            system_prompt="You are a helpful assistant expert in C# programming, protocol fuzzing and Peach Fuzzer.",
        )
        self.agent_graph = build_agent_graph(
            retriever=self.retriever, target="peach", config=self.agent_config
        )

    def step_1_packet_types_extraction(self):
        UI.title("Step 1: Packet Types Extraction")

        step1_prompt = f"""
        For {self.protocol_name} protocol, list ALL the packet types according to the RFC document.

        Use the "RFC_Search" tool to look up the relevant sections in the RFC.
        Return the list as a comma-separated string.
        ONLY output the types, nothing else.

        When using the "RFC_Search" tool, **ASK questions instead of assuming knowledge**.
        For example:
        - "MQTT packet types"
        """

        response = self.call_agent(step1_prompt, "Step 1: Packet Types Extraction")

        packet_types_raw = response["messages"][-1].content
        packet_types = [t.strip() for t in packet_types_raw.split(",") if t.strip()]
        self.state["packet_types"] = packet_types
        self.save_state()
        UI.success(f"[bold]Parsed Types:[/bold] {packet_types}")

    def step_2_datamodel_generation(self):
        UI.title("Step 2: Datamodel Generation")

        packet_types = self.state.get("packet_types") or []
        if not packet_types:
            UI.warn(
                "Warning: packet_types is empty (Step 1 may have been skipped). Step 2 will still run."
            )

        step2_prompt = f"""
        Using the packet types we just identified ({packet_types}), generate a Peach Pit file defining the precise structure of each packet for {self.protocol_name}. 
        **Model the structure as detail as possible**.

        Reference structure (Shot 1):
        ```xml
        <?xml version="1.0" encoding="utf-8"?>
        <Peach xmlns="http://peachfuzzer.com/2012/Peach" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="http://peachfuzzer.com/2012/Peach /peach/peach.xsd">
        <Defaults>
            <Number signed="false" endian="big"/>
        </Defaults>

        <!-- MQTT UTF-8 String: 2-byte length prefix (big endian) + UTF-8 string -->
        <DataModel name="MQTT_String">
            <Number name="length" size="16">
                <Relation type="size" of="value"/>
            </Number>
            <String name="value" type="utf8"/>
        </DataModel>

        <!-- Fixed Header: message_type (4 bits) + flags (4 bits) + remaining_length (MqttVarInt) -->
        <DataModel name="mqtt_fixed_header_t">
            <Number name="message_type" size="4"/>
            <Number name="flags" size="4"/>
            <MqttVarInt name="remaining_length">
                <Relation type="size" of="msg_body"/>
            </MqttVarInt>
        </DataModel>

        <!-- Connect Variable Header (MQTT 5.0) -->
        <DataModel name="mqtt_connect_variable_header_t">
            <Block name="protocol_name" ref="MQTT_String"/>
            <Number name="protocol_level" size="8"/>
            <Number name="connect_flags" size="8"/>
            <Number name="keep_alive" size="16"/>
            <MqttVarInt name="property_length">
                <Relation type="size" of="properties"/>
            </MqttVarInt>
            <Blob name="properties"/>
        </DataModel>

        <!-- Connect Payload (MQTT 5.0) -->
        <DataModel name="mqtt_connect_payload_t">
            <Block name="client_id" ref="MQTT_String"/>
            <!-- Will Properties (optional, if Will Flag is set) -->
            <Optional name="will_optional" src="variable_header.connect_flags" expression="(value & 0x04) != 0">
                <MqttVarInt name="will_property_length">
                    <Relation type="size" of="will_properties"/>
                </MqttVarInt>
                <Blob name="will_properties"/>
                <!-- Will Topic (optional, if Will Flag is set) -->
                <Block name="will_topic" ref="MQTT_String"/>
                <!-- Will Payload (optional, if Will Flag is set) -->
                <Number name="will_payload_length" size="16">
                    <Relation type="size" of="will_payload"/>
                </Number>
                <Blob name="will_payload"/>
            </Optional>
            <!-- User Name (optional, if Username Flag is set) -->
            <Optional name="username_optional" src="variable_header.connect_flags" expression="(value & 0x80) != 0">
                <Block name="user_name" ref="MQTT_String"/>
            </Optional>
            <!-- Password (optional, if Password Flag is set) -->
            <Optional name="password_optional" src="variable_header.connect_flags" expression="(value &amp; 0x40) != 0">
                <Number name="password_length" size="16">
                    <Relation type="size" of="password"/>
                </Number>
                <Blob name="password"/>
            </Optional>
        </DataModel>

        <!-- Connect Packet -->
        <DataModel name="mqtt_connect_packet_t">
            <Block name="fixed_header" ref="mqtt_fixed_header_t">
                <Number name="message_type" size="4" value="1" token="true"/>
                <Number name="flags" size="4" value="0" token="true"/>
            </Block>
            <Block name="msg_body">
                <Block name="variable_header" ref="mqtt_connect_variable_header_t"/>
                <Block name="payload" ref="mqtt_connect_payload_t"/>
            </Block>
        </DataModel>

        <!-- Similar structures would be defined for other packet types (PUBLISH, SUBSCRIBE, etc.) -->

        <!-- Union of all packet types -->
        <DataModel name="mqtt_packet_t">
            <Choice name="packet_union">
                <Block name="connect" ref="mqtt_connect_packet_t"/>
                <Block name="subscribe" ref="mqtt_subscribe_packet_t"/>
                <Block name="publish" ref="mqtt_publish_packet_t"/>
                <Block name="unsubscribe" ref="mqtt_unsubscribe_packet_t"/>
                <Block name="auth" ref="mqtt_auth_packet_t"/>
                <Block name="puback" ref="mqtt_puback_packet_t"/>
                <Block name="pubrec" ref="mqtt_pubrec_packet_t"/>
                <Block name="pubrel" ref="mqtt_pubrel_packet_t"/>
                <Block name="pubcomp" ref="mqtt_pubcomp_packet_t"/>
                <Block name="pingreq" ref="mqtt_pingreq_packet_t"/>
                <Block name="disconnect" ref="mqtt_disconnect_packet_t"/>
            </Choice>
        </DataModel>

        <!-- Array of packets for fuzzing -->
        <!-- mqtt_packet_t[] - Array of MQTT packets -->
        <DataModel name="mqtt_packet_array">
            <Block name="packets" minOccurs="1" maxOccurs="100">
                <Block ref="mqtt_packet_t"/>
            </Block>
        </DataModel>
        </Peach>
        ```

        Hint: 
        1. Usage of size `Relation`: Put a relation with type "size" on the length field. Set the "of" attribute to point to the field whose size it defines. 
        2. Use `Optional` to model optional fields with constraint, with the "src" attribute pointing to the flags field and an "expression" that checks the relevant bit(s) in the flags.
        3. Use `Block` with `minOccurs=0` and `maxOccurs=1` to model optional fields without constraints.
        

        Use the tool "Read_File" to read the usage of each XML element in "./peach/peach.txt" to understand how to define the structure for each packet type.
        Use the tool "RFC_Search" to look up the specific fields for EACH packet type in the RFC.
        Use the tool "Write_File" to save the generated Peach Pit file to "./llm/peach/{self.protocol_lower}/datamodel.xml".
        """

        self.call_agent(step2_prompt, "Step 2: Datamodel Generation")

    def verify_datamodel(self):
        cmd = [
            "./tests/datamodel/run_datamodel_test.sh",
            self.protocol_lower,
            self.seed_dir,
        ]
        result = UI.run_with_live_output(
            cmd, title="Running Datamodel Tests"
        )

        last_line = result.stdout.strip().split("\n")[-1]
        UI.panel(f"Result: [bold]{last_line}[/bold]")

        if "[FAIL]" in last_line:
            return False, result.stdout
        if "[PASS]" in last_line:
            return True, result.stdout

        return (
            False,
            "Verification script did not complete as expected.\n" + result.stdout,
        )

    def step_3_datamodel_validation_and_fix(self):
        UI.title("Step 3: Datamodel Validation & Fix")

        def fix_fn(test_output: str, hint: str | None) -> None:
            prompt = f"""
        We ran a verification test against the generated datamodel, which used the Peach Pit file to parse packets and check if the fields were correctly extracted.
        The test failed, indicating there are issues with the datamodel.
        Here is the test output:

        ```
        {test_output}
        ```
        The test logs were written to "./llm/peach/{self.protocol_lower}/dm_test_logs/<test_file_with_extension>.log". You can read these logs to get more details on what went wrong.

        You need to:
        1. Read EACH test log file in "./llm/peach/{self.protocol_lower}/dm_test_logs/" to identify the specific issues with the datamodel. Look for parsing errors, mismatched fields, or any indications of what part of the datamodel is incorrect.
        2. For each identified issue, output a clear explanation of what the problem is and which part of the datamodel it relates to.
        3. Modify the Peach Pit file to fix the identified issues.

        **CRITICAL**: Simplifying the DataModel is NOT allowed.

        Use the "Read_File" tool to read the current datamodel from "./llm/peach/{self.protocol_lower}/datamodel.xml" and the test logs from "./llm/peach/{self.protocol_lower}/dm_test_logs/".
        Use the "RFC_Search" tool to look up any specific protocol details needed to fix the datamodel.
        Use the "Write_File" tool to save the updated Peach Pit file back to "./llm/peach/{self.protocol_lower}/datamodel.xml".
        """
            if hint:
                prompt += (
                    f"\n\nAdditional guidance from the user:\n{hint}\n"
                )

            self.call_agent(prompt, "Step 3: Datamodel Validation & Fix")

        self.fix_verify_loop(
            "Step 3: Datamodel Validation & Fix",
            self.verify_datamodel,
            fix_fn,
        )

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

            Use the "Read_File" tool to read the datamodel generated in "./llm/peach/{self.protocol_lower}/datamodel.xml".
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
        4. Fix the bug in the C# code. Make sure to handle potential nulls, index out of bounds, etc., that might occur during `PerformMutation`.
        5. Use the "Write_File" tool to update the file with the fix.
        6. Use the "Build_DotNet_DLL" tool to recompile the mutators and ensure there are no syntax errors. The DLL should be at "./llm/peach/{self.protocol_lower}/Mutators/out/{self.protocol_upper}<pkt_type>Mutators.dll".

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
                UI.run_with_live_output(cmd, title="Running Mutator Tests")
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

        1. Read the Datamodel in "./llm/peach/{self.protocol_lower}/datamodel.xml".
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

            Use the "Read_File" tool to read the datamodel generated in "./llm/peach/{self.protocol_lower}/datamodel.xml".
            Use the "Read_File" tool to read the README of llm-peach SDK in "./peach/README.md".
            Use the "Search_Class" tool to check existing classes and class members in the SDK to understand how to implement the fixers.
            Use the "Write_File" tool to save the generated fixer code to "./llm/peach/{self.protocol_lower}/Fixers/{self.protocol_upper}Fixers_part_{index}.cs".
            Use the "Build_DotNet_DLL" tool to compile the generated fixers into a DLL "./llm/peach/{self.protocol_lower}/Fixers/out/{self.protocol_upper}Fixers_part_{index}.dll" and verify there are no syntax errors.
            Use the "RFC_Search" tool to look up protocol details in the RFC as needed.
            """

            agent = build_agent_graph(
                retriever=self.retriever, target="peach", config=self.agent_config
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
            1. Generate a Peach DataElement that **violates** the constraint. The generated structure should be based on the datamodel in "./llm/peach/{self.protocol_lower}/datamodel.xml", and should be a packet_array containing a single packet that violates the constraint.
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

            Use the "Read_File" tool to read the datamodel generated in "./llm/peach/{self.protocol_lower}/datamodel.xml".
            Use the "Write_File" tool to save the generated test code to "./llm/peach/{self.protocol_lower}/Fixers/Validations/{self.protocol_upper}FixerTest_part_{index}.cs".
            Use the "Build_DotNet_DLL" tool to compile the test file. Ensure it compiles successfully without syntax errors. The DLL should be at "./llm/peach/{self.protocol_lower}/Fixers/Validations/out/{self.protocol_upper}FixerTest_part_{index}.dll".
            """

            agent = build_agent_graph(
                retriever=self.retriever, target="peach", config=self.agent_config
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
        5. Use the "Write_File" tool to update the file(s) with the fix.
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

    @override
    def steps(self):
        return [
            ("Step 1: Packet Types Extraction", self.step_1_packet_types_extraction),
            ("Step 2: Datamodel Generation", self.step_2_datamodel_generation),
            (
                "Step 3: Datamodel Validation & Fix",
                self.step_3_datamodel_validation_and_fix,
            ),
            ("Step 4: Mutator Generation", self.step_4_mutator_generation),
            ("Step 5: Mutator Validation & Fix", self.step_5_mutator_validation_and_fix),
            ("Step 6: Constraint Extraction", self.step_6_constraint_extraction),
            ("Step 6.1: Constraint Filtering", self.step_6_1_constraint_filtering),
            ("Step 7: Fixer Generation", self.step_7_fixer_generation),
            ("Step 7.5: Fixer-Constraint Mapping", self.step_7_5_fixer_constraint_mapping),
            ("Step 8: Fixer Test Generation", self.step_8_fixer_test_generation),
            ("Step 9: Fixer Validation & Fix", self.step_9_fixer_validation_and_fix),
        ]
