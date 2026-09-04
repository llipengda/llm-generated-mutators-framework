import json
from pathlib import Path
import subprocess

from core.agent import build_agent_graph
from peach_dsl.error_report import convert_reports_subprocess
from pipeline.peach_steps.common import (
    PeachStepMixin,
    _DATAMODEL_DSL_SOURCE_STYLE,
    _DATAMODEL_MODELING_GUARDRAILS,
)
from core.ui import UI, ask_reuse_diagnosis


_PEACH_DSL_GUIDE = Path(__file__).resolve().parents[2] / "docs" / "peach-dsl.md"


def _datamodel_repair_read_files(
    dsl_dir: Path, *additional_files: Path
) -> tuple[Path, ...]:
    """Return the exact files a Step 3 diagnosis or repair agent may read."""
    editable_modules = sorted(dsl_dir.glob("family_*.py"))
    candidates = (
        _PEACH_DSL_GUIDE,
        dsl_dir / "schema_manifest.json",
        dsl_dir / "shared_model.py",
        *editable_modules,
        *additional_files,
    )
    return tuple(dict.fromkeys(path.resolve() for path in candidates))


def _datamodel_diagnosis_read_files(
    dsl_dir: Path, *additional_files: Path
) -> tuple[Path, ...]:
    """Return diagnosis inputs, including the generated root as read-only context."""
    return _datamodel_repair_read_files(
        dsl_dir,
        dsl_dir / "root.py",
        *additional_files,
    )


class DatamodelValidationSteps(PeachStepMixin):
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
        dsl_dir = output_dir / "datamodel_dsl"
        log_dir = output_dir / "dm_test_logs"
        converted_report_path = output_dir / "datamodel_error_report.txt"
        report_path = output_dir / "datamodel_diagnosis.json"
        report: dict[str, object] = {
            "status": "error",
            "summary": "Diagnosis has not completed.",
            "issues": [],
        }
        diagnosis_completed = False

        try:
            report_path.unlink(missing_ok=True)
            converted_report_path.unlink(missing_ok=True)
            (output_dir / "datamodel_error_report.json").unlink(missing_ok=True)
            validator_summary = next(
                (
                    line.strip()
                    for line in reversed(test_output.splitlines())
                    if line.strip()
                ),
                "validator failed",
            )
            try:
                conversion = convert_reports_subprocess(
                    dsl_dir / "root.py",
                    log_dir,
                    converted_report_path,
                )
            except subprocess.TimeoutExpired as error:
                raise RuntimeError(
                    f"DSL error report conversion timed out after {error.timeout} seconds"
                ) from error
            if conversion.returncode != 0:
                diagnostics = (conversion.stdout + conversion.stderr).strip()
                raise RuntimeError(
                    "DSL error report conversion failed: " + diagnostics
                )
            prompt = f"""
        Diagnose the failed {self.protocol_name} Peach DataModel.
        Validator summary: {validator_summary}

        **FIRST ACTION**: Use "Read_File" to read
        "{_PEACH_DSL_GUIDE}" completely before inspecting the failure report or
        proposing any repair.

        1. Read "{converted_report_path}". It is a compact, mechanical
           conversion of every Peach cracking tree. Indentation preserves the
           complete parent-child hierarchy; each node carries its DSL type,
           bound DSL path, state, offsets, size, and value. Nested ERROR lines
           and trailing PARSE or MISMATCH lines preserve the failure evidence.
        2. Do not read raw files from "{log_dir}". Analyze the converted report
           yourself: group repeated symptoms, ignore cascading Choice failures,
           and determine the most likely root causes.
        3. Read "{dsl_dir / 'schema_manifest.json'}", then use the converted DSL
           paths to select the relevant shared_model.py or family_<id>.py files.
           Use Read_File on those DSL modules and confirm the
           final editable file and line for every proposed repair.
        4. You may read "{dsl_dir / 'root.py'}" to understand the complete
           host-generated assembly and how the editable schemas are composed.
           root.py is strictly read-only: do not write, modify, patch, validate,
           or propose changes to it, and never use it as a diagnosis location.
           Do not read datamodel.xml; it is also a derived artifact, and the
           converted report already contains its relevant evaluated structure.
        5. Identify every distinct root cause supported by the evidence. Do not
           target a fixed number of issues and do not merge independent causes
           merely to limit the issue count. Use RFC_Search only when a
           wire-format fact must be confirmed.

        A root cause may be that Step 1 omitted an RFC-defined packet type which
        is present in the validation corpus. Diagnose this only when the converted
        report shows that no existing packet branch can represent the packet and
        RFC_Search confirms it is a distinct protocol packet type. In that case,
        use action `add_packet_type`, set `packet_type` to the RFC's canonical
        name, and point a locations entry at the proposed new family_<id>.py
        module (line 1 and the proposed packet schema symbol are acceptable).
        Do not relabel a malformed instance of an existing type as a new type.

        {_DATAMODEL_MODELING_GUARDRAILS}

        A converted failure observation proves that a particular seed failed;
        it does not prove
        that the seed's observed value, size, option set, or packet layout is the
        only valid one. Before recommending a new token or any narrowing change,
        confirm the exact invariant with RFC_Search. In each proposed fix, state
        the general protocol rule being restored; do not propose a seed-specific
        fixed value or exception.

        Special Common Problems:
        1. The packet is encrypted.
           Recommend: add a new packet type with a single Blob field for the encrypted payload.
           The new packet type must be added to schema_manifest.json and a new family_<id>.py module must be created.
           The family should be placed as the last family in schema_manifest.json.

        Use Write_File to write exactly this compact JSON object in English to
        "{report_path}":
        {{
          "status": "ok",
          "summary": "One-sentence conclusion",
          "issues": [{{
            "action": "modify_existing | add_packet_type",
            "packet_type": null,
            "locations": [{{
              "dsl_file": "llm/peach/{self.protocol_lower}/datamodel_dsl/family_x.py",
              "dsl_line": 123,
              "dsl_path": "PacketClass.field_name"
            }}],
            "cause": "Why this is a root cause",
            "evidence": "Direct evidence from the validator log",
            "fix": "The complete correction across every listed location"
          }}]
        }}

        Each issue must contain one or more entries in `locations`. Include every
        declaration that must change to fix that root cause, even when those
        declarations are in different editable DSL modules. Do not emit
        `pit_path`.

        Write no other file. After Write_File succeeds, your final response may
        only briefly confirm that the report was saved; do not print the JSON.
        """
            diagnosis_agent_graph = build_agent_graph(
                retriever=self.retriever,
                config=self.diagnosis_agent_config,
                tool_names={
                    "Read_File",
                    "Search_Files",
                    "RFC_Search",
                    "Write_File",
                },
                read_files=_datamodel_diagnosis_read_files(
                    dsl_dir, converted_report_path
                ),
                write_files=(report_path,),
            )
            self.call_agent(
                prompt,
                "Step 3: Datamodel Failure Diagnosis",
                agent_graph=diagnosis_agent_graph,
            )
            diagnosis = report_path.read_text(encoding="utf-8")
            diagnosis_completed = True
        except Exception as error:
            UI.warn(f"Datamodel LLM diagnosis failed: {error}")
            report["error"] = str(error)
            report_path.unlink(missing_ok=True)
            diagnosis = json.dumps(report, indent=2, ensure_ascii=False)

        if diagnosis_completed:
            UI.success(f"Datamodel diagnosis saved to {report_path}.")
        UI.panel(
            diagnosis,
            title="Datamodel LLM Diagnosis",
            border_style="cyan",
            expand=True,
        )
        if not diagnosis_completed:
            raise RuntimeError(
                "DataModel diagnosis agent did not write a diagnosis report"
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
                UI.success(
                    f"Reusing existing diagnosis from {diagnosis_path}."
                )
            if not reuse_diagnosis:
                UI.warning_rule("Step 3: Diagnosing Datamodel Failure")
                self.diagnose_datamodel_failure(test_output)

            dsl_dir = diagnosis_path.parent / "datamodel_dsl"
            autofix_agent_graph = build_agent_graph(
                retriever=self.retriever,
                config=self.datamodel_autofix_agent_config,
                tool_names={
                    "Read_File",
                    "Search_Files",
                    "RFC_Search",
                    "Write_File",
                    "Apply_Patch",
                    "Validate_Peach_DSL_Module",
                },
                read_files=_datamodel_repair_read_files(
                    dsl_dir, diagnosis_path
                ),
                write_roots=(dsl_dir,),
            )

            UI.warning_rule("Step 3: Applying Datamodel Auto-fix")
            prompt = f"""
        Repair the current {self.protocol_name} Peach DSL from the completed
        diagnosis report. You may add a packet type only when an issue explicitly
        uses action `add_packet_type`.

        **FIRST ACTION**: Use "Read_File" to read
        "{_PEACH_DSL_GUIDE}" completely.
        **SECOND ACTION**: Use "Read_File" to read
        "./llm/peach/{self.protocol_lower}/datamodel_diagnosis.json".
        Treat its `issues` array, in order, as the complete repair plan. For a
        `modify_existing` issue, use every entry in `locations` and its
        `dsl_file`, `dsl_line`, and `dsl_path` to find all affected declarations.
        For an `add_packet_type` issue, `locations` identifies the proposed
        declaration(s).

        You need to:
        1. Read the diagnosis report and apply its issues in order.
        2. Read the files needed for the diagnosed action under
           "./llm/peach/{self.protocol_lower}/datamodel_dsl/". Always read
           schema_manifest.json before adding a packet type.
        3. Apply the diagnosed fixes without performing another diagnosis. A
           single issue may require edits in several files: handle every listed
           location and save every affected editable DSL module.
        4. For `modify_existing`, save only the repaired DSL module(s). For
           `add_packet_type`, use RFC_Search to confirm the new packet's general
           wire format, then add
           one packet contract to schema_manifest.json and create or update its
           family_<id>.py module. Preserve every existing packet assignment,
           symbol, and model. Prefer a new family with a unique lower_snake_case
           id. Use only existing DSL constructs and already-supported custom
           ExtendedTypes; never invent a runtime type whose implementation is
           absent.
        5. Call Validate_Peach_DSL_Module on every changed Python module and
           correct only reported DSL violations. root.py is a derived artifact:
           never edit or validate it. It is regenerated and the complete DSL is
           compiled by the host after your edits.

        Do NOT read validator output, failure logs, seed files, or any other
        source. RFC_Search is allowed only while implementing an explicitly
        diagnosed `add_packet_type`; it must not be used to re-diagnose or expand
        any other issue. The diagnosis report is the sole source of failure
        evidence for this repair.

        {_DATAMODEL_MODELING_GUARDRAILS}

        {_DATAMODEL_DSL_SOURCE_STYLE}

        For an existing declaration, use `fixed(...)` only when the diagnosis
        explicitly identifies the field as an exact RFC-mandated fixed value or
        branch discriminator. For an added packet, RFC_Search must directly
        confirm every fixed discriminator. Never derive a token or bound from
        example bytes in the report. If a proposed edit conflicts with these
        invariants, preserve the broader RFC-valid model and report the conflict
        instead of applying a corpus-specific workaround.

        Never edit datamodel.xml; it is derived from the DSL. Simplifying or
        corpus-specializing the DataModel is NOT allowed.
        """
            if hint:
                prompt += (
                    f"\n\nAdditional guidance from the user:\n{hint}\n"
                )

            self.call_agent(
                prompt,
                "Step 3: Datamodel Validation & Fix",
                agent_graph=autofix_agent_graph,
            )
            # Validate the manifest, regenerate the derived root, compile the
            # complete DSL, and expose additions to all downstream steps.
            self._repair_datamodel_assembly(allow_packet_type_additions=True)

        self.fix_verify_loop(
            "Step 3: Datamodel Validation & Fix",
            self.verify_datamodel,
            fix_fn,
        )
