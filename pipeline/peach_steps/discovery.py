import json
from pathlib import Path

from agent import build_agent_graph
from datamodel_dsl import normalize_symbol
from pipeline.peach_steps.common import PeachStepMixin
from ui import (
    UI,
    ask_generate_custom_data_elements,
)


class ProtocolDiscoverySteps(PeachStepMixin):
    def step_1_packet_types_extraction(self):
        UI.title("Step 1: Packet Types Extraction")

        step1_prompt = f"""
        For {self.protocol_name} protocol, list ALL the packet types according to the RFC document.

        Use the "RFC_Search" tool to look up the relevant sections in the RFC.
        Return the list as a comma-separated string.
        ONLY output the types without any additional explanation or formatting. For example: "CONNECT, PUBLISH, SUBSCRIBE, UNSUBSCRIBE, AUTH, PUBACK, PUBREC, PUBREL, PUBCOMP, PINGREQ, DISCONNECT".

        When using the "RFC_Search" tool, **ASK questions instead of assuming knowledge**.
        For example:
        - "MQTT packet types"
        """

        response = self.call_agent(step1_prompt, "Step 1: Packet Types Extraction")

        packet_types_raw = response["messages"][-1].content
        packet_types = [t.strip() for t in packet_types_raw.split(",") if t.strip()]
        self.state["packet_types"] = packet_types
        self.save_state()
        UI.success(f"[bold]Parsed Types:[/bold] {packet_types}")

    def _data_type_paths(self) -> tuple[Path, Path, Path]:
        root = Path("llm") / "peach" / self.protocol_lower
        return (
            root / "data_type_analysis.json",
            root / "DataElements",
            root / "DataElements" / "out" / f"{self.protocol_upper}DataElements.dll",
        )

    def _compile_custom_data_elements(self, source_dir: Path, dll_path: Path) -> None:
        import subprocess

        sources = sorted(str(path) for path in source_dir.glob("*.cs") if path.is_file())
        sdk_dir = Path("peach") / "sdk"
        references = sorted(
            f"-r:{path}" for path in sdk_dir.glob("*.dll") if path.is_file()
        )
        if not sources:
            raise RuntimeError(f"no custom DataElement C# sources found in {source_dir}")
        if not references:
            raise RuntimeError("Peach SDK is unavailable; run './setup.sh peach' first")
        dll_path.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [
                "mcs",
                "-sdk:4.5",
                "-target:library",
                "-warnaserror",
                f"-out:{dll_path}",
                *references,
                *sources,
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0 or not dll_path.is_file():
            diagnostics = (result.stdout + result.stderr).strip()
            raise RuntimeError(
                "custom Peach DataElement compilation failed:\n" + diagnostics[-12000:]
            )

    def _load_data_type_analysis(self, report_path: Path) -> dict:
        with report_path.open("r", encoding="utf-8") as report_file:
            report = json.load(report_file)
        if not isinstance(report, dict) or report.get("protocol") != self.protocol_lower:
            raise ValueError("data type analysis has an invalid protocol")
        if report.get("packet_types") != (self.state.get("packet_types") or []):
            raise ValueError("data type analysis packet type scope is stale")
        types = report.get("unsupported_types")
        if not isinstance(types, list):
            raise ValueError("data type analysis must contain an unsupported_types list")
        required = {
            "wire_type",
            "used_by_fields",
            "encoding",
            "required_behavior",
            "rfc_evidence",
            "dsl_gap",
            "recommended_dsl",
            "confidence",
            "custom_type",
        }
        seen = set()
        deduplicated_types = []
        custom_by_symbol: dict[str, dict] = {}
        custom_by_element: dict[str, dict] = {}
        custom_prefix = normalize_symbol(self.protocol_lower)
        for index, item in enumerate(types):
            if not isinstance(item, dict) or not required.issubset(item):
                raise ValueError(f"data type analysis item {index + 1} has an invalid schema")
            if item["confidence"] not in {"high", "medium", "low"}:
                raise ValueError(f"data type analysis item {index + 1} has invalid confidence")
            name = item["wire_type"]
            if not isinstance(name, str) or not name.strip() or name.casefold() in seen:
                raise ValueError(f"data type analysis item {index + 1} has an invalid wire_type")
            seen.add(name.casefold())
            used_by_fields = item["used_by_fields"]
            if (
                not isinstance(used_by_fields, list)
                or not used_by_fields
                or any(not isinstance(field, str) or not field.strip() for field in used_by_fields)
            ):
                raise ValueError(
                    f"data type analysis item {index + 1} has invalid used_by_fields"
                )
            for field in required - {"confidence", "wire_type", "used_by_fields", "custom_type"}:
                if not isinstance(item[field], str) or not item[field].strip():
                    raise ValueError(
                        f"data type analysis item {index + 1} has an empty {field}"
                    )
            custom_type = item["custom_type"]
            if not isinstance(custom_type, dict) or not all(
                isinstance(custom_type.get(field), str)
                and custom_type[field].strip()
                for field in ("symbol", "element_name", "value_type")
            ):
                raise ValueError(
                    f"data type analysis item {index + 1} needs a custom_type contract"
                )
            if custom_type["value_type"] not in {"int", "float", "str", "bytes"}:
                raise ValueError(
                    f"data type analysis item {index + 1} has invalid custom value_type"
                )
            if not custom_type["symbol"].startswith(custom_prefix) or not custom_type[
                "element_name"
            ].startswith(custom_prefix):
                raise ValueError(
                    f"custom type names must start with protocol prefix {custom_prefix}"
                )
            previous = custom_by_symbol.get(
                custom_type["symbol"]
            ) or custom_by_element.get(custom_type["element_name"])
            if previous is not None:
                if previous != custom_type:
                    raise ValueError(
                        f"conflicting custom type contract for {custom_type['symbol']}"
                    )
                continue
            custom_by_symbol[custom_type["symbol"]] = custom_type
            custom_by_element[custom_type["element_name"]] = custom_type
            deduplicated_types.append(item)
        report["unsupported_types"] = deduplicated_types
        return report

    def _data_type_summary(self, report: dict) -> str:
        if not report["unsupported_types"]:
            return "No unsupported protocol field encodings were found."
        lines = []
        for item in report["unsupported_types"]:
            lines.append(
                f"- **{item['wire_type']}** ({item['confidence']} confidence): "
                f"{item['recommended_dsl']}"
            )
            lines.append(f"  - Fields: {', '.join(item['used_by_fields'])}")
            lines.append(f"  - Encoding: {item['encoding']}")
            lines.append(f"  - Evidence: {item['rfc_evidence']}; {item['dsl_gap']}")
        return "\n".join(lines)

    def _peach_runtime_element_names(self) -> set[str]:
        """Return runtime element names solely for custom plugin collision checks."""
        catalog_path = Path("peach") / "peach.txt"
        lines = catalog_path.read_text(encoding="utf-8").splitlines()
        in_elements = False
        names = set()
        for line in lines:
            if line.startswith("-----Data Element"):
                in_elements = True
                continue
            if in_elements and line.startswith("-----"):
                break
            if not in_elements:
                continue
            if line.startswith("  ") and not line.startswith("    "):
                names.add(line.strip().casefold())
        if not names:
            raise RuntimeError(f"could not extract Data Element catalog from {catalog_path}")
        return names

    def _reusable_custom_elements(
        self, source_dir: Path, dll_path: Path, unsupported: list[dict]
    ) -> list[dict] | None:
        """Return an existing custom runtime manifest when it matches this plan."""
        manifest_path = source_dir / "manifest.json"
        if not manifest_path.is_file():
            return None
        try:
            custom_elements = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(custom_elements, list) or not custom_elements:
            return None
        if any(
            not isinstance(item, dict)
            or not all(
                isinstance(item.get(key), str) and item[key].strip()
                for key in ("wire_type", "element_name", "class_name")
            )
            for item in custom_elements
        ):
            return None
        planned_names = {
            item["custom_type"]["element_name"] for item in unsupported
        }
        existing_names = {item["element_name"] for item in custom_elements}
        if existing_names != planned_names:
            return None
        planned_wire_types = {item["wire_type"].casefold() for item in unsupported}
        existing_wire_types = {
            item["wire_type"].casefold() for item in custom_elements
        }
        if existing_wire_types != planned_wire_types:
            return None
        if not dll_path.is_file() and not any(source_dir.glob("*.cs")):
            return None
        return custom_elements

    def _finalize_data_type_support(self, report: dict) -> None:
        """Validate a planning audit and prepare any required ExtendedType runtime."""
        report_path, source_dir, dll_path = self._data_type_paths()
        UI.result_markdown("DSL Basic Data Type Support", self._data_type_summary(report))
        with report_path.open("w", encoding="utf-8") as report_file:
            json.dump(report, report_file, ensure_ascii=False, indent=2, sort_keys=True)
            report_file.write("\n")

        unsupported = report["unsupported_types"]
        with report_path.open("w", encoding="utf-8") as report_file:
            json.dump(report, report_file, ensure_ascii=False, indent=2, sort_keys=True)
            report_file.write("\n")
        if (
            unsupported
            and report.get("generation_status")
            in {"approved_and_compiled", "reused_existing"}
            and dll_path.is_file()
        ):
            self.state["data_type_analysis"] = report
            self.save_state()
            UI.success(f"Reusing compiled custom Peach DOM elements: {dll_path}")
            return
        if not unsupported:
            report["generation_status"] = "not_required"
            self.state["data_type_analysis"] = report
            self.save_state()
            UI.success("No unsupported protocol field encodings require custom scalars.")
            return

        names = sorted({item["custom_type"]["symbol"] for item in unsupported})
        custom_contracts = [item["custom_type"] for item in unsupported]
        custom_prefix = normalize_symbol(self.protocol_lower)
        if not ask_generate_custom_data_elements(self.protocol_name, names):
            reusable = self._reusable_custom_elements(source_dir, dll_path, unsupported)
            reuse_error = ""
            if reusable is not None and not dll_path.is_file():
                try:
                    self._compile_custom_data_elements(source_dir, dll_path)
                except RuntimeError as error:
                    reuse_error = str(error)
            if reusable is not None and dll_path.is_file():
                report["generation_status"] = "reused_existing"
                report["custom_elements"] = reusable
                report["plugin_dll"] = str(dll_path)
            else:
                report["generation_status"] = "declined"
            with report_path.open("w", encoding="utf-8") as report_file:
                json.dump(report, report_file, ensure_ascii=False, indent=2, sort_keys=True)
                report_file.write("\n")
            self.state["data_type_analysis"] = report
            self.save_state()
            if reusable is not None and dll_path.is_file():
                UI.warn(
                    "Custom DSL scalar generation was skipped; reusing existing "
                    f"runtime implementations from {dll_path}: " + ", ".join(names)
                )
            else:
                UI.warn(
                    "Custom DSL scalar runtime generation was skipped for: "
                    + ", ".join(names)
                    + ". No matching existing runtime was found. The pipeline will "
                    "continue, but DSL compilation or runtime validation may fail."
                    + (f" Existing sources could not be compiled: {reuse_error}" if reuse_error else "")
                )
            return

        source_dir.mkdir(parents=True, exist_ok=True)
        element_manifest_path = source_dir / "manifest.json"
        previous_manifest_mtime = (
            element_manifest_path.stat().st_mtime_ns
            if element_manifest_path.is_file()
            else None
        )
        generation_prompt = f"""
        Generate runtime implementations for the DSL ExtendedType declarations
        required by the confirmed unsupported types in "{report_path}": {names}.
        Exact custom type contracts:
        {json.dumps(custom_contracts, ensure_ascii=False, indent=2)}

        Every generated custom type name must start with the protocol prefix
        "{custom_prefix}". This applies to the DSL ExtendedType element_name,
        the [DataElement]/[PitParsable] name, and the C# class name. For example,
        use "{custom_prefix}VarInt", never the unprefixed "VarInt".

        Before writing code, read
        "./examples/ExampleEscapedUInt.cs" completely. It is the only
        custom DataElement source-code reference you may use. It is a verified,
        demonstration-only encoding: reuse its Peach plugin API patterns, strict
        error handling, Pit parsing, and serialization structure, but do NOT copy
        its invented wire encoding into a real protocol type. Do not read or rely
        on source files from the sibling llm-peach project.

        Use C# 5.0 and the namespace Peach.LLM.Generated.Dom.{self.protocol_upper}.
        Implement complete, strict, symmetric cracking and serialization for every
        valid encoding in the report, including malformed/truncated input handling,
        bounds, Sanitize, framework clone compatibility, PitParser, WritePit,
        common attributes/children/value,
        and Relation behavior when applicable. Use unique [DataElement] and
        [PitParsable] names. Do not use placeholders, TODOs, seed-specific logic,
        silent clamping, or a Blob fallback.

        Put the classes in "{source_dir}". Put `[assembly: PluginAssembly]` exactly
        once in "{source_dir / 'AssemblyInfo.cs'}" (and never in each class).
        Write "{source_dir / 'manifest.json'}" as a JSON array with exactly one
        object per generated type: {{"wire_type": "...", "element_name":
        "exact type name to pass to DSL ExtendedType", "class_name":
        "fully qualified C# class"}}.
        Use the "Build_DotNet_DLL" tool to compile all generated sources in
        "{source_dir}" to "{dll_path}". If it reports errors or warnings, read the
        diagnostics, fix the sources, and invoke the tool again. Continue until it
        reports success. Avoid ambiguous Peach/System type names: fully qualify
        framework types such as System.Text.Encoding when both namespaces expose
        the same short name. Write no files outside "{source_dir}".
        """
        generation_agent = build_agent_graph(
            retriever=self.retriever,
            config=self.agent_config,
            tool_names={
                "Read_File",
                "Search_Class",
                "Build_DotNet_DLL",
                "Write_File",
            },
        )
        self.call_agent(
            generation_prompt,
            "Step 2.1: Custom DSL Scalar Runtime Generation",
            agent_graph=generation_agent,
        )
        sources = [path for path in source_dir.glob("*.cs") if path.is_file()]
        manifest_path = element_manifest_path
        if not sources or not manifest_path.is_file():
            raise RuntimeError(
                f"custom DataElement generation did not produce sources and manifest in {source_dir}"
            )
        if previous_manifest_mtime == manifest_path.stat().st_mtime_ns:
            raise RuntimeError(
                "custom DataElement generation left the manifest artifact unchanged"
            )
        with manifest_path.open("r", encoding="utf-8") as manifest_file:
            custom_elements = json.load(manifest_file)
        if (
            not isinstance(custom_elements, list)
            or len(custom_elements) != len(unsupported)
            or any(
                not isinstance(item, dict)
                or not all(isinstance(item.get(key), str) and item[key].strip()
                           for key in ("wire_type", "element_name", "class_name"))
                for item in custom_elements
            )
            or len({item["element_name"] for item in custom_elements}) != len(custom_elements)
        ):
            raise RuntimeError(f"invalid custom DataElement manifest: {manifest_path}")
        if {item["wire_type"].casefold() for item in custom_elements} != {
            item["wire_type"].casefold() for item in unsupported
        }:
            raise RuntimeError(f"custom DataElement manifest does not match analysis: {manifest_path}")
        if {item["element_name"] for item in custom_elements} != {
            item["element_name"] for item in custom_contracts
        }:
            raise RuntimeError(
                f"custom DataElement manifest does not match planned type names: {manifest_path}"
            )
        invalid_prefixes = sorted(
            item["element_name"]
            for item in custom_elements
            if not item["element_name"].startswith(custom_prefix)
            or not item["class_name"].rsplit(".", 1)[-1].startswith(custom_prefix)
        )
        if invalid_prefixes:
            raise RuntimeError(
                f"custom DataElement and class names must start with {custom_prefix}: "
                + ", ".join(invalid_prefixes)
            )
        built_in_names = self._peach_runtime_element_names()
        collisions = sorted(
            item["element_name"]
            for item in custom_elements
            if item["element_name"].casefold() in built_in_names
        )
        if collisions:
            raise RuntimeError(
                "custom DataElement names collide with Peach built-ins: "
                + ", ".join(collisions)
            )
        source_text = "\n".join(path.read_text(encoding="utf-8") for path in sources)
        if source_text.count("[assembly: PluginAssembly]") != 1:
            raise RuntimeError(
                "custom DataElement sources must contain exactly one PluginAssembly marker"
            )
        for item in custom_elements:
            element_name = item["element_name"]
            if (
                f'[DataElement("{element_name}"' not in source_text
                or f'[PitParsable("{element_name}"' not in source_text
            ):
                raise RuntimeError(
                    f"custom DataElement {element_name} is missing matching plugin attributes"
                )
        self._compile_custom_data_elements(source_dir, dll_path)
        report["generation_status"] = "approved_and_compiled"
        report["custom_elements"] = custom_elements
        report["plugin_dll"] = str(dll_path)
        with report_path.open("w", encoding="utf-8") as report_file:
            json.dump(report, report_file, ensure_ascii=False, indent=2, sort_keys=True)
            report_file.write("\n")
        self.state["data_type_analysis"] = report
        self.save_state()
        UI.success(f"Compiled custom Peach DOM elements: {dll_path}")

    def _custom_data_element_context(self) -> str:
        report_path, _, dll_path = self._data_type_paths()
        report = self.state.get("data_type_analysis") or {}
        unsupported = report.get("unsupported_types", [])
        if unsupported and report.get("generation_status") == "declined":
            contracts = [item["custom_type"] for item in unsupported]
            return f"""
        Custom DSL scalar generation was skipped by the user.
        - Read "{report_path}" for the exact ExtendedType contracts.
        - Declare the protocol-prefixed ExtendedType symbols required by shared_refs
          exactly as planned: {json.dumps(contracts, ensure_ascii=False)}.
        - Do not invent a built-in replacement or change the wire encoding to hide
          the missing runtime implementation.
        - Continue generating the DSL. Later compilation or runtime validation may
          report that the skipped custom implementation is required.
        """
        if unsupported and (
            report.get("generation_status")
            not in {"approved_and_compiled", "reused_existing"}
            or not dll_path.is_file()
        ):
            raise RuntimeError(
                "unsupported protocol data types exist, but approved custom Peach DOM "
                "elements are not compiled"
            )
        if not unsupported:
            return ""
        return f"""
        Custom DSL scalar contract:
        - Read "{report_path}" before generating any DSL module.
        - Declare each confirmed custom scalar with ExtendedType exactly as described
          by that analysis. Its runtime behavior is supplied by "{dll_path}".
        - "docs/peach-dsl.md" is authoritative for every other declaration. Never
          invent another custom type.
        """
