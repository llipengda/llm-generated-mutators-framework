import subprocess
from typing import override

from agent import AgentConfig, build_agent_graph
from pipeline.base import BasePipeline
from ui import UI


class PeachPipeline(BasePipeline):
    def __init__(self):
        super().__init__()
        self.agent_config = AgentConfig(
            temperature=0.7,
            model="gpt-5.4",
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
            <Optional name="password_optional" src="variable_header.connect_flags" expression="(value & 0x40) != 0">
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

        Hint: Usage of size `Relation`: Put a relation with type "size" on the length field. Set the "of" attribute to point to the field whose size it defines. 

        Use the tool "Read_File" to read the usage of each XML element in "./peach/peach.txt" to understand how to define the structure for each packet type.
        Use the tool "RFC_Search" to look up the specific fields for EACH packet type in the RFC.
        Use the tool "Write_File" to save the generated Peach Pit file to "./llm/peach/{self.protocol_lower}/datamodel.xml".
        """

        self.call_agent(step2_prompt, "Step 2: Datamodel Generation")

    def verify_datamodel(self):
        with UI.status("Running Datamodel Tests..."):
            cmd = [
                "./tests/datamodel/run_datamodel_test.sh",
                self.protocol_lower,
                self.seed_dir,
            ]
            result = subprocess.run(
                cmd,
                stderr=subprocess.STDOUT,
                stdout=subprocess.PIPE,
                text=True,
            )

        last_line = result.stdout.strip().split("\n")[-1]

        UI.panel(
            result.stdout[-2000:],
            title="Test Output (Last 2000 chars)",
            border_style="grey50",
        )
        UI.panel(f"Result Line: [bold]{last_line}[/bold]")

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

        success, test_output = self.verify_datamodel()
        if success:
            UI.success("Datamodel tests passed successfully!")
            return

        UI.warn("Datamodel tests failed. Attempting to fix issues...")

        step3_prompt = f"""
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
        
        Use the "Read_File" tool to read the current datamodel from "./llm/peach/{self.protocol_lower}/datamodel.xml" and the test logs from "./llm/peach/{self.protocol_lower}/dm_test_logs/".
        Use the "RFC_Search" tool to look up any specific protocol details needed to fix the datamodel.
        Use the "Write_File" tool to save the updated Peach Pit file back to "./llm/peach/{self.protocol_lower}/datamodel.xml".
        """

        self.call_agent(step3_prompt, "Step 3: Datamodel Validation & Fix")

    def step_4_mutator_generation(self):
        UI.title("Step 4: Mutator Generation")
        packet_types = self.state.get("packet_types") or []
        if not packet_types:
            UI.warn(
                "Warning: packet_types is empty. Step 4 will not generate any mutators."
            )
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
            futures = [executor.submit(run_one, pkt_type, idx) for idx, pkt_type in enumerate(packet_types)]
            for future in as_completed(futures):
                future.result()

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
        ]
