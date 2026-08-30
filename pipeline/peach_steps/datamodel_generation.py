import json
import os
from pathlib import Path

from agent import build_agent_graph
from datamodel_split import assemble_datamodel, load_manifest, write_manifest
from pipeline.peach_steps.common import (
    _DATAMODEL_MODELING_GUARDRAILS,
    _env_int,
    PeachStepMixin,
)
from tools import get_family_validation_result, reset_family_validation_session
from ui import UI, ask_before_step


class DatamodelGenerationSteps(PeachStepMixin):
    def _should_split_datamodel_generation(self, packet_types: list[str]) -> bool:
        mode = os.environ.get("LLM_PEACH_DATAMODEL_SPLIT", "auto").strip().lower()
        if mode in {"1", "true", "yes", "always"}:
            return True
        if mode in {"0", "false", "no", "never"}:
            return False
        threshold = max(1, _env_int("LLM_PEACH_DATAMODEL_SPLIT_THRESHOLD", 6))
        return len(packet_types) >= threshold

    def _generate_split_datamodel(self, packet_types: list[str]) -> None:
        from concurrent.futures import Future, ThreadPoolExecutor, as_completed

        custom_element_context = self._custom_data_element_context()
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

        {custom_element_context}

        FIRST, use Read_File to read "./peach/peach.txt" completely. Treat it as
        the authoritative catalog of supported Peach elements, custom elements,
        attributes, relations, and syntax. This is a lightweight
        interface-planning task; do not generate XML and do not propose any
        element type or construct that is absent from peach.txt or the approved
        custom DataElement contract above.

        {_DATAMODEL_MODELING_GUARDRAILS}

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
                "peach_element": "exact peach.txt or approved custom element",
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
        Every peach_element and planned construct must be supported by peach.txt
        or the approved custom contract; never invent another protocol-specific
        type. If no specialized approved type exists, plan a composition of
        documented primitive elements instead.
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

            {custom_element_context}

            Read "./examples/peach_datamodel_example.xml" and
            "./peach/peach.txt" before writing. Use RFC_Search to confirm every
            shared field and encoding. Write one well-formed standalone <Peach>
            XML document to "{shared_path}". It must contain exactly one
            <Defaults> followed only by reusable primitives and shared
            <DataModel> definitions. Do not define packet-specific models,
            <DataModel name="{self.protocol_lower}_packet_t">, or
            <DataModel name="{self.protocol_lower}_packet_array">.

            {_DATAMODEL_MODELING_GUARDRAILS}

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
            during family repair. Treat a failing seed as a counterexample to a
            general model rule, not as a template: never repair by pinning its
            bytes, length, count, option set, or repetition count.
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

            {custom_element_context}

            Assigned family:
            {json.dumps(group, ensure_ascii=False, indent=2)}

            Before generating XML, use Read_File to read BOTH
            "./examples/peach_datamodel_example.xml" and "./peach/peach.txt".
            Follow peach.txt as the authoritative list and syntax of supported
            Peach elements; the approved custom contract is the only permitted
            exception. Never invent another element or attribute.
            Use RFC_Search separately for every assigned packet type and confirm
            discriminator, field order, bit widths/endianness, length encoding,
            optional conditions, repetitions, and payload structure. References
            outside this fragment may target only shared model names declared by
            the contract. Define all family-local component models and exactly
            one packet model named
            "{self.protocol_lower}_<normalized_packet_type>_packet_t" for every
            assigned type. Do not define a model whose name or wire-level meaning
            belongs to another family; cross-family models belong in shared.xml.

            {_DATAMODEL_MODELING_GUARDRAILS}

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
                raw_validations = result.get("validations", 0)
                validations = (
                    raw_validations
                    if isinstance(raw_validations, int)
                    else int(raw_validations)
                    if isinstance(raw_validations, str) and raw_validations.isdigit()
                    else 0
                )
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
            futures: list[Future[None] | Future[Path]] = [
                executor.submit(generate_shared)
            ]
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
        custom_element_context = self._custom_data_element_context()
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

                {custom_element_context}

                Assembly error:
                {error}

                Read these files before editing:
                - "{manifest_path}"
                - "{shared_path}"
                {os.linesep.join(f'- "{path}"' for path in packet_paths)}

                Preserve every packet type, packet-group id, packet assignment,
                and wire-level field semantics. Modify only the manifest and the
                listed fragment files. Do not create datamodel.xml yourself.

                {_DATAMODEL_MODELING_GUARDRAILS}

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
        custom_element_context = self._custom_data_element_context()

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

        {custom_element_context}

        Before generating anything, use "Read_File" to read BOTH:
        - "./examples/peach_datamodel_example.xml" for the required document shape,
          decomposition, and naming style. It is a structural example, not a
          complete MQTT model; never copy its protocol facts and never omit a
          requested packet merely because the example omits it.
        - "./peach/peach.txt" for the supported Peach XML elements and their syntax.

        Use "RFC_Search" separately for EACH requested packet type. Confirm its
        discriminator, fixed fields, field order, bit widths/endianness, length
        encoding, optional-field conditions, repeated-field termination/count,
        and payload structure. Do not rely on prior protocol knowledge when the
        RFC can answer the question.

        {_DATAMODEL_MODELING_GUARDRAILS}

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
           - Inside that referencing Block, specialize only RFC-mandated exact
             discriminator/fixed fields with `value` and `token="true"`, following
             the token rules above. Leave variable header fields untokenized even
             if every supplied seed happens to contain the same value.
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
