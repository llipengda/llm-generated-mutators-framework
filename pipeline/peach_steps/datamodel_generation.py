import json
from pathlib import Path
import subprocess

from agent import build_agent_graph
from datamodel_dsl import (
    compile_dsl_subprocess,
    load_manifest,
    normalize_symbol,
    write_manifest,
    write_root_module,
)
from peach_dsl.compiler import DSLValidationError, validate_dsl_dependencies
from pipeline.peach_steps.common import (
    _DATAMODEL_DSL_SOURCE_STYLE,
    _DATAMODEL_MODELING_GUARDRAILS,
    _env_int,
    PeachStepMixin,
)
from tools import validate_peach_xml
from ui import UI, ask_before_step, ask_reuse_generated_component


class DatamodelGenerationSteps(PeachStepMixin):
    @staticmethod
    def _packet_type_additions(
        previous: list[str], repaired: list[str]
    ) -> list[str]:
        """Return newly modeled types while rejecting repair-time removals."""
        repaired_keys = {packet.casefold() for packet in repaired}
        removed = [
            packet for packet in previous if packet.casefold() not in repaired_keys
        ]
        if removed:
            raise RuntimeError(
                "DataModel repair may add packet types but must not remove existing "
                "types: " + ", ".join(removed)
            )
        previous_keys = {packet.casefold() for packet in previous}
        return [
            packet for packet in repaired if packet.casefold() not in previous_keys
        ]

    @staticmethod
    def _reuse_existing_dsl_component(path: Path, component_name: str) -> bool:
        if not path.is_file():
            return False

        UI.dim(f"Validating existing {component_name}: {path}")
        try:
            validate_dsl_dependencies(path)
        except (DSLValidationError, OSError, ValueError) as error:
            UI.warn(
                f"Existing {component_name} failed validation and will be "
                f"regenerated: {error}"
            )
            return False

        if ask_reuse_generated_component(component_name, str(path)):
            UI.success(f"Reusing validated {component_name}: {path}")
            return True

        UI.dim(f"Regenerating {component_name}: {path}")
        return False

    def _prepare_dsl_contract(
        self, packet_types: list[str], dsl_dir: Path, group_size: int
    ) -> dict:
        manifest_path = dsl_dir / "schema_manifest.json"
        report_path, _, _ = self._data_type_paths()
        custom_prefix = normalize_symbol(self.protocol_lower)
        planning_available = manifest_path.is_file() and report_path.is_file()
        if planning_available:
            try:
                self._load_data_type_analysis(report_path)
                _, planning_warning = load_manifest(
                    manifest_path, self.protocol_lower, packet_types, group_size
                )
                if planning_warning:
                    raise ValueError(planning_warning)
            except ValueError as error:
                planning_available = False
                UI.dim(f"Ignoring stale combined DSL plan: {error}")
        planner_action, planner_extra = ask_before_step(
            "Step 2.1: DSL Type Analysis & Schema Planning"
            + (" (existing result available)" if planning_available else ""),
            has_previous=False,
        )
        if planner_action == "exit":
            raise RuntimeError("DSL DataModel preparation stopped by user")
        run_planner = planner_action == "continue"
        if not run_planner and not planning_available:
            raise RuntimeError(
                "DSL planning was skipped without an existing manifest and type analysis"
            )

        planner_prompt = f"""
        In one coordinated task, audit DSL type support and plan a split Peach DSL
        DataModel for {self.protocol_name} packet types:
        {packet_types}

        First read "./docs/peach-dsl.md" completely. It is the authoritative DSL
        language and capability reference. Use RFC_Search to identify every basic
        wire type together with shared structures, discriminators, length/count
        rules, and packet families. The type audit and schema plan must agree: every
        dsl_type in the plan must be justified by the audit. Preserve every known
        RFC-defined length/count relationship in the relevant field's wire_contract
        so the generation agents can model it explicitly with DSL references,
        bounded blocks, arrays, or supported field expressions.

        Judge DSL support only from the documented DSL. Do not inspect peach.txt,
        Peach classes, generated XML, or runtime internals. Classify a type as
        supported when documented DSL declarations or compositions express its
        complete wire language; unsupported only when protocol-specific scalar
        parsing/serialization requires ExtendedType; uncertain when evidence is
        incomplete. Resolve dependencies first: once one scalar is represented by
        ExtendedType, assess containing structures using ordinary Block, Array,
        Optional, and Union.

        The types array is a catalog of UNIQUE wire encodings, not a catalog of
        fields, aliases, semantic roles, or usage sites. Emit one item for each
        distinct parsing and serialization algorithm. When several protocol fields
        use the same encoding, describe those uses in that one item's evidence;
        never emit separate type items for them. In particular, one custom_type
        contract must appear in exactly one unsupported item.

        For every unsupported scalar, recommend exactly one ExtendedType whose DSL
        symbol and element name both start with the protocol prefix
        "{custom_prefix}". Its value type must be int, float, bool, str, or bytes.
        Example naming form: {custom_prefix}VarInt =
        ExtendedType[int]("{custom_prefix}VarInt"). Never recommend an unprefixed
        custom name or an ExtendedType of list, dict, or Schema.

        Write the type audit to "{report_path}" with exactly this JSON shape:
        {{
          "protocol": "{self.protocol_lower}",
          "analysis_basis": "peach-dsl-plan-v7",
          "packet_types": {json.dumps(packet_types)},
          "types": [{{
            "wire_type": "precise protocol type name",
            "encoding": "complete wire encoding and validity bounds",
            "rfc_evidence": "section and concise evidence",
            "status": "supported | unsupported | uncertain",
            "dsl_evidence": "exact documented DSL declarations checked",
            "recommended_dsl": "exact DSL declaration or prefixed ExtendedType",
            "confidence": "high | medium | low",
            "custom_type": null
          }}]
        }}

        For an unsupported item only, replace custom_type null with:
        {{"symbol": "{custom_prefix}DescriptiveName",
          "element_name": "{custom_prefix}DescriptiveName",
          "value_type": "int | float | bool | str | bytes"}}.
        Supported and uncertain items must keep custom_type null.

        Include every primitive, including conventional supported types. Then group
        packet types into families of at most {group_size} items and write the
        DataModel plan to "{manifest_path}" with exactly this JSON shape:
        {{
          "protocol": "{self.protocol_lower}",
          "shared_models": [{{
            "symbol": "PublicCamelCaseSymbol",
            "purpose": "wire responsibility",
            "fields": [{{"name": "field_name", "dsl_type": "DSL construct",
                        "wire_contract": "ordered encoding responsibility"}}]
          }}],
          "packet_groups": [{{
            "id": "lower_snake_case",
            "description": "brief whole-group description covering its family role, common outer framing, and packet structure",
            "packet_types": ["exact requested packet type"],
            "shared_refs": [{{
              "symbol": "shared DSL symbol or prefixed custom type symbol",
              "usage": "exact packet models, fields, or enclosing structures where this symbol is used"
            }}],
            "rfc_queries": ["focused evidence query"],
            "packet_models": [{{
              "packet_type": "exact requested packet type",
              "symbol": "PublicCamelCasePacketSymbol"
            }}]
          }}]
        }}

        Every packet type must appear exactly once and all symbols must be globally
        unique valid Python identifiers. Every planned Python identifier, including
        symbols and field names, must avoid Python keywords. Do not specify
        choice_name, runtime names, or Pit model names; the compiler derives them.
        Each packet group description must give its generation agent the whole
        family context, including common outer framing, discriminators, variable
        headers, payloads, and the group's role in the protocol, so it cannot lose
        outer structure while modeling a local detail.

        shared_refs contains one object per DSL dependency, never runtime names.
        Its symbol may name a declaration in shared_models or a protocol-prefixed
        custom type from the type audit, such as "{custom_prefix}VarInt". Its usage
        must say concretely which packet model(s), field(s), or enclosing structure
        use that symbol. Include no speculative or unused reference, and omit no
        dependency described by the group. A structure used by multiple families
        belongs in shared_models. Write both requested JSON files in this single
        planning task. Do not generate Python, C#, XML, or any other file.
        """
        if run_planner:
            manifest_path.unlink(missing_ok=True)
            report_path.unlink(missing_ok=True)
            agent = build_agent_graph(
                retriever=self.retriever,
                target="peach",
                config=self.agent_config,
                tool_names={"Read_File", "RFC_Search", "Write_File"},
                read_files=(Path("docs/peach-dsl.md"),),
            )
            self.call_agent(
                planner_prompt
                + (f"\nAdditional user instruction:\n{planner_extra}" if planner_extra else ""),
                "Step 2.1: DSL Type Analysis & Schema Planning",
                agent_graph=agent,
            )

        manifest, warning = load_manifest(
            manifest_path, self.protocol_lower, packet_types, group_size
        )
        if warning:
            write_manifest(manifest_path, manifest)
            raise ValueError(
                "DSL planner produced an invalid manifest; a diagnostic fallback "
                f"was saved: {warning}"
            )
        if not report_path.is_file():
            raise RuntimeError(
                f"combined DSL planning did not produce type analysis: {report_path}"
            )
        report = self._load_data_type_analysis(report_path)
        self._finalize_data_type_support(report)
        return manifest

    def _generate_dsl_modules(
        self,
        manifest: dict,
        dsl_dir: Path,
    ) -> None:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        custom_element_context = self._custom_data_element_context()
        manifest_json = json.dumps(manifest, ensure_ascii=False, indent=2)
        shared_path = dsl_dir / "shared_model.py"
        for probe_path in dsl_dir.glob("_probe*.py"):
            probe_path.unlink(missing_ok=True)
        (dsl_dir / "shared.py").unlink(missing_ok=True)

        def generate_shared() -> Path:
            shared_path.write_text("from peach_dsl import *\n", encoding="utf-8")
            prompt = f"""
            Generate only the shared Peach DSL schemas for {self.protocol_name}.
            Contract:
            {manifest_json}

            {custom_element_context}

            Use Read_Shared_DSL_Context to read "./docs/peach-dsl.md", the type
            analysis, schema manifest, and current shared module as needed. This
            tool intentionally cannot read Peach XML, C#, examples, packet-family
            modules, or arbitrary project files. Read the DSL guide completely and
            follow it exactly. Use RFC_Search to confirm every shared field. Use
            Write_Shared_DSL to write "{shared_path}" with `from peach_dsl import
            *`, then define every contracted shared symbol exactly once. Also
            declare every protocol-prefixed ExtendedType from the type analysis
            that appears in shared_refs. Do not specify runtime model names or
            invent a naming decorator. Do not define packet models, packet union,
            ROOT, or family-local structures.

            {_DATAMODEL_MODELING_GUARDRAILS}

            {_DATAMODEL_DSL_SOURCE_STYLE}

            Do not perform I/O or import any non-DSL module. Call
            Validate_Peach_DSL_Module after writing, repair at most three times,
            and finish only after PASS. Never write XML.
            """
            agent = build_agent_graph(
                retriever=self.retriever,
                target="peach",
                config=self.agent_config,
                tool_names={
                    "Read_Shared_DSL_Context", "RFC_Search", "Write_Shared_DSL",
                    "Validate_Peach_DSL_Module",
                },
            )
            self.call_agent(prompt, "Step 2.2: Shared DSL Generation", agent_graph=agent)
            unexpected_probes = sorted(dsl_dir.glob("_probe*.py"))
            if unexpected_probes:
                raise RuntimeError(
                    "shared DSL generation created forbidden probe modules: "
                    + ", ".join(path.name for path in unexpected_probes)
                )
            if not shared_path.is_file():
                raise RuntimeError(f"shared DSL was not generated: {shared_path}")
            validate_dsl_dependencies(shared_path)
            return shared_path

        def generate_family(group: dict, index: int) -> Path:
            family_path = dsl_dir / f"family_{group['id']}.py"
            family_path.unlink(missing_ok=True)
            prompt = f"""
            Generate one Peach DSL packet family for {self.protocol_name}.
            Read "./docs/peach-dsl.md" completely.

            Complete immutable contract:
            {manifest_json}

            Assigned family:
            {json.dumps(group, ensure_ascii=False, indent=2)}

            {custom_element_context}

            Treat the assigned family's description and every shared_refs usage as
            a required whole-model checklist. Use RFC_Search separately for each
            assigned packet. Confirm exact
            discriminator, wire order, sizes/endianness, length/count relations,
            optional conditions, repetition and payload structure. Write only
            "{family_path}". Import exact referenced symbols from shared_model;
            define every Python symbol from packet_models exactly once. The compiler
            derives runtime model names; do not express them with a DSL decorator.
            Do not define shared models, packet union, packet array, or ROOT.

            {_DATAMODEL_MODELING_GUARDRAILS}

            {_DATAMODEL_DSL_SOURCE_STYLE}

            Call Validate_Peach_DSL_Module after writing and use Apply_Patch to
            repair type or syntax failures, at most three times. Never modify
            shared_model.py or write XML.
            """
            agent = build_agent_graph(
                retriever=self.retriever,
                target="peach",
                config=self.agent_config,
                tool_names={
                    "Read_File", "RFC_Search", "Write_File", "Apply_Patch",
                    "Validate_Peach_DSL_Module",
                },
                read_files=(
                    Path("docs/peach-dsl.md"),
                    shared_path,
                    family_path,
                ),
            )
            self.call_agent(
                prompt,
                f"Step 2.2.{index + 1}: DSL Family {group['id']}",
                agent_graph=agent,
            )
            if not family_path.is_file():
                raise RuntimeError(f"family DSL was not generated: {family_path}")
            validate_dsl_dependencies(family_path)
            return family_path

        workers = max(1, _env_int("LLM_PEACH_DATAMODEL_WORKERS", 6))
        if not self._reuse_existing_dsl_component(
            shared_path, "shared DSL model"
        ):
            generate_shared()

        pending_families = []
        for index, group in enumerate(manifest["packet_groups"]):
            family_path = dsl_dir / f"family_{group['id']}.py"
            component_name = f"DSL family {group['id']}"
            if not self._reuse_existing_dsl_component(family_path, component_name):
                pending_families.append((group, index))

        if not pending_families:
            return

        with ThreadPoolExecutor(
            max_workers=min(workers, len(pending_families))
        ) as executor:
            futures = [
                executor.submit(generate_family, group, index)
                for group, index in pending_families
            ]
            for future in as_completed(futures):
                future.result()

    def _compile_with_repair(
        self,
        packet_types: list[str],
        manifest: dict,
        dsl_dir: Path,
        output_dir: Path,
        group_size: int,
    ) -> dict:
        manifest_path = dsl_dir / "schema_manifest.json"
        retries = max(0, _env_int("LLM_PEACH_DATAMODEL_ASSEMBLY_RETRIES", 2))
        for attempt in range(retries + 1):
            root_path = write_root_module(dsl_dir, self.protocol_lower, manifest)
            error = "unknown DSL integration error"
            output_path = output_dir / "datamodel.xml"
            UI.dim(
                "Compiling DSL root to Peach XML "
                f"({attempt + 1}/{retries + 1}): {root_path} -> {output_path}"
            )
            try:
                with UI.status("Compiling DSL root to Peach XML..."):
                    result = compile_dsl_subprocess(root_path, output_path)
            except subprocess.TimeoutExpired as timeout_error:
                result = None
                error = f"DSL compiler timed out after {timeout_error.timeout} seconds"
                UI.error(error)
            if result is not None and result.returncode == 0:
                compiler_output = (result.stdout + result.stderr).strip()
                if compiler_output:
                    UI.dim(f"DSL XML compiler output:\n{compiler_output}")
                UI.dim(f"Validating compiled Peach XML: {output_path}")
                xsd_result = str(
                    validate_peach_xml.invoke(
                        {"xml_path": str(output_path)},
                        config={"callbacks": [self.tool_usage_logger]},
                    )
                )
                if xsd_result.startswith("PASS:"):
                    UI.success(f"DSL compiled to {output_path}")
                    return manifest
                error = xsd_result
                UI.error(f"Compiled Peach XML validation failed:\n{error}")
            elif result is not None:
                error = (result.stdout + result.stderr).strip()
                UI.error(f"DSL XML compilation failed:\n{error}")
            if attempt >= retries:
                raise RuntimeError(
                    f"DSL integration still fails after {retries} repair attempts: {error}"
                )
            modules = [dsl_dir / "shared_model.py"] + [
                dsl_dir / f"family_{group['id']}.py" for group in manifest["packet_groups"]
            ]
            prompt = f"""
            Repair the split {self.protocol_name} Peach DSL after this integration
            compiler error:
            {error}

            Read "./docs/peach-dsl.md", "{manifest_path}", "{root_path}", and:
            {chr(10).join(f'- "{path}"' for path in modules)}

            Preserve every packet assignment and RFC wire semantic. Change only
            schema_manifest.json, shared_model.py, and family modules; root.py and
            datamodel.xml are derived and must not be edited. Shared structures
            used by multiple families belong in shared_model.py. Keep DSL symbols
            globally unique. Make the smallest correction with Apply_Patch when
            editing an existing file, then call
            Validate_Peach_DSL_Module on each changed module. Never write XML.

            {_DATAMODEL_DSL_SOURCE_STYLE}
            """
            agent = build_agent_graph(
                retriever=self.retriever,
                target="peach",
                config=self.agent_config,
                tool_names={
                    "Read_File", "RFC_Search", "Write_File", "Apply_Patch",
                    "Validate_Peach_DSL_Module"
                },
                read_files=(Path("docs/peach-dsl.md"), manifest_path, root_path, *modules),
            )
            self.call_agent(
                prompt,
                f"Step 2.3.{attempt + 1}: DSL Integration Repair",
                agent_graph=agent,
            )
            manifest, warning = load_manifest(
                manifest_path, self.protocol_lower, packet_types, group_size
            )
            if warning:
                raise RuntimeError(f"integration repair produced an invalid manifest: {warning}")
        return manifest

    def repair_datamodel_assembly(
        self, *, allow_packet_type_additions: bool = False
    ) -> None:
        output_dir = Path("./llm/peach") / self.protocol_lower
        dsl_dir = output_dir / "datamodel_dsl"
        manifest_path = dsl_dir / "schema_manifest.json"
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            packet_types = [
                str(packet)
                for group in raw["packet_groups"]
                for packet in group["packet_types"]
            ]
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise RuntimeError(f"cannot load DSL manifest {manifest_path}: {error}") from error
        previous_packet_types = self.state.get("packet_types") or []
        added: list[str] = []
        if allow_packet_type_additions:
            added = self._packet_type_additions(previous_packet_types, packet_types)
        group_size = max(
            max((len(group.get("packet_types", [])) for group in raw["packet_groups"]), default=1),
            _env_int("LLM_PEACH_DATAMODEL_GROUP_SIZE", 4),
        )
        manifest, warning = load_manifest(
            manifest_path, self.protocol_lower, packet_types, group_size
        )
        if warning:
            raise RuntimeError(f"invalid DSL manifest: {warning}")
        self._compile_with_repair(packet_types, manifest, dsl_dir, output_dir, group_size)
        if allow_packet_type_additions and packet_types != previous_packet_types:
            self.state["packet_types"] = packet_types
            if added:
                UI.success(
                    "Added packet type(s) during DataModel repair: "
                    + ", ".join(added)
                )
        self.state["datamodel_format"] = "peach-dsl-v1"
        self.save_state()

    def step_2_datamodel_generation(self):
        UI.title("Step 2: Peach DSL DataModel Generation")
        packet_types = self.state.get("packet_types") or []
        if not packet_types:
            raise RuntimeError("packet_types is empty; run Step 1 before DSL generation")
        output_dir = Path("./llm/peach") / self.protocol_lower
        dsl_dir = output_dir / "datamodel_dsl"
        dsl_dir.mkdir(parents=True, exist_ok=True)
        group_size = max(1, _env_int("LLM_PEACH_DATAMODEL_GROUP_SIZE", 4))
        manifest = self._prepare_dsl_contract(packet_types, dsl_dir, group_size)
        self._generate_dsl_modules(manifest, dsl_dir)
        self._compile_with_repair(
            packet_types, manifest, dsl_dir, output_dir, group_size
        )
        self.state["datamodel_format"] = "peach-dsl-v1"
        self.save_state()
