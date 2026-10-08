from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from xml.etree import ElementTree as ET

if __package__ in ("", "tests"):
    __package__ = "peach_dsl.tests"

from ..compiler import (
    DSLValidationError,
    validate_dsl_dependencies,
    validate_dsl_module,
    validate_dsl_source,
)


def compile_dsl_subprocess(entry: Path, output: Path) -> subprocess.CompletedProcess[str]:
    """Exercise the package CLI using the active Python environment."""
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "peach_dsl.compiler",
            "--entry",
            str(entry),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )


class PeachDSLCompilerTests(unittest.TestCase):
    def test_module_validation_rejects_detached_relation_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "family.py"
            path.write_text(
                "from peach_dsl import Schema, Int8, Blob\n"
                "class Other(Schema):\n    length = Int8()\n"
                "class Packet(Schema):\n"
                "    length = Int8()\n"
                "    data = Blob[Other.length]()\n",
                encoding="utf-8",
            )
            with self.assertRaises(DSLValidationError) as raised:
                validate_dsl_module(path)
            self.assertIn("Schema Packet", str(raised.exception))
            self.assertIn("cannot resolve relation source field 'length'", str(raised.exception))
            self.assertFalse((root / "datamodel.xml").exists())

    def test_module_validation_checks_imported_type_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "shared_model.py").write_text(
                "from peach_dsl import Schema, Int8\n"
                "wrong: int = 'bad'\n"
                "class Header(Schema):\n    value = Int8()\n",
                encoding="utf-8",
            )
            path = root / "family.py"
            path.write_text(
                "from peach_dsl import Schema\n"
                "from shared_model import Header\n"
                "class Packet(Schema):\n    header = Header()\n",
                encoding="utf-8",
            )
            with self.assertRaises(DSLValidationError) as raised:
                validate_dsl_module(path)
            self.assertIn("shared_model.py:2:", str(raised.exception))
            self.assertIn("(reportAssignmentType)", str(raised.exception))

    def test_recompile_without_computation_removes_script_references(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = root / "root.py"
            output = root / "datamodel.xml"
            entry.write_text(
                "from peach_dsl import *\n"
                "@compute\n"
                "def increment(value: int) -> int:\n"
                "    return value + 1\n"
                "class Packet(Schema):\n"
                "    source = Int8()\n"
                "    result = Int8(increment(source))\n"
                "ROOT = Packet\n",
                encoding="utf-8",
            )
            first = compile_dsl_subprocess(entry, output)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            self.assertTrue((root / "python_fixup.py").is_file())
            self.assertIn("ScriptFixup", output.read_text(encoding="utf-8"))

            entry.write_text(
                "from peach_dsl import *\n"
                "class Packet(Schema):\n"
                "    source = Int8()\n"
                "    result = Int8(fixed(9))\n"
                "ROOT = Packet\n",
                encoding="utf-8",
            )
            second = compile_dsl_subprocess(entry, output)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            xml = ET.fromstring(output.read_text(encoding="utf-8"))
            ns = {"p": "http://peachfuzzer.com/2012/Peach"}
            self.assertIsNone(xml.find("p:Import[@import='python_fixup']", ns))
            self.assertIsNone(xml.find(".//p:Fixup", ns))
            result = xml.find("p:DataModel/p:Number[@name='result']", ns)
            assert result is not None
            self.assertEqual(result.get("value"), "9")
            self.assertEqual(result.get("token"), "true")

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

    def test_compute_type_checks_one_to_four_symbolic_parameters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "computed.py"
            path.write_text(
                "from peach_dsl import *\n"
                "@compute\n"
                "def one(a: int) -> int:\n"
                "    return a\n"
                "@compute\n"
                "def two(a: int, b: str) -> int:\n"
                "    return a + len(b)\n"
                "@compute\n"
                "def three(a: int, b: str, c: bytearray) -> int:\n"
                "    return a + len(b) + sum(c)\n"
                "@compute\n"
                "def four(a: int, b: str, c: bytearray, d: int) -> int:\n"
                "    return a + len(b) + sum(c) + d\n"
                "class Packet(Schema):\n"
                "    number = Int8()\n"
                "    text = String()\n"
                "    @Block\n"
                "    class header(Schema):\n"
                "        value = Int8()\n"
                "    first = Int16(one(number))\n"
                "    second = Int16(two(number, text))\n"
                "    third = Int16(three(number, text, header))\n"
                "    fourth = Int16(four(number, text, header, number))\n",
                encoding="utf-8",
            )
            validate_dsl_source(path)

            path.write_text(
                path.read_text(encoding="utf-8")
                + "bad = two(Packet.text, Packet.number)\n",
                encoding="utf-8",
            )
            with self.assertRaises(DSLValidationError):
                validate_dsl_source(path)

    def test_compute_rejects_five_parameters_statically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "computed_five.py"
            path.write_text(
                "from peach_dsl import *\n"
                "@compute\n"
                "def five(a: int, b: int, c: int, d: int, e: int) -> int:\n"
                "    return a + b + c + d + e\n",
                encoding="utf-8",
            )
            with self.assertRaises(DSLValidationError) as raised:
                validate_dsl_source(path)
            self.assertIn("(reportCallIssue)", str(raised.exception))
            self.assertIn("(reportArgumentType)", str(raised.exception))

    def test_compute_generates_script_fixup_with_common_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = root / "computed_root.py"
            entry.write_text(
                "from peach_dsl import *\n"
                "@compute\n"
                "def calc_crc(header: bytearray, kind: int) -> int:\n"
                "    return (sum(header) + kind) % 65536\n"
                "@compute\n"
                "def add_fields(left: int, right: int) -> int:\n"
                "    return left + right\n"
                "@compute\n"
                "def block_crc(block: bytearray) -> int:\n"
                "    return sum(block) % 256\n"
                "class Header(Schema):\n"
                "    kind = Int8()\n"
                "    payload = Blob[2]()\n"
                "class Packet(Schema):\n"
                "    prefix = Int8()\n"
                "    header = Header()\n"
                "    @Block\n"
                "    class trailer(Schema):\n"
                "        value = Int8()\n"
                "    crc = Int16(calc_crc(header, header.kind))\n"
                "    total = Int16(add_fields(prefix, crc))\n"
                "    trailer_crc = Int8(block_crc(trailer))\n"
                "ROOT = Packet\n",
                encoding="utf-8",
            )
            output = root / "datamodel.xml"
            result = compile_dsl_subprocess(entry, output)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            xml = output.read_text(encoding="utf-8")
            self.assertIn('<PythonPath path="."', xml)
            self.assertIn('<Import import="python_fixup"', xml)
            self.assertIn('<Fixup class="ScriptFixup">', xml)
            self.assertRegex(
                xml,
                r'<Param name="class" value="python_fixup\.Fixup_calc_crc_[0-9a-f]{12}"',
            )
            self.assertIn('<Param name="ref" value="header"', xml)
            self.assertIn('<Param name="ref" value="Packet"', xml)
            self.assertIn('<Param name="ref" value="trailer"', xml)

            script = (root / "python_fixup.py").read_text(encoding="utf-8")
            self.assertTrue(script.startswith("import clr\n"))
            self.assertRegex(script, r"class Fixup_calc_crc_[0-9a-f]{12}:")
            self.assertIn("def func(self, header, kind):", script)
            self.assertIn("return (sum(header) + kind) % 65536", script)
            self.assertIn("header = bytearray(element.Bytes())", script)
            self.assertIn('kind = int(str(element.find("kind").InternalValue).rstrip(\'\\0\'))', script)
            self.assertIn("return self._convert_result(self.func(header, kind))", script)
            self.assertIn('left = int(str(element.find("prefix").InternalValue).rstrip(\'\\0\'))', script)
            self.assertIn('right = int(str(element.find("crc").InternalValue).rstrip(\'\\0\'))', script)
            self.assertIn("return self._convert_result(self.func(left, right))", script)
            self.assertIn("block = bytearray(element.Bytes())", script)
            self.assertIn("return self._convert_result(self.func(block))", script)

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

            with self.assertRaises(DSLValidationError) as raised:
                validate_dsl_dependencies(entry)
            self.assertIn("(reportUnknownVariableType)", str(raised.exception))

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
            with self.assertRaises(DSLValidationError) as raised:
                validate_dsl_source(path)
            self.assertIn("(reportAttributeAccessIssue)", str(raised.exception))

    def test_pyright_preserves_optional_member_type_without_internal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "optional.py"
            path.write_text(
                "from peach_dsl import *\n"
                "class Header(Schema):\n"
                "    length = Int16()\n"
                "class Packet(Schema):\n"
                "    header = Optional[Header]()\n"
                "    count = Optional[Int8]()\n"
                "class Frame(Schema):\n"
                "    packet = Packet()\n"
                "    length: MemberRef[int] = packet.header.length\n"
                "    count: MemberRef[int] = packet.count\n",
                encoding="utf-8",
            )
            validate_dsl_source(path)

            path.write_text(
                path.read_text(encoding="utf-8")
                + "missing = Frame.packet.header.unknown\n",
                encoding="utf-8",
            )
            with self.assertRaises(DSLValidationError) as raised:
                validate_dsl_source(path)
            self.assertIn("(reportAttributeAccessIssue)", str(raised.exception))

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
            with self.assertRaises(DSLValidationError) as raised:
                validate_dsl_source(path)
            self.assertIn("(reportAssignmentType)", str(raised.exception))

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
            with self.assertRaises(DSLValidationError) as raised:
                validate_dsl_source(path)
            self.assertIn("(reportArgumentType)", str(raised.exception))

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
            with self.assertRaises(DSLValidationError) as raised:
                validate_dsl_source(path)
            self.assertIn("(reportUndefinedVariable)", str(raised.exception))

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
            with self.assertRaises(DSLValidationError) as raised:
                validate_dsl_source(path)
            self.assertIn("(reportArgumentType)", str(raised.exception))

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
            with self.assertRaises(DSLValidationError) as raised:
                validate_dsl_source(path)
            self.assertIn("(reportInvalidTypeArguments)", str(raised.exception))

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
                "from peach_dsl import *\n"
                "class Root(Schema):\n"
                "    data = Blob()\n"
                "ROOT = Root\n"
                "raise TypeError('deliberate DSL failure')\n",
                encoding="utf-8",
            )
            output = root / "datamodel.xml"

            result = compile_dsl_subprocess(entry, output)

            detail = result.stdout + result.stderr
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("TypeError: deliberate DSL failure", detail)
            self.assertIn(f"root cause: {entry.resolve()}:5", detail)
            self.assertIn(
                "raise TypeError('deliberate DSL failure')",
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

if __name__ == "__main__":
    unittest.main()
