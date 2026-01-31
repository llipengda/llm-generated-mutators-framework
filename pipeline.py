import subprocess

from langchain_core.runnables import RunnableConfig

from agent import build_agent_graph
from config import (
    get_protocol_name,
    get_rfc_path,
    get_seed_dir,
    load_env,
    warn_if_rfc_missing,
)
from console import console
from rag import build_retriever
from state import load_pipeline_state, save_pipeline_state
from ui import ask_before_step, run_agent_step


def run_pipeline():
    load_env()

    protocol_name = get_protocol_name()
    rfc_path = get_rfc_path()
    seed_dir = get_seed_dir()

    warn_if_rfc_missing(rfc_path)
    retriever = build_retriever(rfc_path)

    agent_graph = build_agent_graph(retriever=retriever)

    config: RunnableConfig = {"configurable": {"thread_id": "session_001"}}
    protocol = protocol_name.lower()

    state: dict = {
        "packet_types": [],
        "constraints": "",
        **load_pipeline_state(),
    }

    if not isinstance(state.get("packet_types"), list):
        state["packet_types"] = []
    if not isinstance(state.get("constraints"), str):
        state["constraints"] = ""

    def step_1_fetch_packet_types():
        step1_prompt = f"""
        For {protocol_name} protocol, list all the request packet types according to the RFC document.
        Use the "RFC_Search" tool to look up the relevant sections in the RFC.
        Return the list as a comma-separated string.
        ONLY output the types, nothing else.

        When using the "RFC_Search" tool, **ASK questions instead of assuming knowledge**.
        For example:
        - "MQTT packet types"
        """

        response = run_agent_step(
            agent_graph=agent_graph,
            prompt_text=step1_prompt,
            config=config,
            step_title="Step 1: Fetch Packet Types",
        )

        packet_types_raw = response["messages"][-1].content
        packet_types = [t.strip()
                        for t in packet_types_raw.split(",") if t.strip()]
        state["packet_types"] = packet_types
        save_pipeline_state(state)
        console.print(f"[bold]Parsed Types:[/bold] {packet_types}")

    def step_2_generate_c_structures():
        packet_types = state.get("packet_types") or []
        if not packet_types:
            console.print(
                "[yellow]Warning: packet_types is empty (Step 1 may have been skipped). Step 2 will still run.[/yellow]"
            )

        step2_prompt = f"""
        Using the packet types we just identified ({packet_types}), output the precise structure of each packet in C language for {protocol_name}.

        Assign tags to each field using the labels below if matches, field with no tag is allowed:
        - /* Fixed */ : the field has a constant value and must not be mutated.
        - /* Optional */ : the field is optional and may appear or be omitted.
        - /* Repeatable */ : the field may appear multiple times.

        You can add flags to identify whether a optional field is present or not.

        Additionally, write a function `print_{protocol}_packets` to print out the details of each packet, including EVERY field and a function `generate_{protocol}_packets` to generate a list of empty packets.

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
        Use the "Save_And_Verify_Code" tool to save the COMPLETE C code to './llm/{protocol}/{protocol}_packets.h' and './llm/{protocol}/{protocol}_packets.c'.
        """

        run_agent_step(
            agent_graph=agent_graph,
            prompt_text=step2_prompt,
            config=config,
            step_title="Step 2: Generate C Structures",
        )

    def step_3_generate_parser():
        step3_prompt = f"""
        Write a production-ready parser in C language.

        size_t parse_{protocol}_msg(const u8 *buf, u32 buf_len, {protocol}_packet_t *out_packets, u32 max_count);

        Input: A buffer of a message sequence.
        Output: A list of {protocol}_packet_t.
        Return: Number of packets parsed, or 0 on failure.

        Do not use comments like /* implementation here */ or // handle headers. Every byte of the protocol must be accounted for. Use the "RFC_Search" tool to look up protocol details in the RFC.

        Write parse_<packet_type> functions for EACH packet type identified earlier, and call them in parse_{protocol}_msg.

        The {protocol}_packet_t structure is defined in './llm/{protocol}/{protocol}_packets.h'. You can use the "Read_File" tool to read it.

        Use the "Save_And_Verify_Code" tool to save the COMPLETE C code to './llm/{protocol}/{protocol}_parser.c'.
        """

        run_agent_step(
            agent_graph=agent_graph,
            prompt_text=step3_prompt,
            config=config,
            step_title="Step 3: Generate Parser",
        )

    def step_4_generate_reassembler():
        step4_prompt = f"""
        Write a production-ready reassembler in C language.

        int reassemble_{protocol}_msgs(const {protocol}_packet_t *packets, u32 num_packets, u8 *output_buf, u32 *out_len);

        Input: A list of {protocol}_packet_t.
        Output: A buffer containing the reassembled message sequence.
        Return 0 on success, non-zero on failure.

        Do not use comments like /* implementation here */ or // handle headers. Every byte of the protocol must be accounted for. Use the "RFC_Search" tool to look up protocol details in the RFC.

        Write reassemble_<packet_type> functions for EACH packet type identified earlier, and call them in reassemble_{protocol}_msgs.

        The {protocol}_packet_t structure is defined in './llm/{protocol}/{protocol}_packets.h'. You can use the "Read_File" tool to read it.

        Use the "Save_And_Verify_Code" tool to save the COMPLETE C code to './llm/{protocol}/{protocol}_reassembler.c'.
        """

        run_agent_step(
            agent_graph=agent_graph,
            prompt_text=step4_prompt,
            config=config,
            step_title="Step 4: Generate Reassembler",
        )

    def generate_h_file():
        template = f"""
#ifndef {protocol.upper()}_H
#define {protocol.upper()}_H

#include "{protocol}_packets.h"
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef uint32_t u32;
typedef uint8_t u8;

#ifdef __cplusplus
extern "C" {{
#endif

{protocol}_packet_t *generate_{protocol}_packets(int count);

size_t parse_{protocol}_msg(const uint8_t *buf, u32 buf_len,
                    {protocol}_packet_t *out_packets, u32 max_count);

void fix_{protocol}({protocol}_packet_t *pkt, int num_packets);

int reassemble_{protocol}_msgs(const {protocol}_packet_t *packets, u32 num_packets,
                        u8 *output_buf, u32 *out_len);

void print_{protocol}_packets(const {protocol}_packet_t *packets, int count);

#ifdef __cplusplus
}}
#endif

#endif /* {protocol.upper()}_H */
"""

        filepath = f"./llm/{protocol}/{protocol}.h"
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(template)

    def verify_pr():
        generate_h_file()

        console.log(
            f"[dim]Running verification script: ./tests/PR_mr/mr_test.sh {protocol} ...[/dim]"
        )

        with console.status("[bold cyan]Running Metamorphic Tests...[/bold cyan]"):
            cmd = ["./tests/PR_mr/mr_test.sh", protocol, seed_dir]
            result = subprocess.run(
                cmd,
                stderr=subprocess.STDOUT,
                stdout=subprocess.PIPE,
                text=True,
            )

        last_line = result.stdout.strip().split("\n")[-1]

        from rich.panel import Panel

        console.print(
            Panel(
                result.stdout[-2000:],
                title="Test Output (Last 2000 chars)",
                border_style="grey50",
            )
        )
        console.print(f"Result Line: [bold]{last_line}[/bold]")

        if "[FAIL]" in last_line:
            return False, result.stdout
        if "[PASS]" in last_line:
            return True, result.stdout

        return False, "Verification script did not complete as expected.\n" + result.stdout

    def step_5_verification_and_fix():
        console.rule("[bold blue]Step 5: Verification[/bold blue]")

        success, output = verify_pr()

        if not success:
            console.print(
                "[bold red]Tests Failed. Initiating Fixer Agent...[/bold red]")

            step5_prompt = f"""
            The parser and reassembler for {protocol_name} failed the metamorphic tests. Here is the output from the test script:
            {output}

            1. Analyze the test output and read the relevant files to identify the root cause of EACH failure. Output the analysis clearly.
            2. For EACH identified issue, provide the corrected code snippets for the parser and/or reassembler functions that need to be fixed.
            3. Finally, generate the corrected COMPLETE C code to the respective files, and save them using the "Save_And_Verify_Code" tool.

            Use the "Read_File" tool to read the existing code and test files.
            Use the "RFC_Search" tool to look up protocol details in the RFC as needed.
            Use the "Save_And_Verify_Code" tool to save the corrected code to the respective files.
            """

            run_agent_step(
                agent_graph=agent_graph,
                prompt_text=step5_prompt,
                config=config,
                step_title="Step 5: Autofix Parser & Reassembler",
            )

            console.rule(
                "[bold yellow]Re-running Verification After Fixes[/bold yellow]")
            success, output = verify_pr()
            if success:
                console.print(
                    "[bold green]All tests passed after fixes![/bold green]")
            else:
                console.print(
                    "[bold red]Some tests are still failing. Please review the output above.[/bold red]"
                )
        else:
            console.print(
                "[bold green]All tests passed on the first try![/bold green]")

    def step_6_mutator_generation():
        console.rule("[bold blue]Step 6: Mutator Generation[/bold blue]")

        packet_types = state.get("packet_types") or []
        if not packet_types:
            console.print(
                "[yellow]Warning: packet_types is empty. Step 6 will not generate any mutators.[/yellow]"
            )
            return

        for pkt_type in packet_types:
            mutator_prompt = f"""
            List ALL fields for the {protocol} {pkt_type} packet.

            For EACH field <field_name> in the {protocol} {pkt_type} packet:
            1. Fixed value? If the field is fixed per the spec, output exactly: not mutable and stop. Do not generate any mutator functions.
            2. Otherwise (the field is mutable):
            a. If the field is optional, implement:
            void add_<field_name>({protocol}_packet_t *pkts, size_t n);
            void delete_<field_name>({protocol}_packet_t *pkts, size_t n);
            b. If the field may appear multiple times, also implement:
            void repeat_<field_name>({protocol}_packet_t *pkts, size_t n);
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
            void mutate_<field_name>({protocol}_packet_t *pkts, size_t n);

            Write in C (minimal helpers allowed).

            Use the "Read_File" tool to read the existing code files.
            Append the generated mutator functions to './llm/{protocol}/{protocol}_mutators.c' using the "Append_And_Verify_Code" tool.
            Use the "RFC_Search" tool to look up protocol details in the RFC as needed.
            """

            run_agent_step(
                agent_graph=agent_graph,
                prompt_text=mutator_prompt,
                config=config,
                step_title=f"Mutators for {pkt_type}",
            )

    def mutator_sanity_check():
        with console.status(
            "[bold cyan]Running Mutator Sanity Check... May take a long time[/bold cyan]"
        ):
            cmd = ["./tests/mutator_sanity/run_mutator_sanity.sh",
                   protocol, seed_dir]
            result = subprocess.run(
                cmd,
                stderr=subprocess.STDOUT,
                stdout=subprocess.PIPE,
                text=True,
            )

        last_line = result.stdout.strip().split("\n")[-1]

        from rich.panel import Panel

        console.print(
            Panel(
                last_line,
                title="Mutator Sanity Check Output",
                border_style="grey50",
            )
        )

        if "[PASS]" in last_line:
            console.print(
                "[bold green]Mutator Sanity Check Passed![/bold green]")
            return True, result.stdout
        if "[FAIL]" in last_line:
            console.print(
                "[bold red]Mutator Sanity Check Failed! Please review the output above.[/bold red]"
            )
            return False, result.stdout

        console.print(
            "[bold yellow]Mutator Sanity Check did not complete as expected. Please review the output above.[/bold yellow]"
        )
        return False, result.stdout

    def step_7_mutator_sanity_check_and_fix():
        console.rule(
            "[bold blue]Step 7: Mutator Sanity Check & Fix[/bold blue]")

        success, output = mutator_sanity_check()

        if not success:
            console.print(
                "[bold red]Mutator Sanity Check Failed. Initiating Mutator Fixer Agent...[/bold red]"
            )

            step7_prompt = f"""
            The mutators for {protocol_name} failed the mutator sanity check. Here is the output from the test script:
            {output}

            1. Analyze the test output and read the relevant files to identify the root cause of EACH failure. Output the analysis clearly.
            2. For EACH identified issue, provide the corrected code snippets for the mutator functions that need to be fixed.
            3. Finally, generate the corrected COMPLETE C code to './llm/{protocol}/{protocol}_mutators.c', and save it using the "Save_And_Verify_Code" tool.

            Use the "Read_File" tool to read the existing code and test files.
            Use the "RFC_Search" tool to look up protocol details in the RFC as needed.
            Use the "Save_And_Verify_Code" tool to save the corrected code to './llm/{protocol}/{protocol}_mutators.c'.
            """

            run_agent_step(
                agent_graph=agent_graph,
                prompt_text=step7_prompt,
                config=config,
                step_title="Step 7: Autofix Mutators",
            )

            console.rule(
                "[bold yellow]Re-running Mutator Sanity Check After Fixes[/bold yellow]"
            )
            success, output = mutator_sanity_check()
            if success:
                console.print(
                    "[bold green]Mutator sanity check passed after fixes![/bold green]"
                )
            else:
                console.print(
                    "[bold red]Some mutator sanity checks are still failing. Please review the output above.[/bold red]"
                )
        else:
            console.print(
                "[bold green]All mutator sanity checks passed on the first try![/bold green]"
            )

    def step_8_fixer_generation():
        console.rule("[bold blue]Step 8: Fixer Generation[/bold blue]")

        fixer_prompt_1 = f"""
        Extract all constraints related to request (client to server) message format from the {protocol_name} RFC.
        For example, in MQTT there are the following constraints:
        SHOT-1:
            - [MQTT-2.2.1-2] A PUBLISH packet MUST NOT contain a Packet Identifier if its QoS value is set to 0.
            - [MQTT-3.1.2-11] If the Will Flag is set to 0, then the Will QoS MUST be set to 0 (0x00).
            - ...
        Use the "RFC_Search" tool to look up the relevant sections in the RFC.
        Output the constraints ONLY, nothing else.
        """

        response = run_agent_step(
            agent_graph=agent_graph,
            prompt_text=fixer_prompt_1,
            config=config,
            step_title="Extract Constraints",
        )
        constraints = response["messages"][-1].content
        state["constraints"] = constraints
        save_pipeline_state(state)

        fixer_prompt_2 = f"""
        For {protocol}, write fixer functions for EACH constraint below. 
        
        void fix_<constraint_name>({protocol}_packet_t *pkt, int num_packets);
        The input is {protocol}_packet_t array. Fix in place.

        Write a function
        void fix_{protocol}({protocol}_packet_t *pkt, int num_packets);
        that applies ALL fixers to the input packets.
        
        Write in C language.

        Use the "Read_File" tool to read the existing code files.
        Use the "Save_And_Verify_Code" tool to save the COMPLETE C code to './llm/{protocol}/{protocol}_fixers.c'.
        
        Constraints:
        {constraints}
        """

        run_agent_step(
            agent_graph=agent_graph,
            prompt_text=fixer_prompt_2,
            config=config,
            step_title="Generate Fixers",
        )

    def fixer_test_generation():
        cmd = ["python3",
               "./tests/fixer_sanity/gen_fixer_registry.py",
               "--fixers", f"./llm/{protocol}/{protocol}_fixers.c",
               "--out", f"./tests/fixer_sanity/{protocol}_fixer_registry.c",
               "--pkt-type", f"{protocol}_packet_t",
               "--exclude", f"fix_{protocol}"
               ]

        console.log(
            f"[dim]Generating fixer registry by running: {' '.join(cmd)}[/dim]"
        )

        result = subprocess.run(
            cmd,
            stderr=subprocess.STDOUT,
            stdout=subprocess.PIPE,
            text=True,
        )

        fixer_test_prompt = f"""
        You are generating C test code for validating protocol fixers.

        Task:
        Generate a single C source file:
        {protocol}_fixer_sanity_tests.c

        Inputs:
        1) protocol constraints in natural language.
        2) ./tests/fixer_sanity/{protocol}_fixer_registry.c: a list of all implemented fixers.

        Requirements:
        1. Iterate over all fixers defined in {protocol}_fixer_registry.c.
        2. For each fixer:
            1) Construct a valid {protocol} packet/state.
            2) Intentionally violate one or more constraints from the constraint file.
            3) Invoke the fixer.
            4) Check whether constraints are restored.
            5) Out put [PASS]<fixer_name> or [FAIL]<fixer_name> based on the check.
        3. Output the test results in the format:
            <status>([PASS] or [FAIL]) passed=<count> failed=<count> total=<count>
        
        The code must be self-contained, written in C (C11), and compilable.

        You can use the "Read_File" tool to read the existing code files, especially 
            ./tests/fixer_sanity/{protocol}_fixer_registry.c and ./llm/{protocol}/{protocol}_packets.h.
        Use the "Save_And_Verify_Code" tool to save the COMPLETE C code to
            ./tests/fixer_sanity/{protocol}_fixer_sanity_tests.c.
            
        Constraints:
        {state['constraints']}
        """

        new_agent_graph = build_agent_graph(retriever=retriever)
        run_agent_step(
            agent_graph=new_agent_graph,
            prompt_text=fixer_test_prompt,
            config=config,
            step_title="Generate Fixer Tests",
        )

    def fixer_sanity_check():
        fixer_test_generation()

        with console.status(
            "[bold cyan]Running Fixer Sanity Check... May take a long time[/bold cyan]"
        ):
            cmd = ["./tests/fixer_sanity/run_fixer_sanity.sh", protocol]
            result = subprocess.run(
                cmd,
                stderr=subprocess.STDOUT,
                stdout=subprocess.PIPE,
                text=True,
            )

        last_line = result.stdout.strip().split("\n")[-1]

        from rich.panel import Panel

        console.print(
            Panel(
                last_line,
                title="Fixer Sanity Check Output",
                border_style="grey50",
            )
        )

        if "[PASS]" in last_line:
            console.print(
                "[bold green]Fixer Sanity Check Passed![/bold green]")
            return True, result.stdout
        if "[FAIL]" in last_line:
            console.print(
                "[bold red]Fixer Sanity Check Failed! Please review the output above.[/bold red]"
            )
            return False, result.stdout

        console.print(
            "[bold yellow]Fixer Sanity Check did not complete as expected. Please review the output above.[/bold yellow]"
        )
        return False, result.stdout

    def step_9_fixer_sanity_check_and_fix():
        console.rule("[bold blue]Step 9: Fixer Sanity Check & Fix[/bold blue]")

        passed, result = fixer_sanity_check()

        if not passed:
            fix_fixer_prompt = f"""
            The fixers for {protocol_name} failed the fixer sanity check. Here is the output from the test script:
            {result}

            1. Analyze the test output and read the relevant files to identify the root cause of EACH failure. Output the analysis clearly.
            2. For EACH identified issue, provide the corrected code snippets for the fixer functions that need to be fixed.
            3. Finally, generate the corrected COMPLETE C code to './llm/{protocol}/{protocol}_fixers.c', and save it using the "Save_And_Verify_Code" tool.

            Use the "Read_File" tool to read the existing code and test files.
            Use the "RFC_Search" tool to look up protocol details in the RFC as needed.
            Use the "Save_And_Verify_Code" tool to save the corrected code to './llm/{protocol}/{protocol}_fixers.c'.
            """

            run_agent_step(
                agent_graph=agent_graph,
                prompt_text=fix_fixer_prompt,
                config=config,
                step_title="Autofix Fixers",
            )

            console.rule(
                "[bold yellow]Re-running Fixer Sanity Check After Fixes[/bold yellow]"
            )

            passed, result = fixer_sanity_check()

            if passed:
                console.print(
                    "[bold green]Fixer sanity check passed after fixes![/bold green]"
                )
            else:
                console.print(
                    "[bold red]Some fixer sanity checks are still failing. Please review the output above.[/bold red]"
                )
        else:
            console.print(
                "[bold green]All fixer sanity checks passed on the first try![/bold green]"
            )

    steps = [
        ("Step 1: Fetch Packet Types", step_1_fetch_packet_types),
        ("Step 2: Generate C Structures", step_2_generate_c_structures),
        ("Step 3: Generate Parser", step_3_generate_parser),
        ("Step 4: Generate Reassembler", step_4_generate_reassembler),
        ("Step 5: Parser & Reassembler Verification & Fix",
         step_5_verification_and_fix),
        ("Step 6: Mutator Generation", step_6_mutator_generation),
        ("Step 7: Mutator Sanity Check & Fix",
         step_7_mutator_sanity_check_and_fix),
        ("Step 8: Fixer Generation", step_8_fixer_generation),
        ("Step 9: Fixer Sanity Check & Fix", step_9_fixer_sanity_check_and_fix),
    ]

    i = 0
    while i < len(steps):
        step_title, step_fn = steps[i]
        action = ask_before_step(step_title, has_previous=i > 0)

        if action == "exit":
            console.print("[bold red]Exiting pipeline.[/bold red]")
            return
        if action == "retry_prev":
            if i == 0:
                console.print(
                    "[yellow]This is the first step; there is no previous step to retry.[/yellow]"
                )
            else:
                console.rule(
                    f"[yellow]Going back to previous step: {steps[i-1][0]}[/yellow]",
                    style="yellow",
                )
                i -= 1
            continue
        if action == "skip":
            console.rule(
                f"[yellow]Skipping: {step_title}[/yellow]", style="yellow")
            i += 1
            continue

        step_fn()
        i += 1

    from rich.panel import Panel

    console.print(
        Panel(
            f"Generation pipeline execution for {protocol_name} completed successfully.",
            style="bold green",
        )
    )
