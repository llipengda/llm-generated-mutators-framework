import json
import os
from pathlib import Path
from typing import override

from agent import AgentConfig, build_agent_graph
from config import get_fixer_enabled
from pipeline.base import BasePipeline
from ui import (
    UI,
    ask_regenerate,
    ask_reuse_diagnosis,
    ask_select_types,
    ask_skip_verification,
)


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
        diagnosis_config = AgentConfig(
            temperature=0.0,
            model=os.environ.get("LLM_DIAGNOSER_MODEL") or peach_model,
            system_prompt=(
                "You are an expert in binary protocol parsing and Peach Pit "
                "DataModels. Diagnose failures directly from files by using "
                "Read_File, and use RFC_Search when protocol semantics need "
                "confirmation. Write only the completed diagnosis report to "
                "the explicitly requested path with Write_File; never modify "
                "the DataModel, logs, or any other file."
            ),
        )
        self.diagnosis_agent_graph = build_agent_graph(
            retriever=self.retriever,
            target="peach",
            config=diagnosis_config,
            tool_names={"Read_File", "RFC_Search", "Write_File"},
        )
        autofix_config = AgentConfig(
            temperature=self.agent_config.temperature,
            model=peach_model,
            system_prompt=(
                "You repair Peach Pit DataModels strictly from a completed "
                "diagnosis report. Read only that report and the current "
                "DataModel, then write only the repaired DataModel. Do not "
                "inspect validator output, failure logs, or RFC sources."
            ),
        )
        self.datamodel_autofix_agent_graph = build_agent_graph(
            retriever=self.retriever,
            target="peach",
            config=autofix_config,
            tool_names={"Read_File", "Write_File"},
        )

    def step_1_packet_types_extraction(self):
        UI.title("Step 1: Packet Types Extraction")

        step1_prompt = f"""
        For {self.protocol_name} protocol, list ALL the packet types according to the RFC document.

        Use the "RFC_Search" tool to look up the relevant sections in the RFC.
        Return the list as a comma-separated string.
        ONLY output the types without any additional explanation or formatting. For example: "CONNECT, PUBLISH, SUBSCRIBE, UNSUBSCRIBE, AUTH, PUBACK, PUBREC, PUBREL, PUBCOMP, PINGREQ, DISCONNECT".

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
        Generate one complete Peach Pit file that precisely models every requested
        {self.protocol_name} packet type: {packet_types}.

        Before generating anything, use "Read_File" to read BOTH:
        - "./prompts/peach_datamodel_example.xml" for the required document shape,
          decomposition, and naming style. It is a structural example, not a
          complete MQTT model; never copy its protocol facts and never omit a
          requested packet merely because the example omits it.
        - "./peach/peach.txt" for the supported Peach XML elements and their syntax.

        Use "RFC_Search" separately for EACH requested packet type. Confirm its
        discriminator, fixed fields, field order, bit widths/endianness, length
        encoding, optional-field conditions, repeated-field termination/count,
        and payload structure. Do not rely on prior protocol knowledge when the
        RFC can answer the question.

        The output MUST follow all of these format and naming requirements:

        1. Document envelope
           - Emit exactly one XML document with the XML declaration, one <Peach>
             root using the namespaces shown in the reference, one <Defaults>,
             and then all <DataModel> definitions.
           - Keep definitions in dependency order: reusable protocol primitives,
             shared headers, packet-specific components, packet models, union,
             then packet array. Every ref must resolve to a definition in the
             same file; no forward placeholder or "similar structures" omission.

        2. Identifier spelling
           - Let `<proto>` mean `{self.protocol_lower}`. All generated DataElement
             names and ordinary DataModel names must use ASCII lower_snake_case.
             Normalize packet types to lower_snake_case; do not preserve spaces,
             hyphens, mixed case, or RFC display capitalization in identifiers.
           - Name packet models `<proto>_<packet_type>_packet_t`.
           - Name packet-specific component models
             `<proto>_<packet_type>_<component>_t`, for example
             `<proto>_connect_variable_header_t` and
             `<proto>_connect_payload_t`.
           - Name shared structural models `<proto>_<purpose>_t`, for example
             `<proto>_fixed_header_t`.
           - A reusable protocol primitive may use
             `<PROTOCOL_UPPER>_<DescriptiveType>` as in `MQTT_String`; use this
             exception consistently and only for actual reusable primitives.
           - Use semantic lower_snake_case field names from the RFC. Use the
             suffix `_length` for a length field, `_count` for a count field,
             and `_optional` for an <Optional> wrapper. Do not use generic names
             such as field1, data1, block1, or reserved padding unless that is
             truly the wire field's meaning.

        3. Packet decomposition
           - When the protocol has a common header, define it once and reference
             it from every packet as `<Block name="fixed_header" ...>`.
           - Specialize discriminator and other packet-constant header fields
             inside that referencing Block with exact `value` and `token="true"`.
           - Put all bytes covered by a body/remaining-length field inside one
             `<Block name="msg_body">`. Inside it, use
             `<Block name="variable_header" ...>` and
             `<Block name="payload" ...>` when those concepts exist. Preserve
             exact wire order at every nesting level.
           - Model every meaningful field explicitly. A catch-all <Blob> is only
             allowed for RFC-defined opaque bytes or a payload whose internal
             format the RFC genuinely does not define. It must not replace known
             headers, properties, entries, flags, or length/count fields.

        4. Sizes, conditions, and repetitions
           - Put `<Relation type="size" of="target"/>` inside the field that
             encodes target's byte length. `of` must name the exact sibling or
             otherwise valid Peach-relative target and the target must exist.
           - Use the protocol's real length encoding (Number, MqttVarInt, etc.);
             do not force all lengths to 16-bit Numbers.
           - Use <Optional> with an exact `src` path and XML-escaped `expression`
             for conditionally present fields. In XML attributes write bitwise
             AND as `&amp;`. Use `<Block minOccurs="0" maxOccurs="1">` only when
             optionality has no representable controlling condition.
           - Represent repeated wire items with a named plural container and a
             named singular item. Set minOccurs/maxOccurs or a count/size Relation
             according to the RFC; do not flatten multiple items into one Blob.
           - Set Number size, signedness, and endian exactly. Defaults may supply
             common values, but override them wherever the protocol differs.

        5. Required top-level models
           - Define `<DataModel name="{self.protocol_lower}_packet_t">` containing
             exactly one `<Choice name="packet_union">`. Add one branch per
             requested packet type, named with the normalized packet type and
             referencing its `<proto>_<packet_type>_packet_t` model.
           - Define the final `<DataModel name="{self.protocol_lower}_packet_array">`
             with `<Block name="packets" minOccurs="1" maxOccurs="100">` and an
             inner `<Block ref="{self.protocol_lower}_packet_t"/>`, matching the
             reference exactly.

        6. Completeness check before writing
           - Verify that every requested packet type has one packet DataModel and
             one packet_union branch, every ref/relation/src target resolves, all
             identifiers obey the naming scheme, XML special characters are
             escaped, and the XML is well formed.
           - Do not output prose, Markdown fences, TODOs, ellipses, or placeholder
             definitions in the file.

        Use "Write_File" to save only the finished XML to
        "./llm/peach/{self.protocol_lower}/datamodel.xml".
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

    def diagnose_datamodel_failure(self, test_output: str) -> str:
        """Diagnose through the pipeline agent using Read_File tool calls."""
        output_dir = Path("./llm/peach") / self.protocol_lower
        datamodel_path = output_dir / "datamodel.xml"
        log_dir = output_dir / "dm_test_logs"
        report_path = output_dir / "datamodel_diagnosis.json"
        report: dict[str, object] = {
            "diagnosis_mode": "llm",
            "datamodel": str(datamodel_path),
            "logs_analyzed": 0,
            "log_files": [],
            "cross_log_summary": [],
            "reports": [],
            "static_diagnostics": [],
        }

        try:
            prompt = f"""
        Diagnose the failed {self.protocol_name} Peach DataModel directly from
        its source files. Do not use heuristic rules or prior diagnosis.

        The validator summary was:
        ```
        {test_output}
        ```

        You MUST perform these tool calls before answering:
        1. Call "Read_File" for "{datamodel_path}".
        2. Call "Read_File" for "{log_dir}" to obtain the directory listing.
        3. Call "Read_File" separately for EVERY .log file in that listing.

        Analyze the raw file contents yourself. Respect log ordering, distinguish
        root causes from cascading Choice-branch symptoms, and do not invent
        protocol facts. After reading all files, use "RFC_Search" as needed to
        confirm packet layout, field semantics, constraints, byte order, length
        encoding, optionality, or repetition rules. Ask focused RFC questions
        instead of relying on prior knowledge, and distinguish RFC-backed facts
        from conclusions supported only by logs. This is diagnosis only: never
        modify the DataModel or logs.

        Build one JSON report with this shape:
        {{
          "diagnosis_mode": "llm",
          "datamodel": "{datamodel_path}",
          "logs_analyzed": 1,
          "log_files": ["path/to/failure.log"],
          "cross_log_summary": [],
          "reports": [],
          "static_diagnostics": [],
          "llm_judgment": {{
            "status": "ok",
            "model": "{os.environ.get('LLM_DIAGNOSER_MODEL') or self.agent_config.model}",
            "analysis": {{
              "summary": "short Chinese conclusion",
              "root_causes": [{{
                "id": "RC1",
                "title": "concise Chinese title",
                "classification": "root_cause | contributing_factor | symptom | uncertain",
                "category": "reference | endianness | layout | choice | cardinality | boundary | other",
                "confidence": 0.0,
                "affected_seeds": ["seed.raw"],
                "xml_locations": [{{
                  "line": 1,
                  "tag": "Number",
                  "name": "field_name",
                  "model": "packet_model",
                  "attributes": {{}}
                }}],
                "reasoning": "why this is causal",
                "evidence": ["raw file evidence"],
                "suggested_fix": "focused candidate change or null",
                "verification": "focused re-test"
              }}],
              "causal_relationships": [],
              "priority_order": ["RC1"],
              "uncertainties": []
            }}
          }}
        }}

        After the diagnosis is complete, you MUST call "Write_File" exactly
        once with filepath "{report_path}" and the complete JSON report as its
        content. Do not write any other file. After that tool succeeds, return
        only the same JSON object without Markdown fences.
        """
            response = self.call_agent(
                prompt,
                "Step 3: Datamodel Failure Diagnosis",
                agent_graph=self.diagnosis_agent_graph,
            )
            read_calls: dict[str, tuple[str, str | None]] = {}
            write_calls: list[tuple[str, str, str]] = []
            tool_outputs: dict[str, str] = {}
            for message in response["messages"]:
                for call in getattr(message, "tool_calls", []) or []:
                    args = call.get("args", {})
                    if not isinstance(args, dict):
                        continue
                    filepath = args.get("filepath")
                    if call.get("name") == "Read_File" and isinstance(filepath, str):
                        read_calls[os.path.normpath(filepath)] = (
                            str(call.get("id", "")),
                            filepath,
                        )
                    if (
                        call.get("name") == "Write_File"
                        and isinstance(filepath, str)
                        and isinstance(args.get("content"), str)
                    ):
                        write_calls.append(
                            (
                                os.path.normpath(filepath),
                                args["content"],
                                str(call.get("id", "")),
                            )
                        )
                tool_call_id = getattr(message, "tool_call_id", None)
                if isinstance(tool_call_id, str):
                    tool_outputs[tool_call_id] = str(message.content)

            normalized_datamodel = os.path.normpath(str(datamodel_path))
            normalized_log_dir = os.path.normpath(str(log_dir))
            if normalized_datamodel not in read_calls or normalized_log_dir not in read_calls:
                raise RuntimeError(
                    "Diagnosis agent did not read the datamodel and log directory"
                )
            listing_call_id = read_calls[normalized_log_dir][0]
            listing = tool_outputs.get(listing_call_id, "")
            expected_logs = {
                os.path.normpath(str(log_dir / name.strip()))
                for name in listing.splitlines()[1:]
                if name.strip().endswith(".log")
            }
            missing_logs = expected_logs.difference(read_calls)
            if missing_logs:
                raise RuntimeError(
                    "Diagnosis agent did not read every failure log: "
                    + ", ".join(sorted(missing_logs))
                )
            normalized_report_path = os.path.normpath(str(report_path))
            if len(write_calls) != 1 or write_calls[0][0] != normalized_report_path:
                raise RuntimeError(
                    "Diagnosis agent must write exactly one diagnosis report "
                    f"to {report_path}"
                )
            write_output = tool_outputs.get(write_calls[0][2], "")
            if not write_output.startswith("SUCCESS:"):
                raise RuntimeError(
                    f"Diagnosis report write did not succeed: {write_output}"
                )
            stripped = write_calls[0][1].strip()
            if stripped.startswith("```"):
                stripped = stripped.removeprefix("```json").removeprefix("```")
                stripped = stripped.removesuffix("```").strip()
            written_report = json.loads(stripped)
            judgment = (
                written_report.get("llm_judgment", {})
                if isinstance(written_report, dict)
                else {}
            )
            analysis = judgment.get("analysis", {}) if isinstance(judgment, dict) else {}
            if not isinstance(analysis, dict) or not isinstance(
                analysis.get("root_causes"), list
            ):
                raise RuntimeError("Diagnosis agent wrote an invalid JSON schema")
            report = written_report
        except Exception as error:
            UI.warn(f"Datamodel LLM diagnosis failed: {error}")
            report["llm_judgment"] = {
                "status": "error",
                "model": (
                    os.environ.get("LLM_DIAGNOSER_MODEL")
                    or self.agent_config.model
                ),
                "error": str(error),
            }

        diagnosis = json.dumps(report, indent=2, ensure_ascii=False)
        judgment = report.get("llm_judgment", {})
        if isinstance(judgment, dict) and judgment.get("status") == "ok":
            UI.success(f"Datamodel diagnosis saved to {report_path} by the agent.")
        UI.panel(
            diagnosis,
            title="Datamodel LLM Diagnosis",
            border_style="cyan",
            expand=True,
        )
        return diagnosis

    def step_3_datamodel_validation_and_fix(self):
        UI.title("Step 3: Datamodel Validation & Fix")

        def fix_fn(test_output: str, hint: str | None) -> None:
            diagnosis_path = (
                Path("./llm/peach")
                / self.protocol_lower
                / "datamodel_diagnosis.json"
            )
            reuse_diagnosis = diagnosis_path.exists() and ask_reuse_diagnosis(
                self.protocol_lower
            )
            if reuse_diagnosis:
                UI.success(f"Reusing existing diagnosis from {diagnosis_path}.")
            else:
                UI.warning_rule("Step 3: Diagnosing Datamodel Failure")
                self.diagnose_datamodel_failure(test_output)

            UI.warning_rule("Step 3: Applying Datamodel Auto-fix")
            prompt = f"""
        Repair the current {self.protocol_name} Peach DataModel using only the
        completed diagnosis report and the current DataModel.

        **FIRST ACTION**: Use "Read_File" to read
        "./llm/peach/{self.protocol_lower}/datamodel_diagnosis.json".
        Treat its `llm_judgment.analysis.priority_order` and ranked root causes
        as the complete repair plan. Address the highest-priority confirmed root
        cause first.

        You need to:
        1. Read and prioritize the diagnosis report.
        2. Use "Read_File" to read only the current DataModel at
           "./llm/peach/{self.protocol_lower}/datamodel.xml".
        3. Apply the diagnosed fixes without performing another diagnosis.
        4. Use "Write_File" to save the repaired DataModel to that same path.

        Do NOT read validator output, failure logs, seed files, or any other
        source. Do NOT call RFC_Search. The diagnosis report is the sole source
        of failure evidence for this repair.

        **CRITICAL**: Simplifying the DataModel is NOT allowed.
        """
            if hint:
                prompt += (
                    f"\n\nAdditional guidance from the user:\n{hint}\n"
                )

            self.call_agent(
                prompt,
                "Step 3: Datamodel Validation & Fix",
                agent_graph=self.datamodel_autofix_agent_graph,
            )

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
        4. Fix the bug in the C# code. 
        5. Use the "Write_File" tool to update the file with the fix.
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

    @override
    def steps(self):
        steps = [
            ("Step 1: Packet Types Extraction", self.step_1_packet_types_extraction),
            ("Step 2: Datamodel Generation", self.step_2_datamodel_generation),
            (
                "Step 3: Datamodel Validation & Fix",
                self.step_3_datamodel_validation_and_fix,
            ),
            ("Step 4: Mutator Generation", self.step_4_mutator_generation),
            ("Step 5: Mutator Validation & Fix", self.step_5_mutator_validation_and_fix),
        ]

        if get_fixer_enabled():
            steps += [
                ("Step 6: Constraint Extraction", self.step_6_constraint_extraction),
                ("Step 6.1: Constraint Filtering", self.step_6_1_constraint_filtering),
                ("Step 7: Fixer Generation", self.step_7_fixer_generation),
                ("Step 7.5: Fixer-Constraint Mapping", self.step_7_5_fixer_constraint_mapping),
                ("Step 8: Fixer Test Generation", self.step_8_fixer_test_generation),
                ("Step 9: Fixer Validation & Fix", self.step_9_fixer_validation_and_fix),
            ]

        steps.append(
            ("Final Compilation", self.step_final_compile),
        )

        return steps
