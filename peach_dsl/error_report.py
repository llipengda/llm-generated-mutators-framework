"""Parse Peach cracking reports and associate them with evaluated DSL nodes.

The parser intentionally lives outside :mod:`sdk`: a report describes one
runtime cracking attempt, while ``sdk.evaluate_schema`` describes the static
schema.  :func:`evaluate_with_report` joins those two views without mutating the
SDK's frozen result objects.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from .compiler import DSLValidationError, load_dsl_root
from .sdk import (
    ArrayResult,
    EvaluationResult,
    FieldResult,
    OptionalResult,
    ResultMember,
    SchemaInput,
    SchemaResult,
    UnionResult,
    evaluate_schema,
)


_NODE_RE = re.compile(
    r"^(?P<tree>[ |]*?)(?:-\+|\|-\+|\|--)\s+"
    r"(?P<kind>\w+)\s+'(?P<name>[^']+)',\s+"
    r"Bytes:\s*(?P<byte_start>\d+)/(?P<byte_total>\d+),\s+"
    r"Bits:\s*(?P<bit_start>\d+)/(?P<bit_total>\d+)\s*$"
)
_SIZE_RE = re.compile(
    r"^Size:\s+(?:(?P<unknown>\?\?\?)|"
    r"(?P<bytes>\d+)\s+bytes?\s+\|\s+(?P<bits>\d+)\s+bits?)"
    r"(?:\s+\((?P<reason>[^)]*)\))?\s*$"
)
_OCCURS_RE = re.compile(r"^Min:\s*(?P<minimum>\d+),\s*Max:\s*(?P<maximum>\d+)\s*$")
_TOKEN_MISMATCH_RE = re.compile(
    r"^Token did not match '(?P<actual>.*?)' vs\. '(?P<expected>.*?)'\.$"
)
_TRAILER_RE = re.compile(
    r"^Parsed bytes do not match original file for '(?P<file>[^']+)'\s*$"
)
_PARSE_FAILURE_RE = re.compile(
    r"^Failed to parse file '(?P<file>[^']+)':\s*(?P<message>.*)$"
)
_OPTIONAL_CONDITION_ERROR_RE = re.compile(
    r"^Error evaluating condition for Optional 'Optional "
    r"'(?P<path>[^']+)''(?::\s*(?P<message>.*))?$"
)
_HEX_LINE_RE = re.compile(r"^(?:[0-9A-Fa-f]{2})(?:\s+[0-9A-Fa-f]{2})*\s*$")


@dataclass(frozen=True, slots=True)
class ReportError:
    """One failure attached to a Peach report node."""

    message: str
    category: str = "failure"
    actual: str | None = None
    expected: str | None = None


@dataclass(frozen=True, slots=True)
class ReportNode:
    """A structured node from Peach's indented cracking tree."""

    kind: str
    name: str
    depth: int
    byte_start: int
    byte_total: int
    bit_start: int
    bit_total: int
    size_bytes: int | None = None
    size_bits: int | None = None
    size_unknown: bool = False
    size_reason: str | None = None
    min_occurs: int | None = None
    max_occurs: int | None = None
    value: str | None = None
    cache_messages: tuple[str, ...] = ()
    errors: tuple[ReportError, ...] = ()
    failed: bool = False
    succeeded: bool = False
    dsl_path: str | None = None
    children: tuple[ReportNode, ...] = ()

    def walk(self) -> Iterator[ReportNode]:
        yield self
        for child in self.children:
            yield from child.walk()


@dataclass(frozen=True, slots=True)
class ParsedByteMismatch:
    file_name: str
    original: bytes
    parsed: bytes


@dataclass(frozen=True, slots=True)
class ParsedCrackFailure:
    file_name: str
    message: str
    data: bytes


@dataclass(frozen=True, slots=True)
class PeachReport:
    """The parsed runtime report, independent from any DSL declaration."""

    roots: tuple[ReportNode, ...]
    byte_mismatch: ParsedByteMismatch | None = None
    parse_failure: ParsedCrackFailure | None = None
    unparsed_lines: tuple[str, ...] = ()

    def walk(self) -> Iterator[ReportNode]:
        for root in self.roots:
            yield from root.walk()

    @property
    def failures(self) -> tuple[ReportNode, ...]:
        return tuple(node for node in self.walk() if node.failed or node.errors)


@dataclass(frozen=True, slots=True)
class EvaluatedReport:
    """A DSL evaluation together with report observations keyed by DSL path."""

    result: EvaluationResult
    report: PeachReport
    by_path: Mapping[str, tuple[ReportNode, ...]]
    unmatched: tuple[ReportNode, ...]

    def for_path(self, path: str) -> tuple[ReportNode, ...]:
        return self.by_path.get(path, ())

    def for_result(self, result: ResultMember) -> tuple[ReportNode, ...]:
        return () if result.path is None else self.for_path(result.path)

    @property
    def failures(self) -> tuple[ReportNode, ...]:
        return self.report.failures


@dataclass(slots=True)
class _MutableNode:
    kind: str
    name: str
    depth: int
    byte_start: int
    byte_total: int
    bit_start: int
    bit_total: int
    size_bytes: int | None = None
    size_bits: int | None = None
    size_unknown: bool = False
    size_reason: str | None = None
    min_occurs: int | None = None
    max_occurs: int | None = None
    value: str | None = None
    cache_messages: list[str] = field(default_factory=list)
    errors: list[ReportError] = field(default_factory=list)
    failed: bool = False
    succeeded: bool = False
    dsl_path: str | None = None
    children: list[_MutableNode] = field(default_factory=list)


def parse_peach_report(text: str) -> PeachReport:
    """Parse Peach's human-readable cracking tree into immutable objects."""

    roots: list[_MutableNode] = []
    stack: list[_MutableNode] = []
    unparsed: list[str] = []
    mismatch: ParsedByteMismatch | None = None
    parse_failure: ParsedCrackFailure | None = None
    optional_errors: list[tuple[_MutableNode | None, str, ReportError, str]] = []
    seen_optional_errors: set[tuple[str, str]] = set()
    lines = text.splitlines()
    index = 0

    while index < len(lines):
        raw = lines[index]
        node_match = _NODE_RE.match(raw)
        if node_match:
            depth = raw[: raw.index(node_match.group("kind"))].count("|")
            node = _MutableNode(
                kind=node_match.group("kind"),
                name=node_match.group("name"),
                depth=depth,
                byte_start=int(node_match.group("byte_start")),
                byte_total=int(node_match.group("byte_total")),
                bit_start=int(node_match.group("bit_start")),
                bit_total=int(node_match.group("bit_total")),
            )
            while stack and stack[-1].depth >= depth:
                stack.pop()
            if stack:
                stack[-1].children.append(node)
            else:
                roots.append(node)
            stack.append(node)
            index += 1
            continue

        content = _tree_content(raw)
        current = stack[-1] if stack else None
        if current is not None and (size_match := _SIZE_RE.match(content)):
            current.size_unknown = size_match.group("unknown") is not None
            current.size_bytes = _optional_int(size_match.group("bytes"))
            current.size_bits = _optional_int(size_match.group("bits"))
            current.size_reason = size_match.group("reason")
        elif current is not None and (occurs_match := _OCCURS_RE.match(content)):
            current.min_occurs = int(occurs_match.group("minimum"))
            current.max_occurs = int(occurs_match.group("maximum"))
        elif current is not None and content.startswith("Value: "):
            current.value = content.removeprefix("Value: ")
        elif current is not None and content.startswith("Failed: "):
            current.failed = True
            current.errors.append(_parse_error(content.removeprefix("Failed: ")))
        elif current is not None and content.startswith("Cache "):
            current.cache_messages.append(content)
        elif current is not None and (content == "X" or content == "/"):
            closed = _close_node(stack, raw)
            if closed is not None:
                closed.failed = content == "X"
                closed.succeeded = content == "/"
        elif current is not None and content.startswith("X (") and content.endswith(")"):
            closed = _close_node(stack, raw)
            if closed is not None:
                closed.failed = True
                closed.errors.append(ReportError(content[3:-1]))
        elif trailer := _TRAILER_RE.match(raw):
            mismatch, index = _parse_byte_mismatch(lines, index, trailer.group("file"))
            continue
        elif failed_parse := _PARSE_FAILURE_RE.match(raw):
            parse_failure, index = _parse_crack_failure(
                lines,
                index,
                failed_parse.group("file"),
                failed_parse.group("message"),
            )
            continue
        elif optional_error := _OPTIONAL_CONDITION_ERROR_RE.match(raw.strip()):
            path = optional_error.group("path")
            message = optional_error.group("message") or raw.strip()
            key = (path, message)
            if key not in seen_optional_errors:
                seen_optional_errors.add(key)
                target = (
                    current
                    if current is not None
                    and current.kind == "Optional"
                    and current.name == path.rsplit(".", 1)[-1]
                    else None
                )
                category = (
                    "unresolved_reference"
                    if "Referenced element" in message and "not found" in message
                    else "optional_condition"
                )
                optional_errors.append(
                    (target, path, ReportError(message, category=category), raw)
                )
        elif raw.strip():
            unparsed.append(raw)
        index += 1

    for target, path, error, raw in optional_errors:
        resolved = target or _find_mutable_node(roots, tuple(path.split(".")))
        if resolved is None:
            unparsed.append(raw)
            continue
        resolved.errors.append(error)
        resolved.failed = True

    return PeachReport(
        roots=tuple(_freeze_node(node) for node in roots),
        byte_mismatch=mismatch,
        parse_failure=parse_failure,
        unparsed_lines=tuple(unparsed),
    )


def evaluate_with_report(target: SchemaInput, report_text: str) -> EvaluatedReport:
    """Evaluate ``target`` and bind a Peach report to its DSL result paths."""

    return attach_report(evaluate_schema(target), report_text)


def attach_report(result: EvaluationResult, report_text: str) -> EvaluatedReport:
    """Attach a Peach report to an existing :func:`sdk.evaluate_schema` result."""

    paths = tuple(_result_paths(result))
    parsed = parse_peach_report(report_text)
    bound_roots = tuple(_bind_node(root, (), paths) for root in parsed.roots)
    bound_report = PeachReport(
        roots=bound_roots,
        byte_mismatch=parsed.byte_mismatch,
        parse_failure=parsed.parse_failure,
        unparsed_lines=parsed.unparsed_lines,
    )
    grouped: dict[str, list[ReportNode]] = defaultdict(list)
    unmatched: list[ReportNode] = []
    for node in bound_report.walk():
        if node.dsl_path is None:
            unmatched.append(node)
        else:
            grouped[node.dsl_path].append(node)
    return EvaluatedReport(
        result=result,
        report=bound_report,
        by_path=MappingProxyType(
            {path: tuple(nodes) for path, nodes in grouped.items()}
        ),
        unmatched=tuple(unmatched),
    )


def format_dsl_error_reports(
    target: SchemaInput,
    report_texts: Mapping[str, str],
) -> str:
    """Convert Peach cracking trees into compact DSL-oriented trees."""

    result = evaluate_schema(target)
    members = _result_members_by_path(result)
    packet_unions = _packet_union_names(result)
    lines = [f"DSL-REPORT v1 logs={len(report_texts)}"]
    for report_name, report_text in sorted(report_texts.items()):
        evaluated = attach_report(result, report_text)
        lines.append(f"LOG {_quoted(report_name)}")
        for node in evaluated.report.roots:
            lines.extend(
                _format_dsl_tree(node, result, members, packet_unions=packet_unions)
            )
        if evaluated.report.parse_failure is not None:
            lines.append(_format_parse_failure(evaluated.report.parse_failure))
        if evaluated.report.byte_mismatch is not None:
            lines.append(_format_byte_mismatch(evaluated.report.byte_mismatch))
        if evaluated.report.unparsed_lines:
            lines.append(f"UNPARSED count={len(evaluated.report.unparsed_lines)}")
        lines.append("END")
    return "\n".join(lines) + "\n"


def convert_reports_subprocess(
    entry: Path,
    log_dir: Path,
    output: Path,
    *,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    """Convert generated DSL and Peach reports in an isolated interpreter."""

    project_root = Path(__file__).resolve().parent.parent
    runner = (
        "import sys; "
        "sys.path.insert(0, sys.argv.pop(1)); "
        "from peach_dsl.error_report import main; "
        "raise SystemExit(main(sys.argv[1:]))"
    )
    command = [
        sys.executable,
        "-I",
        "-c",
        runner,
        str(project_root),
        "--entry",
        str(entry),
        "--log-dir",
        str(log_dir),
        "--output",
        str(output),
    ]
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONIOENCODING": "utf-8",
    }
    return subprocess.run(
        command,
        cwd=project_root,
        env=environment,
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def _format_dsl_tree(
    node: ReportNode,
    root: EvaluationResult,
    members: Mapping[str, ResultMember],
    depth: int = 0,
    *,
    packet_unions: frozenset[str] = frozenset(),
) -> list[str]:
    children = node.children
    union_candidates_filtered = False
    if node.kind == "Choice" and node.name in packet_unions:
        retained = tuple(
            child for child in children if not _is_packet_type_rejection(child)
        )
        if retained:
            children = retained
            union_candidates_filtered = len(retained) < len(node.children)
        elif len(children) > 1:
            # Every packet candidate was rejected by its packet_type token.
            # Keep only the branch that cracked furthest to avoid cascades.
            children = (_longest_parsed_branch(node, children),)
            union_candidates_filtered = True
    errors = (
        tuple(
            error
            for error in node.errors
            if error.message != "No valid children were found."
        )
        if union_candidates_filtered
        else node.errors
    )
    member: EvaluationResult | ResultMember | None = (
        root
        if depth == 0
        else (members.get(node.dsl_path) if node.dsl_path is not None else None)
    )
    node_type = _dsl_node_type(member, node.kind)
    identity = (
        'path="$root"'
        if depth == 0
        else (
            f"path={_quoted(node.dsl_path)}"
            if node.dsl_path is not None
            else f"name={_quoted(node.name)}"
        )
    )
    size = (
        f"{node.size_bytes}B/{node.size_bits}b"
        if node.size_bytes is not None and node.size_bits is not None
        else "?"
    )
    state = "FAIL" if node.failed or errors else ("PASS" if node.succeeded else "OPEN")
    value = f" value={_quoted(node.value)}" if node.value is not None else ""
    prefix = "  " * depth
    lines = [
        prefix + (
            f"{node_type} {identity} state={state} "
            f"bytes={node.byte_start}/{node.byte_total} "
            f"bits={node.bit_start}/{node.bit_total} size={size}{value}"
        )
    ]
    lines.extend(prefix + "  " + _format_error(error) for error in errors)
    for child in children:
        lines.extend(
            _format_dsl_tree(
                child,
                root,
                members,
                depth + 1,
                packet_unions=packet_unions,
            )
        )
    return lines


def _packet_union_names(result: EvaluationResult) -> frozenset[str]:
    names: set[str] = set()

    def collect(member: EvaluationResult | ResultMember) -> None:
        if isinstance(member, SchemaResult):
            if member.packet_union is not None:
                names.add(member.packet_union)
            for child in member.fields.values():
                collect(child)
        elif isinstance(member, UnionResult):
            for alternative in member.alternatives:
                collect(alternative)
        elif isinstance(member, (ArrayResult, OptionalResult)):
            collect(member.element)

    collect(result)
    return frozenset(names)


def _is_packet_type_rejection(node: ReportNode) -> bool:
    errors = [
        (candidate, error)
        for candidate in node.walk()
        for error in candidate.errors
    ]
    return bool(errors) and all(
        candidate.name == "packet_type" and error.category == "token_mismatch"
        for candidate, error in errors
    )


def _longest_parsed_branch(
    union: ReportNode, children: tuple[ReportNode, ...]
) -> ReportNode:
    """Select the packet candidate that advanced furthest in its input.

    Peach reports offsets relative to the active slice.  Subtracting a node's
    slice size from the Union's size translates nested offsets back into the
    Union's coordinate space.  A known field size counts as consumed input.
    ``max`` keeps declaration/report order when two alternatives tie.
    """

    return max(
        children,
        key=lambda child: _parsed_progress_bits(child, union.bit_total),
    )


def _parsed_progress_bits(node: ReportNode, union_bits: int) -> int:
    progress = 0
    for candidate in node.walk():
        slice_offset = max(0, union_bits - candidate.bit_total)
        consumed = candidate.size_bits or 0
        progress = max(progress, slice_offset + candidate.bit_start + consumed)
    return progress


def _dsl_node_type(
    member: EvaluationResult | ResultMember | None, runtime_kind: str
) -> str:
    if isinstance(member, FieldResult):
        return f"Field<{member.kind}>"
    if isinstance(member, SchemaResult):
        return f"Schema<{member.name}>"
    if isinstance(member, UnionResult):
        return "Union"
    if isinstance(member, ArrayResult):
        return "Array"
    if isinstance(member, OptionalResult):
        return "Optional"
    return {
        "DataModel": "Schema",
        "Block": "Block",
        "Choice": "Union",
        "Array": "Array",
        "Optional": "Optional",
        "Number": "Field<number>",
        "String": "Field<string>",
        "Blob": "Field<bytes>",
        "Flag": "Field<bit>",
        "Flags": "Flags",
    }.get(runtime_kind, f"Field<{runtime_kind.lower()}>")


def _result_members_by_path(result: EvaluationResult) -> dict[str, ResultMember]:
    members: dict[str, ResultMember] = {}

    def collect(member: EvaluationResult | ResultMember) -> None:
        if member.path is not None:
            members[member.path] = member
        if isinstance(member, SchemaResult):
            for child in member.fields.values():
                collect(child)
        elif isinstance(member, UnionResult):
            for alternative in member.alternatives:
                collect(alternative)
        elif isinstance(member, (ArrayResult, OptionalResult)):
            collect(member.element)

    collect(result)
    return members


def _format_error(error: ReportError) -> str:
    details = [f"ERROR category={error.category}"]
    if error.actual is not None:
        details.append(f"actual={_quoted(error.actual)}")
    if error.expected is not None:
        details.append(f"expected={_quoted(error.expected)}")
    details.append(f"message={_quoted(error.message)}")
    return "! " + " ".join(details)


def _format_byte_mismatch(mismatch: ParsedByteMismatch) -> str:
    limit = min(len(mismatch.original), len(mismatch.parsed))
    difference = next(
        (
            index
            for index in range(limit)
            if mismatch.original[index] != mismatch.parsed[index]
        ),
        limit if len(mismatch.original) != len(mismatch.parsed) else None,
    )
    center = difference or 0
    start = max(0, center - 32)
    end = min(max(len(mismatch.original), len(mismatch.parsed)), center + 96)
    difference_text = "none" if difference is None else str(difference)
    return (
        f"MISMATCH seed={_quoted(mismatch.file_name)} "
        f"lengths={len(mismatch.original)}/{len(mismatch.parsed)} "
        f"diff={difference_text} window={start}:{end} "
        f"original={mismatch.original[start:end].hex().upper()} "
        f"parsed={mismatch.parsed[start:end].hex().upper()}"
    )


def _format_parse_failure(failure: ParsedCrackFailure) -> str:
    preview = failure.data[:256].hex().upper()
    suffix = "+" if len(failure.data) > 256 else ""
    return (
        f"PARSE seed={_quoted(failure.file_name)} "
        f"message={_quoted(failure.message)} "
        f"input={len(failure.data)}B:{preview}{suffix}"
    )


def _quoted(value: str) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _tree_content(line: str) -> str:
    content = line
    while content.startswith(" |"):
        content = content[2:]
    return content.lstrip(" |")


def _close_node(stack: list[_MutableNode], line: str) -> _MutableNode | None:
    """Close the node selected by a Peach ``X`` or ``/`` tree marker."""

    depth = line[: line.find("X") if "X" in line else line.find("/")].count("|")
    while stack and stack[-1].depth > depth:
        stack.pop()
    if not stack or stack[-1].depth != depth:
        return None
    return stack.pop()


def _optional_int(value: str | None) -> int | None:
    return None if value is None else int(value)


def _parse_error(message: str) -> ReportError:
    if match := _TOKEN_MISMATCH_RE.match(message):
        return ReportError(
            message=message,
            category="token_mismatch",
            actual=match.group("actual"),
            expected=match.group("expected"),
        )
    return ReportError(message)


def _parse_byte_mismatch(
    lines: list[str], start: int, file_name: str
) -> tuple[ParsedByteMismatch, int]:
    original = b""
    parsed = b""
    index = start + 1
    while index < len(lines):
        label = lines[index].strip()
        if label in {"Original Bytes:", "Parsed   Bytes:"}:
            target_original = label == "Original Bytes:"
            index += 1
            chunks: list[str] = []
            while index < len(lines) and _HEX_LINE_RE.match(lines[index].strip()):
                chunks.append(lines[index].strip())
                index += 1
            value = bytes.fromhex(" ".join(chunks)) if chunks else b""
            if target_original:
                original = value
            else:
                parsed = value
            continue
        if label:
            break
        index += 1
    return ParsedByteMismatch(file_name, original, parsed), index


def _parse_crack_failure(
    lines: list[str], start: int, file_name: str, message: str
) -> tuple[ParsedCrackFailure, int]:
    index = start + 1
    while index < len(lines) and not lines[index].strip():
        index += 1
    if index < len(lines) and lines[index].strip() == "Bytes:":
        index += 1
    chunks: list[str] = []
    while index < len(lines) and _HEX_LINE_RE.match(lines[index].strip()):
        chunks.append(lines[index].strip())
        index += 1
    data = bytes.fromhex(" ".join(chunks)) if chunks else b""
    return ParsedCrackFailure(file_name, message, data), index


def _find_mutable_node(
    roots: list[_MutableNode], path: tuple[str, ...]
) -> _MutableNode | None:
    """Find the runtime node named by a Peach debug path."""

    def visit(
        node: _MutableNode, ancestors: tuple[str, ...]
    ) -> _MutableNode | None:
        current = ancestors + (node.name,)
        if current == path:
            return node
        for child in node.children:
            if found := visit(child, current):
                return found
        return None

    for root in roots:
        if found := visit(root, ()):
            return found
    return None


def _freeze_node(node: _MutableNode) -> ReportNode:
    return ReportNode(
        kind=node.kind,
        name=node.name,
        depth=node.depth,
        byte_start=node.byte_start,
        byte_total=node.byte_total,
        bit_start=node.bit_start,
        bit_total=node.bit_total,
        size_bytes=node.size_bytes,
        size_bits=node.size_bits,
        size_unknown=node.size_unknown,
        size_reason=node.size_reason,
        min_occurs=node.min_occurs,
        max_occurs=node.max_occurs,
        value=node.value,
        cache_messages=tuple(node.cache_messages),
        errors=tuple(node.errors),
        failed=node.failed,
        succeeded=node.succeeded,
        dsl_path=node.dsl_path,
        children=tuple(_freeze_node(child) for child in node.children),
    )


def _result_paths(result: EvaluationResult | ResultMember) -> Iterator[str]:
    if result.path is not None:
        yield result.path
    if isinstance(result, SchemaResult):
        for member in result.fields.values():
            yield from _result_paths(member)
    elif isinstance(result, UnionResult):
        for alternative in result.alternatives:
            yield from _result_paths(alternative)
    elif isinstance(result, (ArrayResult, OptionalResult)):
        yield from _result_paths(result.element)


def _bind_node(
    node: ReportNode, ancestors: tuple[str, ...], paths: tuple[str, ...]
) -> ReportNode:
    names = ancestors + (node.name,)
    path = _best_dsl_path(names, paths)
    return ReportNode(
        kind=node.kind,
        name=node.name,
        depth=node.depth,
        byte_start=node.byte_start,
        byte_total=node.byte_total,
        bit_start=node.bit_start,
        bit_total=node.bit_total,
        size_bytes=node.size_bytes,
        size_bits=node.size_bits,
        size_unknown=node.size_unknown,
        size_reason=node.size_reason,
        min_occurs=node.min_occurs,
        max_occurs=node.max_occurs,
        value=node.value,
        cache_messages=node.cache_messages,
        errors=node.errors,
        failed=node.failed,
        succeeded=node.succeeded,
        dsl_path=path,
        children=tuple(_bind_node(child, names, paths) for child in node.children),
    )


def _best_dsl_path(names: tuple[str, ...], paths: tuple[str, ...]) -> str | None:
    candidates = [path for path in paths if path.rsplit(".", 1)[-1] == names[-1]]
    if not candidates:
        return None

    def suffix_score(path: str) -> int:
        parts = tuple(path.split("."))
        score = 0
        name_index = len(names) - 1
        for part in reversed(parts):
            while name_index >= 0 and names[name_index] != part:
                name_index -= 1
            if name_index < 0:
                break
            score += 1
            name_index -= 1
        return score

    scored = sorted(((suffix_score(path), path) for path in candidates), reverse=True)
    if not scored or scored[0][0] == 0:
        return None
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None
    return scored[0][1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert Peach validator reports to compact DSL paths"
    )
    parser.add_argument("--entry", required=True, type=Path)
    parser.add_argument("--log-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)

    try:
        log_paths = sorted(args.log_dir.glob("*.log"))
        if not log_paths:
            raise ValueError(f"no Peach validator logs found in {args.log_dir}")
        report_texts = {
            path.name: path.read_text(encoding="utf-8", errors="replace")
            for path in log_paths
        }
        converted = format_dsl_error_reports(
            load_dsl_root(args.entry), report_texts
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(converted, encoding="utf-8")
        os.replace(temporary, args.output)
        print(f"[PASS] Compact DSL error report written to {args.output}")
        return 0
    except (DSLValidationError, OSError, TypeError, ValueError) as error:
        print(f"[FAIL] {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
