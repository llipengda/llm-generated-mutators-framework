import json
from pathlib import Path

from pipeline.peach_steps.common import PeachStepMixin, _DATAMODEL_MODELING_GUARDRAILS
from ui import UI, ask_reuse_diagnosis


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

        {_DATAMODEL_MODELING_GUARDRAILS}

        A validator log proves that a particular seed failed; it does not prove
        that the seed's observed value, size, option set, or packet layout is the
        only valid one. Before recommending a new token or any narrowing change,
        confirm the exact invariant with RFC_Search. In each proposed fix, state
        the general protocol rule being restored; do not propose a seed-specific
        constant or exception.

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

        {_DATAMODEL_MODELING_GUARDRAILS}

        Because this repair agent cannot consult the RFC, add `token="true"` only
        when the diagnosis explicitly identifies the field as an exact
        RFC-mandated constant or branch discriminator. Never derive a token or
        bound from example bytes in the report. If a proposed edit conflicts
        with these invariants, preserve the broader RFC-valid model and report
        the conflict instead of applying a corpus-specific workaround.

        **CRITICAL**: Simplifying or corpus-specializing the DataModel is NOT allowed.
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
