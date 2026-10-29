"""Size/count relation expressions across a reusable header and nested body."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from xml.etree import ElementTree as ET

if __package__ in ("", "tests"):
    __package__ = "peach_dsl.tests"

from .. import Array, Blob, Block, Int8, Int16, Int64, Optional, Schema, String, Union, fixed, to_peach_data_model


CASES = (
    ("size_add", "size", lambda n: n + 2, "(size + 2)", "(size - 2)"),
    ("count_add", "count", lambda n: n + 2, "(count + 2)", "(count - 2)"),
    ("size_subtract", "size", lambda n: n - 2, "(size - 2)", "(size + 2)"),
    ("count_subtract", "count", lambda n: n - 2, "(count - 2)", "(count + 2)"),
    ("size_reverse_add", "size", lambda n: 2 + n, "(2 + size)", "(size - 2)"),
    ("count_reverse_add", "count", lambda n: 2 + n, "(2 + count)", "(count - 2)"),
    ("size_reverse_subtract", "size", lambda n: 20 - n,
     "(20 - size)", "(20 - size)"),
    ("count_reverse_subtract", "count", lambda n: 20 - n,
     "(20 - count)", "(20 - count)"),
    ("size_chained_add_subtract", "size", lambda n: (n + 5) - 4,
     "((size + 5) - 4)", "(size - 1)"),
    ("count_chained_subtract_add", "count", lambda n: (n - 3) + 7,
     "((count - 3) + 7)", "(count - 4)"),
    ("size_chained_scale", "size", lambda n: (n + 2) * 3,
     "((size + 2) * 3)",
     "((lambda dividend: dividend // 3 if dividend % 3 == 0 else "
     "(_ for _ in ()).throw(ValueError('relation length is not divisible by its scale')))(size) - 2)"),
    ("count_chained_reverse_subtract", "count", lambda n: 20 - (n + 2),
     "(20 - (count + 2))", "(18 - count)"),
)


class RelationExpressionTests(unittest.TestCase):
    def test_dynamic_relations_emit_only_declared_expressions(self) -> None:
        for kind, units in (
            ("size", "bytes"),
            ("size", "bits"),
            ("count", "bytes"),
        ):
            for arithmetic in (False, True):
                source = Int64()
                amount = source - 2 if arithmetic else source
                target = Blob[amount](length_type=units) if kind == "size" else Array[Int8, amount]()
                packet = type("BoundedRelation", (Schema,), {"source": source, "target": target})
                relation = ET.fromstring(to_peach_data_model(packet, include_header=False)).find("Number/Relation")
                assert relation is not None
                with self.subTest(kind=kind, units=units, arithmetic=arithmetic):
                    self.assertEqual(
                        relation.get("expressionGet"),
                        f"({kind} - 2)" if arithmetic else None,
                    )
                    self.assertEqual(
                        relation.get("expressionSet"),
                        f"({kind} + 2)" if arithmetic else None,
                    )

    def test_dynamic_character_lengths_convert_fixed_width_encodings(self) -> None:
        for encoding, width in (("ascii", 1), ("utf16", 2), ("utf16be", 2)):
            for arithmetic in (False, True):
                for kind in ("plain", "optional", "conditional", "array", "union"):
                    class Packet(Schema):
                        count = Int8(3)
                        field = String[count + 1 if arithmetic else count](type=encoding, length_type="chars")
                        if kind == "plain":
                            text = field
                        elif kind == "optional":
                            text = Optional[field]()
                        elif kind == "conditional":
                            text = Optional[field](when=count != 0)
                        elif kind == "array":
                            text = Array[field, 2]()
                        else:
                            text = Union(a=field, b=Int8())
                        del field

                    with self.subTest(encoding=encoding, arithmetic=arithmetic, kind=kind):
                        root = ET.fromstring(to_peach_data_model(Packet, include_header=False))
                        relation = root.find("Number/Relation")
                        byte_count = (3 + int(arithmetic)) * width
                        self.assertEqual(eval(relation.get("expressionGet", "size"), {"size": 3}), byte_count)
                        self.assertEqual(eval(relation.get("expressionSet", "size"), {"size": byte_count}), 3)
                        if width == 2:
                            with self.assertRaisesRegex(ValueError, "not divisible"):
                                eval(relation.get("expressionSet"), {"size": byte_count + 1})

    def test_variable_width_dynamic_character_lengths_are_rejected(self) -> None:
        for encoding in ("utf8", "utf7", "utf32", "utf32be"):
            class Dynamic(Schema):
                count = Int8()
                text = String[count](type=encoding, length_type="chars")

            with self.subTest(encoding=encoding):
                with self.assertRaisesRegex(ValueError, "dynamic character length.*" + encoding):
                    to_peach_data_model(Dynamic, include_header=False)

                class Constant(Schema):
                    count = Int8(fixed(2))
                    text = String[count](type=encoding, length_type="chars")

                root = ET.fromstring(to_peach_data_model(Constant, include_header=False))
                self.assertEqual(root.find("String").get("length"), "2")
                self.assertEqual(root.findall(".//Relation"), [])

    def test_bit_lengths_preserve_units_across_containers(self) -> None:
        targets = {"plain": "payload", "optional": "payload", "conditional": "payload.value",
                   "array": "payload", "union": "payload.a"}
        for factory in (Blob, String):
            for units in ("bits", "bytes"):
                for arithmetic in (False, True):
                    for kind, target in targets.items():
                        class Packet(Schema):
                            size = Int8(16)
                            field = factory[size + 8 if arithmetic else size](length_type=units)
                            if kind == "plain":
                                payload = field
                            elif kind == "optional":
                                payload = Optional[field]()
                            elif kind == "conditional":
                                payload = Optional[field](when=size != 0)
                            elif kind == "array":
                                payload = Array[field, 2]()
                            else:
                                payload = Union(a=field, b=Int8())
                            del field

                        with self.subTest(factory=factory, units=units, arithmetic=arithmetic, kind=kind):
                            root = ET.fromstring(to_peach_data_model(Packet, include_header=False))
                            relation = root.find("Number/Relation")
                            self.assertIsNotNone(relation)
                            self.assertEqual(relation.get("of"), target)
                            self.assertEqual(relation.get("lengthType", "bytes"), units)
                            self.assertEqual(relation.get("expressionGet"), "(size + 8)" if arithmetic else None)
                            self.assertEqual(relation.get("expressionSet"), "(size - 8)" if arithmetic else None)

    def test_wide_literal_inverse_preserves_integer_precision(self) -> None:
        class Packet(Schema):
            amount = Int64()
            body = Blob[(2**64 - 1) - amount]()

        relation = ET.fromstring(to_peach_data_model(Packet, include_header=False)).find("Number/Relation")
        assert relation is not None
        for size in (0, 2, 2**53 + 1, 2**63 - 1):
            with self.subTest(size=size):
                self.assertEqual(
                    eval(relation.attrib["expressionSet"], {"size": size}),
                    2**64 - 1 - size,
                )

    def test_bijective_bitwise_relations_recompute_source_without_old_value(self) -> None:
        expressions = (
            lambda n: (n + 1) << 2,
            lambda n: n << 0,
            lambda n: (n ^ 7) + 2,
            lambda n: (7 ^ n) * 3,
            lambda n: ((n << 2) ^ 3) + 1,
        )
        for expression in expressions:
            for kind in ("size", "count"):
                source = Int64()
                length = expression(source)
                member = Blob[length]() if kind == "size" else Array[Int8, length]()
                packet = type("Packet", (Schema,), {"source": source, "body": member})
                relation = ET.fromstring(to_peach_data_model(packet, include_header=False)).find("Number/Relation")
                assert relation is not None
                inverse = relation.attrib["expressionSet"]
                for original in (0, 3, 7, 2**53 + 1):
                    with self.subTest(kind=kind, original=original, line=expression.__code__.co_firstlineno):
                        self.assertEqual(eval(inverse, {kind: expression(original)}), original)

        class Shifted(Schema):
            amount = Int16()
            body = Blob[amount << 2]()

        relation = ET.fromstring(to_peach_data_model(Shifted, include_header=False)).find("Number/Relation")
        assert relation is not None
        with self.assertRaisesRegex(ValueError, "not divisible"):
            eval(relation.attrib["expressionSet"], {"size": 7})

    def test_lossy_count_relation_keeps_original_count_field(self) -> None:
        class Packet(Schema):
            amount = Int16(5)
            items = Array[Int8, amount // 2]()

        relation = ET.fromstring(to_peach_data_model(Packet, include_header=False)).find("Number/Relation")
        assert relation is not None
        inverse = relation.attrib["expressionSet"]
        scope = {"self": SimpleNamespace(DefaultValue=5), "count": 2}
        self.assertEqual(int(eval(inverse, scope)), 5)
        scope["count"] = 3
        with self.assertRaisesRegex(ValueError, "cannot uniquely invert"):
            eval(inverse, scope)

    def test_scale_inverse_rejects_unrepresentable_payload_lengths(self) -> None:
        for kind in ("size", "count"):
            for scale in (3, -3):
                source = Int16()
                length = (source + 2) * scale
                member = Blob[length]() if kind == "size" else Array[Int8, length]()
                packet = type("Packet", (Schema,), {"source": source, "payload": member})
                relation = ET.fromstring(to_peach_data_model(packet, include_header=False)).find("Number/Relation")
                assert relation is not None
                inverse = relation.get("expressionSet")
                assert inverse is not None
                for measured in (1, 2, 4, 2**53 + 2):
                    with self.subTest(kind=kind, scale=scale, measured=measured):
                        with self.assertRaisesRegex(ValueError, "not divisible"):
                            eval(inverse, {kind: measured})

        class NestedScale(Schema):
            length = Int16()
            body = Blob[(length * 3 + 1) * 2]()

        relation = ET.fromstring(to_peach_data_model(NestedScale, include_header=False)).find("Number/Relation")
        assert relation is not None
        inverse = relation.get("expressionSet")
        assert inverse is not None
        self.assertEqual(eval(inverse, {"size": 14}), 2)
        # The outer division is exact, but the inner division is not.
        with self.assertRaisesRegex(ValueError, "not divisible"):
            eval(inverse, {"size": 10})

    def test_integer_scale_inverse_preserves_type_and_large_values(self) -> None:
        class Packet(Schema):
            length = Int64()
            payload = Blob[(length + 2) * 3]()

        root = ET.fromstring(to_peach_data_model(Packet, include_header=False))
        relation = root.find("Number[@name='length']/Relation")
        assert relation is not None
        inverse = relation.get("expressionSet")
        assert inverse is not None
        for source in (0, 1, 2**31, 2**53 + 1):
            with self.subTest(source=source):
                recovered = eval(inverse, {"size": (source + 2) * 3})
                self.assertIsInstance(recovered, int)
                self.assertEqual(recovered, source)

    def test_multi_stage_inverse_round_trips_numeric_values(self) -> None:
        expressions = (
            ("add_subtract_scale", lambda n: ((n + 7) - 3) * 2),
            ("reverse_subtract_scale", lambda n: 80 - ((n * 3) + 5)),
            ("scale_after_reverse_subtract", lambda n: (50 - (n + 2)) * 3),
            ("subtract_scale_add", lambda n: ((n - 4) * 5) + 7),
            ("nested_scales", lambda n: ((n * 3) + 1) * 2),
        )
        for label, expression in expressions:
            with self.subTest(expression=label):
                class Header(Schema):
                    amount = Int16()

                header = Header()
                packet = type(
                    f"Inverse{label}",
                    (Schema,),
                    {"header": header, "payload": Blob[expression(header.amount)]()},
                )
                root = ET.fromstring(
                    to_peach_data_model(packet, include_header=False)
                )
                relation = root.find(
                    "Block[@name='header']/Number[@name='amount']/"
                    "Relation[@type='size'][@of='payload']"
                )
                self.assertIsNotNone(relation)
                assert relation is not None
                forward = relation.get("expressionGet")
                inverse = relation.get("expressionSet")
                self.assertIsNotNone(forward)
                self.assertIsNotNone(inverse)
                assert forward is not None and inverse is not None
                for source in (4, 6, 9, 12):
                    wire_size = eval(
                        forward, {"__builtins__": {}}, {"size": source}
                    )
                    recovered = eval(inverse, {"size": wire_size})
                    self.assertAlmostEqual(recovered, source)

    def test_noninvertible_arithmetic_preserves_source_and_validates_length(self) -> None:
        for label, expression in (
            ("multiply_by_zero", lambda n: n * 0),
            ("floor_divide", lambda n: n // 2),
            ("modulo", lambda n: n % 2),
            ("bit_mask", lambda n: n & 3),
            ("shift_right", lambda n: n >> 1),
        ):
            with self.subTest(expression=label):
                class Header(Schema):
                    amount = Int16()

                header = Header()
                packet = type(
                    f"Noninvertible{label}",
                    (Schema,),
                    {"header": header, "body": Blob[expression(header.amount)]()},
                )
                root = ET.fromstring(
                    to_peach_data_model(packet, include_header=False)
                )
                relation = root.find(
                    "Block[@name='header']/Number[@name='amount']/Relation"
                )
                self.assertIsNotNone(relation)
                assert relation is not None
                self.assertIn("expressionGet", relation.attrib)
                inverse = relation.get("expressionSet")
                self.assertIsNotNone(inverse)
                assert inverse is not None
                for original in (5, 7, 2**53 + 1):
                    measured = expression(original)
                    scope = {"self": SimpleNamespace(DefaultValue=original), "size": measured}
                    self.assertEqual(int(eval(inverse, scope)), original)
                    scope["size"] = measured + 1
                    with self.assertRaisesRegex(ValueError, "cannot uniquely invert"):
                        eval(inverse, scope)


def _make_test(index: int):
    label, kind, expression, expected_get, expected_set = CASES[index]

    def run(self: RelationExpressionTests) -> None:
        class Header(Schema):
            amount = Int16()
            tag = Int8()

        header = Header()
        value = expression(header.amount)
        member = Blob[value]() if kind == "size" else Array[Int8, value]()
        packet = type(
            f"RelationPacket{index}", (Schema,),
            {"prefix": Int8(), "header": header,
             "body": Block(data=member, marker=Int8())},
        )
        root = ET.fromstring(to_peach_data_model(packet, include_header=False))
        relation = root.find("Block[@name='header']/Number[@name='amount']/Relation")
        self.assertIsNotNone(relation, label)
        assert relation is not None
        self.assertEqual(relation.get("type"), kind)
        self.assertEqual(relation.get("of"), "body.data")
        self.assertEqual(eval(relation.get('expressionGet'), {'size': 3, 'count': 3}), eval((expected_get) or "size", {'size': 3, 'count': 3}))
        self.assertEqual(relation.get("expressionSet"), expected_set)

    return run


for _index, (_label, *_rest) in enumerate(CASES):
    setattr(RelationExpressionTests, f"test_{_index:02d}_{_label}", _make_test(_index))


if __name__ == "__main__":
    unittest.main()
