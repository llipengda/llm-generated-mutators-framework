from pathlib import Path
import tempfile
import unittest

from core.datamodel_dsl import (
    compile_dsl_subprocess,
    default_manifest,
    render_root_module,
    validate_manifest,
)
from peach_dsl.compiler import (
    DSLValidationError,
    validate_dsl_dependencies,
    validate_dsl_module,
    validate_dsl_source,
)


class PeachDSLCompilerTests(unittest.TestCase):
    def test_module_validation_evaluates_every_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "family.py"
            path.write_text(
                "from peach_dsl import *\n"
                "Custom = ExtendedType[int]('Custom')\n"
                "class Good(Schema):\n"
                "    value = Int8()\n"
                "class Bad(Schema):\n"
                "    value = Custom(name='reserved')\n"
                "class AlsoBad(Schema):\n"
                "    value = Custom(name='still reserved')\n",
                encoding="utf-8",
            )

            # Static validation accepts this module; the reserved Peach
            # attribute is rejected only while evaluating Bad.
            validate_dsl_source(path)
            with self.assertRaises(DSLValidationError) as raised:
                validate_dsl_module(path)
            detail = str(raised.exception)
            self.assertIn("Schema Bad failed to evaluate", detail)
            self.assertIn("Schema AlsoBad failed to evaluate", detail)
            self.assertEqual(detail.count("'name' is reserved"), 2)

    def test_module_validation_reports_evaluated_schema_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "family.py"
            path.write_text(
                "from peach_dsl import *\n"
                "class Outer(Schema):\n"
                "    value = Int8()\n"
                "    class Helper(Schema):\n"
                "        value = Int16()\n",
                encoding="utf-8",
            )

            self.assertEqual(
                validate_dsl_module(path),
                ("Outer", "Outer.Helper"),
            )

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

    def test_pyright_rejects_syntax_and_type_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "root.py"
            path.write_text("from peach_dsl import *\nvalue = Int8(unknown=True)\n")
            with self.assertRaises(DSLValidationError):
                validate_dsl_source(path)

            path.write_text("from peach_dsl import *\nvalue = Int8(\n")
            with self.assertRaises(DSLValidationError):
                validate_dsl_source(path)

    def test_pyright_does_not_apply_an_ast_language_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "root.py"
            path.write_text("import os\nvalue = os.path.basename('/tmp/x')\n")
            validate_dsl_source(path)

    def test_pyright_checks_imported_sibling_dsl_modules(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "shared_model.py").write_text(
                "from peach_dsl import *\nvalue = Int8(unknown=True)\n",
                encoding="utf-8",
            )
            entry = root / "root.py"
            entry.write_text(
                "from shared_model import value\nROOT = value\n", encoding="utf-8"
            )

            with self.assertRaisesRegex(DSLValidationError, "unknown"):
                validate_dsl_dependencies(entry)

    def test_pyright_preserves_decorated_block_internal_type(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "block.py"
            path.write_text(
                "from typing import Self\n"
                "from peach_dsl import *\n"
                "class Envelope(Schema):\n"
                "    @Block\n"
                "    class body(Schema):\n"
                "        length = Int8()\n"
                "        payload = Block(data=Blob())\n"
                "    @classmethod\n"
                "    def build(cls, value: Override) -> Self:\n"
                "        return cls(body=cls.body(payload=value))\n"
                "class Packet(Schema):\n"
                "    envelope = Envelope()\n"
                "    length: MemberRef[int] = envelope.body.length\n",
                encoding="utf-8",
            )
            validate_dsl_source(path)

            path.write_text(
                path.read_text(encoding="utf-8")
                + "bad = Packet().envelope.body.missing\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(DSLValidationError, "missing"):
                validate_dsl_source(path)

    def test_public_field_and_schema_member_aliases_are_type_checked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "aliases.py"
            path.write_text(
                "from peach_dsl import *\n"
                "field: AnyField = Int8()\n"
                "member: SchemaMember = Block(value=String())\n"
                "override: Override = Blob()\n",
                encoding="utf-8",
            )
            validate_dsl_source(path)

            path.write_text(
                path.read_text(encoding="utf-8") + "bad: AnyField = object()\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(DSLValidationError, "object"):
                validate_dsl_source(path)

    def test_override_accepts_every_schema_member_kind(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "overrides.py"
            path.write_text(
                "from peach_dsl import *\n"
                "class Nested(Schema):\n"
                "    value = Int8()\n"
                "class Alternative(Schema):\n"
                "    value = Int16()\n"
                "class Template(Schema):\n"
                "    gate = Int8()\n"
                "    value = Int8()\n"
                "field: Override = Int32()\n"
                "schema: Override = Nested()\n"
                "schema_union: Override = Nested | Alternative\n"
                "named_union: Override = Union(nested=Nested, alternative=Alternative)\n"
                "array: Override = Array[Nested, 2]()\n"
                "optional: Override = Optional[Nested](when=Template.gate == 1)\n"
                "block: Override = Block(value=Blob())\n"
                "a = Template(value=field)\n"
                "b = Template(value=schema)\n"
                "c = Template(value=schema_union)\n"
                "d = Template(value=named_union)\n"
                "e = Template(value=array)\n"
                "f = Template(value=optional)\n"
                "g = Template(value=block)\n",
                encoding="utf-8",
            )
            validate_dsl_source(path)

    def test_decimal_string_is_numeric_but_string_is_not_a_length(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "decimal_length.py"
            path.write_text(
                "from peach_dsl import *\n"
                "class Packet(Schema):\n"
                "    length = DecimalString(type='ascii')\n"
                "    separator = String(fixed('\\r\\n'), type='ascii')\n"
                "    payload = Blob[length + 2]()\n",
                encoding="utf-8",
            )
            validate_dsl_source(path)

            path.write_text(
                "from peach_dsl import *\n"
                "class Packet(Schema):\n"
                "    length = String(type='ascii')\n"
                "    payload = Blob[length]()\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(DSLValidationError, "Length"):
                validate_dsl_source(path)

    def test_field_override_can_be_forwarded_to_schema_constructor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "override.py"
            path.write_text(
                "from typing import Self\n"
                "from peach_dsl import *\n"
                "class Test(Schema):\n"
                "    a = Int8()\n"
                "    b = Int16()\n"
                "class Test2(Schema):\n"
                "    c = Test()\n"
                "    @classmethod\n"
                "    def build(\n"
                "        cls, a: FieldOverride[int], b: FieldOverride[int]\n"
                "    ) -> Self:\n"
                "        return cls(c=cls.c(a=a, b=b))\n",
                encoding="utf-8",
            )
            validate_dsl_source(path)

    def test_override_can_be_forwarded_to_direct_block_member(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = root / "root.py"
            entry.write_text(
                "from peach_dsl import *\n"
                "class Frame(Schema):\n"
                "    length = Int8()\n"
                "    body = Block[length](payload=Blob())\n"
                "def framed(payload: Override) -> Frame:\n"
                "    return Frame(\n"
                "        body=Block[Frame.length](payload=payload),\n"
                "    )\n"
                "class Root(Schema):\n"
                "    framed = framed(Block(kind=Int8(fixed(1))))\n"
                "ROOT = Root\n",
                encoding="utf-8",
            )
            output = root / "datamodel.xml"

            result = compile_dsl_subprocess(entry, output)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            xml = output.read_text(encoding="utf-8")
            self.assertIn('name="payload"', xml)
            self.assertIn('name="kind"', xml)

    def test_string_type_is_limited_to_peach_enum_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "strings.py"
            path.write_text(
                "from peach_dsl import *\n"
                "class Packet(Schema):\n"
                "    value = String(type='utf-8')\n",
                encoding="utf-8",
            )
            with self.assertRaises(DSLValidationError):
                validate_dsl_source(path)

    def test_bool_is_not_a_dsl_field_type(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bool_field.py"
            path.write_text(
                "from peach_dsl import *\n"
                "class Packet(Schema):\n"
                "    enabled = Bool()\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(DSLValidationError, "Bool"):
                validate_dsl_source(path)

            path.write_text(
                "from peach_dsl import *\n"
                "class Packet(Schema):\n"
                "    value = String(encoding='utf8')\n",
                encoding="utf-8",
            )
            with self.assertRaises(DSLValidationError):
                validate_dsl_source(path)

    def test_bit_position_is_compiler_owned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "flags.py"
            path.write_text(
                "from peach_dsl import *\n"
                "@Flags(Int8, endian='big')\n"
                "class Control(Schema):\n"
                "    high = Bit[4](position=0)\n"
                "    low = Bit[4]()\n",
                encoding="utf-8",
            )
            with self.assertRaises(DSLValidationError):
                validate_dsl_source(path)

    def test_integer_constraint_supports_modulo(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "family.py"
            path.write_text(
                "from peach_dsl import *\n"
                "class Packet(Schema):\n"
                "    flags = Int8(constraint=lambda value: value % 2 == 0)\n",
                encoding="utf-8",
            )
            validate_dsl_source(path)

    def test_constraint_and_field_symbol_operators_are_available(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "operators.py"
            path.write_text(
                "from peach_dsl import *\n"
                "class Packet(Schema):\n"
                "    a = Int8(constraint=lambda value: ((value + 1) * 2 // 2) % 3 == 0)\n"
                "    b = Int8(constraint=lambda value: ((value & 15) | 1) ^ 2 == 0)\n"
                "    c = Int8(constraint=lambda value: (value << 1) >> 1 == value)\n"
                "    d = Int8()\n"
                "    size = Blob[d + 1]()\n"
                "    reverse = Blob[1 + d]()\n",
                encoding="utf-8",
            )
            validate_dsl_source(path)

    def test_constraint_len_is_limited_to_string_and_bytes_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "constraints.py"
            path.write_text(
                "from peach_dsl import *\n"
                "class Text(Schema):\n"
                "    value = String(constraint=lambda value: len(value) > 0)\n",
                encoding="utf-8",
            )
            validate_dsl_source(path)

            path.write_text(
                "from peach_dsl import *\n"
                "class Number(Schema):\n"
                "    value = Int8(constraint=lambda value: len(value) > 0)\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(DSLValidationError, "__len__"):
                validate_dsl_source(path)

    def test_constraint_lambda_uses_the_field_logical_type(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "constraints.py"
            path.write_text(
                "from peach_dsl import *\n"
                "class Reply(Schema):\n"
                "    code = String(\n"
                "        type='ascii',\n"
                "        constraint=lambda value: "
                "1 <= len(value) <= 4 and value.isalpha(),\n"
                "    )\n",
                encoding="utf-8",
            )

            validate_dsl_source(path)

    def test_extended_type_accepts_only_scalar_value_types(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "root.py"
            path.write_text(
                'from peach_dsl import *\nCustom = ExtendedType[bytes]("Custom")\n'
            )
            validate_dsl_source(path)

            path.write_text(
                'from peach_dsl import *\nCustom = ExtendedType[list]("Custom")\n'
            )
            with self.assertRaisesRegex(
                DSLValidationError,
                "cannot be assigned to type variable",
            ):
                validate_dsl_source(path)

            path.write_text(
                'from peach_dsl import *\nCustom = ExtendedType[bool]("Custom")\n'
            )
            with self.assertRaises(DSLValidationError):
                validate_dsl_source(path)

    def test_declared_extended_type_can_be_used_as_a_field_factory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "shared_model.py"
            path.write_text(
                'from peach_dsl import *\n'
                'DemoVarInt = ExtendedType[int]("DemoVarInt")\n'
                'class Header(Schema):\n'
                '    length = DemoVarInt()\n'
                '    discriminator = DemoVarInt(fixed(1))\n',
                encoding="utf-8",
            )

            validate_dsl_source(path)

    def test_failed_compile_preserves_previous_xml(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = root / "root.py"
            entry.write_text("from peach_dsl import *\nROOT = Int8\n", encoding="utf-8")
            output = root / "datamodel.xml"
            output.write_text("previous", encoding="utf-8")
            result = compile_dsl_subprocess(entry, output)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(output.read_text(encoding="utf-8"), "previous")

    def test_runtime_error_reports_dsl_file_line_and_expression(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = root / "family_invalid.py"
            entry.write_text(
                "from typing import Any\n"
                "from peach_dsl import *\n"
                "value: BlockField[Any] = Block(data=Blob())\n"
                "class Root(Schema):\n"
                "    data = Blob()\n"
                "ROOT = Root\n",
                encoding="utf-8",
            )
            output = root / "datamodel.xml"

            result = compile_dsl_subprocess(entry, output)

            detail = result.stdout + result.stderr
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("TypeError: type 'BlockField' is not subscriptable", detail)
            self.assertIn(f"root cause: {entry.resolve()}:3", detail)
            self.assertIn(
                "value: BlockField[Any] = Block(data=Blob())",
                detail,
            )

    def test_anonymous_block_fields_can_construct_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = root / "family_request.py"
            entry.write_text(
                "from peach_dsl import *\n"
                "class RequestPacket(Schema):\n"
                "    properties = Block(kind=Int8(), value=Blob())\n"
                "customized = RequestPacket(\n"
                "    properties=RequestPacket.properties(kind=fixed(7)),\n"
                ")\n"
                "class Root(Schema):\n"
                "    request = customized\n"
                "ROOT = Root\n",
                encoding="utf-8",
            )
            output = root / "datamodel.xml"

            result = compile_dsl_subprocess(entry, output)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            xml = output.read_text(encoding="utf-8")
            self.assertIn('name="kind"', xml)
            self.assertIn('value="7"', xml)
            self.assertIn('name="value"', xml)

    def test_compiler_requires_only_a_schema_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = root / "root.py"
            entry.write_text(
                "from peach_dsl import *\n"
                "class DemoPacket(Schema):\n"
                "    kind = Int8()\n"
                "ROOT = DemoPacket\n",
                encoding="utf-8",
            )
            output = root / "datamodel.xml"

            result = compile_dsl_subprocess(entry, output)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('DataModel name="DemoPacket"', output.read_text())

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
