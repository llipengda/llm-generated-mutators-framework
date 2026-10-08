"""Framework manifest integration tests for the Peach DSL compiler."""

from pathlib import Path
import tempfile
import unittest

from core.datamodel_dsl import (
    compile_dsl_subprocess,
    default_manifest,
    render_root_module,
    validate_manifest,
)


class DataModelDSLManifestTests(unittest.TestCase):
    def test_split_modules_compile_with_stable_model_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "shared_model.py").write_text(
                "from peach_dsl import *\n"
                "class Header(Schema):\n"
                "    kind = Int8()\n",
                encoding="utf-8",
            )
            (root / "family_request.py").write_text(
                "from peach_dsl import *\n"
                "from shared_model import Header\n"
                "class RequestPacket(Schema):\n"
                "    header = Header(kind=fixed(1))\n"
                "    payload = Blob()\n",
                encoding="utf-8",
            )
            manifest = default_manifest("demo", ["request"])
            manifest["shared_models"] = [
                {"symbol": "Header", "purpose": "header", "fields": []}
            ]
            manifest["packet_groups"][0]["id"] = "request"
            manifest["packet_groups"][0]["shared_refs"] = [
                {"symbol": "Header", "usage": "RequestPacket.header"}
            ]
            validated = validate_manifest(manifest, "demo", ["request"])
            root_source = render_root_module("demo", validated)
            self.assertNotIn("MODEL_NAMES", root_source)
            (root / "root.py").write_text(root_source, encoding="utf-8")
            output = root / "datamodel.xml"
            result = compile_dsl_subprocess(
                root / "root.py", output
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            xml = output.read_text(encoding="utf-8")
            self.assertIn('DataModel name="Header"', xml)
            self.assertIn('DataModel name="RequestPacket"', xml)
            self.assertIn('DataModel name="demo_packet_array"', xml)
            self.assertIn('DataModel name="DemoPacket"', xml)
            self.assertIn('ref="Header"', xml)
            self.assertIn('ref="RequestPacket"', xml)
            self.assertNotIn("compiler_single_packet_duplicate", xml)
            self.assertFalse((root / "datamodel.map.json").exists())
            second = compile_dsl_subprocess(
                root / "root.py", output
            )
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertEqual(output.read_text(encoding="utf-8"), xml)

    def test_manifest_derives_runtime_names_and_allows_prefixed_custom_refs(self) -> None:
        manifest = default_manifest("demo", ["request"])
        manifest["shared_models"] = [
            {"symbol": "DemoHeader", "purpose": "header", "fields": []}
        ]
        manifest["packet_groups"][0]["shared_refs"] = [
            {"symbol": "DemoHeader", "usage": "RequestPacket.header"},
            {"symbol": "DemoVarInt", "usage": "RequestPacket.payload_length"},
        ]
        manifest["packet_groups"][0]["packet_models"][0]["model_name"] = "wrong"
        validated = validate_manifest(manifest, "demo", ["request"])

        self.assertEqual(validated["shared_models"][0]["name"], "demo_header_t")
        self.assertEqual(
            validated["packet_groups"][0]["packet_models"][0]["model_name"],
            "demo_request_packet_t",
        )

    def test_manifest_omits_choice_name_and_derives_keyword_safe_union_name(self) -> None:
        manifest = default_manifest("demo", ["class"])

        validated = validate_manifest(manifest, "demo", ["class"])
        root_source = render_root_module("demo", validated)

        self.assertNotIn("choice_name", validated["packet_groups"][0]["packet_models"][0])
        self.assertIn("class_packet=ClassPacket", root_source)

        manifest["packet_groups"][0]["packet_models"][0]["choice_name"] = "class_packet"
        with self.assertRaisesRegex(ValueError, "must not specify choice_name"):
            validate_manifest(manifest, "demo", ["class"])

    def test_manifest_requires_group_description_and_shared_ref_usage(self) -> None:
        manifest = default_manifest("demo", ["request"])
        manifest["shared_models"] = [
            {"symbol": "Header", "purpose": "header", "fields": []}
        ]
        manifest["packet_groups"][0]["description"] = ""
        with self.assertRaisesRegex(ValueError, "description must not be empty"):
            validate_manifest(manifest, "demo", ["request"])

        manifest["packet_groups"][0]["description"] = "Request packet framing."
        manifest["packet_groups"][0]["shared_refs"] = [
            {"symbol": "Header", "usage": ""}
        ]
        with self.assertRaisesRegex(ValueError, "usage must not be empty"):
            validate_manifest(manifest, "demo", ["request"])

    def test_manifest_rejects_python_keyword_symbols(self) -> None:
        manifest = default_manifest("demo", ["request"])
        manifest["packet_groups"][0]["packet_models"][0]["symbol"] = "class"

        with self.assertRaisesRegex(ValueError, "must not be a Python keyword"):
            validate_manifest(manifest, "demo", ["request"])

if __name__ == "__main__":
    unittest.main()
