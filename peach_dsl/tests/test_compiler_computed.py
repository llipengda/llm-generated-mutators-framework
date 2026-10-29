"""Regression tests for computed field rendering and instance overrides."""

import unittest
import ast
import math
from types import SimpleNamespace
from unittest.mock import patch
from typing import Sequence
from xml.etree import ElementTree as ET

if __package__ in ("", "tests"):
    __package__ = "peach_dsl.tests"

from .. import Array, Bit, Blob, Block, DecimalString, Double, Flags, Int8, Int64, Optional, PacketUnion, Schema, Union, compute, fixed, to_peach_artifacts

COMPUTED_MASK = 15
COMPUTED_TABLE = (1, 3, 7)


class ComputedFieldTests(unittest.TestCase):
    def test_computed_uses_safe_shorter_anchor_under_model_name_shadow(self) -> None:
        @compute
        def add(left: int, right: int) -> int:
            return left + right

        class Body(Schema):
            left = Int8(3)
            right = Int8(4)

        class Packet(Schema):
            body = Body()
            header = Block(body=Body(left=9, right=10), Packet=Block(body=Body(left=12, right=13)))
            result = Int8(add(body.left, body.right))

        artifacts = to_peach_artifacts(Packet, include_header=False)
        root = ET.fromstring(artifacts.xml)
        self.assertEqual(root.find("Number[@name='result']/Fixup/Param[@name='ref']").get("value"), "body")
        assert artifacts.python_fixup is not None
        module = ast.parse(artifacts.python_fixup)
        module.body = [node for node in module.body if isinstance(node, ast.ClassDef)]
        namespace: dict[str, object] = {}
        exec(compile(module, "<generated-fixup>", "exec"), namespace)
        generated = next(item for name, item in namespace.items() if name.startswith("Fixup_"))
        assert callable(generated)
        body = SimpleNamespace(find=lambda name: SimpleNamespace(InternalValue={"left": 3, "right": 4}[name]))
        self.assertEqual(generated(None).fixup(body), 7)

    def test_computed_rejects_unresolvable_model_name_shadow(self) -> None:
        @compute
        def increment(value: int) -> int:
            return value + 1

        class Packet(Schema):
            source = Int8(3)
            body = Block(source=Int8(9), Packet=Block(source=Int8(12)), result=Int8(increment(source)))

        with self.assertRaisesRegex(ValueError, "computed.*ambiguous.*Packet"):
            to_peach_artifacts(Packet, include_header=False)
        root = ET.fromstring(to_peach_artifacts(Packet, name="Renamed", include_header=False).xml)
        self.assertEqual(root.find("Block/Number[@name='result']/Fixup/Param[@name='ref']").get("value"), "Renamed.source")

    def test_computed_rejects_syntax_unsupported_by_peach_python(self) -> None:
        def assignment(value: int) -> int:
            return (result := value + 1)

        def pattern(value: int) -> int:
            match value:
                case 1:
                    return 2
                case _:
                    return value

        def asynchronous_helper(value: int) -> int:
            async def helper() -> int:
                return value
            return value

        async def asynchronous(value: int) -> int:
            return value

        for function, feature in ((assignment, "assignment expressions"), (pattern, "pattern matching"),
                                  (asynchronous_helper, "async functions"), (asynchronous, "async functions")):
            source = Int8()
            packet = type("UnsupportedPython", (Schema,), {"source": source, "result": Int8(compute(function)(source))})
            with self.subTest(function=function.__name__):
                with self.assertRaisesRegex(ValueError, "IronPython does not support " + feature):
                    to_peach_artifacts(packet, include_header=False)

    def test_computed_keeps_supported_fstrings_and_unpacking(self) -> None:
        @compute
        def calculate(value: int) -> int:
            values = [0, *[value]]
            mapping = {**{"number": value}}
            return int(f"{value}") + sum(values) + mapping["number"]

        class Packet(Schema):
            source = Int8()
            result = Int8(calculate(source))

        artifacts = to_peach_artifacts(Packet, include_header=False)
        assert artifacts.python_fixup is not None
        module = ast.parse(artifacts.python_fixup)
        module.body = [node for node in module.body if isinstance(node, ast.ClassDef)]
        namespace: dict[str, object] = {}
        exec(compile(module, "<generated-fixup>", "exec"), namespace)
        generated = next(item for name, item in namespace.items() if name.startswith("Fixup_"))
        assert callable(generated)
        self.assertEqual(generated(None).fixup(SimpleNamespace(InternalValue=7)), 21)

    def test_local_computed_block_remains_reusable_with_overrides(self) -> None:
        @compute
        def increment(value: int) -> int:
            return value + 1

        source = Int8(3)
        local = Block(source=source, result=Int8(increment(source)))

        class First(Schema):
            body = local()

        class Second(Schema):
            body = local(source=Int8(7))

        for target, expected in ((First, "3"), (Second, "7")):
            with self.subTest(target=target):
                root = ET.fromstring(to_peach_artifacts(target, include_header=False).xml)
                self.assertEqual(root.find("Block/Number[@name='source']").get("value"), expected)
                reference = root.find("Block/Number[@name='result']/Fixup/Param[@name='ref']")
                assert reference is not None
                self.assertEqual(reference.get("value"), "body.source")

    def test_reused_computed_field_cannot_take_unrelated_same_named_source(self) -> None:
        @compute
        def increment(value: int) -> int:
            return value + 1

        class Original(Schema):
            source = Int8(3)
            result = Int8(increment(source))

        class Unrelated(Schema):
            source = Int8(9)
            result = Original.result

        with self.assertRaisesRegex(ValueError, "computed argument.*declaration scope"):
            to_peach_artifacts(Unrelated, include_header=False)

    def test_reused_block_cannot_rebind_outer_computed_source_by_name(self) -> None:
        @compute
        def increment(value: int) -> int:
            return value + 1

        class Original(Schema):
            source = Int8(3)
            body = Block(result=Int8(increment(source)))

        class Unrelated(Schema):
            source = Int8(9)
            body = Original.body()

        with self.assertRaisesRegex(ValueError, "computed argument.*(scope|declar|different)"):
            to_peach_artifacts(Unrelated, include_header=False)

        class Derived(Original):
            source = Int8(7)
            body = Original.body()

        self.assertIsNotNone(to_peach_artifacts(Derived, include_header=False).python_fixup)

    def test_inline_block_computed_references_inherited_source(self) -> None:
        @compute
        def increment(value: int) -> int:
            return value + 1

        class Base(Schema):
            source = Int8(3)

        class Packet(Base):
            body = Block(source=Int8(9), result=Int8(increment(Base.source)))

        root = ET.fromstring(to_peach_artifacts(Packet, include_header=False).xml)
        reference = root.find("Block/Number[@name='result']/Fixup/Param[@name='ref']")
        assert reference is not None
        self.assertEqual(reference.get("value"), "Packet.source")

    def test_outer_computed_paths_across_block_wrappers(self) -> None:
        @compute
        def increment(value: int) -> int:
            return value + 1

        class Header(Schema):
            source = Int8(3)

        for nested in (False, True):
            for kind in ("block", "optional", "array", "matrix", "union"):
                class Packet(Schema):
                    header = Header()
                    result = Int8(increment(header.source))
                    if kind == "optional":
                        result = Optional[result]()
                    elif kind == "array":
                        result = Array[result, 2]()
                    elif kind == "matrix":
                        result = Array[Array[result, 2](), 2]()
                    child = Block(header=Header(source=9), result=result)
                    if nested:
                        child = Block(inner=child)
                    if kind == "union":
                        body = Union(data=child, other=Int8())
                    else:
                        body = child
                    del child
                    del result

                with self.subTest(nested=nested, kind=kind):
                    root = ET.fromstring(to_peach_artifacts(Packet, name="Renamed", include_header=False).xml)
                    references = root.findall(".//Fixup/Param[@name='ref']")
                    self.assertEqual([node.get("value") for node in references], ["Renamed.header.source"])

    def test_inline_block_computed_references_outer_scalar(self) -> None:
        @compute
        def increment(value: int) -> int:
            return value + 1

        class Packet(Schema):
            source = Int8(3)
            body = Block(source=Int8(9), result=Int8(increment(source)))

        artifacts = to_peach_artifacts(Packet, include_header=False)
        root = ET.fromstring(artifacts.xml)
        reference = root.find("Block/Number[@name='result']/Fixup/Param[@name='ref']")
        assert reference is not None
        self.assertEqual(reference.get("value"), "Packet.source")

    def test_class_captures_include_conditional_and_exception_fallbacks(self) -> None:
        @compute
        def calculate(value: int) -> int:
            class Conditional:
                if value:
                    COMPUTED_TABLE = (9,)
                result = COMPUTED_TABLE[0]

            class Handler:
                try:
                    if value:
                        raise ValueError("skip assignment")
                    COMPUTED_TABLE = (8,)
                except ValueError:
                    pass
                result = COMPUTED_TABLE[0]

            return Conditional.result + Handler.result

        class Packet(Schema):
            source = Int8()
            result = Int8(calculate(source))

        artifacts = to_peach_artifacts(Packet, include_header=False)
        assert artifacts.python_fixup is not None
        module = ast.parse(artifacts.python_fixup)
        module.body = [node for node in module.body if isinstance(node, ast.ClassDef)]
        namespace: dict[str, object] = {}
        exec(compile(module, "<generated-fixup>", "exec"), namespace)
        generated = next(item for name, item in namespace.items() if name.startswith("Fixup_"))
        assert callable(generated)
        for value, expected in ((0, 9), (1, 10)):
            with self.subTest(value=value):
                self.assertEqual(generated(None).fixup(SimpleNamespace(InternalValue=value)), expected)

    def test_class_captures_preserve_method_lambda_and_comprehension_scopes(self) -> None:
        @compute
        def calculate(value: int) -> int:
            class Settings:
                COMPUTED_MASK = 2
                numbers = [COMPUTED_MASK for _ in range(2)]
                deferred = lambda: COMPUTED_MASK

                def method(default=COMPUTED_MASK):
                    return COMPUTED_MASK + default

            return Settings.numbers[0] + Settings.deferred() + Settings.method() + value

        class Packet(Schema):
            source = Int8()
            result = Int8(calculate(source))

        artifacts = to_peach_artifacts(Packet, include_header=False)
        assert artifacts.python_fixup is not None
        module = ast.parse(artifacts.python_fixup)
        module.body = [node for node in module.body if isinstance(node, ast.ClassDef)]
        namespace: dict[str, object] = {}
        exec(compile(module, "<generated-fixup>", "exec"), namespace)
        generated = next(item for name, item in namespace.items() if name.startswith("Fixup_"))
        assert callable(generated)
        self.assertEqual(generated(None).fixup(SimpleNamespace(InternalValue=1)), 48)

    def test_class_names_fall_back_to_globals_before_assignment_and_after_deletion(self) -> None:
        @compute
        def calculate(value: int) -> int:
            class Settings:
                COMPUTED_MASK = COMPUTED_MASK + 1
                initial = COMPUTED_MASK
                del COMPUTED_MASK
                restored = COMPUTED_MASK
                if value:
                    COMPUTED_MASK = 2
                conditional = COMPUTED_MASK
            return Settings.initial + Settings.restored + Settings.conditional

        class Packet(Schema):
            source = Int8()
            result = Int8(calculate(source))

        artifacts = to_peach_artifacts(Packet, include_header=False)
        assert artifacts.python_fixup is not None
        module = ast.parse(artifacts.python_fixup)
        module.body = [node for node in module.body if isinstance(node, ast.ClassDef)]
        namespace: dict[str, object] = {}
        exec(compile(module, "<generated-fixup>", "exec"), namespace)
        generated = next(item for name, item in namespace.items() if name.startswith("Fixup_"))
        assert callable(generated)
        for value in (0, 1):
            with self.subTest(value=value):
                self.assertEqual(generated(None).fixup(SimpleNamespace(InternalValue=value)),
                                 46 if value == 0 else 33)

    def test_computed_decimal_text_preserves_width_terminator_and_large_values(self) -> None:
        @compute
        def identity(value: int) -> int:
            return value

        source = DecimalString()
        models = (
            (DecimalString[6](identity(source), null_terminated=True), 42, "00042\0"),
            (DecimalString[12](identity(source), type="utf16", null_terminated=True), -42, "-0042\0"),
            (DecimalString(identity(source), null_terminated=True), 2**80, str(2**80)),
            (DecimalString[30](identity(source)), 2**80, str(2**80).zfill(30)),
        )
        for field, value, expected in models:
            with self.subTest(field=field, value=value):
                packet = type("Packet", (Schema,), {"source": source, "result": field})
                artifacts = to_peach_artifacts(packet, include_header=False)
                assert artifacts.python_fixup is not None
                module = ast.parse(artifacts.python_fixup)
                module.body = [node for node in module.body if isinstance(node, ast.ClassDef)]
                namespace: dict[str, object] = {}
                exec(compile(module, "<generated-fixup>", "exec"), namespace)
                generated = next(value for name, value in namespace.items() if name.startswith("Fixup_"))
                assert callable(generated)
                self.assertEqual(generated(None).fixup(SimpleNamespace(InternalValue=str(value))), expected)
                self.assertEqual(generated(None).fixup(SimpleNamespace(InternalValue=expected)), expected)
                if "length" in field.options:
                    with self.assertRaisesRegex(OverflowError, "does not fit"):
                        generated(None).fixup(SimpleNamespace(InternalValue=str(10**100)))

    def test_local_helper_class_preserves_module_and_closure_constants(self) -> None:
        offset = 3

        @compute
        def calculate(value: int) -> int:
            class Settings:
                bias = offset
                mask = COMPUTED_MASK
                # This class-local name must not capture the imported type.
                Sequence = 2
                multiplier = Sequence
            return ((value + Settings.bias) * Settings.multiplier) & Settings.mask

        class Packet(Schema):
            source = Int8()
            result = Int8(calculate(source))

        artifacts = to_peach_artifacts(Packet, include_header=False)
        assert artifacts.python_fixup is not None
        module = ast.parse(artifacts.python_fixup)
        module.body = [node for node in module.body if isinstance(node, ast.ClassDef)]
        namespace: dict[str, object] = {}
        exec(compile(module, "<generated-fixup>", "exec"), namespace)
        generated = next(value for name, value in namespace.items() if name.startswith("Fixup_"))
        assert callable(generated)
        self.assertEqual(generated(None).fixup(SimpleNamespace(InternalValue=7)), 4)

    def test_annotation_only_targets_preserve_evaluation_order_without_access(self) -> None:
        @compute
        def calculate(value: int) -> int:
            calls: list[int] = []

            def mark(number: int) -> int:
                calls.append(number)
                return number

            mark(1).missing: int
            mark(2)[mark(3)]: int
            mark(4)[mark(5):mark(6):mark(7), mark(8)]: int
            return value + sum(number * 10**index for index, number in enumerate(calls))

        class Packet(Schema):
            source = Int8()
            result = Int64(calculate(source))

        artifacts = to_peach_artifacts(Packet, include_header=False)
        assert artifacts.python_fixup is not None
        module = ast.parse(artifacts.python_fixup)
        self.assertFalse(any(isinstance(node, ast.AnnAssign) for node in ast.walk(module)))
        module.body = [node for node in module.body if isinstance(node, ast.ClassDef)]
        namespace: dict[str, object] = {}
        exec(compile(module, "<generated-fixup>", "exec"), namespace)
        generated = next(value for name, value in namespace.items() if name.startswith("Fixup_"))
        assert callable(generated)
        self.assertEqual(generated(None).fixup(SimpleNamespace(InternalValue=7)), 87654328)

    def test_annotation_only_dependencies_are_not_captured(self) -> None:
        local_type = int

        @compute
        def calculate(value: int) -> int:
            def total(values: Sequence[local_type]) -> int:
                return sum(values)
            return total((value, 1))

        class Packet(Schema):
            source = Int8()
            result = Int8(calculate(source))

        artifacts = to_peach_artifacts(Packet, include_header=False)
        assert artifacts.python_fixup is not None
        module = ast.parse(artifacts.python_fixup)
        module.body = [node for node in module.body if isinstance(node, ast.ClassDef)]
        namespace: dict[str, object] = {}
        exec(compile(module, "<generated-fixup>", "exec"), namespace)
        generated = next(value for name, value in namespace.items() if name.startswith("Fixup_"))
        assert callable(generated)
        self.assertEqual(generated(None).fixup(SimpleNamespace(InternalValue=7)), 8)

    def test_computed_integer_input_does_not_use_variants_int32_conversion(self) -> None:
        @compute
        def low_byte(value: int) -> int:
            return value & 255

        class Packet(Schema):
            source = Int64()
            result = Int8(low_byte(source))

        artifacts = to_peach_artifacts(Packet, include_header=False)
        assert artifacts.python_fixup is not None
        module = ast.parse(artifacts.python_fixup)
        module.body = [node for node in module.body if isinstance(node, ast.ClassDef)]
        namespace: dict[str, object] = {}
        exec(compile(module, "<generated-fixup>", "exec"), namespace)
        generated = next(value for name, value in namespace.items() if name.startswith("Fixup_"))
        assert callable(generated)

        class Variant:
            def __init__(self, value: int) -> None:
                self.value = value

            def __int__(self) -> int:
                raise OverflowError("Peach Variant converts directly to Int32")

            def __str__(self) -> str:
                return str(self.value)

        for value in (-(2**63), -(2**31) - 1, 2**31, 2**63 - 1, 2**64 - 1):
            with self.subTest(value=value):
                result = generated(None).fixup(SimpleNamespace(InternalValue=Variant(value)))
                self.assertEqual(result, value & 255)

    def test_computed_float_results_preserve_precision_and_special_values(self) -> None:
        @compute
        def identity(value: float) -> float:
            return value

        class Packet(Schema):
            source = Double()
            result = Double(identity(source))

        artifacts = to_peach_artifacts(Packet, include_header=False)
        assert artifacts.python_fixup is not None
        module = ast.parse(artifacts.python_fixup)
        module.body = [node for node in module.body if isinstance(node, ast.ClassDef)]
        namespace: dict[str, object] = {}
        exec(compile(module, "<generated-fixup>", "exec"), namespace)
        generated = next(value for name, value in namespace.items() if name.startswith("Fixup_"))
        assert callable(generated)
        values = (0.0, -0.0, 1.2345678901234567, 5e-324, 1.7976931348623157e308,
                  float("inf"), float("-inf"), float("nan"))
        for value in values:
            with self.subTest(value=value):
                result = generated(None).fixup(SimpleNamespace(InternalValue=value))
                self.assertIsInstance(result, str)
                if math.isnan(value):
                    self.assertEqual(result, "NaN")
                elif math.isinf(value):
                    self.assertEqual(result, "Infinity" if value > 0 else "-Infinity")
                else:
                    self.assertEqual(float(result).hex(), value.hex())

    def test_computed_large_integer_results_use_clr_integer_types(self) -> None:
        @compute
        def identity(value: int) -> int:
            return value

        class Packet(Schema):
            source = Int64()
            result = Int64(identity(source))

        artifacts = to_peach_artifacts(Packet, include_header=False)
        assert artifacts.python_fixup is not None
        module = ast.parse(artifacts.python_fixup)
        module.body = [node for node in module.body if isinstance(node, ast.ClassDef)]
        namespace: dict[str, object] = {}
        exec(compile(module, "<generated-fixup>", "exec"), namespace)
        generated = next(value for name, value in namespace.items() if name.startswith("Fixup_"))
        assert callable(generated)

        class CLRInt64(int):
            def __new__(cls, value: int):
                if not -(2**63) <= value < 2**63:
                    raise OverflowError("Int64 conversion overflow")
                return super().__new__(cls, value)

        class CLRUInt64(int):
            def __new__(cls, value: int):
                if not 0 <= value < 2**64:
                    raise OverflowError("UInt64 conversion overflow")
                return super().__new__(cls, value)

        system = SimpleNamespace(Int64=CLRInt64, UInt64=CLRUInt64)
        cases = (
            (-(2**63), CLRInt64), (-(2**31) - 1, CLRInt64),
            (-(2**31), int), (2**31 - 1, int), (2**31, CLRInt64),
            (2**63 - 1, CLRInt64), (2**63, CLRUInt64), (2**64 - 1, CLRUInt64),
        )
        with patch.dict("sys.modules", {"System": system}):
            for value, expected_type in cases:
                with self.subTest(value=value):
                    result = generated(None).fixup(SimpleNamespace(InternalValue=value))
                    self.assertIs(type(result), expected_type)
                    self.assertEqual(result, value)
            for value in (-(2**63) - 1, 2**64):
                with self.subTest(overflow=value):
                    with self.assertRaisesRegex(OverflowError, "conversion overflow"):
                        generated(None).fixup(SimpleNamespace(InternalValue=value))

    def test_computed_binary_result_is_converted_to_clr_byte_array(self) -> None:
        @compute
        def binary(value: int) -> bytes:
            return bytes((value, 255))

        class Packet(Schema):
            source = Int8()
            result = Blob[2](binary(source))

        artifacts = to_peach_artifacts(Packet, include_header=False)
        assert artifacts.python_fixup is not None
        module = ast.parse(artifacts.python_fixup)
        module.body = [node for node in module.body if isinstance(node, ast.ClassDef)]
        namespace: dict[str, object] = {}
        exec(compile(module, "<generated-fixup>", "exec"), namespace)
        generated = next(value for name, value in namespace.items() if name.startswith("Fixup_"))
        assert callable(generated)

        class CLRByteArray(list[int]):
            pass

        # Model the CLR array constructor for this CPython regression test.
        # Actual CLR interoperability is checked separately with IronPython.
        with patch.dict("sys.modules", {"System": SimpleNamespace(Array=CLRByteArray, Byte=int)}):
            result = generated(None).fixup(SimpleNamespace(InternalValue=7))
        self.assertIsInstance(result, CLRByteArray)
        self.assertEqual(result, [7, 255])

    def test_computed_nested_helpers_export_without_type_annotations(self) -> None:
        @compute
        def calculate(value: int) -> int:
            def increment(number: int) -> int:
                result: int = number + 1
                return result

            pending: int
            try:
                return pending
            except UnboundLocalError:
                pass
            result: int = increment(value)
            return result

        class Packet(Schema):
            source = Int8()
            result = Int8(calculate(source))

        artifacts = to_peach_artifacts(Packet, include_header=False)
        assert artifacts.python_fixup is not None
        module = ast.parse(artifacts.python_fixup)
        for node in ast.walk(module):
            self.assertNotIsInstance(node, ast.AnnAssign)
            if isinstance(node, ast.arg):
                self.assertIsNone(node.annotation)
            if isinstance(node, ast.FunctionDef):
                self.assertIsNone(node.returns)
        module.body = [node for node in module.body if isinstance(node, ast.ClassDef)]
        namespace: dict[str, object] = {}
        exec(compile(module, "<generated-fixup>", "exec"), namespace)
        generated = next(value for name, value in namespace.items() if name.startswith("Fixup_"))
        assert callable(generated)
        self.assertEqual(generated(None).fixup(SimpleNamespace(InternalValue=7)), 8)

    def test_computed_flags_paths_survive_optional_and_array_wrappers(self) -> None:
        @compute
        def parity(value: int) -> int:
            return value & 1

        @Flags(Int8, endian="big")
        class Header(Schema):
            source = Bit[7]()
            parity_bit = Bit[1](parity(source))

        class Packet(Schema):
            flags = Int8()
            plain = Header()
            optional = Optional[Header]()
            conditional = Optional[Header](when=flags != 0)
            repeated = Array[Header, 2]()
            optional_array = Optional[Array[Header, 2]()]()

        for model, expected_count in ((Header, 1), (Packet, 5)):
            for include_header in (False, True):
                with self.subTest(model=model, include_header=include_header):
                    root = ET.fromstring(to_peach_artifacts(model, include_header=include_header).xml)
                    for element in root.iter():
                        element.tag = element.tag.rsplit("}", 1)[-1]
                    data_model = root if root.tag == "DataModel" else root.find("DataModel")
                    assert data_model is not None
                    references = data_model.findall(".//Fixup/Param[@name='ref']")
                    self.assertEqual(len(references), expected_count)
                    for reference in references:
                        path = reference.get("value") or ""
                        target = data_model
                        for name in path.split("."):
                            child = target.find(f"*[@name='{name}']")
                            self.assertIsNotNone(child, path)
                            assert child is not None
                            target = child
                        self.assertEqual(target.tag, "Flag", path)
                        self.assertEqual(target.get("name"), "source")

    def test_computed_inside_nested_optional_uses_emitted_source_path(self) -> None:
        @compute
        def increment(value: int) -> int:
            return value + 1

        class Header(Schema):
            source = Int8()
            result = Int8(increment(source))

        class Envelope(Schema):
            flags = Int8()
            header = Optional[Header](when=flags != 0)

        class Packet(Schema):
            envelope = Optional[Envelope]()

        root = ET.fromstring(to_peach_artifacts(Packet, include_header=False).xml)
        reference = root.find(".//Fixup/Param[@name='ref']")
        assert reference is not None
        self.assertEqual(
            reference.get("value"),
            "envelope.envelope_internal.header.header_internal.source",
        )
        target = root
        for name in (reference.get("value") or "").split("."):
            child = target.find(f"*[@name='{name}']")
            self.assertIsNotNone(child)
            assert child is not None
            target = child
        self.assertEqual(target.tag, "Number")

    def test_computed_unconditional_optional_paths_match_choice_children(self) -> None:
        @compute
        def increment(value: int) -> int:
            return value + 1

        class Header(Schema):
            value = Int8()

        class Part(Schema):
            value = Optional[Int8]()
            header = Optional[Header]()

        class Packet(Schema):
            source = Int8()
            part = Part()
            scalar_result = Int8(increment(part.value))
            schema_result = Int8(increment(part.header.value))
            optional_result = Optional[Int8(increment(source))]()

        root = ET.fromstring(to_peach_artifacts(Packet, include_header=False).xml)
        references = root.findall(".//Fixup/Param[@name='ref']")
        self.assertEqual(len(references), 3)
        for reference in references:
            path = reference.get("value") or ""
            target = root
            for name in path.split("."):
                child = target.find(f"*[@name='{name}']")
                self.assertIsNotNone(child, path)
                assert child is not None
                target = child
            self.assertEqual(target.tag, "Number", path)

    def test_computed_optional_scalar_targets_value_not_wrapper(self) -> None:
        @compute
        def increment(value: int) -> int:
            return value + 1

        class Part(Schema):
            flags = Int8()
            value = Optional[Int8](when=flags != 0)

        class Packet(Schema):
            part = Part()
            result = Int8(increment(part.value))

        root = ET.fromstring(to_peach_artifacts(Packet, include_header=False).xml)
        reference = root.find("Number[@name='result']/Fixup/Param[@name='ref']")
        assert reference is not None
        target = root
        for name in (reference.get("value") or "").split("."):
            child = target.find(f"*[@name='{name}']")
            assert child is not None
            target = child
        self.assertEqual(target.tag, "Number")

    def test_inherited_computation_tracks_overrides_and_rejects_removed_source(self) -> None:
        @compute
        def increment(value: int) -> int:
            return value + 1

        class Base(Schema):
            source = Int8()
            result = Int8(increment(source))

        class Changed(Base):
            source = Int8(fixed(7))

        class Removed(Base):
            source = None

        for target in (Changed, Base(source=fixed(7))):
            with self.subTest(target=target):
                artifacts = to_peach_artifacts(target, include_header=False)
                root = ET.fromstring(artifacts.xml)
                source = root.find("Number[@name='source']")
                reference = root.find("Number[@name='result']/Fixup/Param[@name='ref']")
                assert source is not None and reference is not None
                self.assertEqual(source.get("value"), "7")
                self.assertEqual(reference.get("value"), "source")

        with self.assertRaisesRegex(ValueError, "computed argument.*absent"):
            to_peach_artifacts(Removed)

    def test_computed_rejects_detached_instance_with_matching_nested_path(self) -> None:
        @compute
        def increment(value: int) -> int:
            return value + 1

        @compute
        def checksum(value: bytearray) -> int:
            return sum(value)

        class Header(Schema):
            value = Int8()

        class Envelope(Schema):
            header = Header()

        detached = Envelope()

        class Packet(Schema):
            header = Header()
            result = Int8(increment(detached.header.value))

        with self.assertRaisesRegex(ValueError, "computed argument.*detached"):
            to_peach_artifacts(Packet)

        class DetachedBlock(Schema):
            header = Header()
            result = Int8(checksum(detached.header))

        with self.assertRaisesRegex(ValueError, "computed argument.*detached"):
            to_peach_artifacts(DetachedBlock)

        class Attached(Schema):
            envelope = detached
            result = Int8(increment(envelope.header.value))
            checksum_value = Int8(checksum(envelope.header))

        root = ET.fromstring(to_peach_artifacts(Attached, include_header=False).xml)
        references = root.findall(".//Fixup/Param[@name='ref']")
        self.assertEqual(
            [reference.get("value") for reference in references],
            ["envelope.header.value", "envelope.header"],
        )

    def test_computed_array_schema_uses_its_own_same_named_source(self) -> None:
        @compute
        def increment(value: int) -> int:
            return value + 1

        class Item(Schema):
            source = Int8()
            result = Int8(increment(source))

        class Packet(Schema):
            source = Int8()
            items = Array[Item, 2]()

        root = ET.fromstring(to_peach_artifacts(Packet, include_header=False).xml)
        reference = root.find("Block[@name='items']/Number[@name='result']/Fixup/Param[@name='ref']")
        assert reference is not None
        self.assertEqual(reference.get("value"), "items.source")

    def test_computed_wrapped_scalar_references_enclosing_source(self) -> None:
        @compute
        def increment(value: int) -> int:
            return value + 1

        class Packet(Schema):
            source = Int8()
            optional = Optional[Int8(increment(source))](when=source != 0)
            items = Array[Int8(increment(source)), 2]()
            matrix = Array[Array[Int8(increment(source)), 2](), 3]()
            choice = Union(number=Int8(increment(source)), other=Int8())

        artifacts = to_peach_artifacts(Packet, include_header=False)
        root = ET.fromstring(artifacts.xml)
        references = root.findall(".//Fixup/Param[@name='ref']")
        self.assertEqual(len(references), 4)
        self.assertEqual([r.get("value") for r in references], ["source"] * 4)

    def test_computed_optional_member_reference_targets_emitted_field(self) -> None:
        @compute
        def increment(value: int) -> int:
            return value + 1

        @compute
        def combine(left: int, right: int) -> int:
            return left + right

        class Header(Schema):
            value = Int8()

        class Part(Schema):
            flags = Int8()
            header = Optional[Header](when=flags != 0)

        class Packet(Schema):
            prefix = Int8()
            part = Part()
            result = Int8(increment(part.header.value))
            combined = Int8(combine(prefix, part.header.value))

        artifacts = to_peach_artifacts(Packet, include_header=False)
        root = ET.fromstring(artifacts.xml)
        reference = root.find("Number[@name='result']/Fixup/Param[@name='ref']")
        assert reference is not None
        path = reference.get("value") or ""
        target = root
        for part in path.split("."):
            child = target.find(f"*[@name='{part}']")
            self.assertIsNotNone(child, f"computed ref does not exist in XML: {path}")
            assert child is not None
            target = child
        self.assertEqual(target.tag, "Number")
        self.assertEqual(target.get("name"), "value")
        combined_ref = root.find("Number[@name='combined']/Fixup/Param[@name='ref']")
        assert combined_ref is not None
        self.assertEqual(combined_ref.get("value"), "Packet")
        assert artifacts.python_fixup is not None
        self.assertIn(f'element.find("{path}")', artifacts.python_fixup)

    def test_computed_rejects_detached_field_arguments(self) -> None:
        @compute
        def increment(value: int) -> int:
            return value + 1

        class Other(Schema):
            source = Int8()

        class Missing(Schema):
            result = Int8(increment(Other.source))

        class SameName(Schema):
            source = Int8()
            result = Int8(increment(Other.source))

        for target in (Missing, SameName):
            with self.subTest(target=target.__name__):
                with self.assertRaisesRegex(ValueError, "cannot resolve computed argument 'source'"):
                    to_peach_artifacts(target)

    def test_computed_generator_preserves_module_lookup_table(self) -> None:
        @compute
        def calculate(value: int) -> int:
            return sum(COMPUTED_TABLE[index] for index in range(value))

        class Packet(Schema):
            source = Int8()
            result = Int8(calculate(source))

        artifacts = to_peach_artifacts(Packet, include_header=False)
        assert artifacts.python_fixup is not None
        module = ast.parse(artifacts.python_fixup)
        module.body = [node for node in module.body if isinstance(node, ast.ClassDef)]
        namespace: dict[str, object] = {}
        exec(compile(module, "<generated-fixup>", "exec"), namespace)
        generated = next(value for name, value in namespace.items() if name.startswith("Fixup_"))
        assert callable(generated)
        self.assertEqual(generated(None).fixup(SimpleNamespace(InternalValue=3)), 11)

    def test_computed_capture_rejects_unexportable_helper(self) -> None:
        def helper(value: int) -> int:
            return value + 1

        @compute
        def calculate(value: int) -> int:
            return helper(value)

        class Packet(Schema):
            source = Int8()
            result = Int8(calculate(source))

        with self.assertRaisesRegex(TypeError, "computed capture 'helper'.*literal"):
            to_peach_artifacts(Packet)

    def test_computed_globals_and_distinct_closure_values_are_preserved(self) -> None:
        def make_calculation(offset: int):
            @compute
            def calculate(value: int) -> int:
                return (value + offset) & COMPUTED_MASK
            return calculate

        first = make_calculation(1)
        second = make_calculation(7)

        class Packet(Schema):
            source = Int8()
            first_value = Int8(first(source))
            second_value = Int8(second(source))

        artifacts = to_peach_artifacts(Packet, include_header=False)
        assert artifacts.python_fixup is not None
        module = ast.parse(artifacts.python_fixup)
        module.body = [node for node in module.body if isinstance(node, ast.ClassDef)]
        namespace: dict[str, object] = {}
        exec(compile(module, "<generated-fixup>", "exec"), namespace)
        root = ET.fromstring(artifacts.xml)
        class_names: list[str] = []
        for field, expected in (("first_value", 11), ("second_value", 1)):
            param = root.find(f"Number[@name='{field}']/Fixup/Param[@name='class']")
            assert param is not None
            class_name = (param.get("value") or "").split(".")[-1]
            class_names.append(class_name)
            generated = namespace[class_name]
            assert callable(generated)
            self.assertEqual(generated(None).fixup(SimpleNamespace(InternalValue=10)), expected)
        self.assertNotEqual(*class_names)

    def test_computed_parameter_names_do_not_shadow_conversion_builtins(self) -> None:
        @compute
        def total(int: int, float: int, str: int, bytes: int) -> int:
            return int + float + str + bytes

        @compute
        def increment(bytearray: bytearray) -> int:
            return sum(bytearray) + 1

        class Packet(Schema):
            first = Int8()
            second = Int8()
            third = Int8()
            fourth = Int8()
            sum_value = Int8(total(first, second, third, fourth))
            body = Block(data=Blob[1]())
            next_value = Int8(increment(body))

        artifacts = to_peach_artifacts(Packet)
        assert artifacts.python_fixup is not None
        module = ast.parse(artifacts.python_fixup)
        module.body = [node for node in module.body if isinstance(node, ast.ClassDef)]
        namespace: dict[str, object] = {}
        exec(compile(module, "<generated-fixup>", "exec"), namespace)
        total_class = next(value for name, value in namespace.items() if name.startswith("Fixup_total_"))
        increment_class = next(value for name, value in namespace.items() if name.startswith("Fixup_increment_"))
        assert callable(total_class) and callable(increment_class)
        values = {
            name: SimpleNamespace(InternalValue=value)
            for name, value in (("first", 1), ("second", 2), ("third", 3), ("fourth", 4))
        }
        self.assertEqual(total_class(None).fixup(SimpleNamespace(find=values.__getitem__)), 10)
        self.assertEqual(increment_class(None).fixup(SimpleNamespace(Bytes=lambda: b"\x01")), 2)

    def test_positional_only_computed_parameters_execute_correctly(self) -> None:
        @compute
        def increment(value: int, /) -> int:
            return value + 1

        class Packet(Schema):
            source = Int8()
            result = Int8(increment(source))

        artifacts = to_peach_artifacts(Packet)
        assert artifacts.python_fixup is not None
        module = ast.parse(artifacts.python_fixup)
        # Execute only the generated classes; importing Peach requires the SDK.
        module.body = [node for node in module.body if isinstance(node, ast.ClassDef)]
        namespace: dict[str, object] = {}
        exec(compile(module, "<generated-fixup>", "exec"), namespace)
        generated = next(value for name, value in namespace.items() if name.startswith("Fixup_"))
        self.assertTrue(callable(generated))
        assert callable(generated)
        instance = generated(None)
        self.assertEqual(instance.func(7), 8)
        self.assertEqual(instance.fixup(SimpleNamespace(InternalValue=7)), 8)

    def test_computed_parameter_named_self_does_not_collide_with_receiver(self) -> None:
        @compute
        def increment(self: int) -> int:
            return self + 1

        class Packet(Schema):
            source = Int8()
            result = Int8(increment(source))

        artifacts = to_peach_artifacts(Packet)
        assert artifacts.python_fixup is not None
        module = ast.parse(artifacts.python_fixup)
        module.body = [node for node in module.body if isinstance(node, ast.ClassDef)]
        namespace: dict[str, object] = {}
        exec(compile(module, "<generated-fixup>", "exec"), namespace)
        generated = next(value for name, value in namespace.items() if name.startswith("Fixup_"))
        assert callable(generated)
        instance = generated(None)
        self.assertEqual(instance.func(7), 8)
        self.assertEqual(instance.fixup(SimpleNamespace(InternalValue=7)), 8)

    def test_computed_element_parameter_does_not_shadow_fixup_lookup(self) -> None:
        @compute
        def add(element: int, _peach_argument_0: int) -> int:
            return element + _peach_argument_0

        class Packet(Schema):
            left = Int8()
            right = Int8()
            result = Int8(add(left, right))

        artifacts = to_peach_artifacts(Packet)
        assert artifacts.python_fixup is not None
        module = ast.parse(artifacts.python_fixup)
        module.body = [node for node in module.body if isinstance(node, ast.ClassDef)]
        namespace: dict[str, object] = {}
        exec(compile(module, "<generated-fixup>", "exec"), namespace)
        generated = next(value for name, value in namespace.items() if name.startswith("Fixup_"))
        assert callable(generated)
        values = {"left": SimpleNamespace(InternalValue=3), "right": SimpleNamespace(InternalValue=7)}
        self.assertEqual(generated(None).fixup(SimpleNamespace(find=values.__getitem__)), 10)

    def test_model_library_overrides_replace_computed_fields(self) -> None:
        @compute
        def increment(value: int) -> int:
            return value + 1

        class Part(Schema):
            source = Int8()
            result = Int8(increment(source))

        class Request(Schema):
            first = Part(result=fixed(9))
            second = Part()

        class Ack(Schema):
            kind = Int8(fixed(1))

        @PacketUnion
        class Packet(Schema):
            packet_union = Union(request=Request, ack=Ack)

        artifacts = to_peach_artifacts(Packet)
        root = ET.fromstring(artifacts.xml)
        ns = {"p": "http://peachfuzzer.com/2012/Peach"}
        first = root.find("p:DataModel[@name='Request']/p:Block[@name='first']", ns)
        second = root.find("p:DataModel[@name='Request']/p:Block[@name='second']", ns)
        assert first is not None and second is not None
        self.assertEqual(first.get("ref"), "Part")
        self.assertEqual(second.get("ref"), "Part")
        replacement = first.find("p:Number[@name='result']", ns)
        assert replacement is not None
        self.assertEqual(replacement.get("value"), "9")
        self.assertIsNone(replacement.find("p:Fixup", ns))
        self.assertEqual(len(second), 0)
        self.assertIsNotNone(root.find(
            "p:DataModel[@name='Part']/p:Number[@name='result']/p:Fixup", ns
        ))
        assert artifacts.python_fixup is not None
        self.assertEqual(artifacts.python_fixup.count("class Fixup_"), 1)

    def test_explicit_values_replace_computation(self) -> None:
        @compute
        def increment(value: int) -> int:
            return value + 1

        class Packet(Schema):
            source = Int8()
            result = Int8(increment(source))

        for override, token in ((fixed(9), "true"), (9, "false"), (Int8(9), "false")):
            with self.subTest(override=override):
                artifacts = to_peach_artifacts(Packet(result=override), include_header=False)
                root = ET.fromstring(artifacts.xml)
                result = root.find("Number[@name='result']")
                assert result is not None
                self.assertEqual(result.get("value"), "9")
                self.assertEqual(result.get("token"), token)
                self.assertIsNone(result.find("Fixup"))
                self.assertIsNone(artifacts.python_fixup)

        original = to_peach_artifacts(Packet, include_header=False)
        self.assertIsNotNone(ET.fromstring(original.xml).find("Number[@name='result']/Fixup"))
        self.assertIsNotNone(original.python_fixup)

    def test_override_removes_only_its_own_fixup(self) -> None:
        @compute
        def increment(value: int) -> int:
            return value + 1

        class Part(Schema):
            source = Int8()
            result = Int8(increment(source))

        class Packet(Schema):
            first = Part(result=fixed(9))
            second = Part()

        artifacts = to_peach_artifacts(Packet, include_header=False)
        root = ET.fromstring(artifacts.xml)
        self.assertIsNone(root.find("Block[@name='first']/Number[@name='result']/Fixup"))
        self.assertIsNotNone(root.find("Block[@name='second']/Number[@name='result']/Fixup"))
        assert artifacts.python_fixup is not None
        self.assertEqual(artifacts.python_fixup.count("class Fixup_"), 1)
