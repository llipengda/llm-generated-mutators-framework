import json
from pathlib import Path
import subprocess
from typing import Any

from peach_dsl.error_report import convert_reports_subprocess
from pipeline.peach_steps.common import (
    PeachStepMixin,
    _DATAMODEL_DSL_SOURCE_STYLE,
    _DATAMODEL_MODELING_GUARDRAILS,
)
from ui import UI, ask_reuse_diagnosis


def _validate_diagnosis_report(
    candidate: Any, dsl_dir: Path
) -> dict[str, object]:
    if (
        not isinstance(candidate, dict)
        or candidate.get("status") != "ok"
        or not isinstance(candidate.get("summary"), str)
        or not isinstance(candidate.get("issues"), list)
    ):
        raise RuntimeError("Diagnosis agent wrote an invalid JSON schema")

    for issue_index, issue in enumerate(candidate["issues"]):
        if (
            not isinstance(issue, dict)
            or not all(
                isinstance(issue.get(key), str)
                for key in ("cause", "evidence", "fix")
            )
        ):
            raise RuntimeError(
                f"Diagnosis issue {issue_index + 1} has an invalid JSON schema"
            )

        locations = issue.get("locations")
        if not isinstance(locations, list) or not locations:
            raise RuntimeError(
                f"Diagnosis issue {issue_index + 1} must have at least one location"
            )

        action = issue.get("action", "modify_existing")
        if action not in {"modify_existing", "add_packet_type"}:
            raise RuntimeError(
                f"Diagnosis issue {issue_index + 1} has an invalid action"
            )
        issue["action"] = action
        if action == "add_packet_type" and (
            not isinstance(issue.get("packet_type"), str)
            or not issue["packet_type"].strip()
        ):
            raise RuntimeError(
                f"Diagnosis issue {issue_index + 1} does not name the packet type to add"
            )

        for location_index, location in enumerate(locations):
            if (
                not isinstance(location, dict)
                or type(location.get("dsl_line")) is not int
                or location["dsl_line"] < 1
                or not all(
                    isinstance(location.get(key), str) and location[key].strip()
                    for key in ("dsl_file", "dsl_path")
                )
            ):
                raise RuntimeError(
                    f"Diagnosis issue {issue_index + 1} location "
                    f"{location_index + 1} has an invalid JSON schema"
                )

            dsl_file = Path(location["dsl_file"])
            if not dsl_file.is_absolute():
                dsl_file = Path.cwd() / dsl_file
            resolved_file = dsl_file.resolve()
            try:
                resolved_file.relative_to(dsl_dir.resolve())
            except ValueError as error:
                raise RuntimeError(
                    f"Diagnosis issue {issue_index + 1} location "
                    f"{location_index + 1} points outside datamodel_dsl"
                ) from error
            if resolved_file == (dsl_dir / "root.py").resolve():
                raise RuntimeError(
                    f"Diagnosis issue {issue_index + 1} location "
                    f"{location_index + 1} points to derived root.py"
                )
            if dsl_file.suffix != ".py":
                raise RuntimeError(
                    f"Diagnosis issue {issue_index + 1} location "
                    f"{location_index + 1} does not point to a DSL module"
                )

    return candidate


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

        1. Read "{converted_report_path}" first. It is a compact, mechanical
           conversion of every Peach cracking tree. Indentation preserves the
           complete parent-child hierarchy; each node carries its DSL type,
           bound DSL path, state, offsets, size, and value. Nested ERROR lines
           and trailing PARSE or MISMATCH lines preserve the failure evidence.
        2. Do not read raw files from "{log_dir}". Analyze the converted report
           yourself: group repeated symptoms, ignore cascading Choice failures,
           and determine the most likely root causes.
        3. Read "{dsl_dir / 'schema_manifest.json'}", then use the converted DSL
           paths to select the relevant shared_model.py or family_<id>.py files.
           Use Read_File_With_Line_Numbers on those DSL modules and confirm the
           final editable file and line for every proposed repair.
        4. Do not read datamodel.xml or root.py. Both are derived artifacts and
           the converted report already contains the relevant evaluated
           structure. root.py is generated by the host, is never an editable
           repair target, and must never appear in a diagnosis location.
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
            self.call_agent(
                prompt,
                "Step 3: Datamodel Failure Diagnosis",
                agent_graph=self.diagnosis_agent_graph,
            )
            candidate = json.loads(report_path.read_text(encoding="utf-8"))
            report = _validate_diagnosis_report(candidate, dsl_dir)
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
                try:
                    _validate_diagnosis_report(
                        json.loads(diagnosis_path.read_text(encoding="utf-8")),
                        diagnosis_path.parent / "datamodel_dsl",
                    )
                except Exception as error:
                    UI.warn(
                        "Existing diagnosis does not follow the current "
                        f"multi-location schema; regenerating it: {error}"
                    )
                    reuse_diagnosis = False
                else:
                    UI.success(
                        f"Reusing existing diagnosis from {diagnosis_path}."
                    )
            if not reuse_diagnosis:
                UI.warning_rule("Step 3: Diagnosing Datamodel Failure")
                self.diagnose_datamodel_failure(test_output)

            UI.warning_rule("Step 3: Applying Datamodel Auto-fix")
            prompt = f"""
        Repair the current {self.protocol_name} Peach DSL from the completed
        diagnosis report. You may add a packet type only when an issue explicitly
        uses action `add_packet_type`.

        **FIRST ACTION**: Use "Read_File" to read
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
           `add_packet_type`, read "./docs/peach-dsl.md" completely, use
           RFC_Search to confirm the new packet's general wire format, then add
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
                agent_graph=self.datamodel_autofix_agent_graph,
            )
            # Validate the manifest, regenerate the derived root, compile the
            # complete DSL, and expose additions to all downstream steps.
            self.repair_datamodel_assembly(allow_packet_type_additions=True)

        self.fix_verify_loop(
            "Step 3: Datamodel Validation & Fix",
            self.verify_datamodel,
            fix_fn,
        )
