from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import cast
import unittest

from core.mutator_codegen import PacketMutatorPlan
from core.mutator_validation import collect_issues, parse_report, validate_report


class MutatorReportTests(unittest.TestCase):
    def report(self, *rows: str) -> str:
        return "Mutator Test Results:\n" + "\n".join(rows) + "\n"

    def test_current_statuses_and_interleaved_diagnostics(self) -> None:
        rows = parse_report(self.report(
            "Unable to parse data: bad seed", "   at Parser.Crack()",
            "[P] Good TOTAL=100 PASS=1 FAIL=49 ERROR=0 UNCHANGED=50",
            "[N] NoPass TOTAL=100 PASS=0 FAIL=0 ERROR=0 UNCHANGED=100",
            "[N] Error TOTAL=100 PASS=99 FAIL=0 ERROR=1 UNCHANGED=0",
            "[-] Skipped", "Saved mutator summary: /tmp/summary.log",
        ))
        self.assertFalse(rows["Good"].needs_action)
        self.assertTrue(rows["NoPass"].needs_action)
        self.assertTrue(rows["Error"].needs_action)
        self.assertTrue(rows["Skipped"].skipped)
        self.assertFalse(rows["Skipped"].needs_action)

    def test_invalid_counts_tags_duplicates_and_old_formats_are_rejected(self) -> None:
        invalid = (
            "[P] M TOTAL=100 PASS=0 FAIL=100 ERROR=0 UNCHANGED=0",
            "[N] M TOTAL=100 PASS=100 FAIL=0 ERROR=0 UNCHANGED=0",
            "[P] M TOTAL=100 PASS=99 FAIL=0 ERROR=1 UNCHANGED=0",
            "[N] M TOTAL=0 PASS=0 FAIL=0 ERROR=0 UNCHANGED=0",
            "[P] M TOTAL=100 PASS=1 FAIL=0 ERROR=0 UNCHANGED=0",
            "[N] M TOTAL=100 PASS=-1 FAIL=101 ERROR=0 UNCHANGED=0",
            "[P] M TOTAL=100 PASS=100 FAIL=0 UNCHANGED=0",
            "[PASS][100/100] M", "[RESULT] M TOTAL=100 PASS=100 FAIL=0 UNCHANGED=0",
            "[-] M\n[-] M",
        )
        for row in invalid:
            with self.subTest(row=row), self.assertRaises(RuntimeError):
                parse_report(self.report(row))

    def test_missing_or_repeated_headers_and_failed_commands_are_rejected(self) -> None:
        for text in ("", "[P] M TOTAL=100 PASS=100 FAIL=0 ERROR=0 UNCHANGED=0",
                     self.report() + self.report(), "[ERROR] Mutator validator command failed\n" + self.report()):
            with self.subTest(text=text), self.assertRaises(RuntimeError):
                parse_report(text)

    def packet(self, **enabled: bool) -> PacketMutatorPlan:
        return cast(PacketMutatorPlan, SimpleNamespace(mutators=tuple(
            SimpleNamespace(class_name=name, enabled=value) for name, value in enabled.items()
        )))

    def test_manifest_membership_and_skip_rules(self) -> None:
        results = parse_report(self.report(
            "[P] Good TOTAL=100 PASS=100 FAIL=0 ERROR=0 UNCHANGED=0", "[-] Skipped",
        ))
        validate_report(results, (self.packet(Good=True, Skipped=True),))
        validate_report(results, (self.packet(Good=True, Skipped=False),))
        for packet, message in (
            (self.packet(Good=True, Skipped=True, Missing=True), "omitted planned"),
            (self.packet(Good=True), "unplanned"),
            (self.packet(Good=False, Skipped=True), "Disabled mutators"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(RuntimeError, message):
                validate_report(results, (packet,))
        skipped = parse_report(self.report("[-] Skipped"))
        validate_report(skipped, (self.packet(Skipped=True),))
        validate_report(skipped, (self.packet(Skipped=False),))
        validate_report({}, (self.packet(),))

    def test_error_log_mismatch_is_not_silently_repaired(self) -> None:
        results = parse_report(self.report("[N] M TOTAL=100 PASS=0 FAIL=0 ERROR=100 UNCHANGED=0"))
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with self.assertRaisesRegex(RuntimeError, "log files disagree"):
                collect_issues(results, directory)
            (directory / "error").mkdir()
            (directory / "error" / "M.log").write_text("error evidence")
            (directory / "error" / "Unknown.log").write_text("stale evidence")
            with self.assertRaisesRegex(RuntimeError, "log files disagree"):
                collect_issues(results, directory)

    def test_only_n_evidence_is_collected_and_error_preserves_all_issue_categories(self) -> None:
        results = parse_report(self.report(
            "[N] M TOTAL=100 PASS=1 FAIL=30 ERROR=1 UNCHANGED=68",
            "[P] Good TOTAL=100 PASS=1 FAIL=99 ERROR=0 UNCHANGED=0",
        ))
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for category in ("error", "fail", "unchanged"):
                (directory / category).mkdir()
                (directory / category / "M.log").write_text(f"{category} evidence")
            (directory / "fail" / "Good.log").write_text("accepted evidence")
            issues = collect_issues(results, directory)
            self.assertEqual(len(issues), 1)
            self.assertEqual(issues[0].result.errors, 1)
            for category in ("error", "fail", "unchanged"):
                self.assertIn(f"{category} evidence", issues[0].evidence)
            self.assertNotIn("accepted evidence", issues[0].evidence)


if __name__ == "__main__":
    unittest.main()
