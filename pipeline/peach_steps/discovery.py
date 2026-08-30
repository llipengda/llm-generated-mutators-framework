import json
from pathlib import Path

from agent import build_agent_graph
from pipeline.peach_steps.common import PeachStepMixin
from ui import UI, ask_generate_custom_data_elements


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
        types = report.get("types")
        if not isinstance(types, list) or not types:
            raise ValueError("data type analysis must contain a non-empty types list")
        allowed = {"supported", "unsupported", "uncertain"}
        required = {
            "wire_type",
            "encoding",
            "rfc_evidence",
            "status",
            "peach_evidence",
            "recommended_model",
            "confidence",
        }
        seen = set()
        for index, item in enumerate(types):
            if not isinstance(item, dict) or not required.issubset(item):
                raise ValueError(f"data type analysis item {index + 1} has an invalid schema")
            if item["status"] not in allowed:
                raise ValueError(f"data type analysis item {index + 1} has an invalid status")
            if item["confidence"] not in {"high", "medium", "low"}:
                raise ValueError(f"data type analysis item {index + 1} has invalid confidence")
            name = item["wire_type"]
            if not isinstance(name, str) or not name.strip() or name.casefold() in seen:
                raise ValueError(f"data type analysis item {index + 1} has an invalid wire_type")
            seen.add(name.casefold())
            for field in required - {"status", "confidence", "wire_type"}:
                if not isinstance(item[field], str) or not item[field].strip():
                    raise ValueError(
                        f"data type analysis item {index + 1} has an empty {field}"
                    )
        return report

    def _data_type_summary(self, report: dict) -> str:
        lines = []
        for item in report["types"]:
            lines.append(
                f"- **{item['wire_type']}** — `{item['status']}` "
                f"({item['confidence']} confidence): {item['recommended_model']}"
            )
            if item["status"] != "supported":
                lines.append(f"  - Encoding: {item['encoding']}")
                lines.append(f"  - Evidence: {item['rfc_evidence']}; {item['peach_evidence']}")
        return "\n".join(lines)

    def _peach_data_element_catalog(self) -> str:
        """Build a small local capability index without spending LLM tokens on peach.txt."""
        catalog_path = Path("peach") / "peach.txt"
        lines = catalog_path.read_text(encoding="utf-8").splitlines()
        in_elements = False
        compact = []
        for line in lines:
            if line.startswith("-----Data Element"):
                in_elements = True
                continue
            if in_elements and line.startswith("-----"):
                break
            if not in_elements:
                continue
            if line.startswith("  ") and not line.startswith("    "):
                compact.append(line.strip())
            elif line.startswith("    * "):
                compact.append("  required: " + line.strip()[2:].strip())
        if not compact:
            raise RuntimeError(f"could not extract Data Element catalog from {catalog_path}")
        return "\n".join(compact)

    def step_1_5_data_type_support(self):
        UI.title("Step 1.5: Peach Basic Data Type Support")
        report_path, source_dir, dll_path = self._data_type_paths()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        peach_catalog = self._peach_data_element_catalog()

        cached = self.state.get("data_type_analysis") or {}
        if (
            cached.get("protocol") == self.protocol_lower
            and cached.get("packet_types") == (self.state.get("packet_types") or [])
            and report_path.is_file()
        ):
            report = self._load_data_type_analysis(report_path)
            UI.dim(f"Reusing saved data type analysis: {report_path}")
        else:
            report = None

        analysis_prompt = f"""
        Audit every distinct BASIC WIRE DATA TYPE required by the
        {self.protocol_name} protocol before its Peach DataModel is generated.
        Packet types in scope: {self.state.get('packet_types') or []}.

        Be conservative and evidence-driven. The compact catalog below was extracted
        locally from the authoritative "./peach/peach.txt" Data Element section.
        Use at most six focused RFC_Search calls to establish the actual byte/bit
        encoding of every protocol primitive. Search_Class may be used only when the
        catalog is ambiguous; a similar class name is not proof of wire support.

        Peach DataElement capability catalog:
        {peach_catalog}

        Classify each primitive as:
        - supported: an existing element, or an explicit composition of existing
          elements, can crack AND serialize every valid encoding losslessly;
        - unsupported: the encoding needs protocol-specific crack/serialize logic
          that no existing element or composition can provide, and this conclusion
          has strong RFC and Peach evidence;
        - uncertain: evidence is incomplete or support depends on an unverified
          assumption. Never turn uncertainty into an unsupported claim merely to
          justify code generation.

        Do not treat semantic constraints, packet containers, optionality,
        checksums/fixups, enumerations over an ordinary integer, or ordinary
        length/count relations as new basic data types. Prefer standard Peach
        composition whenever it preserves the exact wire language. Conversely,
        do not call Blob support when doing so would hide a defined primitive
        encoding needed for cracking, relations, or mutation.

        Write exactly this JSON shape to "{report_path}":
        {{
          "protocol": "{self.protocol_lower}",
          "packet_types": {json.dumps(self.state.get('packet_types') or [])},
          "types": [{{
            "wire_type": "precise protocol type name",
            "encoding": "complete wire encoding and validity bounds",
            "rfc_evidence": "section and concise evidence",
            "status": "supported | unsupported | uncertain",
            "peach_evidence": "exact existing elements/composition checked",
            "recommended_model": "exact Peach mapping or reason custom code is needed",
            "confidence": "high | medium | low"
          }}]
        }}
        Include all primitives, including conventional supported ones, so omissions
        are visible. Do not generate C#, XML, or any other file.
        """
        if report is None:
            analysis_agent = build_agent_graph(
                retriever=self.retriever,
                target="peach",
                config=self.agent_config,
                tool_names={"RFC_Search", "Search_Class", "Write_File"},
            )
            self.call_agent(
                analysis_prompt,
                "Step 1.5: Peach Basic Data Type Support Analysis",
                agent_graph=analysis_agent,
            )
            if not report_path.is_file():
                raise RuntimeError(f"data type analysis was not written: {report_path}")
            report = self._load_data_type_analysis(report_path)
        UI.result_markdown("Peach Basic Data Type Support", self._data_type_summary(report))

        unsupported = [item for item in report["types"] if item["status"] == "unsupported"]
        uncertain = [item for item in report["types"] if item["status"] == "uncertain"]
        if uncertain:
            report["generation_status"] = "manual_review_required"
            self.state["data_type_analysis"] = report
            self.save_state()
            raise RuntimeError(
                "uncertain wire types require manual review before DataModel generation: "
                + ", ".join(item["wire_type"] for item in uncertain)
            )
        if (
            unsupported
            and report.get("generation_status") == "approved_and_compiled"
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
            UI.success("All confirmed protocol primitives are representable by Peach.")
            return

        names = [item["wire_type"] for item in unsupported]
        if not ask_generate_custom_data_elements(self.protocol_name, names):
            report["generation_status"] = "declined"
            self.state["data_type_analysis"] = report
            self.save_state()
            raise RuntimeError(
                "custom Peach DOM element generation was not approved; stopping before "
                "DataModel generation"
            )

        source_dir.mkdir(parents=True, exist_ok=True)
        element_manifest_path = source_dir / "manifest.json"
        previous_manifest_mtime = (
            element_manifest_path.stat().st_mtime_ns
            if element_manifest_path.is_file()
            else None
        )
        generation_prompt = f"""
        Generate protocol-specific Peach DOM DataElement implementations for the
        confirmed unsupported types in "{report_path}": {names}.

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
        [PitParsable] names. Do not generate a custom class for supported or uncertain
        items. Do not use placeholders, TODOs, seed-specific logic, silent clamping,
        or a Blob fallback.

        Put the classes in "{source_dir}". Put `[assembly: PluginAssembly]` exactly
        once in "{source_dir / 'AssemblyInfo.cs'}" (and never in each class).
        Write "{source_dir / 'manifest.json'}" as a JSON array with exactly one
        object per generated type: {{"wire_type": "...", "element_name":
        "exact Pit XML tag", "class_name": "fully qualified C# class"}}.
        Do not compile or attempt repair loops; the pipeline will run one local,
        deterministic `mcs -warnaserror` compile after you finish. Write no files
        outside "{source_dir}".
        """
        generation_agent = build_agent_graph(
            retriever=self.retriever,
            target="peach",
            config=self.agent_config,
            tool_names={"Read_File", "Search_Class", "Write_File"},
        )
        self.call_agent(
            generation_prompt,
            "Step 1.5: Custom Peach DataElement Generation",
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
        built_in_names = {
            line.strip().casefold()
            for line in peach_catalog.splitlines()
            if line and not line.startswith("  required:")
        }
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
        unsupported = [
            item for item in report.get("types", []) if item.get("status") == "unsupported"
        ]
        if unsupported and (
            report.get("generation_status") != "approved_and_compiled" or not dll_path.is_file()
        ):
            raise RuntimeError(
                "unsupported protocol data types exist, but approved custom Peach DOM "
                "elements are not compiled"
            )
        if not unsupported:
            return ""
        return f"""
        Custom DataElement contract:
        - Read "{report_path}" before planning or generating any DataModel.
        - Read "{report_path.parent / 'DataElements' / 'manifest.json'}" for the
          exact Pit XML element names.
        - The confirmed custom elements in that report are available through the
          compiled plugin "{dll_path}" and MAY be used exactly as documented there.
        - peach.txt remains authoritative for all other elements. Never invent any
          additional custom type or use an uncertain type as if it were supported.
        """
