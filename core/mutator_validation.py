"""Current validator report contract and packet-local repair evidence."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from core.mutator_codegen import MutatorSpec, PacketMutatorPlan


REPORT_HEADER = "Mutator Test Results:"
COMMAND_FAILURE_PREFIX = "[ERROR] Mutator validator command failed"
_RESULT = re.compile(
    r"\[(P|N)\] (\w+) TOTAL=(\d+) PASS=(\d+) FAIL=(\d+) ERROR=(\d+) UNCHANGED=(\d+)"
)
_SKIP = re.compile(r"\[-\] (\w+)")


@dataclass(frozen=True)
class MutatorResult:
    name: str
    total: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    unchanged: int = 0

    @property
    def skipped(self) -> bool:
        return self.total == 0

    @property
    def needs_action(self) -> bool:
        return not self.skipped and (self.errors > 0 or self.passed == 0)

    def summary(self) -> str:
        if self.skipped:
            return f"[-] {self.name}"
        tag = "N" if self.needs_action else "P"
        return (f"[{tag}] {self.name} TOTAL={self.total} PASS={self.passed} "
                f"FAIL={self.failed} ERROR={self.errors} UNCHANGED={self.unchanged}")


@dataclass(frozen=True)
class MutatorIssue:
    result: MutatorResult
    evidence: str


def parse_report(output: str) -> dict[str, MutatorResult]:
    """Accept only the current compact format; never infer missing counts."""
    if output.startswith(COMMAND_FAILURE_PREFIX):
        raise RuntimeError("Saved mutator verification records a failed validator command. Rerun Step 5.")
    if output.splitlines().count(REPORT_HEADER) != 1:
        raise RuntimeError("Mutator validator did not complete a single results report. Rerun validation.")
    report = output.split(REPORT_HEADER, 1)[1]
    results: dict[str, MutatorResult] = {}
    for line in report.splitlines():
        line = line.strip()
        if not line or line.startswith("Saved mutator summary: "):
            continue
        skip = _SKIP.fullmatch(line)
        match = _RESULT.fullmatch(line)
        if skip:
            result = MutatorResult(skip[1])
        elif match:
            tag, name, *counts = match.groups()
            result = MutatorResult(name, *map(int, counts))
            if result.total <= 0 or result.total != sum((result.passed, result.failed, result.errors, result.unchanged)):
                raise RuntimeError(f"Invalid mutator iteration counts for {name}; rerun validation.")
            if (tag == "N") != result.needs_action:
                raise RuntimeError(f"Invalid mutator result status for {name}; rerun validation.")
        else:
            # Seed cracking diagnostics and stack traces can be interleaved
            # with completed results. Bracketed rows must obey the contract.
            if not line.startswith("["):
                continue
            raise RuntimeError(f"Unsupported or malformed mutator result; rerun with the current SDK: {line}")
        if result.name in results:
            raise RuntimeError(f"Mutator validator reported duplicate class names: {result.name}")
        results[result.name] = result
    return results


def validate_report(
    results: dict[str, MutatorResult], packets: tuple[PacketMutatorPlan, ...],
) -> None:
    specs = {spec.class_name: spec for packet in packets for spec in packet.mutators}
    missing = specs.keys() - results.keys()
    unexpected = results.keys() - specs.keys()
    if missing:
        raise RuntimeError("Mutator validator omitted planned classes: " + ", ".join(sorted(missing)))
    if unexpected:
        raise RuntimeError("Mutator validator reported unplanned classes: " + ", ".join(sorted(unexpected)))
    if any(not spec.enabled and not results[name].skipped for name, spec in specs.items()):
        raise RuntimeError("Disabled mutators were still tested; rebuild the managed mutator assemblies.")


def collect_issues(results: dict[str, MutatorResult], directory: Path) -> tuple[MutatorIssue, ...]:
    reported_errors = {name for name, result in results.items() if result.errors}
    logged_errors = {path.stem for path in (directory / "error").glob("*.log")}
    if reported_errors != logged_errors:
        raise RuntimeError("Mutator ERROR results and log files disagree; rerun validation.")
    issues: list[MutatorIssue] = []
    for name, result in results.items():
        if not result.needs_action:
            continue
        evidence = [result.summary()]
        # A class can have ERROR, FAIL and UNCHANGED outcomes in the same run.
        for category, count in (("error", result.errors), ("fail", result.failed), ("unchanged", result.unchanged)):
            path = directory / category / f"{name}.log"
            if count and path.is_file():
                evidence.append(f"{category.upper()} log:\n{path.read_text(encoding='utf-8')}")
        issues.append(MutatorIssue(result, "\n\n".join(evidence)))
    return tuple(issues)


def require_semantic_repair(spec: MutatorSpec, issue: MutatorIssue) -> None:
    generated_frame = re.search(
        re.escape(spec.class_name) + r"\.(?:supportedDataElement|\.ctor)\s*\(", issue.evidence,
    )
    if not spec.hooks or generated_frame:
        raise RuntimeError(
            f"{spec.class_name} failed in generated code or the structural SDK. "
            f"Fix the generator/SDK rather than editing partial sources.\n{issue.evidence}"
        )
