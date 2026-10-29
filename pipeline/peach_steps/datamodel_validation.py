import json
from pathlib import Path

from core.prompts import render_prompt
from core.agent import build_agent_graph
from pipeline.peach_steps.common import PeachStepMixin
from core.ui import UI, ask_reuse_diagnosis


_PEACH_DSL_GUIDE = Path(__file__).resolve().parents[2] / "docs" / "peach-dsl.md"


def _validator_summary(output: str) -> str | None:
    return next(
        (
            line.strip()
            for line in reversed(output.splitlines())
            if "tests passed." in line
            and (
                line.strip().startswith("[PASS]")
                or line.strip().startswith("[FAIL]")
            )
        ),
        None,
    )


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

        summary = _validator_summary(result.stdout)
        UI.panel(f"Result: [bold]{summary or 'validator failed'}[/bold]")

        if summary is not None and summary.startswith("[FAIL]"):
            return False, result.stdout
        if summary is not None and summary.startswith("[PASS]"):
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
            (output_dir / "datamodel_error_report.json").unlink(missing_ok=True)
            validator_summary = _validator_summary(test_output) or "validator failed"
            if not converted_report_path.is_file():
                raise RuntimeError(
                    "Datamodel test script did not generate the DSL error report: "
                    f"{converted_report_path}"
                )
            prompt = render_prompt(
                "datamodel/diagnosis.txt",
                protocol_name=self.protocol_name,
                validator_summary=validator_summary,
                converted_report_path=converted_report_path,
                log_dir=log_dir,
                dsl_dir=dsl_dir,
                report_path=report_path,
                protocol_lower=self.protocol_lower,
            )
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
            prompt = render_prompt(
                "datamodel/repair.txt",
                protocol_name=self.protocol_name,
                protocol_lower=self.protocol_lower,
            )
            if hint:
                prompt += (
                    render_prompt("shared/user_guidance.txt", hint=hint)
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
