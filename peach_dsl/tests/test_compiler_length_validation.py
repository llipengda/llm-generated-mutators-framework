"""Validate constant lengths before they become Peach XML attributes."""

import unittest
from typing import Literal, cast
from xml.etree import ElementTree as ET

if __package__ in ("", "tests"):
    __package__ = "peach_dsl.tests"

from .. import (
    Array, Bit, Blob, Block, DecimalString, Double, Expr, Flags, Int8, Int64, Occurs, Optional, Schema, String,
    Union, compute, fixed, to_peach_artifacts, to_peach_data_model,
)

CONSTRAINT_LIMITS = (1, 3, 7)


class LengthValidationTests(unittest.TestCase):
    def test_constant_lengths_cannot_overflow_peach_bit_capacity(self) -> None:
        for field in (Blob[2**60](), Blob[2**63](length_type="bits"),
                      String[2**60](), String[2**63](length_type="bits"),
                      Block[2**60](data=Int8())):
            packet = type("OverflowingCapacity", (Schema,), {"data": field})
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, "length exceeds Peach's maximum"):
                    to_peach_data_model(packet, include_header=False)

        for field in (Blob[2**60 - 1](), Blob[2**63 - 1](length_type="bits"),
                      Block[2**60 - 1](data=Int8())):
            packet = type("MaximumCapacity", (Schema,), {"data": field})
            with self.subTest(field=field):
                self.assertIsNotNone(ET.fromstring(to_peach_data_model(packet, include_header=False)))

    def test_computed_and_literal_strings_allow_representable_capacities(self) -> None:
        @compute
        def identity(value: str) -> str:
            return value

        cases = (
            (8, "ascii", "bits", False),
            (4, "utf16", "bytes", True),
            (4, "utf16be", "bytes", False),
            (8, "utf32", "bytes", True),
            (8, "utf32be", "bytes", False),
            (32, "utf16", "bits", False),
            (64, "utf32", "bits", False),
            (1, "utf16", "chars", False),
        )
        for length, encoding, units, terminated in cases:
            for computed in (False, True):
                source = String("A")
                text = String[length](identity(source) if computed else "'A'", type=encoding,
                                      length_type=units, null_terminated=terminated,
                                      value_type="string" if computed else "literal")
                packet = type("ValidStringCapacity", (Schema,), {"source": source, "text": text})
                with self.subTest(length=length, encoding=encoding, units=units, computed=computed):
                    root = ET.fromstring(to_peach_artifacts(packet, include_header=False).xml)
                    self.assertIsNotNone(root.find("String[@name='text']"))

    def test_string_encoding_capacity_is_checked_for_computed_and_literal_values(self) -> None:
        @compute
        def identity(value: str) -> str:
            return value

        cases = (
            (7, "ascii", "bits", False),
            (3, "utf16", "bytes", False),
            (3, "utf16be", "bytes", False),
            (6, "utf32", "bytes", False),
            (6, "utf32be", "bytes", False),
            (24, "utf16", "bits", False),
            (48, "utf32", "bits", False),
            (0, "utf8", "bytes", True),
        )
        for length, encoding, units, terminated in cases:
            for value_kind in ("computed", "literal"):
                for wrapper in ("plain", "optional", "array", "union"):
                    source = String("A")
                    value = identity(source) if value_kind == "computed" else "'A'"
                    field = String[length](value, type=encoding, length_type=units,
                                           null_terminated=terminated,
                                           value_type="literal" if value_kind == "literal" else "string")
                    if wrapper == "optional":
                        field = Optional[field]()
                    elif wrapper == "array":
                        field = Array[field, 2]()
                    elif wrapper == "union":
                        field = Union(text=field, other=Int8())
                    packet = type("InvalidStringCapacity", (Schema,), {"source": source, "text": field})
                    with self.subTest(length=length, encoding=encoding, units=units,
                                      value_kind=value_kind, wrapper=wrapper):
                        with self.assertRaisesRegex(ValueError, "String.*length"):
                            to_peach_artifacts(packet, include_header=False)

    def test_array_occurrence_counts_respect_peach_int32_limit(self) -> None:
        for value in (2**31, 2**63, 10**30):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "occurrence.*2147483647"):
                    Occurs(0, value)
                with self.assertRaisesRegex(ValueError, "occurrence.*2147483647"):
                    Occurs(value, value)
                packet = type("Packet", (Schema,), {"items": Array[Int8, value]()})
                with self.assertRaisesRegex(ValueError, "array count.*2147483647"):
                    to_peach_data_model(packet, include_header=False)

        class Folded(Schema):
            count = Int64(fixed(2**31 - 1))
            items = Array[Int8, count + 1]()

        with self.assertRaisesRegex(ValueError, "array count.*2147483647"):
            to_peach_data_model(Folded, include_header=False)

        for count in (2**31 - 1, Occurs(0, 2**31 - 1), None):
            packet = type("Boundary", (Schema,), {"items": Array[Int8, count]()})
            root = ET.fromstring(to_peach_data_model(packet, include_header=False))
            self.assertIsNotNone(root.find("Number[@name='items']"))

    def test_direct_decimal_relation_writeback_uses_native_integer_range(self) -> None:
        for kind in ("size", "count"):
            for width in (None, 6):
                amount = DecimalString() if width is None else DecimalString[width]()
                target = Blob[amount]() if kind == "size" else Array[Int8, amount]()
                packet = type("Packet", (Schema,), {"amount": amount, "body": target})
                relation = ET.fromstring(to_peach_data_model(packet, include_header=False)).find("String/Relation")
                assert relation is not None
                expression = relation.attrib["expressionSet"]
                with self.subTest(kind=kind, width=width):
                    if width is None:
                        self.assertEqual(expression, f"str({kind})")
                        boundary = 2**60 - 1 if kind == "size" else 2**31 - 1
                        self.assertEqual(eval(expression, {kind: boundary}), str(boundary))
                    else:
                        self.assertNotIn("signed 64-bit", expression)
                        self.assertEqual(eval(expression, {kind: 42}), "000042")
                        with self.assertRaisesRegex(OverflowError, "declared length"):
                            eval(expression, {kind: 10**width})

    def test_decimal_relation_writeback_rejects_values_that_cannot_be_read(self) -> None:
        for boundary in (-(2**63), 2**63 - 1):
            for decimal_type in (DecimalString, DecimalString[30]):
                for kind in ("size", "count"):
                    amount = decimal_type()
                    length = amount - boundary if boundary > 0 else boundary - amount
                    target = Blob[length]() if kind == "size" else Array[Int8, length]()
                    packet = type("Packet", (Schema,), {"amount": amount, "body": target})
                    relation = ET.fromstring(to_peach_data_model(packet, include_header=False)).find("String/Relation")
                    assert relation is not None
                    expression = relation.attrib["expressionSet"]
                    with self.subTest(kind=kind, decimal_type=decimal_type, boundary=boundary):
                        self.assertEqual(int(eval(expression, {kind: 0})), boundary)
                        with self.assertRaisesRegex(OverflowError, "signed 64-bit"):
                            eval(expression, {kind: 1})

    def test_decimal_relation_defaults_must_fit_peach_signed_integer_parser(self) -> None:
        for value in (-(2**63) - 1, 2**63, 2**80):
            for kind in ("size", "count"):
                amount = DecimalString(value)
                target = Blob[amount - value]() if kind == "size" else Array[Int8, amount - value]()
                packet = type("Packet", (Schema,), {"amount": amount, "body": target})
                with self.subTest(value=value, kind=kind):
                    with self.assertRaisesRegex(ValueError, "DecimalString.*relation.*signed 64-bit"):
                        to_peach_data_model(packet, include_header=False)

            standalone = type("Standalone", (Schema,), {"amount": DecimalString(value)})
            self.assertIn(str(value), to_peach_data_model(standalone, include_header=False))

        for value in (-(2**63), 2**63 - 1):
            amount = DecimalString(value)
            packet = type("Boundary", (Schema,), {"amount": amount, "body": Blob[amount - value]()})
            root = ET.fromstring(to_peach_data_model(packet, include_header=False))
            self.assertIsNotNone(root.find("String/Relation"))

    def test_decimal_relation_writeback_preserves_terminator_and_width(self) -> None:
        for terminated in (False, True):
            class Packet(Schema):
                size = DecimalString[6](null_terminated=terminated)
                body = Blob[size + 2]()
                count = DecimalString[6](null_terminated=terminated)
                items = Array[Int8, count]()

            root = ET.fromstring(to_peach_data_model(Packet, include_header=False))
            for name, variable, measured in (("size", "size", 44), ("count", "count", 42)):
                relation = root.find(f"String[@name='{name}']/Relation")
                assert relation is not None
                expression = relation.get("expressionSet")
                self.assertIsNotNone(expression)
                assert expression is not None
                with self.subTest(terminated=terminated, relation=name):
                    expected = "00042\0" if terminated else "000042"
                    self.assertEqual(eval(expression, {variable: measured}), expected)
                    with self.assertRaises(OverflowError):
                        eval(expression, {variable: 10**10})

    def test_constraint_captures_module_and_closure_constants(self) -> None:
        for threshold in (2, 5):
            constraint = lambda value: value < threshold and any(value == limit for limit in CONSTRAINT_LIMITS)
            packet = type("Packet", (Schema,), {"amount": Int8(constraint=constraint)})
            element = ET.fromstring(to_peach_data_model(packet, include_header=False)).find("Number")
            assert element is not None
            expression = element.get("constraint")
            assert expression is not None
            for value in (0, 1, 3, 7):
                with self.subTest(threshold=threshold, value=value):
                    self.assertEqual(eval(expression, {"value": value}), constraint(value))

    def test_constraint_rejects_unexportable_captures_without_executing_them(self) -> None:
        calls: list[int] = []

        def helper(value: int) -> bool:
            calls.append(value)
            return True

        constraint = lambda value: helper(value)
        with self.assertRaisesRegex(TypeError, "constraint capture 'helper'.*literal"):
            Int8(constraint=constraint)
        self.assertEqual(calls, [])

    def test_constraint_snapshots_captures_without_shadowing_field_conversion(self) -> None:
        value = 2
        int = 8
        constraint = lambda amount: value < amount < int
        field = DecimalString(constraint=constraint)
        value, int = 20, 80
        packet = type("Packet", (Schema,), {"amount": field})
        element = ET.fromstring(to_peach_data_model(packet, include_header=False)).find("String")
        assert element is not None
        expression = element.get("constraint")
        assert expression is not None
        for text, expected in (("1\0", False), ("3\0", True), ("9\0", False)):
            with self.subTest(text=text):
                self.assertEqual(eval(expression, {"value": text}), expected)

    def test_nested_constraints_preserve_short_circuit_conversion(self) -> None:
        constraints = (
            (lambda value: True or all(value > limit for limit in (0, 1)), True),
            (lambda value: False and any(value > limit for limit in (0, 1)), False),
            (lambda value: all(value > limit for limit in ()), True),
            (lambda value: (lambda: True)(), True),
        )
        for constraint, expected in constraints:
            field = DecimalString(constraint=constraint)
            packet = type("Packet", (Schema,), {"amount": field})
            element = ET.fromstring(to_peach_data_model(packet, include_header=False)).find("String")
            assert element is not None
            expression = element.get("constraint")
            assert expression is not None
            with self.subTest(line=constraint.__code__.co_firstlineno):
                self.assertEqual(eval(expression, {"value": "not a number"}), expected)

        class Required(Schema):
            amount = DecimalString(constraint=lambda value: all(value > limit for limit in (0,)))

        element = ET.fromstring(to_peach_data_model(Required, include_header=False)).find("String")
        assert element is not None
        expression = element.get("constraint")
        assert expression is not None
        with self.assertRaises(ValueError):
            eval(expression, {"value": "not a number"})

    def test_constraint_accepts_one_positional_only_parameter(self) -> None:
        constraints = (
            lambda number, /: number > 1,
            lambda number=42, /: number > 1,
            lambda number=42: number > 1,
        )
        for constraint in constraints:
            packet = type("Packet", (Schema,), {"amount": Int8(constraint=constraint)})
            field = ET.fromstring(to_peach_data_model(packet, include_header=False)).find("Number")
            assert field is not None
            expression = field.get("constraint")
            assert expression is not None
            for value in (0, 1, 2):
                self.assertEqual(eval(expression, {"value": value}), constraint(value))

    def test_constraint_rejects_parameters_that_cannot_be_supplied(self) -> None:
        constraints = (
            lambda: True,
            lambda value, threshold: value > threshold,
            lambda value, threshold=1: value > threshold,
            lambda value, /, threshold=1: value > threshold,
            lambda value, *, threshold=1: value > threshold,
            lambda value, *others: value > len(others),
            lambda value, **options: value > len(options),
            lambda *, value: value > 1,
        )
        for constraint in constraints:
            with self.subTest(line=constraint.__code__.co_firstlineno):
                with self.assertRaisesRegex(TypeError, "exactly one positional parameter"):
                    Int8(constraint=constraint)

    def test_same_line_constraint_lambdas_keep_distinct_expressions(self) -> None:
        positive, negative = (lambda value: value > 0), (lambda value: value < 0)
        inner = (lambda: lambda value: value == 0)()
        中文 = "source columns use UTF-8 bytes"; unicode_constraint = lambda value: value != 0
        multiline_positive, multiline_negative = (lambda value: (
            value > 0
        )), (lambda value: (
            value < 0
        ))
        for constraint in (positive, negative, inner, unicode_constraint, multiline_positive, multiline_negative):
            packet = type("Packet", (Schema,), {"amount": Int8(constraint=constraint)})
            field = ET.fromstring(to_peach_data_model(packet, include_header=False)).find("Number")
            assert field is not None
            expression = field.get("constraint")
            assert expression is not None
            for value in (-1, 0, 1):
                with self.subTest(constraint=expression, value=value):
                    self.assertEqual(eval(expression, {"value": value}), constraint(value))

    def test_ambiguous_constraint_source_without_columns_is_rejected(self) -> None:
        positive, negative = (lambda value: value > 0), (lambda value: value < 0)
        negative.__code__ = negative.__code__.replace(co_linetable=b"")
        with self.assertRaisesRegex(TypeError, "cannot distinguish constraint lambdas"):
            Int8(constraint=negative)

    def test_constraints_preserve_nested_variable_scopes(self) -> None:
        constraints = (
            lambda value: value > 1 and all(value >= 0 for value in (1, 2)),
            lambda amount: all(amount > value for value in (0, 1)),
            lambda value: (lambda value=value: value > 1)(),
            lambda amount: (lambda value: amount > value)(1),
            lambda value: all(value > 1 for value in (value,)),
            lambda amount: all(amount > right for value in (0, 1) for right in (value,)),
            lambda amount: all([amount > value for value in (0, 1)]),
            lambda amount: {amount > value for value in (0, 1)} == {True},
            lambda amount: all({value: amount > value for value in (0, 1)}.values()),
            lambda amount: all(amount > value for value, _peach_constraint_value in ((0, 1), (1, 2))),
            lambda amount: (lambda *, value=1: amount > value)(),
            lambda amount: (lambda value: amount > value)(value=1),
            lambda amount: (lambda fn: fn(value=1))(lambda value: amount > value),
            lambda amount: all(amount > int for int in (0, 1)),
            lambda amount: (lambda int: amount > int)(1),
        )
        for constraint in constraints:
            for decimal_type in (DecimalString, DecimalString[6]):
                with self.subTest(line=constraint.__code__.co_firstlineno, kind=decimal_type):
                    packet = type("Packet", (Schema,), {"amount": decimal_type(constraint=constraint)})
                    root = ET.fromstring(to_peach_data_model(packet, include_header=False))
                    field = root.find("String")
                    assert field is not None
                    expression = field.get("constraint")
                    assert expression is not None
                    for number in (0, 1, 2, 42):
                        self.assertEqual(eval(expression, {"value": str(number) + "\0"}), constraint(number))

    def test_raw_and_text_constraints_preserve_nested_variable_scopes(self) -> None:
        class Packet(Schema):
            amount = DecimalString(constraint="value > 1 and all(value > 0 for value in (1, 2))")
            text = String(constraint=lambda text: all(text != value for value in ("a", "b")))

        root = ET.fromstring(to_peach_data_model(Packet, include_header=False))
        for name, values in (
            ("amount", (("0\0", False), ("2\0", True))),
            ("text", (("a", False), ("b", False), ("c", True))),
        ):
            field = root.find(f"String[@name='{name}']")
            assert field is not None
            expression = field.get("constraint")
            assert expression is not None
            for value, expected in values:
                with self.subTest(name=name, value=value):
                    self.assertEqual(eval(expression, {"value": value}), expected)

    def test_decimal_constraints_and_conditions_accept_null_terminated_text(self) -> None:
        for decimal_type in (DecimalString, DecimalString[6]):
            class Packet(Schema):
                amount = decimal_type(null_terminated=True, constraint=lambda value: 0 <= value <= 999)
                extra = Optional[Int8](when=amount > 0)

            root = ET.fromstring(to_peach_data_model(Packet, include_header=False))
            amount = root.find("String[@name='amount']")
            optional = root.find("Optional[@name='extra']")
            assert amount is not None and optional is not None
            constraint = amount.get("constraint")
            condition = optional.get("expression")
            assert constraint is not None and condition is not None
            for text, allowed, present in (("00042\0", True, True), ("-0042\0", False, False),
                                           ("00000\0", True, False), ("42", True, True)):
                with self.subTest(text=text):
                    self.assertEqual(eval(constraint, {"value": text}), allowed)
                    self.assertEqual(eval(condition, {"value": text}), present)
            for text in ("\0", "4\x002\0"):
                for expression in (constraint, condition):
                    with self.subTest(text=text, expression=expression):
                        with self.assertRaises(ValueError):
                            eval(expression, {"value": text})

    def test_decimal_integer_padding_respects_encoding_units_and_terminator(self) -> None:
        for encoding, units, width, terminated in (
            ("ascii", "bytes", 5, False), ("utf8", "bits", 40, False),
            ("utf16", "bytes", 10, False), ("utf16be", "chars", 5, False),
            ("utf32", "bits", 160, False), ("ascii", "chars", 6, True),
        ):
            with self.subTest(encoding=encoding, units=units, terminated=terminated):
                class Packet(Schema):
                    amount = DecimalString[width](42, type=encoding, length_type=units, null_terminated=terminated)
                root = ET.fromstring(to_peach_data_model(Packet, include_header=False))
                amount = root.find("String[@name='amount']")
                assert amount is not None
                self.assertEqual(amount.get("value"), "00042")

        for field in (DecimalString[2](-42), DecimalString[3](42, type="utf16"),
                      DecimalString[7](0, length_type="bits"), DecimalString[1](0, null_terminated=True)):
            target = type("InvalidDecimal", (Schema,), {"amount": field})
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, "DecimalString"):
                    to_peach_data_model(target, include_header=False)

    def test_fixed_width_decimal_integer_values_preserve_the_number(self) -> None:
        for value, expected in ((42, "00042"), (-42, "-0042"), (0, "00000")):
            for token in (False, True):
                with self.subTest(value=value, token=token):
                    class Packet(Schema):
                        amount = DecimalString[5](fixed(value) if token else value, type="ascii", pad_character="0")
                    root = ET.fromstring(to_peach_data_model(Packet, include_header=False))
                    amount = root.find("String[@name='amount']")
                    assert amount is not None
                    self.assertEqual(amount.get("value"), expected)
                    self.assertEqual(amount.get("token"), str(token).lower())

    def test_length_references_validate_effective_field_type_after_replacement(self) -> None:
        class Header(Schema):
            length = Int8()

        class Direct(Schema):
            length = Int8()
            payload = Blob[length]()

        class Nested(Schema):
            header = Header()
            payload = Blob[header.length + 1]()

        class NestedDirect(Schema):
            header = Header()
            payload = Blob[header.length]()

        for replacement in (String(), Blob[1](), Double()):
            inherited = type("Inherited", (Direct,), {"length": replacement})
            for model in (
                Direct(length=replacement), inherited,
                Nested(header=Header(length=replacement)),
                NestedDirect(header=Header(length=replacement)),
            ):
                with self.subTest(replacement=replacement.kind, model=model):
                    with self.assertRaisesRegex(TypeError, "length.*integer field"):
                        to_peach_data_model(model, include_header=False)

        # DecimalString is logically an integer and remains a valid replacement.
        for model in (Direct(length=DecimalString()), Nested(header=Header(length=DecimalString()))):
            root = ET.fromstring(to_peach_data_model(model, include_header=False))
            self.assertIsNotNone(root.find(".//String[@name='length']/Relation"))

    def test_dynamic_lengths_reject_definite_arithmetic_errors(self) -> None:
        for expression, message in (
            (lambda n: n // 0, "zero"),
            (lambda n: n % 0, "zero"),
            (lambda n: n << -1, "shift"),
            (lambda n: n >> -1, "shift"),
            (lambda n: n // Expr("-", 3, 3), "zero"),
            (lambda n: n + Expr("//", 1, 0), "zero"),
        ):
            for is_array in (False, True):
                with self.subTest(expression=expression, is_array=is_array):
                    length = Int8()
                    count = expression(length)
                    target = type("Packet", (Schema,), {
                        "length": length,
                        "payload": Array[Int8, count]() if is_array else Blob[count](),
                    })
                    with self.assertRaisesRegex(ValueError, message):
                        to_peach_data_model(target, include_header=False)

    def test_dynamic_divisor_is_checked_only_when_fixed(self) -> None:
        class Packet(Schema):
            divisor = Int8(0)
            payload = Blob[8 // divisor]()

        to_peach_data_model(Packet, include_header=False)
        with self.assertRaisesRegex(ValueError, "zero"):
            to_peach_data_model(Packet(divisor=fixed(0)), include_header=False)
        root = ET.fromstring(to_peach_data_model(Packet(divisor=fixed(2)), include_header=False))
        payload = root.find("Blob[@name='payload']")
        assert payload is not None
        self.assertEqual(payload.get("length"), "4")

    def test_dynamic_lengths_reject_noninteger_expression_operands(self) -> None:
        expressions = (
            lambda n: n + 0.5,
            lambda n: n * 2.0,
            lambda n: (n + 1) * True,
            lambda n: n + "2",
            lambda n: n == 2,
            lambda n: (n > 0) + 1,
        )
        for expression in expressions:
            for container in ("blob", "array"):
                with self.subTest(expression=expression, container=container):
                    length = Int8()
                    count = expression(length)
                    target = type("Packet", (Schema,), {
                        "length": length,
                        "payload": Blob[count]() if container == "blob" else Array[Int8, count](),
                    })
                    for model in (target, target(length=fixed(2))):
                        with self.subTest(model=model):
                            with self.assertRaisesRegex(TypeError, "length.*integer"):
                                to_peach_data_model(model, include_header=False)

    def test_flags_reject_boolean_width_at_declaration(self) -> None:
        with self.assertRaisesRegex(ValueError, "positive width"):
            @Flags(Int8, endian="big")
            class Header(Schema):
                enabled = Bit[True]()
                reserved = Bit[7]()

    def test_double_rejects_invalid_storage_widths(self) -> None:
        for width in (True, False, 0, -1, 16, 128, 32.0, "64"):
            with self.subTest(width=width):
                class Packet(Schema):
                    value = Double(size=cast(Literal[32, 64], width))

                with self.assertRaisesRegex((TypeError, ValueError), "size must be"):
                    to_peach_data_model(Packet, include_header=False)

    def test_double_valid_storage_widths_survive_overrides(self) -> None:
        class Packet(Schema):
            value = Double(size=32)

        for target, width in ((Packet, "32"), (Packet(value=Double(size=64)), "64")):
            with self.subTest(width=width):
                root = ET.fromstring(to_peach_data_model(target, include_header=False))
                field = root.find("Double[@name='value']")
                assert field is not None
                self.assertEqual(field.get("size"), width)

    def test_flags_preserve_explicit_field_options(self) -> None:
        @Flags(Int8, endian="big")
        class Header(Schema):
            first = Bit[3](
                fixed(5), mutable=False, token=True, field_id="flag_id",
                value_type="string",
            )
            second = Bit[5](1, token=True)

        root = ET.fromstring(to_peach_data_model(Header, include_header=False))
        first = root.find("Flags/Flag[@name='first']")
        second = root.find("Flags/Flag[@name='second']")
        assert first is not None and second is not None
        self.assertEqual(first.get("mutable"), "false")
        self.assertEqual(first.get("token"), "true")
        self.assertEqual(first.get("fieldId"), "flag_id")
        self.assertNotIn("constraint", first.attrib)
        self.assertEqual(first.get("valueType"), "string")
        self.assertEqual(second.get("token"), "true")

    def test_flags_reject_member_constraints_but_plain_bits_allow_them(self) -> None:
        for constraint in ("value < 6", lambda value: value < 6):
            class Header(Schema):
                mode = Bit[3](constraint=constraint)
                reserved = Bit[5]()

            with self.subTest(constraint=constraint):
                root = ET.fromstring(to_peach_data_model(Header, include_header=False))
                mode = root.find("Number[@name='mode']")
                assert mode is not None
                self.assertIn("constraint", mode.attrib)
                with self.assertRaisesRegex(ValueError, r"^Flags field Header\.mode cannot use constraint$"):
                    Flags(Int8, endian="big")(Header)

    def test_flags_overrides_and_subclasses_cannot_add_constraints(self) -> None:
        @Flags(Int8, endian="big")
        class Header(Schema):
            mode = Bit[3]()
            reserved = Bit[5]()

        for constraint in ("value < 6", lambda value: value < 6):
            replacement = Bit[3](constraint=constraint)

            class Packet(Schema):
                header = Header(mode=replacement)

            class Derived(type(Header)):
                mode = replacement

            for target in (Header(mode=replacement), Packet, Derived):
                with self.subTest(constraint=constraint, target=target):
                    with self.assertRaisesRegex(ValueError, r"^Flags field \w+\.mode cannot use constraint$"):
                        to_peach_data_model(target, include_header=False)

    def test_computed_flag_emits_fixup_and_script(self) -> None:
        @compute
        def low_bit(value: int) -> int:
            return value & 1

        @Flags(Int8, endian="big")
        class Header(Schema):
            source = Bit[7]()
            parity = Bit[1](low_bit(source))

        class Packet(Schema):
            header = Header()

        for target, container in ((Header, "value"), (Packet, "header")):
            with self.subTest(container=container):
                artifacts = to_peach_artifacts(target, include_header=False)
                root = ET.fromstring(artifacts.xml)
                fixup = root.find("Flags/Flag[@name='parity']/Fixup[@class='ScriptFixup']")
                assert fixup is not None
                reference = fixup.find("Param[@name='ref']")
                assert reference is not None
                self.assertEqual(reference.get("value"), f"{container}.source")
                self.assertIsNotNone(artifacts.python_fixup)
                assert artifacts.python_fixup is not None
                self.assertIn("return value & 1", artifacts.python_fixup)

    def test_flags_override_must_still_fill_storage(self) -> None:
        @Flags(Int8, endian="big")
        class Header(Schema):
            first = Bit[4]()
            second = Bit[4]()

        class Packet(Schema):
            header = Header(first=Bit[2]())

        for target in (Header(first=Bit[2]()), Packet):
            with self.subTest(target=target):
                with self.assertRaisesRegex(ValueError, "covers 6 of 8 bits"):
                    to_peach_data_model(target, include_header=False)

    def test_root_flags_preserve_packed_layout(self) -> None:
        for endian in ("big", "little"):
            @Flags(Int8, endian=endian)
            class Header(Schema):
                first = Bit[3](fixed(5))
                second = Bit[5]()

            for include_header in (False, True):
                with self.subTest(endian=endian, include_header=include_header):
                    root = ET.fromstring(to_peach_data_model(
                        Header, name="Packed", include_header=include_header
                    ))
                    namespace = "{http://peachfuzzer.com/2012/Peach}" if include_header else ""
                    model = root.find(f"{namespace}DataModel") if include_header else root
                    assert model is not None
                    self.assertEqual(model.get("name"), "Packed")
                    self.assertEqual(len(model), 1)
                    flags = model.find(f"{namespace}Flags[@name='value']")
                    assert flags is not None
                    self.assertEqual(flags.get("size"), "8")
                    self.assertEqual(flags.get("endian"), endian)
                    self.assertEqual(
                        [(f.get("name"), f.get("size"), f.get("position")) for f in flags],
                        [("first", "3", "0"), ("second", "5", "3")],
                    )
                    self.assertEqual(flags[0].get("value"), "5")
                    self.assertEqual(flags[0].get("token"), "true")

    def test_flags_overrides_cannot_bypass_bit_validation(self) -> None:
        @Flags(Int8, endian="big")
        class Header(Schema):
            enabled = Bit[1]()
            reserved = Bit[7]()

        for label, replacement in (("zero width", Bit[0]()), ("blob", Blob[1]())):
            with self.subTest(label=label):
                class Packet(Schema):
                    header = Header(enabled=replacement)

                with self.assertRaises((TypeError, ValueError)):
                    to_peach_data_model(Packet, include_header=False)

    def test_flags_valid_width_override_recalculates_positions(self) -> None:
        @Flags(Int8, endian="big")
        class Header(Schema):
            enabled = Bit[1]()
            reserved = Bit[7]()

        class Packet(Schema):
            header = Header(enabled=Bit[2](), reserved=Bit[6]())

        root = ET.fromstring(to_peach_data_model(Packet, include_header=False))
        self.assertIsNotNone(root.find(
            "Flags/Flag[@name='enabled'][@size='2'][@position='0']"
        ))
        self.assertIsNotNone(root.find(
            "Flags/Flag[@name='reserved'][@size='6'][@position='2']"
        ))

    def test_occurrence_bounds_require_integers(self) -> None:
        for value in (True, False, 0.5, float("nan"), float("inf")):
            for bound in ("min_occurs", "max_occurs"):
                with self.subTest(value=value, bound=bound):
                    # Exercise runtime validation despite the static int API.
                    invalid = cast(int, value)
                    with self.assertRaisesRegex(TypeError, bound):
                        if bound == "min_occurs":
                            Occurs(invalid, 3)
                        else:
                            Occurs(0, invalid)

    def test_zero_occurrence_range_remains_valid(self) -> None:
        class Packet(Schema):
            items = Array[Int8, Occurs(0, 0)]()

        root = ET.fromstring(to_peach_data_model(Packet, include_header=False))
        self.assertIsNotNone(root.find(
            "Number[@name='items'][@minOccurs='0'][@maxOccurs='0']"
        ))

    def test_invalid_literal_lengths_and_counts_are_rejected(self) -> None:
        for value in (-1, True, False):
            for kind in ("blob", "array", "block"):
                with self.subTest(value=value, kind=kind):
                    with self.assertRaises((ValueError, TypeError)):
                        member = {
                            "blob": lambda: Blob[value](),
                            "array": lambda: Array[Int8, value](),
                            "block": lambda: Block[value](data=Int8()),
                        }[kind]()

                        class Packet(Schema):
                            data = member

                        to_peach_data_model(Packet, include_header=False)

    def test_negative_folded_length_is_rejected(self) -> None:
        class Packet(Schema):
            size = Int8()
            data = Blob[size - 4]()

        with self.assertRaisesRegex(ValueError, "non-negative"):
            to_peach_data_model(Packet(size=fixed(3)), include_header=False)

    def test_zero_length_and_zero_count_remain_valid(self) -> None:
        class Packet(Schema):
            data = Blob[0]()
            items = Array[Int8, 0]()

        root = ET.fromstring(to_peach_data_model(Packet, include_header=False))
        self.assertIsNotNone(root.find("Blob[@name='data'][@length='0']"))
        self.assertIsNotNone(root.find("Number[@name='items'][@occurs='0']"))
