from core.prompts import render_prompt
from core.log import run_logged_task
import json
from pathlib import Path
import subprocess
from typing import Any, Callable, cast

from core.agent import build_agent_graph
from core.datamodel_dsl import (
    compile_dsl_subprocess,
    load_manifest,
    normalize_symbol,
    write_manifest,
    write_root_module,
)
from peach_dsl.compiler import DSLValidationError, validate_dsl_dependencies
from pipeline.peach_steps.common import (
    env_int,
    PeachStepMixin,
)
from core.tools import validate_peach_xml
from core.ui import UI, ask_before_step, ask_reuse_generated_component


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
    ) -> dict[str, Any]:
        checkpoint = "step_2_1_dsl_type_analysis_and_schema_planning"
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
        resumed_checkpoint = self.has_completed_checkpoint(checkpoint)
        # States created before sub-step checkpoints are still recoverable: a
        # validated plan is conclusive evidence that 2.1 finished.  Do this only
        # when the user selected resume, so a deliberately fresh run still asks
        # whether to reuse or regenerate its artifacts.
        if planning_available and self.resumed_from_saved_state:
            if not resumed_checkpoint:
                self.mark_checkpoint_completed(checkpoint)
            UI.success("Resuming after completed Step 2.1: DSL plan is valid.")
            run_planner = False
            planner_extra = None
        else:
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

        planner_prompt = render_prompt(
            "datamodel/planning.txt",
            protocol_name=self.protocol_name,
            packet_types=packet_types,
            custom_prefix=custom_prefix,
            report_path=report_path,
            protocol_lower=self.protocol_lower,
            packet_types_json=json.dumps(packet_types),
            group_size=group_size,
            manifest_path=manifest_path,
        )
        if run_planner:
            manifest_path.unlink(missing_ok=True)
            report_path.unlink(missing_ok=True)
            agent = build_agent_graph(
                retriever=self.retriever,
                config=self.agent_config,
                tool_names={"Read_File", "Search_Files", "RFC_Search", "Write_File"},
                read_files=(Path("docs/peach-dsl.md"),),
                write_files=(manifest_path, report_path),
            )
            self.call_agent(
                planner_prompt
                + (render_prompt("shared/planner_user_instruction.txt", planner_extra=planner_extra) if planner_extra else ""),
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
        self.mark_checkpoint_completed(checkpoint)
        return manifest

    def _generate_dsl_modules(
        self,
        manifest: dict[str, Any],
        dsl_dir: Path,
    ) -> None:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        custom_element_context = self._custom_data_element_context()
        manifest_json = json.dumps(manifest, ensure_ascii=False, indent=2)
        shared_path = dsl_dir / "shared_model.py"
        report_path = dsl_dir.parent / "data_type_analysis.json"
        manifest_path = dsl_dir / "schema_manifest.json"
        for probe_path in dsl_dir.glob("_probe*.py"):
            probe_path.unlink(missing_ok=True)
        (dsl_dir / "shared.py").unlink(missing_ok=True)

        def generate_shared() -> Path:
            shared_path.write_text("from peach_dsl import *\n", encoding="utf-8")
            prompt = render_prompt(
                "datamodel/shared_generation.txt",
                protocol_name=self.protocol_name,
                manifest_json=manifest_json,
                custom_element_context=custom_element_context,
                shared_path=shared_path,
            )
            agent = build_agent_graph(
                retriever=self.retriever,
                config=self.agent_config,
                tool_names={
                    "Read_File", "Search_Files", "RFC_Search", "Write_File",
                    "Validate_Peach_DSL_Module",
                },
                read_files=(
                    Path("docs/peach-dsl.md"),
                    report_path,
                    manifest_path,
                    shared_path,
                ),
                write_files=(shared_path,),
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

        def generate_family(group: dict[str, Any], index: int) -> Path:
            family_path = dsl_dir / f"family_{group['id']}.py"
            family_path.unlink(missing_ok=True)
            prompt = render_prompt(
                "datamodel/family_generation.txt",
                protocol_name=self.protocol_name,
                manifest_json=manifest_json,
                group_json=json.dumps(group, ensure_ascii=False, indent=2),
                custom_element_context=custom_element_context,
                family_path=family_path,
            )
            agent = build_agent_graph(
                retriever=self.retriever,
                config=self.agent_config,
                tool_names={
                    "Read_File", "Search_Files", "RFC_Search", "Write_File", "Apply_Patch",
                    "Validate_Peach_DSL_Module",
                },
                read_files=(
                    Path("docs/peach-dsl.md"),
                    shared_path,
                    family_path,
                ),
                write_files=(family_path,),
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

        workers = max(1, env_int("LLM_PEACH_DATAMODEL_WORKERS", 6))
        if not self._reuse_existing_dsl_component(
            shared_path, "shared DSL model"
        ):
            generate_shared()

        pending_families: list[tuple[dict[str, Any], int]] = []
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
                executor.submit(run_logged_task, f"DSL {group['id']}", generate_family, group, index)
                for group, index in pending_families
            ]
            for future in as_completed(futures):
                future.result()

    def _compile_with_repair(
        self,
        packet_types: list[str],
        manifest: dict[str, Any],
        dsl_dir: Path,
        output_dir: Path,
        group_size: int,
    ) -> dict[str, Any]:
        manifest_path = dsl_dir / "schema_manifest.json"
        retries = max(0, env_int("LLM_PEACH_DATAMODEL_ASSEMBLY_RETRIES", 2))
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
                xsd_result = cast(
                    dict[str, Any],
                    cast(
                        Callable[..., object], getattr(validate_peach_xml, "invoke")
                    )(
                    {"xml_path": str(output_path)},
                    config={"callbacks": [self.tool_usage_logger]},
                    ),
                )
                if xsd_result.get("ok", False):
                    UI.success(f"DSL compiled to {output_path}")
                    return manifest
                error = xsd_result.get("message", str(xsd_result))
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
            prompt = render_prompt(
                "datamodel/assembly_repair.txt",
                protocol_name=self.protocol_name,
                error=error,
                manifest_path=manifest_path,
                root_path=root_path,
                module_paths=chr(10).join((f'- "{path}"' for path in modules)),
            )
            agent = build_agent_graph(
                retriever=self.retriever,
                config=self.agent_config,
                tool_names={
                    "Read_File", "Search_Files", "RFC_Search", "Write_File", "Apply_Patch",
                    "Validate_Peach_DSL_Module"
                },
                read_files=(Path("docs/peach-dsl.md"), manifest_path, root_path, *modules),
                write_files=(manifest_path, *modules),
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

    def _repair_datamodel_assembly(
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
            env_int("LLM_PEACH_DATAMODEL_GROUP_SIZE", 4),
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
        group_size = max(1, env_int("LLM_PEACH_DATAMODEL_GROUP_SIZE", 4))
        manifest = self._prepare_dsl_contract(packet_types, dsl_dir, group_size)
        self._generate_dsl_modules(manifest, dsl_dir)
        self._compile_with_repair(
            packet_types, manifest, dsl_dir, output_dir, group_size
        )
        self.state["datamodel_format"] = "peach-dsl-v1"
        self.save_state()
