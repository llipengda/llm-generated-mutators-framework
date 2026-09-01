"""Manifest, aggregation, and compilation helpers for generated Peach DSL."""

from __future__ import annotations

import argparse
import keyword
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parent


def normalize_identifier(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", value.strip()).strip("_").lower()
    if not normalized:
        raise ValueError(f"cannot normalize empty identifier from {value!r}")
    if normalized[0].isdigit():
        normalized = "packet_" + normalized
    return normalized


def normalize_symbol(value: str) -> str:
    parts = re.findall(r"[A-Za-z0-9]+", value)
    symbol = "".join(part[:1].upper() + part[1:] for part in parts) or "Packet"
    if symbol[0].isdigit():
        symbol = "Packet" + symbol
    return symbol


def shared_model_name(protocol: str, symbol: str) -> str:
    """Derive a stable runtime name from a DSL symbol."""
    snake_symbol = re.sub(r"(?<!^)(?=[A-Z])", "_", symbol)
    normalized = normalize_identifier(snake_symbol)
    prefix = protocol + "_"
    if normalized.startswith(prefix):
        normalized = normalized[len(prefix) :]
    return f"{protocol}_{normalized}_t"


def default_manifest(protocol: str, packet_types: list[str], group_size: int = 4) -> dict[str, Any]:
    groups = []
    for offset in range(0, len(packet_types), max(1, group_size)):
        members = packet_types[offset : offset + max(1, group_size)]
        groups.append(
            {
                "id": normalize_identifier("_".join(members)),
                "description": (
                    "Packet group containing " + ", ".join(members) + "."
                ),
                "packet_types": members,
                "shared_refs": [],
                "rfc_queries": [],
                "packet_models": [
                    {
                        "packet_type": packet,
                        "symbol": normalize_symbol(packet) + "Packet",
                        "model_name": f"{protocol}_{normalize_identifier(packet)}_packet_t",
                    }
                    for packet in members
                ],
            }
        )
    return {"protocol": protocol, "shared_models": [], "packet_groups": groups}


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _python_symbol(value: object, context: str) -> str:
    symbol = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", symbol):
        raise ValueError(f"{context} must be a public Python identifier")
    if keyword.iskeyword(symbol):
        raise ValueError(f"{context} must not be a Python keyword")
    return symbol


def _shared_references(value: object, context: str) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ValueError(f"{context} must be a list")
    references: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(
                f"{context}[{index}] must contain symbol and usage"
            )
        symbol = _python_symbol(item.get("symbol"), f"{context}[{index}].symbol")
        usage = str(item.get("usage", "")).strip()
        if not usage:
            raise ValueError(f"{context}[{index}].usage must not be empty")
        if symbol in seen:
            raise ValueError(f"duplicate shared ref in {context}: {symbol}")
        seen.add(symbol)
        references.append({"symbol": symbol, "usage": usage})
    return references


def packet_choice_name(packet_type: str) -> str:
    """Derive a safe Union member name without exposing it in the plan."""
    choice = normalize_identifier(packet_type)
    if keyword.iskeyword(choice):
        choice += "_packet"
    return choice


def validate_manifest(
    raw: object,
    protocol: str,
    packet_types: list[str],
    max_group_size: int | None = None,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("schema manifest must be an object")
    if normalize_identifier(protocol) != protocol:
        raise ValueError("protocol must already be an ASCII lower_snake_case identifier")
    if str(raw.get("protocol", "")).casefold() != protocol.casefold():
        raise ValueError("schema manifest protocol does not match the pipeline protocol")

    shared_models = []
    shared_names: set[str] = set()
    shared_symbols: set[str] = set()
    for index, item in enumerate(raw.get("shared_models", [])):
        if not isinstance(item, dict):
            raise ValueError(f"shared_models[{index}] must be an object")
        symbol = _python_symbol(item.get("symbol"), f"shared_models[{index}].symbol")
        name = shared_model_name(protocol, symbol)
        if name in shared_names:
            raise ValueError(f"duplicate derived shared model name: {name!r}")
        if symbol in shared_symbols:
            raise ValueError(f"duplicate shared model symbol: {symbol}")
        shared_names.add(name)
        shared_symbols.add(symbol)
        shared_models.append(
            {
                "name": name,
                "symbol": symbol,
                "purpose": str(item.get("purpose", "")).strip(),
                "fields": item.get("fields", []) if isinstance(item.get("fields"), list) else [],
            }
        )

    expected = {packet.casefold(): packet for packet in packet_types}
    seen: set[str] = set()
    group_ids: set[str] = set()
    model_names: set[str] = set(shared_names)
    symbols: set[str] = set(shared_symbols)
    groups = []
    raw_groups = raw.get("packet_groups")
    if not isinstance(raw_groups, list) or not raw_groups:
        raise ValueError("packet_groups must be a non-empty list")
    for index, item in enumerate(raw_groups):
        if not isinstance(item, dict):
            raise ValueError(f"packet_groups[{index}] must be an object")
        members = _strings(item.get("packet_types"))
        if not members:
            raise ValueError(f"packet_groups[{index}].packet_types must not be empty")
        if max_group_size is not None and len(members) > max(1, max_group_size):
            raise ValueError(f"packet_groups[{index}] exceeds maximum size {max_group_size}")
        canonical_members = []
        for packet in members:
            key = packet.casefold()
            if key not in expected:
                raise ValueError(f"unknown packet type in manifest: {packet}")
            if key in seen:
                raise ValueError(f"packet type appears in multiple groups: {packet}")
            seen.add(key)
            canonical_members.append(expected[key])

        group_id = normalize_identifier(str(item.get("id") or "_".join(members)))
        if group_id in group_ids:
            raise ValueError(f"duplicate packet group id: {group_id}")
        group_ids.add(group_id)
        description = str(item.get("description", "")).strip()
        if not description:
            raise ValueError(f"packet_groups[{index}].description must not be empty")
        shared_refs = _shared_references(
            item.get("shared_refs"),
            f"packet_groups[{index}].shared_refs",
        )
        protocol_symbol_prefix = normalize_symbol(protocol)
        unknown = {
            reference["symbol"]
            for reference in shared_refs
            if reference["symbol"] not in shared_symbols
            and not reference["symbol"].startswith(protocol_symbol_prefix)
        }
        if unknown:
            raise ValueError(
                "shared refs must name a shared DSL symbol or a protocol-prefixed "
                "custom type: " + ", ".join(sorted(unknown))
            )

        raw_models = item.get("packet_models")
        if not isinstance(raw_models, list) or len(raw_models) != len(canonical_members):
            raise ValueError(f"packet_groups[{index}].packet_models must cover every packet type")
        by_packet = {
            str(model.get("packet_type", "")).casefold(): model
            for model in raw_models
            if isinstance(model, dict)
        }
        packet_models = []
        for packet in canonical_members:
            model = by_packet.get(packet.casefold())
            if model is None:
                raise ValueError(f"packet model contract missing for {packet}")
            if "choice_name" in model:
                raise ValueError(
                    f"packet model {packet} must not specify choice_name"
                )
            symbol = _python_symbol(model.get("symbol"), f"packet model {packet}.symbol")
            expected_name = f"{protocol}_{normalize_identifier(packet)}_packet_t"
            model_name = expected_name
            if symbol in symbols or model_name in model_names:
                raise ValueError(f"duplicate packet model contract for {packet}")
            symbols.add(symbol)
            model_names.add(model_name)
            packet_models.append(
                {
                    "packet_type": packet,
                    "symbol": symbol,
                    "model_name": model_name,
                }
            )
        groups.append(
            {
                "id": group_id,
                "description": description,
                "packet_types": canonical_members,
                "shared_refs": shared_refs,
                "rfc_queries": _strings(item.get("rfc_queries")),
                "packet_models": packet_models,
            }
        )

    missing = set(expected).difference(seen)
    if missing:
        raise ValueError("packet types missing from manifest: " + ", ".join(expected[key] for key in sorted(missing)))
    return {"protocol": protocol, "shared_models": shared_models, "packet_groups": groups}


def load_manifest(path: Path, protocol: str, packet_types: list[str], group_size: int = 4) -> tuple[dict[str, Any], str | None]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return validate_manifest(raw, protocol, packet_types, group_size), None
    except (OSError, json.JSONDecodeError, ValueError) as error:
        return default_manifest(protocol, packet_types, group_size), str(error)


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def render_root_module(protocol: str, manifest: dict[str, Any], group_ids: Iterable[str] | None = None) -> str:
    selected = set(group_ids) if group_ids is not None else None
    groups = [group for group in manifest["packet_groups"] if selected is None or group["id"] in selected]
    models = [model for group in groups for model in group["packet_models"]]
    if not models:
        raise ValueError("cannot build a DSL root without packet models")
    lines = ["from peach_dsl import *", "from shared_model import *"]
    for group in groups:
        imported = ", ".join(model["symbol"] for model in group["packet_models"])
        lines.append(f"from family_{group['id']} import {imported}")
    root_prefix = normalize_symbol(protocol)
    lines.extend(
        [
            "",
            "@PacketUnion",
            f"class {root_prefix}Packet(Schema):",
            "    packet_union = Union(",
        ]
    )
    used_choices: set[str] = set()
    for model in models:
        choice = packet_choice_name(model["packet_type"])
        if choice in used_choices:
            suffix = 2
            while f"{choice}_{suffix}" in used_choices:
                suffix += 1
            choice = f"{choice}_{suffix}"
        used_choices.add(choice)
        lines.append(f"        {choice}={model['symbol']},")
    # The playground DSL intentionally requires a Union to contain at least two
    # alternatives. Keep that public rule unchanged for single-packet protocols;
    # the compiler removes this private assembly-only duplicate after export.
    if len(models) == 1:
        lines.append(f"        compiler_single_packet_duplicate={models[0]['symbol']},")
    lines.extend(
        [
            "    )",
            "",
            '@Default(endian="big", signed=False)',
            f"class {root_prefix}PacketArray(Schema):",
            f"    packets = Array[{root_prefix}Packet, Occurs(1, 100)]()",
            "",
            f"ROOT = {root_prefix}PacketArray",
            "",
        ]
    )
    return "\n".join(lines)


def write_root_module(directory: Path, protocol: str, manifest: dict[str, Any], group_ids: Iterable[str] | None = None, filename: str = "root.py") -> Path:
    path = directory / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(render_root_module(protocol, manifest, group_ids), encoding="utf-8")
    os.replace(temporary, path)
    return path


def compile_dsl_subprocess(
    entry: Path, output: Path, timeout: int = 30
) -> subprocess.CompletedProcess[str]:
    runner = (
        "import sys; "
        "sys.path.insert(0, sys.argv.pop(1)); "
        "from peach_dsl.compiler import main; "
        "raise SystemExit(main(sys.argv[1:]))"
    )
    command = [sys.executable, "-I", "-c", runner, str(PROJECT_ROOT), "--entry", str(entry), "--output", str(output)]
    environment = {"PATH": os.environ.get("PATH", ""), "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(command, cwd=PROJECT_ROOT, env=environment, text=True, capture_output=True, timeout=timeout)


def assemble_from_manifest(protocol: str, *, dsl_dir: Path | None = None, output_path: Path | None = None) -> Path:
    protocol = protocol.strip().lower()
    directory = dsl_dir or Path("llm/peach") / protocol / "datamodel_dsl"
    manifest_path = directory / "schema_manifest.json"
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    packet_types = [str(packet) for group in raw.get("packet_groups", []) for packet in group.get("packet_types", [])]
    manifest = validate_manifest(raw, protocol, packet_types)
    entry = write_root_module(directory, protocol, manifest)
    output = output_path or Path("llm/peach") / protocol / "datamodel.xml"
    result = compile_dsl_subprocess(entry, output)
    if result.returncode != 0:
        raise ValueError((result.stdout + result.stderr).strip())
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Assemble generated Peach DSL modules")
    parser.add_argument("protocol")
    parser.add_argument("--dsl-dir", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.check:
            with tempfile.TemporaryDirectory() as directory:
                assemble_from_manifest(args.protocol, dsl_dir=args.dsl_dir, output_path=Path(directory) / "datamodel.xml")
            print("[PASS] DataModel DSL can be compiled.")
        else:
            destination = assemble_from_manifest(args.protocol, dsl_dir=args.dsl_dir, output_path=args.output)
            print(f"[PASS] DataModel compiled at {destination}")
        return 0
    except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as error:
        print(f"[FAIL] {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
