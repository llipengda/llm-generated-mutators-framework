#!/usr/bin/env python3
"""Diagnose Peach DataModel failures from existing cracker logs.

The tool deliberately separates observations from protocol-specific conclusions.
It can locate likely XML elements and recognize common failure shapes without
requiring access to the Peach runtime or an RFC.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import xml.parsers.expat
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence


TREE_RE = re.compile(
    r"^(?P<prefix>.*?)"
    r"(?P<tag>DataModel|Block|Choice|Array|Optional|Number|Blob|String) "
    r"'(?P<name>[^']+)', Bytes: (?P<offset>\d+)/(?P<total>\d+)"
)
SIZE_RE = re.compile(r"Size: (?P<bytes>\d+) bytes? \| (?P<bits>\d+) bits")
VALUE_RE = re.compile(r"Value: (?P<value>-?\d+)(?: \(0x[0-9A-Fa-f]+\))?")
BUFFER_RE = re.compile(
    r"Length is (?P<wanted>\d+) bits but buffer only has "
    r"(?P<remaining>\d+) bits left"
)
REFERENCE_RE = re.compile(
    r"Referenced element '(?P<reference>[^']+)' not found"
)
BINDING_RE = re.compile(
    r"Unable to resolve binding '(?P<binding>[^']+)' attached to "
    r"'(?P<path>[^']+)'"
)
UNSIZED_RE = re.compile(r"Failed: Element is unsized\.")
OPTIONAL_PATH_RE = re.compile(r"Optional 'Optional '(?P<path>[^']+)''")
PARSE_FAILURE_RE = re.compile(r"Failed to parse file '(?P<seed>[^']+)':")
ROUNDTRIP_RE = re.compile(
    r"Parsed bytes do not match original file for '(?P<seed>[^']+)'"
)
TOKEN_MISMATCH_RE = re.compile(r"Token did not match")
HEX_LINE_RE = re.compile(r"^(?:[0-9A-Fa-f]{2})(?:\s+[0-9A-Fa-f]{2})*\s*$")


@dataclass
class XmlLocation:
    line: int
    tag: str
    name: str | None
    model: str | None
    attributes: dict[str, str]


@dataclass
class Diagnostic:
    code: str
    severity: str
    confidence: float
    message: str
    evidence: list[str] = field(default_factory=list)
    log_lines: list[int] = field(default_factory=list)
    xml_locations: list[XmlLocation] = field(default_factory=list)


@dataclass
class ElementEvent:
    line: int
    depth: int
    tag: str
    name: str
    offset: int
    total: int
    size_bytes: int | None = None
    value: int | None = None


@dataclass
class XmlNode:
    tag: str
    attributes: dict[str, str]
    line: int
    parent: "XmlNode | None" = None
    children: list["XmlNode"] = field(default_factory=list)

    @property
    def name(self) -> str | None:
        return self.attributes.get("name")

    @property
    def model(self) -> str | None:
        node: XmlNode | None = self
        while node is not None:
            if node.tag == "DataModel":
                return node.name
            node = node.parent
        return None


class XmlIndex:
    """Small source-line-preserving XML index based only on the stdlib."""

    def __init__(self, path: Path):
        self.path = path
        self.nodes: list[XmlNode] = []
        self.by_name: dict[str, list[XmlNode]] = {}
        self.models: dict[str, XmlNode] = {}
        self._parse(path)

    @staticmethod
    def _local_name(name: str) -> str:
        return name.rsplit(":", 1)[-1]

    def _parse(self, path: Path) -> None:
        stack: list[XmlNode] = []
        parser = xml.parsers.expat.ParserCreate()

        def start(name: str, attrs: dict[str, str]) -> None:
            node = XmlNode(
                tag=self._local_name(name),
                attributes={self._local_name(k): v for k, v in attrs.items()},
                line=parser.CurrentLineNumber,
                parent=stack[-1] if stack else None,
            )
            if node.parent is not None:
                node.parent.children.append(node)
            self.nodes.append(node)
            if node.name:
                self.by_name.setdefault(node.name, []).append(node)
                if node.tag == "DataModel":
                    self.models[node.name] = node
            stack.append(node)

        def end(_name: str) -> None:
            stack.pop()

        parser.StartElementHandler = start
        parser.EndElementHandler = end
        with path.open("rb") as stream:
            parser.ParseFile(stream)

    def locations_for_name(
        self, name: str | None, tag: str | None = None
    ) -> list[XmlLocation]:
        if not name:
            return []
        candidates = self.by_name.get(name, [])
        if tag:
            matching = [n for n in candidates if n.tag == tag]
            if matching:
                candidates = matching
        return [self._location(n) for n in candidates[:6]]

    def locations_for_runtime_path(
        self, runtime_path: str, tag: str | None = None
    ) -> list[XmlLocation]:
        parts = [part for part in runtime_path.split(".") if part]
        for name in reversed(parts):
            candidates = self.by_name.get(name, [])
            if tag:
                tagged = [node for node in candidates if node.tag == tag]
                if tagged:
                    candidates = tagged
            if not candidates:
                continue

            # Runtime paths use Choice block names while the XML definition is
            # usually in a referenced DataModel. Prefer candidates reachable
            # from any named ref-bearing ancestor in that runtime path.
            scored: list[tuple[int, XmlNode]] = []
            for candidate in candidates:
                score = 0
                for part in parts:
                    for anchor in self.by_name.get(part, []):
                        ref = anchor.attributes.get("ref")
                        if ref and candidate.model in self._reachable_models(ref):
                            score += 1
                scored.append((score, candidate))
            best = max((score for score, _node in scored), default=0)
            selected = [node for score, node in scored if score == best]
            return [self._location(node) for node in selected[:6]]
        return []

    def _reachable_models(self, model_name: str) -> set[str]:
        pending = [model_name]
        reached: set[str] = set()
        while pending:
            current = pending.pop()
            if current in reached:
                continue
            reached.add(current)
            model = self.models.get(current)
            if model is None:
                continue
            for node in self._descendants(model):
                ref = node.attributes.get("ref")
                if ref and ref not in reached:
                    pending.append(ref)
        return reached

    @staticmethod
    def _descendants(node: XmlNode) -> Iterable[XmlNode]:
        for child in node.children:
            yield child
            yield from XmlIndex._descendants(child)

    def static_diagnostics(self) -> list[Diagnostic]:
        diagnostics: list[Diagnostic] = []
        for node in self.nodes:
            if node.tag not in {"Blob", "String", "Optional", "Block"}:
                continue
            if node.parent is None:
                continue
            if not self._can_consume_remainder(node):
                continue
            siblings = node.parent.children
            position = siblings.index(node)
            following = siblings[position + 1 :]
            if not following:
                continue
            diagnostics.append(
                Diagnostic(
                    code="unbounded_element_before_sibling",
                    severity="warning",
                    confidence=0.88,
                    message=(
                        f"Unbounded {node.tag} '{node.name}' is followed by other "
                        "elements and may consume their bytes."
                    ),
                    evidence=[
                        "Following siblings: "
                        + ", ".join(
                            f"{child.tag} '{child.name or '<anonymous>'}'"
                            for child in following[:4]
                        )
                    ],
                    xml_locations=[self._location(node)],
                )
            )
        return diagnostics

    @staticmethod
    def _is_unbounded(node: XmlNode) -> bool:
        bounded_attributes = {"length", "lengthType"}
        if bounded_attributes.intersection(node.attributes):
            return False
        return not any(
            child.tag == "Relation"
            and child.attributes.get("type") in {"size", "count"}
            for child in node.children
        )

    @classmethod
    def _can_consume_remainder(cls, node: XmlNode) -> bool:
        if node.tag in {"Blob", "String"}:
            return cls._is_unbounded(node)
        # Optional/Block wrappers around one trailing unbounded field inherit
        # that ambiguity at the wrapper's position among its siblings.
        return bool(node.children) and cls._can_consume_remainder(node.children[-1])

    @staticmethod
    def _location(node: XmlNode) -> XmlLocation:
        return XmlLocation(
            line=node.line,
            tag=node.tag,
            name=node.name,
            model=node.model,
            attributes=dict(node.attributes),
        )


class LogAnalysis:
    def __init__(self, path: Path, xml_index: XmlIndex | None):
        self.path = path
        self.xml_index = xml_index
        self.lines = path.read_text(errors="replace").splitlines()
        self.events = self._parse_events()
        self.original = self._hex_after("Original Bytes:")
        self.parsed = self._hex_after("Parsed   Bytes:")
        self.raw = self._hex_after("Bytes:")
        self.seed = self._seed_name()

    def diagnose(self) -> list[Diagnostic]:
        diagnostics: list[Diagnostic] = []
        diagnostics.extend(self._reference_failures())
        diagnostics.extend(self._binding_failures())
        diagnostics.extend(self._unsized_failures())
        diagnostics.extend(self._buffer_failures())
        diagnostics.extend(self._choice_failures())
        diagnostics.extend(self._roundtrip_failure())
        diagnostics.extend(self._terminal_summary())
        return _deduplicate(diagnostics)

    def _parse_events(self) -> list[ElementEvent]:
        events: list[ElementEvent] = []
        for index, line in enumerate(self.lines):
            match = TREE_RE.search(line)
            if not match:
                continue
            event = ElementEvent(
                line=index + 1,
                depth=match.group("prefix").count("|"),
                tag=match.group("tag"),
                name=match.group("name"),
                offset=int(match.group("offset")),
                total=int(match.group("total")),
            )
            for following in self.lines[index + 1 : index + 5]:
                if TREE_RE.search(following):
                    break
                size_match = SIZE_RE.search(following)
                if size_match:
                    event.size_bytes = int(size_match.group("bytes"))
                value_match = VALUE_RE.search(following)
                if value_match:
                    event.value = int(value_match.group("value"))
            events.append(event)
        return events

    def _seed_name(self) -> str:
        for line in reversed(self.lines):
            match = PARSE_FAILURE_RE.search(line) or ROUNDTRIP_RE.search(line)
            if match:
                return match.group("seed")
        return self.path.name.removesuffix(".log")

    def _hex_after(self, heading: str) -> bytes | None:
        # Use the final occurrence: cracker tracing can contain unrelated text.
        positions = [i for i, line in enumerate(self.lines) if line.strip() == heading]
        if not positions:
            return None
        tokens: list[str] = []
        for line in self.lines[positions[-1] + 1 :]:
            stripped = line.strip()
            if not HEX_LINE_RE.fullmatch(stripped):
                break
            tokens.extend(stripped.split())
        return bytes(int(token, 16) for token in tokens) if tokens else None

    def _reference_failures(self) -> list[Diagnostic]:
        grouped: dict[tuple[str, str], list[int]] = {}
        paths: dict[tuple[str, str], str] = {}
        for index, line in enumerate(self.lines):
            reference = REFERENCE_RE.search(line)
            if not reference:
                continue
            optional_path = OPTIONAL_PATH_RE.search(line)
            runtime_path = optional_path.group("path") if optional_path else ""
            key = (runtime_path, reference.group("reference"))
            grouped.setdefault(key, []).append(index + 1)
            paths[key] = runtime_path

        diagnostics: list[Diagnostic] = []
        for (runtime_path, reference), line_numbers in grouped.items():
            diagnostics.append(
                Diagnostic(
                    code="unresolved_runtime_reference",
                    severity="error",
                    confidence=1.0,
                    message=(
                        f"Element '{runtime_path or '<unknown>'}' cannot resolve "
                        f"reference '{reference}'."
                    ),
                    evidence=[f"Repeated {len(line_numbers)} time(s) in this log."],
                    log_lines=line_numbers[:8],
                    xml_locations=self._locations_for_path(runtime_path, "Optional"),
                )
            )
        return diagnostics

    def _binding_failures(self) -> list[Diagnostic]:
        grouped: dict[tuple[str, str], list[int]] = {}
        for index, line in enumerate(self.lines):
            match = BINDING_RE.search(line)
            if match:
                key = (match.group("path"), match.group("binding"))
                grouped.setdefault(key, []).append(index + 1)

        diagnostics: list[Diagnostic] = []
        for (runtime_path, binding), line_numbers in grouped.items():
            diagnostics.append(
                Diagnostic(
                    code="unresolved_relation_binding",
                    severity="error",
                    confidence=1.0,
                    message=(
                        f"Element '{runtime_path}' cannot resolve relation binding "
                        f"'{binding}'."
                    ),
                    evidence=[
                        "A failed size/count binding commonly leaves the target "
                        "element unsized.",
                        f"Repeated {len(line_numbers)} time(s) in this log.",
                    ],
                    log_lines=line_numbers[:8],
                    xml_locations=self._locations_for_path(runtime_path),
                )
            )
        return diagnostics

    def _unsized_failures(self) -> list[Diagnostic]:
        grouped: dict[tuple[str, str], list[int]] = {}
        events: dict[tuple[str, str], ElementEvent] = {}
        for index, line in enumerate(self.lines):
            if not UNSIZED_RE.search(line):
                continue
            event = self._nearest_event(index + 1)
            if event is None:
                continue
            key = (event.tag, event.name)
            grouped.setdefault(key, []).append(index + 1)
            events[key] = event

        diagnostics: list[Diagnostic] = []
        for key, line_numbers in grouped.items():
            event = events[key]
            diagnostics.append(
                Diagnostic(
                    code="unsized_element",
                    severity="error",
                    confidence=0.99,
                    message=(
                        f"{event.tag} '{event.name}' has no resolvable size while "
                        "it is followed by other data."
                    ),
                    evidence=[
                        "This is often downstream of a failed size Relation or an "
                        "unbounded non-final field."
                    ],
                    log_lines=line_numbers[:8],
                    xml_locations=self._locations_for_name(event.name, event.tag),
                )
            )
        return diagnostics

    def _buffer_failures(self) -> list[Diagnostic]:
        occurrences: list[tuple[int, int, int, ElementEvent | None]] = []
        for index, line in enumerate(self.lines):
            match = BUFFER_RE.search(line)
            if not match:
                continue
            event = self._nearest_event(index + 1)
            occurrences.append(
                (
                    index + 1,
                    int(match.group("wanted")),
                    int(match.group("remaining")),
                    event,
                )
            )

        diagnostics: list[Diagnostic] = []
        seen: set[tuple[int, int, str | None]] = set()
        for line_number, wanted, remaining, event in occurrences:
            key = (wanted, remaining, event.name if event else None)
            if key in seen:
                continue
            seen.add(key)

            length_event = self._nearest_length_field(line_number)
            endian = self._endianness_evidence(length_event, wanted, remaining)
            if endian:
                diagnostics.append(
                    Diagnostic(
                        code="probable_endianness_mismatch",
                        severity="error",
                        confidence=0.99,
                        message=(
                            f"Length field '{length_event.name}' is likely decoded "
                            "with the wrong byte order."
                        ),
                        evidence=endian,
                        log_lines=[length_event.line, line_number],
                        xml_locations=self._locations_for_name(length_event.name),
                    )
                )
                continue

            element_name = event.name if event else "<unknown>"
            diagnostics.append(
                Diagnostic(
                    code="unexpected_end_of_input",
                    severity="error",
                    confidence=0.96,
                    message=(
                        f"Element '{element_name}' requires {wanted} bits but only "
                        f"{remaining} bits remain."
                    ),
                    evidence=[
                        f"Furthest reported offset: {event.offset} byte(s)."
                        if event
                        else "No element position was available."
                    ],
                    log_lines=[line_number],
                    xml_locations=self._locations_for_name(
                        event.name if event else None, event.tag if event else None
                    ),
                )
            )

        eof_groups: dict[tuple[int, int, str], int] = {}
        for _line, _wanted, remaining, event in occurrences:
            if event and remaining == 0:
                key = (event.offset, event.total, event.name)
                eof_groups[key] = eof_groups.get(key, 0) + 1
        for (offset, total, name), count in eof_groups.items():
            if count < 2:
                continue
            diagnostics.append(
                Diagnostic(
                    code="all_choice_branches_hit_eof",
                    severity="error",
                    confidence=0.94,
                    message=(
                        f"{count} alternative parses all require '{name}' at EOF; "
                        "the model may lack an empty alternative or allow too few items."
                    ),
                    evidence=[f"EOF is at byte {offset} of {total}."],
                    xml_locations=self._locations_for_name(name),
                )
            )
        return diagnostics

    def _choice_failures(self) -> list[Diagnostic]:
        diagnostics: list[Diagnostic] = []
        for pos, choice in enumerate(self.events):
            if choice.tag != "Choice":
                continue
            end_line = len(self.lines) + 1
            for later in self.events[pos + 1 :]:
                if later.line > choice.line and later.depth <= choice.depth:
                    end_line = later.line
                    break
            branches = [
                event
                for event in self.events[pos + 1 :]
                if choice.line < event.line < end_line
                and event.depth == choice.depth + 1
                and event.tag == "DataModel"
            ]
            for branch_index, branch in enumerate(branches):
                branch_end = (
                    branches[branch_index + 1].line
                    if branch_index + 1 < len(branches)
                    else end_line
                )
                section = self.lines[branch.line - 1 : branch_end - 1]
                token_event = next(
                    (
                        event
                        for event in self.events
                        if branch.line < event.line < branch_end
                        and event.name in {"type", "packet_type", "submessage_id"}
                    ),
                    None,
                )
                if not token_event:
                    continue
                # At EOF Peach logs the token element before it manages to read
                # a value. Absence of a mismatch does not mean it matched.
                if token_event.value is None:
                    continue
                token_window = self.lines[
                    token_event.line - 1 : min(token_event.line + 5, branch_end - 1)
                ]
                if any(TOKEN_MISMATCH_RE.search(line) for line in token_window):
                    continue
                buffer_failure = next(
                    (
                        (branch.line + i, BUFFER_RE.search(line))
                        for i, line in enumerate(section)
                        if BUFFER_RE.search(line)
                    ),
                    None,
                )
                if not buffer_failure:
                    continue
                failure_line, _match = buffer_failure
                deepest = self._nearest_event(failure_line)
                diagnostics.append(
                    Diagnostic(
                        code="matched_choice_branch_failed",
                        severity="error",
                        confidence=0.92,
                        message=(
                            f"Choice branch '{branch.name}' matched its token but "
                            f"failed later near '{deepest.name if deepest else '<unknown>'}'."
                        ),
                        evidence=[
                            f"Token element '{token_event.name}' matched at byte "
                            f"{token_event.offset}; the branch then reached byte "
                            f"{deepest.offset if deepest else '?'}.",
                            "Later token mismatches from sibling branches are fallback noise.",
                        ],
                        log_lines=[branch.line, failure_line],
                        xml_locations=self._locations_for_name(branch.name),
                    )
                )

            # A compact compound-message failure has the Choice itself followed by X.
            local = self.lines[choice.line - 1 : min(choice.line + 7, len(self.lines))]
            if any("No valid children were found" in line for line in local):
                diagnostics.append(
                    Diagnostic(
                        code="choice_has_no_valid_branch",
                        severity="error",
                        confidence=0.97,
                        message=(
                            f"Choice '{choice.name}' has no valid branch at byte "
                            f"{choice.offset} of {choice.total}."
                        ),
                        evidence=[
                            "This often indicates a wrong repetition boundary, missing "
                            "variant, or an unexpected discriminator."
                        ],
                        log_lines=[choice.line],
                        xml_locations=self._locations_for_name(choice.name, "Choice"),
                    )
                )
        return diagnostics

    def _roundtrip_failure(self) -> list[Diagnostic]:
        if self.original is None or self.parsed is None:
            return []
        offset = _first_difference(self.original, self.parsed)
        original_byte = (
            f"0x{self.original[offset]:02X}" if offset < len(self.original) else "<EOF>"
        )
        parsed_byte = (
            f"0x{self.parsed[offset]:02X}" if offset < len(self.parsed) else "<EOF>"
        )
        covering = [
            event
            for event in self.events
            if event.total == len(self.original)
            and event.size_bytes is not None
            and event.offset <= offset < event.offset + event.size_bytes
        ]
        candidate = covering[-1] if covering else None
        evidence = [
            f"First difference at byte {offset}: original={original_byte}, parsed={parsed_byte}.",
            f"Lengths: original={len(self.original)}, parsed={len(self.parsed)}.",
        ]
        if len(self.parsed) < len(self.original):
            evidence.append(
                f"Serialization dropped {len(self.original) - len(self.parsed)} byte(s)."
            )
        return [
            Diagnostic(
                code="roundtrip_bytes_mismatch",
                severity="error",
                confidence=1.0,
                message=(
                    "Parsed DataModel does not reproduce the original bytes"
                    + (
                        f"; the first changed field is near '{candidate.name}'."
                        if candidate
                        else "."
                    )
                ),
                evidence=evidence,
                xml_locations=self._locations_for_name(
                    candidate.name if candidate else None,
                    candidate.tag if candidate else None,
                ),
            )
        ]

    def _terminal_summary(self) -> list[Diagnostic]:
        if not any(PARSE_FAILURE_RE.search(line) for line in self.lines):
            return []
        furthest = max(self.events, key=lambda event: event.offset, default=None)
        return [
            Diagnostic(
                code="parse_failed",
                severity="summary",
                confidence=1.0,
                message=f"Seed '{self.seed}' could not be parsed.",
                evidence=[
                    f"Furthest logged offset: {furthest.offset} of {furthest.total} bytes."
                    if furthest
                    else "The log contains no element offsets."
                ],
                xml_locations=self._locations_for_name(
                    furthest.name if furthest else None,
                    furthest.tag if furthest else None,
                ),
            )
        ]

    def _nearest_event(self, line_number: int) -> ElementEvent | None:
        candidates = [event for event in self.events if event.line <= line_number]
        return candidates[-1] if candidates else None

    def _nearest_length_field(self, line_number: int) -> ElementEvent | None:
        candidates = [
            event
            for event in self.events
            if event.line < line_number
            and event.tag == "Number"
            and "length" in event.name.lower()
            and event.value is not None
            and event.size_bytes in {2, 4, 8}
        ]
        return candidates[-1] if candidates else None

    def _endianness_evidence(
        self,
        length_event: ElementEvent | None,
        wanted_bits: int,
        remaining_bits: int,
    ) -> list[str] | None:
        raw = self.raw or self.original
        if not length_event or raw is None or length_event.size_bytes is None:
            return None
        start = length_event.offset
        end = start + length_event.size_bytes
        if end > len(raw):
            return None
        encoded = raw[start:end]
        little = int.from_bytes(encoded, "little")
        big = int.from_bytes(encoded, "big")
        parsed_value = wanted_bits // 8
        alternate = big if parsed_value == little else little
        remaining = remaining_bits // 8
        if parsed_value == alternate or alternate != remaining:
            return None
        return [
            f"Raw bytes at {start}:{end}: {encoded.hex(' ')}.",
            f"Parsed length={parsed_value}; opposite byte order={alternate}.",
            f"Opposite byte order exactly matches the {remaining} bytes remaining.",
        ]

    def _locations_for_name(
        self, name: str | None, tag: str | None = None
    ) -> list[XmlLocation]:
        return self.xml_index.locations_for_name(name, tag) if self.xml_index else []

    def _locations_for_path(
        self, path: str, tag: str | None = None
    ) -> list[XmlLocation]:
        return (
            self.xml_index.locations_for_runtime_path(path, tag)
            if self.xml_index
            else []
        )


def _first_difference(a: bytes, b: bytes) -> int:
    for index, (left, right) in enumerate(zip(a, b)):
        if left != right:
            return index
    return min(len(a), len(b))


def _deduplicate(diagnostics: Iterable[Diagnostic]) -> list[Diagnostic]:
    result: list[Diagnostic] = []
    seen: set[tuple[str, str]] = set()
    for diagnostic in diagnostics:
        key = (diagnostic.code, diagnostic.message)
        if key in seen:
            continue
        seen.add(key)
        result.append(diagnostic)
    return result


def collect_logs(paths: Sequence[Path]) -> list[Path]:
    logs: set[Path] = set()
    for path in paths:
        if path.is_dir():
            logs.update(candidate for candidate in path.rglob("*.log") if candidate.is_file())
        elif path.is_file():
            logs.add(path)
        else:
            raise FileNotFoundError(path)
    return sorted(logs)


def analyze(
    log_paths: Sequence[Path], datamodel: Path | None, include_static: bool = True
) -> dict[str, object]:
    xml_index = XmlIndex(datamodel) if datamodel else None
    logs = collect_logs(log_paths)
    reports = []
    for log_path in logs:
        analysis = LogAnalysis(log_path, xml_index)
        reports.append(
            {
                "log": str(log_path),
                "seed": analysis.seed,
                "diagnostics": [asdict(item) for item in analysis.diagnose()],
            }
        )
    static = (
        [asdict(item) for item in xml_index.static_diagnostics()]
        if xml_index and include_static
        else []
    )
    return {
        "diagnosis_mode": "heuristic",
        "datamodel": str(datamodel) if datamodel else None,
        "logs_analyzed": len(logs),
        "cross_log_summary": _cross_log_summary(reports),
        "reports": reports,
        "static_diagnostics": static,
    }


def prepare_llm_report(
    log_paths: Sequence[Path], datamodel: Path | None
) -> dict[str, object]:
    """Collect inputs for LLM-only diagnosis without running heuristics."""
    logs = collect_logs(log_paths)
    return {
        "diagnosis_mode": "llm",
        "datamodel": str(datamodel) if datamodel else None,
        "logs_analyzed": len(logs),
        "log_files": [str(path) for path in logs],
        "cross_log_summary": [],
        "reports": [],
        "static_diagnostics": [],
    }


def _cross_log_summary(reports: list[dict[str, object]]) -> list[dict[str, object]]:
    groups: dict[tuple[object, ...], dict[str, object]] = {}
    for report in reports:
        seed = str(report["seed"])
        for diagnostic in report["diagnostics"]:  # type: ignore[index]
            if diagnostic["severity"] == "summary":
                continue
            locations = diagnostic["xml_locations"]
            location_key = tuple(
                (location["line"], location["tag"], location["name"])
                for location in locations
            )
            key = (
                diagnostic["code"],
                location_key,
                # Without an XML location, keep distinct observations apart.
                "" if location_key else diagnostic["message"],
            )
            group = groups.setdefault(
                key,
                {
                    "code": diagnostic["code"],
                    "severity": diagnostic["severity"],
                    "confidence": diagnostic["confidence"],
                    "xml_locations": locations,
                    "seeds": [],
                },
            )
            group["confidence"] = max(
                float(group["confidence"]), float(diagnostic["confidence"])
            )
            if seed not in group["seeds"]:
                group["seeds"].append(seed)
    repeated = [group for group in groups.values() if len(group["seeds"]) >= 2]
    repeated.sort(
        key=lambda group: (
            -len(group["seeds"]),
            -float(group["confidence"]),
            str(group["code"]),
        )
    )
    return repeated


LLM_SYSTEM_PROMPT = """You are an expert in binary protocol parsing and Peach Pit
DataModels. Diagnose failures directly from raw Peach validator logs and the Pit
XML. No heuristic diagnosis is supplied: perform the complete root-cause analysis
yourself. Separate root causes from cascading symptoms and normal Choice fallback
noise. Never invent RFC facts: no RFC is available unless explicitly included. A
seed filename is only a clue, not proof. Every conclusion must cite the supplied
seed, raw log evidence, or XML line. Return only one valid JSON object matching
the requested schema."""


def add_llm_judgment(
    report: dict[str, object],
    datamodel: Path | None,
    *,
    model_name: str | None = None,
    temperature: float = 0.0,
    language: str = "zh-CN",
    max_input_chars: int = 50000,
    llm: Any | None = None,
) -> dict[str, object]:
    """Ask an LLM to diagnose directly from raw logs and the complete Pit context.

    ``llm`` is injectable so this layer can be tested without making a network
    request. Heuristic findings in a caller-provided report are never sent to the
    model or used to shape its judgment.
    """

    raw_log_files = [str(path) for path in _report_log_paths(report)]
    report["diagnosis_mode"] = "llm"
    report["log_files"] = raw_log_files
    report["cross_log_summary"] = []
    report["reports"] = []
    report["static_diagnostics"] = []

    if llm is None:
        try:
            import dotenv

            dotenv.load_dotenv(".env")
        except ImportError as error:
            raise RuntimeError(
                "LLM dependencies are unavailable; install requirements.txt"
            ) from error

    resolved_model = model_name or _default_llm_model()
    prompt = _build_llm_prompt(report, datamodel, language, max_input_chars)

    if llm is None:
        try:
            from langchain_openai import ChatOpenAI
        except ImportError as error:
            raise RuntimeError(
                "LLM dependencies are unavailable; install requirements.txt"
            ) from error

        if not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY is required when --llm is used")
        llm = ChatOpenAI(
            model=resolved_model,
            temperature=temperature,
            max_retries=2,
        ).bind(response_format={"type": "json_object"})

    response = llm.invoke(
        [
            ("system", LLM_SYSTEM_PROMPT),
            ("human", prompt),
        ]
    )
    content = _message_text(response)
    analysis = _parse_llm_json(content)
    _validate_llm_analysis(analysis)
    report["llm_judgment"] = {
        "status": "ok",
        "model": resolved_model,
        "usage": _response_usage(response),
        "analysis": analysis,
    }
    return report


def _default_llm_model() -> str:
    return (
        os.environ.get("LLM_DIAGNOSER_MODEL")
        or os.environ.get("LLM_PEACH_MODEL")
        or os.environ.get("LLM_MODEL")
        or "gpt-5.2"
    )


def _build_llm_prompt(
    report: dict[str, object],
    datamodel: Path | None,
    language: str,
    max_input_chars: int,
) -> str:
    if max_input_chars < 4000:
        raise ValueError("max_input_chars must be at least 4000")

    xml_budget = min(20000, max_input_chars // 2)
    xml_context = _numbered_file_context(datamodel, xml_budget)
    log_budget = max(max_input_chars - len(xml_context) - 3500, 1000)
    log_context = _raw_log_context(report, log_budget)
    schema = {
        "summary": "short overall conclusion",
        "root_causes": [
            {
                "id": "RC1",
                "title": "concise title",
                "classification": (
                    "root_cause | contributing_factor | symptom | uncertain"
                ),
                "category": "reference | endianness | layout | choice | cardinality | boundary | other",
                "confidence": 0.0,
                "affected_seeds": ["seed.raw"],
                "xml_locations": [
                    {"line": 1, "element": "DataModel/field name"}
                ],
                "reasoning": "why this is causal rather than a downstream symptom",
                "evidence": ["specific supplied observation"],
                "suggested_fix": "candidate change, or null when evidence is insufficient",
                "verification": "focused test that would confirm or reject it",
            }
        ],
        "causal_relationships": [
            {
                "cause_id": "RC1",
                "downstream_observations": ["raw log symptom"],
                "explanation": "causal link",
            }
        ],
        "priority_order": ["RC1"],
        "uncertainties": ["what cannot be determined from these logs alone"],
    }
    requested_language = "Chinese" if language == "zh-CN" else "English"
    return f"""Diagnose this DataModel failure directly from the supplied raw inputs.

Requirements:
1. Write all explanatory strings in {requested_language}.
2. Perform the diagnosis independently; no heuristic findings are available.
3. Merge repeated raw-log symptoms into shared candidate root causes.
4. A changed size/length field can be a downstream symptom of omitted body data.
5. Treat rejected sibling Choice token mismatches as noise.
6. Use log line ordering: an error in a later fallback branch can be only a
   symptom of an earlier preferred branch failure. In particular, do not rank
   an endian mismatch in a fallback branch above an earlier binding/unsized error.
7. If logs cannot prove a semantic claim, mark it uncertain and propose a test.
8. Suggested fixes must name supplied XML lines/elements. Do not output a full XML file.
9. Return only JSON with this shape:
{json.dumps(schema, ensure_ascii=False, indent=2)}

RAW VALIDATOR LOGS:
{log_context or '<no validator logs supplied>'}

DATAMODEL XML:
{xml_context or '<datamodel not supplied>'}
"""


def _report_log_paths(report: dict[str, object]) -> list[Path]:
    paths = report.get("log_files")
    if isinstance(paths, list):
        return [Path(str(path)) for path in paths]

    result: list[Path] = []
    reports = report.get("reports")
    if isinstance(reports, list):
        for item in reports:
            if isinstance(item, dict) and item.get("log"):
                result.append(Path(str(item["log"])))
    return result


def _raw_log_context(report: dict[str, object], max_chars: int) -> str:
    rendered: list[str] = []
    used = 0
    for path in _report_log_paths(report):
        header = f"\n===== {path} =====\n"
        if used + len(header) >= max_chars:
            break
        rendered.append(header)
        used += len(header)
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError as error:
            text = f"<unable to read log: {error}>\n"
            rendered.append(text)
            used += len(text)
            continue
        for line_number, line in enumerate(lines, 1):
            text = f"{line_number:5d}: {line}\n"
            if used + len(text) > max_chars:
                rendered.append("... <raw logs truncated for context limit>\n")
                return "".join(rendered)
            rendered.append(text)
            used += len(text)
    return "".join(rendered)


def _numbered_file_context(path: Path | None, max_chars: int) -> str:
    if path is None:
        return ""
    rendered: list[str] = []
    used = 0
    for line_number, line in enumerate(
        path.read_text(errors="replace").splitlines(), 1
    ):
        text = f"{line_number:5d}: {line}\n"
        if used + len(text) > max_chars:
            rendered.append("... <datamodel truncated for context limit>\n")
            break
        rendered.append(text)
        used += len(text)
    return "".join(rendered)


def _compact_llm_report(
    report: dict[str, object], max_chars: int
) -> dict[str, object]:
    compact: dict[str, object] = {
        "logs_analyzed": report["logs_analyzed"],
        "cross_log_hotspots": report["cross_log_summary"],
        "static_diagnostics": report["static_diagnostics"],
        "seeds": [],
    }
    seeds: list[dict[str, object]] = []
    omitted = 0
    for item in report["reports"]:  # type: ignore[index]
        diagnostics = []
        for diagnostic in item["diagnostics"]:
            if diagnostic["severity"] == "summary":
                continue
            diagnostics.append(
                {
                    "code": diagnostic["code"],
                    "confidence": diagnostic["confidence"],
                    "message": diagnostic["message"],
                    "evidence": diagnostic["evidence"],
                    "log_lines": diagnostic["log_lines"],
                    "xml_locations": [
                        {
                            "line": location["line"],
                            "tag": location["tag"],
                            "name": location["name"],
                            "model": location["model"],
                        }
                        for location in diagnostic["xml_locations"]
                    ],
                }
            )
        candidate = {"seed": item["seed"], "diagnostics": diagnostics}
        tentative = dict(compact)
        tentative["seeds"] = seeds + [candidate]
        if len(json.dumps(tentative, ensure_ascii=False)) > max_chars:
            omitted += 1
            continue
        seeds.append(candidate)
    compact["seeds"] = seeds
    compact["seeds_omitted_for_context_limit"] = omitted
    return compact


def _selected_xml_context(
    datamodel: Path | None,
    report: dict[str, object],
    max_chars: int,
    radius: int = 5,
) -> str:
    if datamodel is None:
        return ""
    source_lines = datamodel.read_text(errors="replace").splitlines()
    selected: set[int] = set()

    def add_locations(diagnostics: Iterable[dict[str, object]]) -> None:
        for diagnostic in diagnostics:
            for location in diagnostic.get("xml_locations", []):  # type: ignore[union-attr]
                line = int(location["line"])
                selected.update(
                    range(max(1, line - radius), min(len(source_lines), line + radius) + 1)
                )

    for item in report["reports"]:  # type: ignore[index]
        add_locations(item["diagnostics"])
    add_locations(report["static_diagnostics"])  # type: ignore[arg-type]

    rendered: list[str] = []
    previous = 0
    for line_number in sorted(selected):
        if previous and line_number > previous + 1:
            rendered.append("...")
        rendered.append(f"{line_number:5d}: {source_lines[line_number - 1]}")
        previous = line_number
        if sum(len(line) + 1 for line in rendered) >= max_chars:
            rendered.append("... <XML context truncated>")
            break
    return "\n".join(rendered)


def _message_text(response: Any) -> str:
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts)
    return str(content)


def _parse_llm_json(content: str) -> dict[str, object]:
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"LLM returned invalid JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise RuntimeError("LLM response must be a JSON object")
    return parsed


def _validate_llm_analysis(analysis: dict[str, object]) -> None:
    required = {"summary", "root_causes", "priority_order", "uncertainties"}
    missing = required.difference(analysis)
    if missing:
        raise RuntimeError(
            "LLM JSON is missing required keys: " + ", ".join(sorted(missing))
        )
    if not isinstance(analysis["summary"], str):
        raise RuntimeError("LLM JSON field 'summary' must be a string")
    if not isinstance(analysis["root_causes"], list):
        raise RuntimeError("LLM JSON field 'root_causes' must be a list")
    for index, cause in enumerate(analysis["root_causes"]):
        if not isinstance(cause, dict):
            raise RuntimeError(f"root_causes[{index}] must be an object")
        for key in ("id", "title", "classification", "confidence", "reasoning"):
            if key not in cause:
                raise RuntimeError(f"root_causes[{index}] is missing '{key}'")
        try:
            confidence = float(cause["confidence"])
        except (TypeError, ValueError) as error:
            raise RuntimeError(
                f"root_causes[{index}].confidence must be numeric"
            ) from error
        if not 0.0 <= confidence <= 1.0:
            raise RuntimeError(
                f"root_causes[{index}].confidence must be between 0 and 1"
            )


def _response_usage(response: Any) -> dict[str, int]:
    usage = getattr(response, "usage_metadata", None)
    if not isinstance(usage, dict):
        return {}
    result: dict[str, int] = {}
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        value = usage.get(key)
        if isinstance(value, int):
            result[key] = value
    return result


def render_text(report: dict[str, object]) -> str:
    output = [
        f"DataModel diagnosis: {report['logs_analyzed']} log(s)",
        f"DataModel: {report['datamodel'] or '<not provided>'}",
    ]
    llm_only = report.get("diagnosis_mode") == "llm"
    summary = report["cross_log_summary"]
    if summary and not llm_only:
        output.append("\n== Cross-log hotspots ==")
        for item in summary:
            confidence = int(round(item["confidence"] * 100))
            output.append(
                f"  [{item['severity'].upper()}] {item['code']} ({confidence}%) "
                f"affects {len(item['seeds'])} seeds"
            )
            output.append("    - Seeds: " + ", ".join(item["seeds"]))
            for location in item["xml_locations"]:
                output.append(
                    f"    - XML line {location['line']}: {location['tag']} "
                    f"'{location['name'] or '<anonymous>'}'"
                )
    llm_judgment = report.get("llm_judgment")
    if isinstance(llm_judgment, dict):
        output.extend(_render_llm_judgment(llm_judgment))
    if llm_only:
        return "\n".join(output) + "\n"
    for item in report["reports"]:  # type: ignore[index]
        diagnostics = item["diagnostics"]
        output.append(f"\n== {item['seed']} ==")
        if not diagnostics:
            output.append("  No recognized failure pattern.")
            continue
        for diagnostic in diagnostics:
            confidence = int(round(diagnostic["confidence"] * 100))
            output.append(
                f"  [{diagnostic['severity'].upper()}] {diagnostic['code']} "
                f"({confidence}%): {diagnostic['message']}"
            )
            for evidence in diagnostic["evidence"]:
                output.append(f"    - {evidence}")
            if diagnostic["log_lines"]:
                output.append(
                    "    - Log lines: "
                    + ", ".join(str(line) for line in diagnostic["log_lines"])
                )
            for location in diagnostic["xml_locations"]:
                output.append(
                    f"    - XML line {location['line']}: {location['tag']} "
                    f"'{location['name'] or '<anonymous>'}'"
                    + (f" in {location['model']}" if location["model"] else "")
                )

    static = report["static_diagnostics"]
    if static:
        output.append("\n== Static DataModel warnings ==")
        for diagnostic in static:
            confidence = int(round(diagnostic["confidence"] * 100))
            output.append(
                f"  [{diagnostic['severity'].upper()}] {diagnostic['code']} "
                f"({confidence}%): {diagnostic['message']}"
            )
            for evidence in diagnostic["evidence"]:
                output.append(f"    - {evidence}")
            for location in diagnostic["xml_locations"]:
                output.append(
                    f"    - XML line {location['line']}: {location['tag']} "
                    f"'{location['name'] or '<anonymous>'}'"
                )
    return "\n".join(output) + "\n"


def _render_llm_judgment(judgment: dict[str, object]) -> list[str]:
    output = ["\n== LLM root-cause judgment =="]
    if judgment.get("status") != "ok":
        output.append(f"  [ERROR] {judgment.get('error', 'LLM judgment failed')}")
        return output

    output.append(f"  Model: {judgment.get('model', '<unknown>')}")
    usage = judgment.get("usage")
    if isinstance(usage, dict) and usage:
        output.append(
            "  Usage: "
            + ", ".join(f"{key}={value}" for key, value in usage.items())
        )
    analysis = judgment.get("analysis")
    if not isinstance(analysis, dict):
        output.append("  [ERROR] Missing structured analysis")
        return output
    output.append(f"  Summary: {analysis.get('summary', '')}")
    priority = analysis.get("priority_order")
    if isinstance(priority, list) and priority:
        output.append("  Priority: " + " -> ".join(str(item) for item in priority))
    for cause in analysis.get("root_causes", []) or []:
        if not isinstance(cause, dict):
            continue
        confidence = int(round(float(cause.get("confidence", 0)) * 100))
        output.append(
            f"\n  [{cause.get('id', '?')}] {cause.get('title', '<untitled>')} "
            f"({cause.get('classification', 'uncertain')}, {confidence}%)"
        )
        output.append(f"    - Reasoning: {cause.get('reasoning', '')}")
        seeds = cause.get("affected_seeds")
        if isinstance(seeds, list) and seeds:
            output.append("    - Seeds: " + ", ".join(str(seed) for seed in seeds))
        for location in cause.get("xml_locations", []) or []:
            if isinstance(location, dict):
                output.append(
                    f"    - XML line {location.get('line', '?')}: "
                    f"{location.get('element', '<unknown>')}"
                )
        suggested_fix = cause.get("suggested_fix")
        if suggested_fix:
            output.append(f"    - Candidate fix: {suggested_fix}")
        verification = cause.get("verification")
        if verification:
            output.append(f"    - Verify: {verification}")
    uncertainties = analysis.get("uncertainties")
    if isinstance(uncertainties, list) and uncertainties:
        output.append("\n  Uncertainties:")
        output.extend(f"    - {item}" for item in uncertainties)
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Diagnose Peach DataModel failures from cracker log files."
    )
    parser.add_argument("logs", nargs="+", type=Path, help="Log file(s) or directories")
    parser.add_argument(
        "--datamodel", type=Path, help="Peach Pit XML used to map findings to source lines"
    )
    parser.add_argument(
        "--format", choices=("text", "json"), default="text", dest="output_format"
    )
    parser.add_argument("--output", type=Path, help="Write the report to this file")
    parser.add_argument(
        "--no-static", action="store_true", help="Skip static DataModel warnings"
    )
    parser.add_argument(
        "--llm",
        action="store_true",
        help="Diagnose directly with an LLM instead of heuristic analysis",
    )
    parser.add_argument(
        "--llm-model",
        help=(
            "Model for --llm (default: LLM_DIAGNOSER_MODEL, LLM_PEACH_MODEL, "
            "LLM_MODEL, then gpt-5.2)"
        ),
    )
    parser.add_argument(
        "--llm-temperature", type=float, default=0.0, help="Temperature for --llm"
    )
    parser.add_argument(
        "--llm-language",
        choices=("zh-CN", "en"),
        default="zh-CN",
        help="Language for LLM explanations (default: zh-CN)",
    )
    parser.add_argument(
        "--llm-max-input-chars",
        type=int,
        default=50000,
        help="Maximum diagnostic/XML characters sent to the LLM",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    llm_failed = False
    try:
        report = (
            prepare_llm_report(args.logs, args.datamodel)
            if args.llm
            else analyze(args.logs, args.datamodel, not args.no_static)
        )
    except (FileNotFoundError, OSError, xml.parsers.expat.ExpatError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if args.llm:
        try:
            add_llm_judgment(
                report,
                args.datamodel,
                model_name=args.llm_model,
                temperature=args.llm_temperature,
                language=args.llm_language,
                max_input_chars=args.llm_max_input_chars,
            )
        except Exception as error:
            llm_failed = True
            report["llm_judgment"] = {
                "status": "error",
                "model": args.llm_model or _default_llm_model(),
                "error": str(error),
            }
    rendered = (
        json.dumps(report, indent=2, ensure_ascii=False) + "\n"
        if args.output_format == "json"
        else render_text(report)
    )
    if args.output:
        args.output.write_text(rendered)
    else:
        sys.stdout.write(rendered)
    return 3 if llm_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
