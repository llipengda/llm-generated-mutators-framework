"""Helpers for assembling independently generated Peach DataModel fragments."""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


PEACH_NS = "http://peachfuzzer.com/2012/Peach"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"


def normalize_identifier(value: str) -> str:
    """Convert an RFC display name to the identifier style used by the Pit."""
    normalized = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    return normalized or "packet"


def default_manifest(
    protocol: str, packet_types: list[str], group_size: int = 4
) -> dict[str, Any]:
    """Build a deterministic fallback when the planning agent output is unusable."""
    size = max(1, group_size)
    groups = []
    for offset in range(0, len(packet_types), size):
        members = packet_types[offset : offset + size]
        groups.append(
            {
                "id": "_".join(normalize_identifier(item) for item in members),
                "packet_types": members,
                "shared_refs": [],
                "rfc_queries": [],
            }
        )
    return {
        "protocol": protocol,
        "shared_models": [],
        "packet_groups": groups,
    }


def validate_manifest(
    manifest: object,
    protocol: str,
    packet_types: list[str],
    max_group_size: int | None = None,
) -> dict[str, Any]:
    """Validate the planner contract and return a normalized manifest."""
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be a JSON object")
    raw_groups = manifest.get("packet_groups")
    if not isinstance(raw_groups, list) or not raw_groups:
        raise ValueError("manifest.packet_groups must be a non-empty list")

    raw_shared_models = manifest.get("shared_models")
    if not isinstance(raw_shared_models, list):
        raise ValueError("manifest.shared_models must be a list")
    shared_models: list[dict[str, Any]] = []
    shared_names: set[str] = set()
    for index, model in enumerate(raw_shared_models):
        if not isinstance(model, dict) or not str(model.get("name") or "").strip():
            raise ValueError(f"shared_models[{index}] must have a name")
        name = str(model["name"]).strip()
        if name in shared_names:
            raise ValueError(f"duplicate shared model name: {name}")
        shared_names.add(name)
        shared_models.append(model)

    expected = {item.casefold(): item for item in packet_types}
    seen: set[str] = set()
    groups: list[dict[str, Any]] = []
    ids: set[str] = set()
    for index, raw_group in enumerate(raw_groups):
        if not isinstance(raw_group, dict):
            raise ValueError(f"packet_groups[{index}] must be an object")
        raw_members = raw_group.get("packet_types")
        if not isinstance(raw_members, list) or not raw_members:
            raise ValueError(f"packet_groups[{index}].packet_types must not be empty")
        if max_group_size is not None and len(raw_members) > max(1, max_group_size):
            raise ValueError(
                f"packet_groups[{index}] exceeds maximum size {max_group_size}"
            )

        members: list[str] = []
        for member in raw_members:
            key = str(member).strip().casefold()
            if key not in expected:
                raise ValueError(f"unknown packet type in manifest: {member}")
            if key in seen:
                raise ValueError(f"packet type appears in multiple groups: {member}")
            seen.add(key)
            members.append(expected[key])

        group_id = normalize_identifier(str(raw_group.get("id") or "_".join(members)))
        if group_id in ids:
            raise ValueError(f"duplicate packet group id: {group_id}")
        ids.add(group_id)
        shared_refs = _string_list(raw_group.get("shared_refs"))
        unknown_shared_refs = set(shared_refs).difference(shared_names)
        if unknown_shared_refs:
            raise ValueError(
                f"packet_groups[{index}] uses undeclared shared refs: "
                + ", ".join(sorted(unknown_shared_refs))
            )
        groups.append(
            {
                "id": group_id,
                "packet_types": members,
                "shared_refs": shared_refs,
                "rfc_queries": _string_list(raw_group.get("rfc_queries")),
            }
        )

    missing = set(expected).difference(seen)
    if missing:
        names = ", ".join(expected[key] for key in sorted(missing))
        raise ValueError(f"packet types missing from manifest: {names}")

    return {
        "protocol": protocol,
        "shared_models": shared_models,
        "packet_groups": groups,
    }


def load_manifest(
    path: Path, protocol: str, packet_types: list[str], group_size: int = 4
) -> tuple[dict[str, Any], str | None]:
    """Load a planner manifest, falling back to deterministic packet chunks."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return validate_manifest(data, protocol, packet_types, group_size), None
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return default_manifest(protocol, packet_types, group_size), str(exc)


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp_path, path)


def assemble_datamodel(
    *,
    protocol: str,
    packet_types: list[str],
    shared_fragment: Path,
    packet_fragments: list[Path],
    output_path: Path,
    expected_shared_models: list[str] | None = None,
) -> None:
    """Merge generated fragments and add the union/array models deterministically."""
    ET.register_namespace("", PEACH_NS)
    ET.register_namespace("xsi", XSI_NS)
    root = ET.Element(
        _q("Peach"),
        {f"{{{XSI_NS}}}schemaLocation": f"{PEACH_NS} /peach/peach.xsd"},
    )

    shared_root = _parse_peach_fragment(shared_fragment)
    _reject_unexpected_children(shared_root, {"Defaults", "DataModel"}, shared_fragment)
    defaults = _children(shared_root, "Defaults")
    if len(defaults) != 1:
        raise ValueError("shared fragment must contain exactly one Defaults element")
    root.append(defaults[0])

    shared_definitions = _children(shared_root, "DataModel")
    shared_names = _definition_names(shared_definitions, shared_fragment)
    missing_shared = set(expected_shared_models or []).difference(shared_names)
    if missing_shared:
        raise ValueError(
            "shared fragment is missing contracted DataModels: "
            + ", ".join(sorted(missing_shared))
        )
    shared_unresolved = _referenced_models(shared_definitions).difference(shared_names)
    if shared_unresolved:
        raise ValueError(
            "shared fragment has unresolved DataModel refs: "
            + ", ".join(sorted(shared_unresolved))
        )

    definitions = list(shared_definitions)
    definition_sources = [(definition, shared_fragment) for definition in shared_definitions]
    for path in packet_fragments:
        fragment_root = _parse_peach_fragment(path)
        _reject_unexpected_children(fragment_root, {"DataModel"}, path)
        fragment_definitions = _children(fragment_root, "DataModel")
        local_names = _definition_names(fragment_definitions, path)
        unresolved = _referenced_models(fragment_definitions).difference(
            shared_names | local_names
        )
        if unresolved:
            raise ValueError(
                f"packet fragment {path} has unresolved DataModel refs: "
                + ", ".join(sorted(unresolved))
            )
        definitions.extend(fragment_definitions)
        definition_sources.extend((definition, path) for definition in fragment_definitions)

    reserved = {f"{protocol}_packet_t", f"{protocol}_packet_array"}
    sources_by_name: dict[str, list[Path]] = {}
    for definition, source in definition_sources:
        name = definition.get("name")
        if not name:
            raise ValueError("every generated DataModel must have a name")
        if name in reserved:
            raise ValueError(f"fragment must not define reserved model {name}")
        sources_by_name.setdefault(name, []).append(source)

    duplicates = {
        name: sources for name, sources in sources_by_name.items() if len(sources) > 1
    }
    if duplicates:
        details = []
        for name, sources in sorted(duplicates.items()):
            locations = ", ".join(str(path) for path in sources)
            details.append(f"{name} ({locations})")
        raise ValueError("duplicate DataModel definitions: " + "; ".join(details))

    names = set(sources_by_name)
    definitions = _sort_definitions_by_reference(definitions)
    for definition in definitions:
        root.append(definition)

    expected_models = {
        f"{protocol}_{normalize_identifier(packet_type)}_packet_t"
        for packet_type in packet_types
    }
    missing_models = expected_models.difference(names)
    if missing_models:
        raise ValueError(
            "missing packet DataModel definitions: " + ", ".join(sorted(missing_models))
        )

    unresolved = sorted(_referenced_models(definitions).difference(names))
    if unresolved:
        raise ValueError("unresolved DataModel refs: " + ", ".join(unresolved))

    packet_model = ET.SubElement(root, _q("DataModel"), {"name": f"{protocol}_packet_t"})
    choice = ET.SubElement(packet_model, _q("Choice"), {"name": "packet_union"})
    for packet_type in packet_types:
        normalized = normalize_identifier(packet_type)
        ET.SubElement(
            choice,
            _q("Block"),
            {"name": normalized, "ref": f"{protocol}_{normalized}_packet_t"},
        )

    packet_array = ET.SubElement(
        root, _q("DataModel"), {"name": f"{protocol}_packet_array"}
    )
    packets = ET.SubElement(
        packet_array,
        _q("Block"),
        {"name": "packets", "minOccurs": "1", "maxOccurs": "100"},
    )
    ET.SubElement(packets, _q("Block"), {"ref": f"{protocol}_packet_t"})

    ET.indent(root, space="  ")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    ET.ElementTree(root).write(tmp_path, encoding="utf-8", xml_declaration=True)
    os.replace(tmp_path, output_path)


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _q(local_name: str) -> str:
    return f"{{{PEACH_NS}}}{local_name}"


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(root: ET.Element, local_name: str) -> list[ET.Element]:
    return [child for child in root if _local_name(child.tag) == local_name]


def _parse_peach_fragment(path: Path) -> ET.Element:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise ValueError(f"invalid XML fragment {path}: {exc}") from exc
    if _local_name(root.tag) != "Peach":
        raise ValueError(f"fragment root must be Peach: {path}")
    for element in root.iter():
        if not str(element.tag).startswith("{"):
            element.tag = _q(str(element.tag))
    return root


def _referenced_models(definitions: list[ET.Element]) -> set[str]:
    refs: set[str] = set()
    for definition in definitions:
        for element in definition.iter():
            ref = element.get("ref")
            if ref and "." not in ref and ":" not in ref:
                refs.add(ref)
    return refs


def _sort_definitions_by_reference(
    definitions: list[ET.Element],
) -> list[ET.Element]:
    """Stably order DataModels so every local ref points backward."""
    by_name = {definition.get("name"): definition for definition in definitions}
    original_order = {
        definition.get("name"): index for index, definition in enumerate(definitions)
    }
    dependencies = {
        name: _referenced_models([definition]).intersection(by_name)
        for name, definition in by_name.items()
    }
    ordered: list[ET.Element] = []
    emitted: set[str] = set()
    while len(ordered) < len(definitions):
        ready = [
            name
            for name in by_name
            if name not in emitted and dependencies[name].issubset(emitted)
        ]
        if not ready:
            remaining = sorted(set(by_name).difference(emitted))
            details = "; ".join(
                f"{name} -> {', '.join(sorted(dependencies[name].difference(emitted)))}"
                for name in remaining
            )
            raise ValueError(
                "cyclic or forward-only DataModel reference dependencies: " + details
            )
        ready.sort(key=original_order.__getitem__)
        for name in ready:
            ordered.append(by_name[name])
            emitted.add(name)
    return ordered


def _definition_names(definitions: list[ET.Element], path: Path) -> set[str]:
    names: set[str] = set()
    for definition in definitions:
        name = definition.get("name")
        if not name:
            raise ValueError(f"every DataModel in {path} must have a name")
        if name in names:
            raise ValueError(f"duplicate DataModel definition in {path}: {name}")
        names.add(name)
    return names


def _reject_unexpected_children(
    root: ET.Element, allowed: set[str], path: Path
) -> None:
    unexpected = sorted(
        {_local_name(child.tag) for child in root if _local_name(child.tag) not in allowed}
    )
    if unexpected:
        raise ValueError(
            f"fragment {path} contains unexpected top-level elements: "
            + ", ".join(unexpected)
        )


def assemble_from_manifest(
    protocol: str,
    *,
    fragment_dir: Path | None = None,
    output_path: Path | None = None,
) -> Path:
    """Load a generated manifest and assemble its shared/family fragments."""
    normalized_protocol = protocol.strip().lower()
    if not normalized_protocol:
        raise ValueError("protocol must not be empty")
    fragments = fragment_dir or (
        Path("llm/peach") / normalized_protocol / "datamodel_fragments"
    )
    manifest_path = fragments / "schema_manifest.json"
    try:
        raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read schema manifest {manifest_path}: {exc}") from exc

    raw_groups = raw_manifest.get("packet_groups") if isinstance(raw_manifest, dict) else None
    if not isinstance(raw_groups, list):
        raise ValueError("manifest.packet_groups must be a list")
    packet_types = [
        str(packet)
        for group in raw_groups
        if isinstance(group, dict) and isinstance(group.get("packet_types"), list)
        for packet in group["packet_types"]
    ]
    manifest = validate_manifest(raw_manifest, normalized_protocol, packet_types)
    packet_fragments = [
        fragments / f"packet_{group['id']}.xml"
        for group in manifest["packet_groups"]
    ]
    destination = output_path or (
        Path("llm/peach") / normalized_protocol / "datamodel.xml"
    )
    assemble_datamodel(
        protocol=normalized_protocol,
        packet_types=packet_types,
        shared_fragment=fragments / "shared.xml",
        packet_fragments=packet_fragments,
        output_path=destination,
        expected_shared_models=[
            str(model["name"]) for model in manifest["shared_models"]
        ],
    )
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Assemble Peach DataModel fragments and report validation errors."
    )
    parser.add_argument("protocol", help="protocol directory name under llm/peach")
    parser.add_argument(
        "--fragment-dir",
        type=Path,
        help="override the default llm/peach/<protocol>/datamodel_fragments path",
    )
    parser.add_argument("--output", type=Path, help="override datamodel.xml output path")
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate and assemble in a temporary directory without replacing datamodel.xml",
    )
    args = parser.parse_args(argv)

    try:
        if args.check:
            with tempfile.TemporaryDirectory() as directory:
                assemble_from_manifest(
                    args.protocol,
                    fragment_dir=args.fragment_dir,
                    output_path=Path(directory) / "datamodel.xml",
                )
            print("[PASS] DataModel fragments can be assembled.")
        else:
            destination = assemble_from_manifest(
                args.protocol,
                fragment_dir=args.fragment_dir,
                output_path=args.output,
            )
            print(f"[PASS] DataModel assembled at {destination}")
        return 0
    except (OSError, ValueError) as exc:
        print(f"[FAIL] {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
