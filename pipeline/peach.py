import json
import os
from pathlib import Path
from typing import override

from agent import AgentConfig, build_agent_graph
from config import get_fixer_enabled
from datamodel_split import (
    assemble_datamodel,
    load_manifest,
    write_manifest,
)
from pipeline.base import BasePipeline
from tools import get_family_validation_result, reset_family_validation_session
from ui import (
    UI,
    ask_before_step,
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


def _env_int(key: str, default: int) -> int:
    val = os.environ.get(key)
    if val is None:
        return default
    try:
        return int(val)
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
                "DataModels. Read the DataModel and validator logs, identify a "
                "small number of actionable root causes, and use RFC_Search only "
                "when protocol semantics need confirmation. Write only the "
                "requested diagnosis JSON report; never modify other files."
            ),
        )
        self.diagnosis_agent_graph = build_agent_graph(
            retriever=self.retriever,
            target="peach",
            config=diagnosis_config,
            tool_names={
                "Read_File",
                "Read_File_With_Line_Numbers",
                "RFC_Search",
                "Write_File",
            },
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
            tool_names={"Read_File", "Write_File", "Validate_Peach_XML"},
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

    def _should_split_datamodel_generation(self, packet_types: list[str]) -> bool:
        mode = os.environ.get("LLM_PEACH_DATAMODEL_SPLIT", "auto").strip().lower()
        if mode in {"1", "true", "yes", "always"}:
            return True
        if mode in {"0", "false", "no", "never"}:
            return False
        threshold = max(1, _env_int("LLM_PEACH_DATAMODEL_SPLIT_THRESHOLD", 6))
        return len(packet_types) >= threshold

    def _generate_split_datamodel(self, packet_types: list[str]) -> None:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        output_dir = Path("./llm/peach") / self.protocol_lower
        fragment_dir = output_dir / "datamodel_fragments"
        manifest_path = fragment_dir / "schema_manifest.json"
        seed_manifest_path = fragment_dir / "seed_classification.json"
        shared_path = fragment_dir / "shared.xml"
        shared_ready_path = fragment_dir / "shared.xml.ready"
        group_size = max(1, _env_int("LLM_PEACH_DATAMODEL_GROUP_SIZE", 4))
        workers = max(1, _env_int("LLM_PEACH_DATAMODEL_WORKERS", 4))
        assembly_retries = max(
            0, _env_int("LLM_PEACH_DATAMODEL_ASSEMBLY_RETRIES", 2)
        )
        fragment_dir.mkdir(parents=True, exist_ok=True)
        shared_ready_path.unlink(missing_ok=True)

        planner_action, planner_extra = ask_before_step(
            "Step 2.1a: Datamodel Schema Planning"
            + (" (existing result available)" if manifest_path.is_file() else ""),
            has_previous=False,
        )
        classifier_action, classifier_extra = ask_before_step(
            "Step 2.1b: Seed Classification"
            + (" (existing result available)" if seed_manifest_path.is_file() else ""),
            has_previous=False,
        )
        if "exit" in {planner_action, classifier_action}:
            raise RuntimeError("split DataModel preparation stopped by user")
        run_planner_task = planner_action == "continue"
        run_classifier_task = classifier_action == "continue"

        if run_planner_task:
            manifest_path.unlink(missing_ok=True)
        elif manifest_path.is_file():
            UI.dim(f"Schema planning skipped; reusing {manifest_path}.")
        else:
            raise RuntimeError(
                "schema planning was skipped, but no existing schema manifest "
                f"is available at {manifest_path}"
            )

        if run_classifier_task:
            seed_manifest_path.unlink(missing_ok=True)
        elif seed_manifest_path.is_file():
            UI.dim(f"Seed classification skipped; reusing {seed_manifest_path}.")
        else:
            UI.dim(
                "Seed classification skipped with no existing result; early "
                "family validation will be skipped."
            )

        selected_tasks = []
        if run_planner_task:
            selected_tasks.append("schema planning")
        if run_classifier_task:
            selected_tasks.append("seed classification")
        if selected_tasks:
            UI.dim(
                f"Starting {' and '.join(selected_tasks)} for "
                f"{len(packet_types)} packet types."
            )

        planner_prompt = f"""
        Plan the decomposition of a complete Peach Pit DataModel for the
        {self.protocol_name} protocol and these packet types: {packet_types}.

        FIRST, use Read_File to read "./peach/peach.txt" completely. Treat it as
        the authoritative catalog of supported Peach elements, custom elements,
        attributes, relations, and syntax. This is a lightweight
        interface-planning task; do not generate XML and do not propose any
        element type or construct that is absent from peach.txt.

        Use RFC_Search to identify common wire primitives, shared headers, shared
        option/property structures, packet discriminators, and closely related
        packet families. Group all packet types into families of at most
        {group_size} types so those families can be generated independently.
        Any wire element or semantic DataModel used by packet types in more than
        one family MUST be declared as a shared model; family tasks must never
        independently define the same model name.

        Write exactly one JSON object to "{manifest_path}" using Write_File:
        {{
          "protocol": "{self.protocol_lower}",
          "shared_models": [
            {{"name": "model_name", "purpose": "wire-level responsibility",
              "fields": [{{"name": "field_name",
                "peach_element": "exact supported element from peach.txt",
                "wire_contract": "ordered encoding responsibility"}}]}}
          ],
          "packet_groups": [
            {{"id": "ascii_lower_snake_case_id",
              "packet_types": ["exact values from the requested list"],
              "shared_refs": ["shared model names this family may reference"],
              "rfc_queries": ["focused evidence queries for this family"]}}
          ]
        }}

        Every requested packet type must occur exactly once. Shared model names
        are an immutable interface contract for the parallel generation tasks.
        Every peach_element and planned construct must be supported by the
        peach.txt file you read; never invent a convenient protocol-specific
        type. If no specialized type exists, plan a composition of documented
        primitive elements instead.
        Keep the plan concise. Do not write any other file.
        """
        seed_classifier_prompt = f"""
        Classify every binary seed in "{self.seed_dir}" for the
        {self.protocol_name} protocol. Requested packet types: {packet_types}.

        First call Inspect_Seed_Directory for that directory. Use RFC_Search to
        identify the packet discriminator and framing/length rules. Walk the
        entire byte sequence of each seed: a seed is `single_packet=true` only
        when exactly one complete packet consumes the whole file. Do not infer
        packet count from the filename alone.

        Write exactly one JSON object to "{seed_manifest_path}":
        {{
          "protocol": "{self.protocol_lower}",
          "seeds": [{{
            "file": "relative/path.raw",
            "packet_count": 1,
            "packet_types": ["exact requested packet type"],
            "single_packet": true,
            "confidence": "high | medium | low",
            "evidence": "discriminator and framing evidence"
          }}]
        }}

        Include every inspected seed exactly once. If framing is ambiguous, use
        low confidence and set single_packet=false. Do not write any other file.
        """

        def run_planner() -> None:
            planner_agent = build_agent_graph(
                retriever=self.retriever,
                target="peach",
                config=self.agent_config,
                tool_names={"Read_File", "RFC_Search", "Write_File"},
            )
            self.call_agent(
                planner_prompt
                + (f"\n\nAdditional user instruction:\n{planner_extra}" if planner_extra else ""),
                "Step 2.1: Datamodel Schema Planning",
                agent_graph=planner_agent,
            )

        def run_seed_classifier() -> None:
            try:
                classifier_agent = build_agent_graph(
                    retriever=self.retriever,
                    target="peach",
                    config=self.agent_config,
                    tool_names={
                        "Inspect_Seed_Directory",
                        "RFC_Search",
                        "Write_File",
                    },
                )
                self.call_agent(
                    seed_classifier_prompt
                    + (
                        f"\n\nAdditional user instruction:\n{classifier_extra}"
                        if classifier_extra
                        else ""
                    ),
                    "Step 2.1: Seed Classification",
                    agent_graph=classifier_agent,
                )
            except Exception as error:
                UI.warn(
                    "Seed classification failed; family validation will be "
                    f"skipped, but full validation is preserved: {error}"
                )

        preparation_tasks = []
        if run_planner_task:
            preparation_tasks.append(run_planner)
        if run_classifier_task:
            preparation_tasks.append(run_seed_classifier)
        if preparation_tasks:
            with ThreadPoolExecutor(max_workers=len(preparation_tasks)) as executor:
                planning_futures = [
                    executor.submit(task) for task in preparation_tasks
                ]
                for future in as_completed(planning_futures):
                    future.result()
            UI.dim("Selected DataModel preparation tasks completed.")

        manifest, warning = load_manifest(
            manifest_path,
            self.protocol_lower,
            packet_types,
            group_size,
        )
        if warning:
            write_manifest(manifest_path, manifest)
            raise ValueError(
                "schema planner did not produce a valid manifest; a diagnostic "
                f"fallback manifest was saved: {warning}"
            )

        classified_seeds = self._load_single_packet_seeds(
            seed_manifest_path, packet_types
        )
        classified_count = sum(len(paths) for paths in classified_seeds.values())
        UI.dim(
            f"Seed classification selected {classified_count} high-confidence "
            "single-packet seed(s) for early family validation."
        )

        manifest_json = json.dumps(manifest, ensure_ascii=False, indent=2)

        def generate_shared() -> None:
            shared_path.unlink(missing_ok=True)
            UI.dim(f"Generating shared DataModels at {shared_path}.")
            prompt = f"""
            Generate only the shared portion of a Peach Pit for
            {self.protocol_name}. The immutable schema contract is:

            {manifest_json}

            Read "./prompts/peach_datamodel_example.xml" and
            "./peach/peach.txt" before writing. Use RFC_Search to confirm every
            shared field and encoding. Write one well-formed standalone <Peach>
            XML document to "{shared_path}". It must contain exactly one
            <Defaults> followed only by reusable primitives and shared
            <DataModel> definitions. Do not define packet-specific models,
            <DataModel name="{self.protocol_lower}_packet_t">, or
            <DataModel name="{self.protocol_lower}_packet_array">.

            Every DataModel referenced by `ref` must be defined earlier in this
            fragment. Order shared definitions by dependency and never create a
            cyclic DataModel reference.

            Honor the shared model names in the contract exactly. Do not write
            prose, Markdown, TODOs, or any other file.

            After writing, call Validate_Peach_XML on "{shared_path}". If it
            returns FAIL, use its line-specific XSD diagnostics to correct this
            fragment and validate again. Make at most three XSD repair attempts
            and finish only after PASS. An ERROR means validator infrastructure
            is unavailable and must be reported; do not claim validation passed.
            """
            agent = build_agent_graph(
                retriever=self.retriever,
                target="peach",
                config=self.agent_config,
                tool_names={
                    "Read_File",
                    "RFC_Search",
                    "Write_File",
                    "Validate_Peach_XML",
                },
            )
            self.call_agent(
                prompt,
                "Step 2.2: Shared Datamodel Generation",
                agent_graph=agent,
            )
            if not shared_path.is_file():
                raise RuntimeError(f"shared DataModel was not generated: {shared_path}")
            shared_ready_path.touch()
            UI.success(f"Shared DataModels generated: {shared_path}")

        def generate_group(group: dict, index: int) -> Path:
            group_path = fragment_dir / f"packet_{group['id']}.xml"
            group_path.unlink(missing_ok=True)
            seed_paths = [
                path
                for packet_type in group["packet_types"]
                for path in classified_seeds.get(packet_type, [])
            ]
            reset_family_validation_session(str(group_path))
            validation_instructions = ""
            tool_names = {
                "Read_File",
                "RFC_Search",
                "Write_File",
                "Validate_Peach_XML",
            }
            if seed_paths:
                tool_names.add("Validate_DataModel_Family")
                validation_instructions = f"""
            After writing the fragment, validate it yourself by calling
            Validate_DataModel_Family with:
            - protocol: "{self.protocol_lower}"
            - group_id: "{group['id']}"
            - seed_files: {json.dumps([str(path) for path in seed_paths])}
            - fragment_dir: "{fragment_dir}"
            - output_dir: "{output_dir}"

            The validation tool checks for shared.xml and waits when shared
            generation is still running. If it returns WAITING, do not edit or
            count a repair: call it again. If it returns BLOCKED_SHARED, stop
            family repair and defer the shared problem to integration repair. If
            it returns FAIL, read the reported logs, diagnose the family
            fragment, make the smallest correction, and call the tool again. You
            may repair at most THREE times. The tool enforces this limit. Stop
            immediately on PASS or REPAIR_LIMIT_REACHED. Never modify shared.xml
            during family repair.
            """
            UI.dim(
                f"Starting family {group['id']} generation with "
                f"{len(seed_paths)} early-validation seed(s)."
            )
            prompt = f"""
            Generate the packet-specific Peach DataModels for one independent
            {self.protocol_name} packet family.

            Complete immutable schema contract:
            {manifest_json}

            Assigned family:
            {json.dumps(group, ensure_ascii=False, indent=2)}

            Before generating XML, use Read_File to read BOTH
            "./prompts/peach_datamodel_example.xml" and "./peach/peach.txt".
            Follow peach.txt as the authoritative list and syntax of supported
            Peach elements; never invent an element or attribute it does not
            document.
            Use RFC_Search separately for every assigned packet type and confirm
            discriminator, field order, bit widths/endianness, length encoding,
            optional conditions, repetitions, and payload structure. References
            outside this fragment may target only shared model names declared by
            the contract. Define all family-local component models and exactly
            one packet model named
            "{self.protocol_lower}_<normalized_packet_type>_packet_t" for every
            assigned type. Do not define a model whose name or wire-level meaning
            belongs to another family; cross-family models belong in shared.xml.

            Write one well-formed standalone <Peach> XML document containing
            only this family's <DataModel> definitions to "{group_path}". Do not
            include <Defaults>, shared model definitions, packet_union, or
            packet_array. Do not write prose, Markdown, TODOs, or any other file.

            Within this fragment, and in the final assembled file, every
            DataModel referenced by `ref` MUST be defined before the DataModel
            that references it. Write local component definitions in dependency
            order. Cyclic DataModel references are forbidden.

            Immediately after writing "{group_path}", call Validate_Peach_XML
            on it. If XSD validation fails, repair only this fragment from the
            reported line-specific errors and retry, with at most three XSD
            repair attempts. Do not call Validate_DataModel_Family until
            Validate_Peach_XML returns PASS. If the XSD tool returns ERROR,
            report the infrastructure problem and never claim the XML is valid.
            {validation_instructions}
            """
            agent = build_agent_graph(
                retriever=self.retriever,
                target="peach",
                config=self.agent_config,
                tool_names=tool_names,
            )
            self.call_agent(
                prompt,
                f"Step 2.2.{index + 1}: Datamodel Family {group['id']}",
                agent_graph=agent,
            )
            if not group_path.is_file():
                raise RuntimeError(f"family fragment was not generated: {group_path}")
            if seed_paths:
                result = get_family_validation_result(str(group_path))
                status = str(result.get("status"))
                validations = int(result.get("validations", 0))
                if status == "PASS":
                    UI.success(
                        f"Family {group['id']} passed early validation after "
                        f"{validations} validation run(s)."
                    )
                else:
                    UI.warn(
                        f"Family {group['id']} early validation ended with "
                        f"status={status}, runs={validations}; final validation "
                        "and repair remain enabled."
                    )
            else:
                UI.dim(
                    f"Family {group['id']} has no eligible single-packet seeds; "
                    "early validation skipped."
                )
            return group_path

        packet_groups = manifest["packet_groups"]
        max_workers = min(workers, len(packet_groups) + 1)
        UI.dim(
            f"Launching shared generation and {len(packet_groups)} family "
            f"generation task(s) with {max_workers} worker(s)."
        )
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(generate_shared)]
            for index, group in enumerate(packet_groups):
                futures.append(executor.submit(generate_group, group, index))
            for future in as_completed(futures):
                future.result()
        UI.dim("All shared and family generation tasks completed; assembling fragments.")

        manifest = self._assemble_split_with_repair(
            packet_types=packet_types,
            manifest=manifest,
            manifest_path=manifest_path,
            shared_path=shared_path,
            fragment_dir=fragment_dir,
            output_dir=output_dir,
            group_size=group_size,
            assembly_retries=assembly_retries,
        )
        UI.success(
            f"Assembled {len(manifest['packet_groups'])} packet-family fragments "
            f"into {output_dir / 'datamodel.xml'}."
        )

    def _load_single_packet_seeds(
        self, seed_manifest_path: Path, packet_types: list[str]
    ) -> dict[str, list[Path]]:
        try:
            classification = json.loads(seed_manifest_path.read_text(encoding="utf-8"))
            entries = classification["seeds"]
            if not isinstance(entries, list):
                raise TypeError("seeds must be a list")
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
            UI.warn(f"Seed classification unavailable; skipping family validation: {error}")
            return {}

        known_types = {packet_type.casefold(): packet_type for packet_type in packet_types}
        seed_root = Path(self.seed_dir).resolve()
        classified: dict[str, list[Path]] = {}
        seen: set[Path] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            entry_types = entry.get("packet_types")
            if (
                entry.get("single_packet") is not True
                or entry.get("packet_count") != 1
                or str(entry.get("confidence", "")).lower() != "high"
                or not isinstance(entry_types, list)
                or len(entry_types) != 1
            ):
                UI.dim(
                    f"Skipping seed {entry.get('file', '')} due to "
                    f"{'single_packet' if entry.get('single_packet') is not True else ''}"
                    f"{'packet_count' if entry.get('packet_count') != 1 else ''}"
                    f"{'confidence' if str(entry.get('confidence', '')).lower() != 'high' else ''}"
                    f"{'packet_types' if not isinstance(entry_types, list) or len(entry_types) != 1 else ''}"
                )
                continue
            type_key = str(entry_types[0]).casefold()
            if type_key not in known_types:
                continue
            relative = Path(str(entry.get("file", "")))
            seed_path = (seed_root / relative).resolve()
            try:
                seed_path.relative_to(seed_root)
            except ValueError:
                continue
            if not seed_path.is_file() or seed_path in seen:
                continue
            seen.add(seed_path)
            classified.setdefault(known_types[type_key], []).append(seed_path)
        return classified

    def _assemble_split_with_repair(
        self,
        *,
        packet_types: list[str],
        manifest: dict,
        manifest_path: Path,
        shared_path: Path,
        fragment_dir: Path,
        output_dir: Path,
        group_size: int,
        assembly_retries: int,
    ) -> dict:
        for attempt in range(assembly_retries + 1):
            packet_groups = manifest["packet_groups"]
            packet_paths = [
                fragment_dir / f"packet_{group['id']}.xml"
                for group in packet_groups
            ]
            try:
                assemble_datamodel(
                    protocol=self.protocol_lower,
                    packet_types=packet_types,
                    shared_fragment=shared_path,
                    packet_fragments=packet_paths,
                    output_path=output_dir / "datamodel.xml",
                    expected_shared_models=[
                        str(model["name"])
                        for model in manifest["shared_models"]
                    ],
                )
                break
            except (OSError, ValueError) as error:
                if attempt >= assembly_retries:
                    raise RuntimeError(
                        "split DataModel assembly still fails after "
                        f"{assembly_retries} integration repair attempts: {error}"
                    ) from error

                UI.warning_rule(
                    "Step 2.3: Datamodel Integration Repair "
                    f"{attempt + 1}/{assembly_retries}"
                )
                repair_prompt = f"""
                Repair the generated {self.protocol_name} DataModel fragments so
                they can be assembled without discarding the split design.

                Assembly error:
                {error}

                Read these files before editing:
                - "{manifest_path}"
                - "{shared_path}"
                {os.linesep.join(f'- "{path}"' for path in packet_paths)}

                Preserve every packet type, packet-group id, packet assignment,
                and wire-level field semantics. Modify only the manifest and the
                listed fragment files. Do not create datamodel.xml yourself.

                Integration rules:
                - A model used by more than one family belongs in shared.xml.
                  Add it to shared_models, add it to each consuming shared_refs,
                  keep one canonical definition, and remove local duplicates.
                - If equal names intentionally represent different wire formats,
                  rename them with family-specific names and update all refs.
                - Every external ref from a packet fragment must resolve to a
                  model declared and defined in shared.xml.
                - Keep exactly one Defaults in shared.xml and none in packet
                  fragments. Do not define packet_union or packet_array.
                - In every fragment and in the assembled output, a referenced
                  DataModel must appear before the DataModel that references it.
                  Reorder definitions as needed; cyclic refs are forbidden.
                - Make the smallest changes required by the reported error.

                Use RFC_Search only if choosing a canonical wire definition
                requires protocol evidence. Finish only after writing all needed
                corrections with Write_File. Then call Validate_Peach_XML on
                shared.xml and every packet fragment listed above. Repair any
                XSD violations and revalidate, with at most three XSD repair
                attempts per file. Finish only when every file returns PASS; an
                ERROR must be reported as validator infrastructure failure.
                """
                repair_agent = build_agent_graph(
                    retriever=self.retriever,
                    target="peach",
                    config=self.agent_config,
                    tool_names={
                        "Read_File",
                        "RFC_Search",
                        "Write_File",
                        "Validate_Peach_XML",
                    },
                )
                self.call_agent(
                    repair_prompt,
                    f"Step 2.3.{attempt + 1}: Datamodel Integration Repair",
                    agent_graph=repair_agent,
                )
                manifest, warning = load_manifest(
                    manifest_path,
                    self.protocol_lower,
                    packet_types,
                    group_size,
                )
                if warning:
                    raise RuntimeError(
                        "integration repair produced an invalid schema manifest: "
                        f"{warning}"
                    )
        return manifest

    def repair_datamodel_assembly(self) -> None:
        """Repair and assemble existing fragments without regenerating them."""
        output_dir = Path("./llm/peach") / self.protocol_lower
        fragment_dir = output_dir / "datamodel_fragments"
        manifest_path = fragment_dir / "schema_manifest.json"
        shared_path = fragment_dir / "shared.xml"
        try:
            raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            raw_groups = raw_manifest["packet_groups"]
            packet_types = [
                str(packet)
                for group in raw_groups
                for packet in group["packet_types"]
            ]
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise RuntimeError(
                f"cannot load split DataModel manifest {manifest_path}: {error}"
            ) from error

        group_size = max(
            max((len(group.get("packet_types", [])) for group in raw_groups), default=1),
            _env_int("LLM_PEACH_DATAMODEL_GROUP_SIZE", 4),
        )
        manifest, warning = load_manifest(
            manifest_path,
            self.protocol_lower,
            packet_types,
            group_size,
        )
        if warning:
            raise RuntimeError(f"invalid schema manifest: {warning}")
        retries = max(1, _env_int("LLM_PEACH_DATAMODEL_ASSEMBLY_RETRIES", 2))
        manifest = self._assemble_split_with_repair(
            packet_types=packet_types,
            manifest=manifest,
            manifest_path=manifest_path,
            shared_path=shared_path,
            fragment_dir=fragment_dir,
            output_dir=output_dir,
            group_size=group_size,
            assembly_retries=retries,
        )
        UI.success(
            f"Repaired and assembled {len(manifest['packet_groups'])} packet-family "
            f"fragments into {output_dir / 'datamodel.xml'}."
        )

    def step_2_datamodel_generation(self):
        UI.title("Step 2: Datamodel Generation")

        packet_types = self.state.get("packet_types") or []
        if not packet_types:
            UI.warn(
                "Warning: packet_types is empty (Step 1 may have been skipped). Step 2 will still run."
            )

        if packet_types and self._should_split_datamodel_generation(packet_types):
            UI.dim(
                "Using adaptive split generation: one schema plan followed by "
                "parallel shared and packet-family generation."
            )
            self._generate_split_datamodel(packet_types)
            return

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
        Then call Validate_Peach_XML on that file. If it returns FAIL, correct
        the line-specific XSD violations and validate again, with at most three
        XSD repair attempts. Finish only after PASS. If it returns ERROR, report
        the validator infrastructure failure and do not claim the XML is valid.
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
        """Produce a small, actionable diagnosis from the DataModel and logs."""
        output_dir = Path("./llm/peach") / self.protocol_lower
        datamodel_path = output_dir / "datamodel.xml"
        log_dir = output_dir / "dm_test_logs"
        report_path = output_dir / "datamodel_diagnosis.json"
        report: dict[str, object] = {
            "status": "error",
            "summary": "诊断尚未完成。",
            "issues": [],
        }

        try:
            report_path.unlink(missing_ok=True)
            validator_summary = next(
                (
                    line.strip()
                    for line in reversed(test_output.splitlines())
                    if line.strip()
                ),
                "validator failed",
            )
            prompt = f"""
        Diagnose the failed {self.protocol_name} Peach DataModel.
        Validator summary: {validator_summary}

        1. Use Read_File_With_Line_Numbers to read "{datamodel_path}" so every
           reported location uses the real 1-based XML source line.
        2. List "{log_dir}" and read up to three representative .log files;
           do not exhaustively analyze duplicate failures.
        3. Identify at most three root causes. Ignore cascading Choice token
           mismatches and repeated symptoms.
        4. Use RFC_Search only when a wire-format fact must be confirmed.

        Use Write_File to write exactly this compact JSON object in Chinese to
        "{report_path}":
        {{
          "status": "ok",
          "summary": "一句话结论",
          "issues": [{{
            "location": {{
              "line": 123,
              "path": "DataModel[@name='模型名']/Block[@name='元素名']/Relation"
            }},
            "cause": "为什么这里是根因",
            "evidence": "日志中的直接证据",
            "fix": "具体且局部的修改"
          }}]
        }}

        Write no other file. After Write_File succeeds, your final response may
        only briefly confirm that the report was saved; do not print the JSON.
        """
            self.call_agent(
                prompt,
                "Step 3: Datamodel Failure Diagnosis",
                agent_graph=self.diagnosis_agent_graph,
            )
            candidate = json.loads(report_path.read_text(encoding="utf-8"))
            if (
                not isinstance(candidate, dict)
                or candidate.get("status") != "ok"
                or not isinstance(candidate.get("summary"), str)
                or not isinstance(candidate.get("issues"), list)
            ):
                raise RuntimeError("Diagnosis agent wrote an invalid JSON schema")
            candidate["issues"] = candidate["issues"][:3]
            for index, issue in enumerate(candidate["issues"]):
                location = issue.get("location") if isinstance(issue, dict) else None
                if (
                    not isinstance(issue, dict)
                    or not all(
                        isinstance(issue.get(key), str)
                        for key in ("cause", "evidence", "fix")
                    )
                    or not isinstance(location, dict)
                    or type(location.get("line")) is not int
                    or location["line"] < 1
                    or not isinstance(location.get("path"), str)
                    or not location["path"].strip()
                    or "DataModel" not in location["path"]
                ):
                    raise RuntimeError(
                        f"Diagnosis issue {index + 1} has an invalid JSON schema"
                    )
            report = candidate
        except Exception as error:
            UI.warn(f"Datamodel LLM diagnosis failed: {error}")
            report["error"] = str(error)
            report_path.unlink(missing_ok=True)

        diagnosis = json.dumps(report, indent=2, ensure_ascii=False)
        if report.get("status") == "ok":
            UI.success(f"Datamodel diagnosis saved to {report_path}.")
        UI.panel(
            diagnosis,
            title="Datamodel LLM Diagnosis",
            border_style="cyan",
            expand=True,
        )
        if report.get("status") != "ok":
            raise RuntimeError(
                "DataModel diagnosis agent did not write a valid diagnosis report"
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
        Treat its `issues` array, in order, as the complete repair plan.
        For every issue, use `location.line` and `location.path` to find and
        confirm the exact XML element before modifying it.

        You need to:
        1. Read the diagnosis report and apply its issues in order.
        2. Use "Read_File" to read only the current DataModel at
           "./llm/peach/{self.protocol_lower}/datamodel.xml".
        3. Apply the diagnosed fixes without performing another diagnosis.
        4. Use "Write_File" to save the repaired DataModel to that same path.
        5. Call Validate_Peach_XML on the repaired file. If it returns FAIL,
           correct only the reported schema violations and validate again. Do
           not finish until it returns PASS; report ERROR as infrastructure
           failure rather than claiming success.

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
