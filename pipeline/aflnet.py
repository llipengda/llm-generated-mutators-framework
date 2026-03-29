import subprocess
from typing import override

from pipeline.base import BasePipeline
from ui import UI

class AFLNetPipeline(BasePipeline):
    def step_1_fetch_packet_types(self):
        step1_prompt = f"""
        For {self.protocol_name} protocol, list all the request packet types according to the RFC document.
        Use the "RFC_Search" tool to look up the relevant sections in the RFC.
        Return the list as a comma-separated string.
        ONLY output the types, nothing else.

        When using the "RFC_Search" tool, **ASK questions instead of assuming knowledge**.
        For example:
        - "MQTT packet types"
        """

        response = self.call_agent(step1_prompt, "Step 1: Fetch Packet Types")

        packet_types_raw = response["messages"][-1].content
        packet_types = [t.strip()
                        for t in packet_types_raw.split(",") if t.strip()]
        self.state["packet_types"] = packet_types
        self.save_state()
        UI.success(f"Parsed Types: {packet_types}")

    def step_2_generate_c_structures(self):
        packet_types = self.state.get("packet_types") or []
        if not packet_types:
            UI.warn("Warning: packet_types is empty (Step 1 may have been skipped). Step 2 will still run.")

        step2_prompt = f"""
        Using the packet types we just identified ({packet_types}), output the precise structure of each packet in C language for {self.protocol_name}.

        Assign tags to each field using the labels below if matches, field with no tag is allowed:
        - /* Fixed */ : the field has a constant value and must not be mutated.
        - /* Optional */ : the field is optional and may appear or be omitted.
        - /* Repeatable */ : the field may appear multiple times.

        You can add flags to identify whether a optional field is present or not.

        Additionally, write a function `print_{self.protocol}_packets` to print out the details of each packet, including EVERY field and a function `generate_{self.protocol}_packets` to generate a list of empty packets.

        Reference structure (Shot 1):
        ```c
        // .h file
        // headers
        // macros

        typedef struct {{
            uint8_t packet_type;            /* Fixed */
            uint32_t remaining_length;
        }} mqtt_fixed_header_t;

        typedef struct {{
            uint16_t packet_identifier;
            uint32_t property_len;
            uint8_t properties[MAX_PROPERTIES_LEN]; /* Optional, Repeatable */
        }} mqtt_subscribe_variable_header_t;

        typedef struct {{
            struct {{
                char topic_filter[MAX_TOPIC_LEN];
                uint8_t qos;
            }} topic_filters[MAX_TOPIC_FILTERS];     /* Repeatable */
            uint8_t topic_count;
        }} mqtt_subscribe_payload_t;

        typedef struct {{
            mqtt_fixed_header_t fixed_header;
            mqtt_subscribe_variable_header_t variable_header;
            mqtt_subscribe_payload_t payload;
        }} mqtt_subscribe_packet_t;

        // other packet definitions...

        // enum for packet types
        typedef enum {{
            TYPE_CONNECT,
            TYPE_SUBSCRIBE,
            TYPE_PUBLISH,
            TYPE_UNSUBSCRIBE,
            TYPE_AUTH,
            TYPE_PUBACK,
            TYPE_PUBREC,
            TYPE_PUBREL,
            TYPE_PUBCOMP,
            TYPE_PINGREQ,
            TYPE_DISCONNECT,
            TYPE_UNKNOWN
        }} mqtt_type_t;

        // union of all packet types
        typedef struct {{
            mqtt_type_t type;
            union {{
                mqtt_connect_packet_t      connect;
                mqtt_subscribe_packet_t    subscribe;
                mqtt_publish_packet_t      publish;
                mqtt_unsubscribe_packet_t  unsubscribe;
                mqtt_auth_packet_t         auth;
                mqtt_puback_packet_t   puback;
                mqtt_pubrec_packet_t   pubrec;
                mqtt_pubrel_packet_t   pubrel;
                mqtt_pubcomp_packet_t  pubcomp;
                mqtt_pingreq_packet_t      pingreq;
                mqtt_disconnect_packet_t   disconnect;
            }};
        }} mqtt_packet_t;

        // .c file
        mqtt_packet_t* generate_mqtt_packets(int count) {{
            mqtt_packet_t *packets = (mqtt_packet_t *)malloc(sizeof(mqtt_packet_t) * count);
            if (packets == NULL) {{
                return NULL;
            }}
            memset(packets, 0, sizeof(mqtt_packet_t) * count);
            return packets;
        }}

        // add a function to print packet details
        void print_mqtt_packets(const mqtt_packet_t *packets, int count) {{
            // Implementation to print packet details
            // for each packet, print its type and EVERY field
        }}
        ```

        Use the tool "RFC_Search" to look up the specific fields for EACH packet type in the RFC.
        Use the "Save_And_Verify_Code" tool to save the COMPLETE C code to './llm/{self.protocol}/{self.protocol}_packets.h' and './llm/{self.protocol}/{self.protocol}_packets.c'.
        """

        self.call_agent(step2_prompt, "Step 2: Generate C Structures")

    def step_3_generate_parser(self):
        step3_prompt = f"""
        Write a production-ready parser in C language.

        size_t parse_{self.protocol_name}_msg(const u8 *buf, u32 buf_len, {self.protocol_name}_packet_t *out_packets, u32 max_count);

        Input: A buffer of a message sequence.
        Output: A list of {self.protocol}_packet_t.
        Return: Number of packets parsed, or 0 on failure.

        Do not use comments like /* implementation here */ or // handle headers. Every byte of the protocol must be accounted for. Use the "RFC_Search" tool to look up protocol details in the RFC.

        Write parse_<packet_type> functions for EACH packet type identified earlier, and call them in parse_{self.protocol}_msg.

        The {self.protocol}_packet_t structure is defined in './llm/{self.protocol}/{self.protocol}_packets.h'. You can use the "Read_File" tool to read it.

        Use the "Save_And_Verify_Code" tool to save the COMPLETE C code to './llm/{self.protocol}/{self.protocol}_parser.c'.
        """

        self.call_agent(step3_prompt, "Step 3: Generate Parser")

    def step_4_generate_reassembler(self):
        step4_prompt = f"""
        Write a production-ready reassembler in C language.

        int reassemble_{self.protocol_name}_msgs(const {self.protocol_name}_packet_t *packets, u32 num_packets, u8 *output_buf, u32 *out_len);

        Input: A list of {self.protocol_name}_packet_t.
        Output: A buffer containing the reassembled message sequence.
        Return 0 on success, non-zero on failure.

        Do not use comments like /* implementation here */ or // handle headers. Every byte of the protocol must be accounted for. Use the "RFC_Search" tool to look up protocol details in the RFC.

        Write reassemble_<packet_type> functions for EACH packet type identified earlier, and call them in reassemble_{self.protocol_name}_msgs.

        The {self.protocol_name}_packet_t structure is defined in './llm/{self.protocol_name}/{self.protocol_name}_packets.h'. You can use the "Read_File" tool to read it.

        Use the "Save_And_Verify_Code" tool to save the COMPLETE C code to './llm/{self.protocol_name}/{self.protocol_name}_reassembler.c'.
        """

        self.call_agent(step4_prompt, "Step 4: Generate Reassembler")

    def generate_h_file(self):
        template = f"""
    #ifndef {self.protocol.upper()}_H
    #define {self.protocol.upper()}_H

    #include "{self.protocol}_packets.h"
    #include <stdbool.h>
    #include <stddef.h>
    #include <stdint.h>

    typedef uint32_t u32;
    typedef uint8_t u8;

    #ifdef __cplusplus
    extern "C" {{
    #endif

    {self.protocol}_packet_t *generate_{self.protocol}_packets(int count);

    size_t parse_{self.protocol}_msg(const uint8_t *buf, u32 buf_len,
                    {self.protocol}_packet_t *out_packets, u32 max_count);

    void fix_{self.protocol}({self.protocol}_packet_t *pkt, int num_packets);

    int reassemble_{self.protocol}_msgs(const {self.protocol}_packet_t *packets, u32 num_packets,
                        u8 *output_buf, u32 *out_len);

    void print_{self.protocol}_packets(const {self.protocol}_packet_t *packets, int count);

    #ifdef __cplusplus
    }}
    #endif

    #endif /* {self.protocol.upper()}_H */
    """

        filepath = f"./llm/{self.protocol}/{self.protocol}.h"
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(template)

    def verify_pr(self):
        self.generate_h_file()

        UI.dim(f"Running verification script: ./tests/PR_mr/mr_test.sh {self.protocol} ...")

        with UI.status("Running Metamorphic Tests..."):
            cmd = ["./tests/PR_mr/mr_test.sh", self.protocol, self.seed_dir]
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
        UI.dim(f"Result Line: [bold]{last_line}[/bold]")

        if "[FAIL]" in last_line:
            return False, result.stdout
        if "[PASS]" in last_line:
            return True, result.stdout

        return False, "Verification script did not complete as expected.\n" + result.stdout

    def step_5_verification_and_fix(self):
        UI.title("Step 5: Verification")

        success, output = self.verify_pr()

        if not success:
            UI.error("Tests Failed. Initiating Fixer Agent...")

            step5_prompt = f"""
            The parser and reassembler for {self.protocol_name} failed the metamorphic tests. Here is the output from the test script:
            {output}

            1. Analyze the test output and read the relevant files to identify the root cause of EACH failure. Output the analysis clearly.
            2. For EACH identified issue, provide the corrected code snippets for the parser and/or reassembler functions that need to be fixed.
            3. Finally, generate the corrected COMPLETE C code to the respective files, and save them using the "Save_And_Verify_Code" tool.

            Use the "Read_File" tool to read the existing code and test files.
            Use the "RFC_Search" tool to look up protocol details in the RFC as needed.
            Use the "Save_And_Verify_Code" tool to save the corrected code to the respective files.
            """

            self.call_agent(step5_prompt, "Step 5: Autofix Parser & Reassembler")

            UI.warn("Re-running Verification After Fixes")
            success, output = self.verify_pr()
            if success:
                UI.success("All tests passed after fixes!")
            else:
                UI.error("Some tests are still failing. Please review the output above.")
        else:
            UI.success("All tests passed on the first try!")

    def step_6_mutator_generation(self):
        UI.title("Step 6: Mutator Generation")

        packet_types = self.state.get("packet_types") or []
        if not packet_types:
            UI.warn("Warning: packet_types is empty. Step 6 will not generate any mutators.")
            return

        for pkt_type in packet_types:
            mutator_prompt = f"""
            List ALL fields for the {self.protocol} {pkt_type} packet.

            For EACH field <field_name> in the {self.protocol} {pkt_type} packet:
            1. Fixed value? If the field is fixed per the spec, output exactly: not mutable and stop. Do not generate any mutator functions.
            2. Otherwise (the field is mutable):
            a. If the field is optional, implement:
            void add_<field_name>({self.protocol}_packet_t *pkts, size_t n);
            void delete_<field_name>({self.protocol}_packet_t *pkts, size_t n);
            b. If the field may appear multiple times, also implement:
            void repeat_<field_name>({self.protocol}_packet_t *pkts, size_t n);
            c. Mutate pkts in place. Design semantic-aware mutators for this field by covering the following field-local semantic categories:
                A. Canonical form
                B. Boundaries
                C. Equivalence-class alternatives
                D. Allowed bitfield/enum/range
                E. Encoding-shape variant
                F. Padding/alignment
                G. prefix/suffix
                H. Random valid mix
            Add randomized perturbations mixing shallow and deep changes to preserve long-term diversity and avoid collapse into a single pattern.
            void mutate_<field_name>({self.protocol}_packet_t *pkts, size_t n);

            Write in C (minimal helpers allowed).

            Use the "Read_File" tool to read the existing code files.
            Append the generated mutator functions to './llm/{self.protocol}/{self.protocol}_mutators.c' using the "Append_And_Verify_Code" tool.
            Use the "RFC_Search" tool to look up protocol details in the RFC as needed.
            """

            self.call_agent(mutator_prompt, f"Mutators for {pkt_type}")

    def mutator_sanity_check(self):
        with UI.status("Running Mutator Sanity Check... May take a long time"):
            cmd = ["./tests/mutator_sanity/run_mutator_sanity.sh",
                    self.protocol, self.seed_dir]
            result = subprocess.run(
                cmd,
                stderr=subprocess.STDOUT,
                stdout=subprocess.PIPE,
                text=True,
            )

        last_line = result.stdout.strip().split("\n")[-1]

        UI.panel(
            last_line,
            title="Mutator Sanity Check Output",
            border_style="grey50",
        )

        if "[PASS]" in last_line:
            UI.success("Mutator Sanity Check Passed!")
            return True, result.stdout
        if "[FAIL]" in last_line:
            UI.error("Mutator Sanity Check Failed! Please review the output above.")
            return False, result.stdout

        UI.warn("Mutator Sanity Check did not complete as expected. Please review the output above.")
        return False, result.stdout

    def step_7_mutator_sanity_check_and_fix(self):
        UI.title("Step 7: Mutator Sanity Check & Fix")

        success, output = self.mutator_sanity_check()

        if not success:
            UI.error("Mutator Sanity Check Failed. Initiating Mutator Fixer Agent...")

            step7_prompt = f"""
            The mutators for {self.protocol_name} failed the mutator sanity check. Here is the output from the test script:
            {output}

            1. Analyze the test output and read the relevant files to identify the root cause of EACH failure. Output the analysis clearly.
            2. For EACH identified issue, provide the corrected code snippets for the mutator functions that need to be fixed.
            3. Finally, generate the corrected COMPLETE C code to './llm/{self.protocol}/{self.protocol}_mutators.c', and save it using the "Save_And_Verify_Code" tool.

            Use the "Read_File" tool to read the existing code and test files.
            Use the "RFC_Search" tool to look up protocol details in the RFC as needed.
            Use the "Save_And_Verify_Code" tool to save the corrected code to './llm/{self.protocol}/{self.protocol}_mutators.c'.
            """

            self.call_agent(step7_prompt, "Step 7: Autofix Mutators")

            UI.warning_rule("Re-running Mutator Sanity Check After Fixes")
            success, output = self.mutator_sanity_check()
            if success:
                UI.success("Mutator sanity check passed after fixes!")
            else:
                UI.error("Some mutator sanity checks are still failing. Please review the output above.")
        else:
            UI.success("All mutator sanity checks passed on the first try!")

    def step_8_fixer_generation(self):
        UI.title("Step 8: Fixer Generation")

        fixer_prompt_1 = f"""
        Extract all constraints related to request (client to server) message format from the {self.protocol_name} RFC.
        For example, in MQTT there are the following constraints:
        SHOT-1:
            - [MQTT-2.2.1-2] A PUBLISH packet MUST NOT contain a Packet Identifier if its QoS value is set to 0.
            - [MQTT-3.1.2-11] If the Will Flag is set to 0, then the Will QoS MUST be set to 0 (0x00).
            - ...
        Use the "RFC_Search" tool to look up the relevant sections in the RFC.
        Output the constraints ONLY, nothing else.
        """

        response = self.call_agent(fixer_prompt_1, "Extract Constraints")
        constraints = response["messages"][-1].content
        self.state["constraints"] = constraints
        self.save_state()

        fixer_prompt_2 = f"""
        For {self.protocol}, write fixer functions for EACH constraint below. 
        
        void fix_<constraint_name>({self.protocol}_packet_t *pkt, int num_packets);
        The input is {self.protocol}_packet_t array. Fix in place.

        Write a function
        void fix_{self.protocol}({self.protocol}_packet_t *pkt, int num_packets);
        that applies ALL fixers to the input packets.
        
        Write in C language.

        Use the "Read_File" tool to read the existing code files.
        Use the "Save_And_Verify_Code" tool to save the COMPLETE C code to './llm/{self.protocol}/{self.protocol}_fixers.c'.
        
        Constraints:
        {constraints}
        """

        self.call_agent(fixer_prompt_2, "Generate Fixers")

    def fixer_test_generation(self):
        cmd = ["python3",
                "./tests/fixer_sanity/gen_fixer_registry.py",
                "--fixers", f"./llm/{self.protocol}/{self.protocol}_fixers.c",
                "--out", f"./tests/fixer_sanity/{self.protocol}_fixer_registry.c",
                "--pkt-type", f"{self.protocol}_packet_t",
                "--exclude", f"fix_{self.protocol}"
                ]

        UI.dim(
            f"Generating fixer registry by running: {' '.join(cmd)}"
        )

        subprocess.run(
            cmd,
            stderr=subprocess.STDOUT,
            stdout=subprocess.PIPE,
            text=True,
        )

        fixer_test_prompt = f"""
        You are generating C test code for validating protocol fixers.

        Task:
        Generate a single C source file:
        {self.protocol}_fixer_sanity_tests.c

        Inputs:
        1) protocol constraints in natural language.
        2) ./tests/fixer_sanity/{self.protocol}_fixer_registry.c: a list of all implemented fixers.

        Requirements:
        1. Iterate over all fixers defined in {self.protocol}_fixer_registry.c.
        2. For each fixer:
            1) Construct a valid {self.protocol} packet/state.
            2) Intentionally violate one or more constraints from the constraint file.
            3) Invoke the fixer.
            4) Check whether constraints are restored.
            5) Out put [PASS]<fixer_name> or [FAIL]<fixer_name> based on the check.
        3. Output the test results in the format:
            <status>([PASS] or [FAIL]) passed=<count> failed=<count> total=<count>
        
        The code must be self-contained, written in C (C11), and compilable.

        You can use the "Read_File" tool to read the existing code files, especially 
            ./tests/fixer_sanity/{self.protocol}_fixer_registry.c and ./llm/{self.protocol}/{self.protocol}_packets.h.
        Use the "Save_And_Verify_Code" tool to save the COMPLETE C code to
            ./tests/fixer_sanity/{self.protocol}_fixer_sanity_tests.c.
            
        Constraints:
        {self.state['constraints']}
        """

        new_agent_graph = self.new_agent()
        self.call_agent(fixer_test_prompt, "Generate Fixer Tests", agent_graph=new_agent_graph)

    def fixer_sanity_check(self):
        self.fixer_test_generation()

        with UI.status("Running Fixer Sanity Check... May take a long time"):
            cmd = ["./tests/fixer_sanity/run_fixer_sanity.sh", self.protocol]
            result = subprocess.run(
                cmd,
                stderr=subprocess.STDOUT,
                stdout=subprocess.PIPE,
                text=True,
            )

        last_line = result.stdout.strip().split("\n")[-1]

        UI.panel(
            last_line,
            title="Fixer Sanity Check Output",
            border_style="grey50",
        )

        if "[PASS]" in last_line:
            UI.success("Fixer Sanity Check Passed!")
            return True, result.stdout
        if "[FAIL]" in last_line:
            UI.error("Fixer Sanity Check Failed! Please review the output above.")
            return False, result.stdout

        UI.warn("Fixer Sanity Check did not complete as expected. Please review the output above.")
        return False, result.stdout

    def step_9_fixer_sanity_check_and_fix(self):
        UI.title("Step 9: Fixer Sanity Check & Fix")

        passed, result = self.fixer_sanity_check()

        if not passed:
            fix_fixer_prompt = f"""
            The fixers for {self.protocol_name} failed the fixer sanity check. Here is the output from the test script:
            {result}

            1. Analyze the test output and read the relevant files to identify the root cause of EACH failure. Output the analysis clearly.
            2. For EACH identified issue, provide the corrected code snippets for the fixer functions that need to be fixed.
            3. Finally, generate the corrected COMPLETE C code to './llm/{self.protocol}/{self.protocol}_fixers.c', and save it using the "Save_And_Verify_Code" tool.

            Use the "Read_File" tool to read the existing code and test files.
            Use the "RFC_Search" tool to look up protocol details in the RFC as needed.
            Use the "Save_And_Verify_Code" tool to save the corrected code to './llm/{self.protocol}/{self.protocol}_fixers.c'.
            """

            self.call_agent(fix_fixer_prompt, "Autofix Fixers")

            UI.warning_rule("Re-running Fixer Sanity Check After Fixes")

            passed, result = self.fixer_sanity_check()

            if passed:
                UI.success("Fixer sanity check passed after fixes!")
            else:
                UI.error("Some fixer sanity checks are still failing. Please review the output above.")
        else:
            UI.success("All fixer sanity checks passed on the first try!")

    @override
    def steps(self):
        steps = [
            ("Step 1: Fetch Packet Types", self.step_1_fetch_packet_types),
            ("Step 2: Generate C Structures", self.step_2_generate_c_structures),
            ("Step 3: Generate Parser", self.step_3_generate_parser),
            ("Step 4: Generate Reassembler", self.step_4_generate_reassembler),
            ("Step 5: Parser & Reassembler Verification & Fix",
                self.step_5_verification_and_fix),
            ("Step 6: Mutator Generation", self.step_6_mutator_generation),
            ("Step 7: Mutator Sanity Check & Fix",
                self.step_7_mutator_sanity_check_and_fix),
            ("Step 8: Fixer Generation", self.step_8_fixer_generation),
            ("Step 9: Fixer Sanity Check & Fix", self.step_9_fixer_sanity_check_and_fix),
        ]

        return steps