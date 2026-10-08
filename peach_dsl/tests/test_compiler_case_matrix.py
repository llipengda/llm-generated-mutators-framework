"""One hundred independent wire-model compiler cases.

Each subtest compiles a distinct schema to Peach XML and checks the emitted
structure or a specific rejection.  The matrix deliberately combines fields
instead of checking only whether a declaration can be constructed.
"""

from __future__ import annotations

import unittest
from xml.etree import ElementTree as ET

if __package__ in ("", "tests"):
    __package__ = "peach_dsl.tests"

from .. import (
    Array, Bit, Blob, Block, DecimalString, Default, Double, ExtendedType,
    Flags, Int8, Int16, Int32, Int64, Occurs, Optional, Schema, String,
    Union, fixed, to_peach_data_model,
)


def schema(name: str, **fields: object) -> type[Schema]:
    return type(name, (Schema,), fields)


def xml_for(model: type[Schema]) -> ET.Element:
    return ET.fromstring(to_peach_data_model(model, include_header=False))


class CompilerCaseMatrixTests(unittest.TestCase):
    case_index: int

    def _check_01_numeric_wire_options(self) -> None:
        cases = [
            (Int8, 8, "big", False), (Int8, 8, "little", True),
            (Int16, 16, "big", True), (Int16, 16, "little", False),
            (Int32, 32, "big", False), (Int32, 32, "little", True),
            (Int64, 64, "big", True), (Int64, 64, "little", False),
            (Int16, 16, "big", False), (Int32, 32, "little", False),
        ]
        for index, (kind, width, endian, signed) in enumerate(cases):
            if index != self.case_index:
                continue
            with self.subTest(case=index):
                model = schema(f"Numeric{index}", magic=Int8(fixed(index)),
                               value=kind(index + 1, endian=endian, signed=signed),
                               tail=Blob[2](fixed(b"\x00\xff")))
                root = xml_for(model)
                number = root.find("Number[@name='value']")
                self.assertIsNotNone(number)
                assert number is not None
                self.assertEqual(number.get("size"), str(width))
                self.assertEqual(number.get("endian"), endian)
                self.assertEqual(number.get("signed"), str(signed).lower())
                self.assertEqual(root.find("Number[@name='magic']").get("token"), "true")
                self.assertEqual(root.find("Blob[@name='tail']").get("value"), "00ff")

    def _check_02_size_relations_and_expressions(self) -> None:
        for index in range(10):
            if index != self.case_index:
                continue
            with self.subTest(case=index):
                length = (Int8, Int16, Int32, DecimalString)[index % 4]()
                size = length if index < 5 else length + (index - 4)
                payload = (Blob, String)[index % 2][size]()
                model = schema(f"Size{index}", prefix=Int8(fixed(0xA0 + index)),
                               length=length, payload=payload)
                root = xml_for(model)
                relation = root.find(".//*[@name='length']/Relation")
                self.assertIsNotNone(relation)
                assert relation is not None
                self.assertEqual(relation.get("type"), "size")
                self.assertEqual(relation.get("of"), "payload")
                self.assertIsNotNone(root.find("*[@name='payload']"))
                if index >= 5:
                    self.assertIn("expressionGet", relation.attrib)
                    self.assertIn("expressionSet", relation.attrib)

    def _check_03_array_cardinality(self) -> None:
        counts = [0, 1, 3, 8, Occurs(0, 2), Occurs(1, 4),
                  Occurs(2, 6), None, "field", "expression"]
        for index, count in enumerate(counts):
            if index != self.case_index:
                continue
            with self.subTest(case=index):
                element = schema(f"ArrayItem{index}", tag=Int8(), data=Blob[2]())
                quantity = Int8()
                bound = quantity if count == "field" else quantity + 1 if count == "expression" else count
                items = Array[element, bound]() if bound is not None else Array[element]()
                model = schema(f"ArrayPacket{index}", quantity=quantity, items=items)
                root = xml_for(model)
                item = root.find("*[@name='items']")
                self.assertIsNotNone(item)
                assert item is not None
                if isinstance(count, Occurs):
                    self.assertEqual(item.get("minOccurs"), str(count.min_occurs))
                    self.assertEqual(item.get("maxOccurs"), str(count.max_occurs))
                elif isinstance(count, int):
                    self.assertEqual(item.get("occurs"), str(count))
                elif count is None:
                    self.assertEqual(item.get("minOccurs"), "0")
                else:
                    relation = root.find("Number[@name='quantity']/Relation")
                    self.assertIsNotNone(relation)
                    self.assertEqual(relation.get("type"), "count")

    def _check_04_conditional_optional_members(self) -> None:
        for index in range(10):
            if index != self.case_index:
                continue
            with self.subTest(case=index):
                flags = Int16()
                condition = (flags & (1 << (index % 8))) != 0
                inner = schema(f"OptionalBody{index}", marker=Int8(fixed(index)),
                               data=Blob[2]())
                element = (Int8, Int16, String, Blob[2], inner)[index % 5]
                model = schema(f"OptionalPacket{index}", flags=flags,
                               value=Optional[element](when=condition),
                               trailer=Int8(fixed(0xEE)))
                root = xml_for(model)
                optional = root.find("Optional[@name='value']")
                self.assertIsNotNone(optional)
                assert optional is not None
                self.assertEqual(optional.get("src"), "flags")
                self.assertIn(str(1 << (index % 8)), optional.get("expression", ""))
                self.assertGreater(len(optional), 0)

    def _check_05_bounded_blocks_with_nested_members(self) -> None:
        for index in range(10):
            if index != self.case_index:
                continue
            with self.subTest(case=index):
                length = Int16()
                body_length = length if index < 5 else length + (index - 4)
                record = schema(f"BlockRecord{index}", code=Int8(),
                                data=Blob[2]())
                body = Block[body_length](kind=Int8(fixed(index)),
                                          record=record(),
                                          trailer=Blob[index % 3]())
                model = schema(f"BlockPacket{index}", length=length, body=body)
                root = xml_for(model)
                relation = root.find("Number[@name='length']/Relation")
                self.assertIsNotNone(relation)
                self.assertEqual(relation.get("type"), "size")
                self.assertEqual(relation.get("of"), "body")
                block = root.find("Block[@name='body']")
                self.assertIsNotNone(block)
                self.assertIsNotNone(block.find("Number[@name='kind']"))
                self.assertIsNotNone(block.find("Block[@name='record']"))

    def _check_06_union_discriminators(self) -> None:
        for index in range(10):
            if index != self.case_index:
                continue
            with self.subTest(case=index):
                first = schema(f"VariantA{index}", kind=Int8(fixed(index)),
                               value=Int16())
                second = schema(f"VariantB{index}", kind=Int8(fixed(index + 10)),
                                value=Blob[3]())
                choice = (Union(left=first, right=second) if index % 2 == 0
                          else first | second)
                model = schema(f"UnionPacket{index}", prefix=Int8(),
                               choice=choice, suffix=Int8())
                root = xml_for(model)
                node = root.find("Choice[@name='choice']")
                self.assertIsNotNone(node)
                assert node is not None
                self.assertEqual(len(node), 2)
                self.assertEqual([child.get("name") for child in node],
                                 ["left", "right"] if index % 2 == 0
                                 else [first.__name__, second.__name__])

    def _check_07_reusable_schema_overrides(self) -> None:
        replacements = [
            Int8(fixed(9)), Int16(fixed(10)), Int32(fixed(11)),
            Blob[2](fixed(b"xy")), String(fixed("ok")),
            Optional[Int8](), Array[Int8, 2](),
            Block(code=Int8()), Double(1.5, size=32),
            ExtendedType[int]("CustomNumber")(7),
        ]
        for index, replacement in enumerate(replacements):
            if index != self.case_index:
                continue
            with self.subTest(case=index):
                base = schema(f"Reusable{index}", marker=Int8(), slot=Int8(),
                              tail=Int8())
                model = schema(f"OverridePacket{index}",
                               before=base(slot=replacement),
                               after=base(slot=Int8(fixed(index))))
                root = xml_for(model)
                blocks = root.findall("Block")
                self.assertEqual(len(blocks), 2)
                self.assertEqual(blocks[0].get("name"), "before")
                self.assertEqual(blocks[1].get("name"), "after")
                self.assertIsNotNone(blocks[0].find("*[@name='slot']"))
                self.assertEqual(blocks[1].find("Number[@name='slot']").get("token"), "true")

    def _check_08_string_and_decimal_codecs(self) -> None:
        encodings = ["ascii", "utf7", "utf8", "utf16", "utf16be",
                     "utf32", "utf32be", "ascii", "utf8", "utf16"]
        for index, encoding in enumerate(encodings):
            if index != self.case_index:
                continue
            with self.subTest(case=index):
                length = Int8()
                if index < 7:
                    text = String[length](type=encoding)
                elif index == 7:
                    text = String[4](fixed("AB"), type=encoding, pad_character=" ")
                elif index == 8:
                    text = String(type=encoding, null_terminated=True)
                else:
                    text = DecimalString[length](type=encoding)
                model = schema(f"TextPacket{index}", length=length,
                               text=text, tail=Int8(fixed(0)))
                root = xml_for(model)
                node = root.find("String[@name='text']")
                self.assertIsNotNone(node)
                assert node is not None
                self.assertEqual(node.get("type"), encoding)
                if index < 7 or index == 9:
                    self.assertEqual(root.find("Number[@name='length']/Relation").get("type"), "size")
                if index == 8:
                    self.assertEqual(node.get("nullTerminated"), "true")

    def _check_09_packed_flags_layouts(self) -> None:
        layouts = [(Int8, 8, split) for split in range(1, 6)] + [
            (Int16, 16, split) for split in (1, 3, 5, 8, 15)
        ]
        for index, (storage, width, split) in enumerate(layouts):
            if index != self.case_index:
                continue
            with self.subTest(case=index):
                bits = schema(f"FlagBits{index}", low=Bit[split](),
                              high=Bit[width - split](fixed(0)))
                packed = Flags(storage, endian="little" if index % 2 else "big")(bits)
                prefix = Int8()
                model = schema(f"FlagPacket{index}", prefix=prefix, flags=packed,
                               body=Optional[Blob[2]](when=(prefix & 1) != 0))
                root = xml_for(model)
                flags = root.find("Flags[@name='flags']")
                self.assertIsNotNone(flags)
                assert flags is not None
                self.assertEqual(flags.get("size"), str(width))
                children = flags.findall("Flag")
                self.assertEqual([child.get("position") for child in children],
                                 ["0", str(split)])
                self.assertEqual([child.get("size") for child in children],
                                 [str(split), str(width - split)])

    def _check_10_invalid_wire_models(self) -> None:
        cases = [
            ("negative occurrences", lambda: Occurs(-1, 2), ValueError),
            ("reversed occurrences", lambda: Occurs(3, 2), ValueError),
            ("empty extension name", lambda: ExtendedType[int](""), ValueError),
            ("single union alternative", lambda: Union(only=Int8()), ValueError),
            ("empty union", lambda: Union(), ValueError),
            ("invalid default endian", lambda: Default(endian="middle"), ValueError),
            ("invalid flags endian", lambda: Flags(Int8, endian="middle"), ValueError),
            ("flags underfilled", lambda: Flags(Int8, endian="big")(
                schema("ShortFlags", bit=Bit[7]())), ValueError),
            ("flags overflow", lambda: Flags(Int8, endian="big")(
                schema("LongFlags", first=Bit[5](), second=Bit[4]())), ValueError),
            ("flags non-bit member", lambda: Flags(Int8, endian="big")(
                schema("MixedFlags", bit=Bit[7](), other=Int8())), TypeError),
        ]
        for index, (label, action, exception) in enumerate(cases):
            if index != self.case_index:
                continue
            with self.subTest(case=label):
                with self.assertRaises(exception):
                    action()


def _make_case(group: str, index: int):
    def run(self: CompilerCaseMatrixTests) -> None:
        self.case_index = index
        getattr(self, f"_check_{group}")()

    return run


for _group in (
    "01_numeric_wire_options",
    "02_size_relations_and_expressions",
    "03_array_cardinality",
    "04_conditional_optional_members",
    "05_bounded_blocks_with_nested_members",
    "06_union_discriminators",
    "07_reusable_schema_overrides",
    "08_string_and_decimal_codecs",
    "09_packed_flags_layouts",
    "10_invalid_wire_models",
):
    for _index in range(10):
        setattr(CompilerCaseMatrixTests, f"test_{_group}_{_index:02d}",
                _make_case(_group, _index))


if __name__ == "__main__":
    unittest.main()
