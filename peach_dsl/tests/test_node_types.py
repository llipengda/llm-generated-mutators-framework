"""Node classification with combinations and unique root instance paths."""
from __future__ import annotations

import unittest
from xml.etree import ElementTree as ET

if __package__ in ("", "tests"):
    __package__ = "peach_dsl.tests"

from .. import Array, Block, Int8, Int16, NodeType, Occurs, Optional, PacketUnion, Schema, Union, fixed, compute, evaluate_schema, get_node_types, to_peach_data_model


class NodeTypeTests(unittest.TestCase):
    def test_fixed_mutable_and_optional_properties_are_combined(self) -> None:
        class Packet(Schema):
            tag = Int8(fixed(1))
            value = Int8(1)
            immutable = Int8(1, mutable=False)
            token = Int8(1, token=True)
            gate = Int8()
            tail = Optional[Int8(fixed(2))](when=gate != 0)

        rows = get_node_types(Packet, name="Renamed", include_header=False)
        types = {path: kind for kind, path in rows}
        self.assertEqual(types["Renamed.tag"], NodeType.FIXED)
        self.assertEqual(types["Renamed.value"], NodeType.MUTABLE)
        self.assertEqual(types["Renamed.immutable"], NodeType.FIXED)
        self.assertEqual(types["Renamed.token"], NodeType.FIXED)
        self.assertEqual(types["Renamed.tail"], NodeType.MUTABLE | NodeType.OPTIONAL)
        self.assertEqual(types["Renamed.tail.value"], NodeType.FIXED)
        for kind, _ in rows:
            self.assertNotEqual(bool(kind & NodeType.FIXED), bool(kind & NodeType.MUTABLE))

    def test_nested_arrays_and_optional_choice_include_generated_nodes(self) -> None:
        class Packet(Schema):
            payload = Optional[Array[Int8(fixed(2)), Occurs(0, 3)]()]()
            matrix = Array[Array[Int8, 1](), 2]()
            ordinary = Union(first=Int8(), second=Int16())

        rows = get_node_types(Packet, include_header=False)
        types = {path: kind for kind, path in rows}
        self.assertEqual(types["Packet.payload"], NodeType.MUTABLE | NodeType.OPTIONAL)
        self.assertEqual(types["Packet.payload.value"], NodeType.FIXED | NodeType.REPEATABLE | NodeType.OPTIONAL)
        self.assertEqual(types["Packet.payload.value[]"], NodeType.FIXED)
        self.assertEqual(types["Packet.payload.empty"], NodeType.MUTABLE)
        self.assertEqual(types["Packet.matrix"], NodeType.MUTABLE | NodeType.REPEATABLE)
        self.assertEqual(types["Packet.matrix[]"], NodeType.MUTABLE)
        self.assertEqual(types["Packet.matrix[].item"], NodeType.MUTABLE | NodeType.REPEATABLE)
        self.assertEqual(types["Packet.matrix[].item[]"], NodeType.MUTABLE)
        self.assertEqual(types["Packet.ordinary.first"], NodeType.MUTABLE)
        self.assertEqual(rows[0], (NodeType.MUTABLE, "Packet"))
        xml = ET.fromstring(to_peach_data_model(Packet, include_header=False))
        self.assertEqual(len(rows), len(list(xml.iter())) + 3)
        self.assertEqual(len(rows), len(types))

    def test_repeated_block_descendants_and_dynamic_counts(self) -> None:
        class Item(Schema):
            tag = Int8(fixed(1))
            data = Int8()

        class Packet(Schema):
            count = Int8()
            items = Array[Item, count]()
            exact = Array[Int8, 2]()
            bounded = Array[Int8, Occurs(1, 2)]()
            empty = Array[Int8, 0]()

        types = {path: kind for kind, path in get_node_types(Packet, include_header=False)}
        self.assertEqual(types["Packet.items"], NodeType.MUTABLE | NodeType.REPEATABLE | NodeType.OPTIONAL)
        self.assertEqual(types["Packet.items[]"], NodeType.MUTABLE)
        self.assertEqual(types["Packet.items[].tag"], NodeType.FIXED)
        self.assertEqual(types["Packet.items[].data"], NodeType.MUTABLE)
        self.assertEqual(types["Packet.exact"], NodeType.MUTABLE | NodeType.REPEATABLE)
        self.assertEqual(types["Packet.bounded"], NodeType.MUTABLE | NodeType.REPEATABLE)
        self.assertEqual(types["Packet.empty"], NodeType.MUTABLE | NodeType.REPEATABLE | NodeType.OPTIONAL)
        self.assertNotIn("Packet.count.Relation", types)

    def test_references_and_overrides_keep_wrapper_classifications_local(self) -> None:
        class Header(Schema):
            tag = Int8(fixed(1))
            value = Int8()
            tail = Optional[Int8]()

        class Packet(Schema):
            first = Array[Header, 2]()
            second = Header()
            overridden = Header(tag=Int16())

        @PacketUnion
        class Packets(Schema):
            packet_union = Union(first=Packet, second=Packet)

        rows = get_node_types(Packets)
        types = {path: kind for kind, path in rows}
        self.assertEqual(len(rows), len(types))
        prefix = "Packets.packet_union.first"
        self.assertEqual(types[prefix + ".first[].tag"], NodeType.FIXED)
        self.assertEqual(types[prefix + ".first"], NodeType.MUTABLE | NodeType.REPEATABLE)
        self.assertEqual(types[prefix + ".second"], NodeType.MUTABLE)
        self.assertEqual(types[prefix + ".overridden.tag"], NodeType.MUTABLE)
        self.assertEqual(types[prefix + ".second.tail.value"], NodeType.MUTABLE)
        self.assertEqual(types[prefix + ".second.tail.empty"], NodeType.MUTABLE)
        self.assertEqual(types[prefix + ".first[].tail.value"], NodeType.MUTABLE)
        self.assertEqual(types[prefix + ".first[].tail"], NodeType.MUTABLE | NodeType.OPTIONAL)
        self.assertTrue(all(path.startswith("Packets") for _, path in rows))
        self.assertIn("Packets.packet_union.second.second.tag", types)

    def test_nested_wrappers_keep_their_own_properties_without_inheritance(self) -> None:
        class Item(Schema):
            fixed_value = Int8(fixed(1))
            optional_value = Optional[Int8]()
            repeated_value = Array[Int8, 2]()

        class Packet(Schema):
            optional_block = Optional[Item]()
            repeated_block = Array[Item, 2]()

        types = {path: kind for kind, path in get_node_types(Packet)}
        self.assertEqual(types["Packet.optional_block"], NodeType.MUTABLE | NodeType.OPTIONAL)
        self.assertEqual(types["Packet.repeated_block"], NodeType.MUTABLE | NodeType.REPEATABLE)
        for prefix in ("Packet.optional_block.optional_block_internal", "Packet.repeated_block[]"):
            with self.subTest(prefix=prefix):
                self.assertEqual(types[prefix], NodeType.MUTABLE)
                self.assertEqual(types[prefix + ".fixed_value"], NodeType.FIXED)
                self.assertEqual(types[prefix + ".optional_value"], NodeType.MUTABLE | NodeType.OPTIONAL)
                self.assertEqual(types[prefix + ".optional_value.value"], NodeType.MUTABLE)
                self.assertEqual(types[prefix + ".optional_value.empty"], NodeType.MUTABLE)
                self.assertEqual(types[prefix + ".repeated_value"], NodeType.MUTABLE | NodeType.REPEATABLE)
                self.assertEqual(types[prefix + ".repeated_value[]"], NodeType.MUTABLE)

    def test_stored_paths_and_classification_paths_agree_under_renamed_root(self) -> None:
        class Header(Schema):
            flags = Int8()
            tail = Optional[Int8](when=flags != 0)

        class Packet(Schema):
            first = Array[Header, 2]()
            second = Header()

        @PacketUnion
        class Packets(Schema):
            packet_union = Union(first=Packet, second=Packet)

        for include_header in (False, True):
            with self.subTest(include_header=include_header):
                result = evaluate_schema(Packets, name="Root", include_header=include_header)
                rows = get_node_types(Packets, name="Root", include_header=include_header)
                paths = {path for _, path in rows}
                self.assertEqual(len(rows), len(paths))
                self.assertEqual(result.path, "Root")
                members = list(result.walk())
                stored = [member.path for member in members]
                self.assertEqual(len(stored), len(set(stored)))
                self.assertTrue(set(stored) <= paths)
                self.assertTrue(all(path == "Root" or path.startswith("Root.") for path in paths))
                for member in members:
                    source = getattr(member, "source_path", None)
                    if source is not None:
                        self.assertIn(source, paths)

    def test_scalar_optional_and_nested_arrays_use_item_patterns(self) -> None:
        class Packet(Schema):
            scalar = Array[Int8, 1]()
            optional = Optional[Array[Int8, Occurs(0, 3)]()]()
            matrix = Array[Array[Int8, 2](), 3]()

        result = evaluate_schema(Packet, name="Root")
        paths = {path for _, path in get_node_types(Packet, name="Root")}
        self.assertEqual(result.fields["scalar"].path, "Root.scalar")
        self.assertEqual(result.fields["scalar"].element.path, "Root.scalar[]")
        self.assertEqual(result.fields["optional"].element.path, "Root.optional.value")
        self.assertEqual(result.fields["optional"].element.element.path, "Root.optional.value[]")
        self.assertEqual(result.fields["matrix"].element.path, "Root.matrix[].item")
        self.assertEqual(result.fields["matrix"].element.element.path, "Root.matrix[].item[]")
        self.assertEqual(paths, {
            "Root", "Root.scalar", "Root.scalar[]", "Root.optional",
            "Root.optional.value", "Root.optional.value[]", "Root.optional.empty",
            "Root.matrix", "Root.matrix[]", "Root.matrix[].item", "Root.matrix[].item[]",
        })

    def test_computed_metadata_is_not_classified_as_data_nodes(self) -> None:
        @compute
        def increment(value: int) -> int:
            return value + 1

        class Packet(Schema):
            value = Int8()
            derived = Int16(increment(value))

        rows = get_node_types(Packet, include_header=False)
        self.assertEqual(rows, [
            (NodeType.MUTABLE, "Packet"),
            (NodeType.MUTABLE, "Packet.value"),
            (NodeType.MUTABLE, "Packet.derived"),
        ])
        xml = ET.fromstring(to_peach_data_model(Packet, include_header=False))
        self.assertIsNotNone(xml.find("Number[@name='derived']/Fixup/Param"))

    def test_failed_export_does_not_leak_classification_state(self) -> None:
        class Packet(Schema):
            value = Optional[Int8]()

        before = to_peach_data_model(Packet, include_header=False)
        with self.assertRaisesRegex(ValueError, "cannot contain"):
            get_node_types(Packet, name="invalid.name", include_header=False)
        rows = get_node_types(Packet, name="Valid", include_header=False)
        self.assertTrue(all(path.startswith("Valid") for _, path in rows))
        self.assertEqual(to_peach_data_model(Packet, include_header=False), before)


if __name__ == "__main__":
    unittest.main()
