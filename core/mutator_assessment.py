"""Review packet-local semantic value before creating managed mutator sources."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import tempfile
from typing import cast

from core.prompts import render_prompt
from core.mutator_codegen import MutatorPlan, PacketMutatorPlan


SEMANTIC_VALUE_CRITERION = render_prompt("mutators/semantic_value_criterion.txt")
_ROOT = Path(__file__).resolve().parents[1]
RANDOM_BASELINE_DOCUMENT = _ROOT / "docs" / "random-mutation-baseline.md"


@dataclass(frozen=True)
class FieldAssessment:
    path: str
    generate: bool
    reason: str


@dataclass(frozen=True)
class PacketAssessment:
    packet_type: str
    fields: tuple[FieldAssessment, ...]


def assessment_path(packet: PacketMutatorPlan) -> Path:
    return packet.generated_file.with_name(
        packet.generated_file.name.removesuffix(".g.cs") + ".assessment.json"
    )


def assessment_prompt(plan: MutatorPlan, packet: PacketMutatorPlan) -> str:
    fields: dict[str, list[str]] = {}
    for spec in packet.mutators:
        fields.setdefault(spec.path, []).append(spec.operation)
    prefix = ""
    if fields:
        first = next(iter(fields))
        parent, separator, tail = first.partition(".packet_union.")
        alternative, dot, _ = tail.partition(".")
        if separator and dot:
            prefix = parent + separator + alternative + "."
    candidates = "\n".join(
        f"{index}|{path.removeprefix(prefix) if prefix else path}|{','.join(operations)}"
        for index, (path, operations) in enumerate(fields.items())
    )
    return render_prompt(
        "mutators/assessment.txt",
        plan_protocol=plan.protocol,
        packet_packet_type=packet.packet_type,
        plan_dsl_dir=plan.dsl_dir,
        packet_path_prefix=prefix or '(paths below are already rooted)',
        candidates=candidates,
    )


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    record = cast(dict[object, object], value)
    if any(not isinstance(key, str) for key in record):
        raise ValueError(f"{label} must be a JSON object with string keys")
    return cast(dict[str, object], record)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")
    return value.strip()


def parse_assessment(text: str, packet: PacketMutatorPlan) -> PacketAssessment:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    data = _object(json.loads(stripped), "assessment")
    raw_fields = data.get("decisions")
    if not isinstance(raw_fields, list):
        raise ValueError("assessment decisions must be a JSON array")
    paths = tuple(dict.fromkeys(spec.path for spec in packet.mutators))
    decisions: dict[int, FieldAssessment] = {}
    for raw_field in cast(list[object], raw_fields):
        if not isinstance(raw_field, list):
            raise ValueError("each decision must be [candidate id, boolean, brief reason]")
        row = cast(list[object], raw_field)
        if len(row) != 3:
            raise ValueError("each decision must be [candidate id, boolean, brief reason]")
        index, generate, reason = row
        if type(index) is not int or index < 0 or index >= len(paths) or index in decisions:
            raise ValueError(f"unexpected or duplicate candidate id: {index}")
        if not isinstance(generate, bool):
            raise ValueError(f"generate must be a JSON boolean for candidate {index}")
        decisions[index] = FieldAssessment(paths[index], generate, _text(reason, "reason"))
    missing = set(range(len(paths))) - decisions.keys()
    if missing:
        raise ValueError("assessment omitted candidate ids: " + ", ".join(map(str, sorted(missing))))
    return PacketAssessment(packet.packet_type, tuple(decisions[index] for index in range(len(paths))))


def filter_packet(packet: PacketMutatorPlan, assessment: PacketAssessment) -> PacketMutatorPlan:
    retained = {field.path for field in assessment.fields if field.generate}
    specs = tuple(spec for spec in packet.mutators if spec.path in retained)
    if specs == packet.mutators:
        return packet
    return PacketMutatorPlan(packet.packet_type, packet.generated_file, packet.semantic_file,
                             packet.output_dll, specs)


def save_assessment(plan: MutatorPlan, packet: PacketMutatorPlan, assessment: PacketAssessment) -> Path:
    destination = assessment_path(packet)
    data = {
        "version": 1, "protocol": plan.protocol, "packet_type": assessment.packet_type,
        "criterion": SEMANTIC_VALUE_CRITERION,
        "fields": [{
            "path": field.path, "generate": field.generate, "reason": field.reason,
            "operations": [spec.operation for spec in packet.mutators if spec.path == field.path],
        } for field in assessment.fields],
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent,
                                     prefix=destination.name + ".", delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    try:
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def assessment_notes(packet: PacketMutatorPlan) -> str:
    """Pass only retained-field reasons to the implementation agent."""
    path = assessment_path(packet)
    if not path.is_file():
        return ""
    data = _object(json.loads(path.read_text(encoding="utf-8")), "saved assessment")
    retained = {spec.path for spec in packet.mutators}
    fields = data.get("fields")
    if not isinstance(fields, list):
        raise ValueError("saved assessment fields must be a JSON array")
    notes: list[str] = []
    for raw_field in cast(list[object], fields):
        field = _object(raw_field, "saved field assessment")
        target = _text(field.get("path"), "path")
        if field.get("generate") is True and target in retained:
            notes.append(f"{target}: {_text(field.get('reason'), 'reason')}")
    return "\n".join(notes)
