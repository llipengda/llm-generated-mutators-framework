"""Declared mutation targets retain exact rooted compiler metadata."""
from __future__ import annotations

from dataclasses import FrozenInstanceError
import unittest

if __package__ in ("", "tests"):
    __package__ = "peach_dsl.tests"

from .. import (
    Array,
    Bit,
    Blob,
    ExtendedType,
    Flags,
    Int8,
    Int16,
    MutationNode,
    NodeType,
    Occurs,
    Optional,
    PacketUnion,
    Schema,
    String,
    Union,
    evaluate_schema,
    fixed,
    get_mutation_nodes,
    get_node_types,
)


class MutationNodeTests(unittest.TestCase):
    def nodes(self, target: type[Schema], *, name: str = "Root", include_header: bool = True) -> dict[str, MutationNode]:
        rows = get_mutation_nodes(target, name=name, include_header=include_header)
        nodes = {row.path: row for row in rows}
        self.assertEqual(len(nodes), len(rows))
        self.assertNotIn(name, nodes)
        classifications = {path: node_type for node_type, path in get_node_types(target, name=name, include_header=include_header)}
        for node in rows:
            self.assertEqual(node.node_type, classifications[node.path])
        return nodes

    def test_local_flags_and_fixed_children_match_runtime_nodes(self) -> None:
        class Item(Schema):
            tag = Int8(fixed(1))
            value = Int16()
            immutable = Int8(0, mutable=False)

        class Packet(Schema):
            optional = Optional[Item]()
            items = Array[Item, Occurs(0, 4)]()
            fixed_items = Array[Int8(fixed(2)), 2]()

        nodes = self.nodes(Packet)
        self.assertEqual(nodes["Root.optional"].node_type, NodeType.MUTABLE | NodeType.OPTIONAL)
        self.assertEqual(nodes["Root.optional"].runtime_type, "Choice")
        self.assertEqual(nodes["Root.optional"].kind, "Optional")
        self.assertEqual(nodes["Root.optional"].empty_branch_name, "empty")
        self.assertEqual(nodes["Root.optional.optional_internal"].node_type, NodeType.MUTABLE)
        self.assertEqual(nodes["Root.optional.optional_internal.tag"].node_type, NodeType.FIXED)
        self.assertEqual(nodes["Root.optional.optional_internal.immutable"].node_type, NodeType.FIXED)
        self.assertEqual(nodes["Root.items"].node_type, NodeType.MUTABLE | NodeType.OPTIONAL | NodeType.REPEATABLE)
        self.assertEqual(nodes["Root.items"].runtime_type, "Array")
        self.assertEqual(nodes["Root.items[]"].node_type, NodeType.MUTABLE)
        self.assertEqual(nodes["Root.items[].tag"].node_type, NodeType.FIXED)
        self.assertEqual(nodes["Root.fixed_items"].node_type, NodeType.FIXED | NodeType.REPEATABLE)
        self.assertEqual(nodes["Root.fixed_items[]"].node_type, NodeType.FIXED)
        self.assertNotIn("Root.optional.empty", nodes)
        self.assertIn("Occurs(0, 4)", nodes["Root.items"].description)
        with self.assertRaises(FrozenInstanceError):
            nodes["Root.items"].kind = "changed"

    def test_reference_overrides_use_effective_runtime_types_and_paths(self) -> None:
        class Header(Schema):
            value = Int8(fixed(1))
            tail = Optional[Int8]()

        class Packet(Schema):
            first = Header()
            second = Header(value=String[2]("hi"))
            repeated = Array[Header, 2]()

        @PacketUnion
        class Packets(Schema):
            packet_union = Union(first=Packet, second=Packet)

        for include_header in (False, True):
            with self.subTest(include_header=include_header):
                nodes = self.nodes(Packets, include_header=include_header)
                prefix = "Root.packet_union.first"
                self.assertEqual(nodes[prefix + ".first.value"].runtime_type, "Number")
                self.assertEqual(nodes[prefix + ".first.value"].node_type, NodeType.FIXED)
                self.assertEqual(nodes[prefix + ".second.value"].runtime_type, "String")
                self.assertEqual(nodes[prefix + ".second.value"].node_type, NodeType.MUTABLE)
                self.assertEqual(nodes[prefix + ".repeated[].tail"].empty_branch_name, "empty")
                self.assertIn("Root.packet_union.second.second.value", nodes)
                self.assertTrue(all(node.dsl_path is not None for node in nodes.values()))

    def test_optional_union_empty_name_collision_keeps_declared_alternatives(self) -> None:
        class Packet(Schema):
            choice = Optional[Union(empty=Int8(), optional_empty=Int16())]()

        nodes = self.nodes(Packet)
        self.assertEqual(nodes["Root.choice"].kind, "Optional")
        self.assertEqual(nodes["Root.choice"].empty_branch_name, "optional_optional_empty")
        self.assertEqual(set(nodes), {"Root.choice", "Root.choice.empty", "Root.choice.optional_empty"})
        self.assertEqual(nodes["Root.choice.empty"].runtime_type, "Number")
        self.assertEqual(nodes["Root.choice.empty"].node_type, NodeType.MUTABLE)

    def test_conditional_sources_rebind_to_each_rooted_array_instance(self) -> None:
        class Item(Schema):
            flags = Int8()
            payload = Optional[Blob[2]](when=(flags & 4) != 0)

        class Packet(Schema):
            first = Item()
            second = Item()
            items = Array[Item, 2]()

        nodes = self.nodes(Packet)
        evaluated = evaluate_schema(Packet, name="Root")
        for prefix in ("Root.first", "Root.second", "Root.items[]"):
            with self.subTest(prefix=prefix):
                node = nodes[prefix + ".payload"]
                self.assertEqual(node.runtime_type, "Optional")
                self.assertEqual(node.node_type, NodeType.MUTABLE | NodeType.OPTIONAL)
                self.assertEqual(node.source_path, prefix + ".flags")
                self.assertIsNone(node.empty_branch_name)
                self.assertIsNotNone(node.condition)
                assert node.condition is not None
                self.assertEqual(node.condition.operation, "!=")
                self.assertEqual(nodes[prefix + ".payload.value"].runtime_type, "Blob")
                self.assertEqual(nodes[prefix + ".payload.value"].node_type, NodeType.MUTABLE)
                member = next(member for member in evaluated.walk() if member.path == node.path)
                self.assertEqual(node.dsl_path, member._dsl_path)

    def test_nested_arrays_include_logical_prototypes_and_exclude_scaffold(self) -> None:
        class Packet(Schema):
            matrix = Array[Array[Int8, 2](), 3]()
            optional = Optional[Array[Int8, Occurs(0, 3)]()]()

        nodes = self.nodes(Packet)
        self.assertEqual(set(nodes), {
            "Root.matrix", "Root.matrix[].item", "Root.matrix[].item[]",
            "Root.optional", "Root.optional.value", "Root.optional.value[]",
        })
        self.assertEqual(nodes["Root.matrix[].item"].runtime_type, "Array")
        self.assertEqual(nodes["Root.matrix[].item"].node_type, NodeType.MUTABLE | NodeType.REPEATABLE)
        self.assertEqual(nodes["Root.matrix[].item[]"].runtime_type, "Number")
        self.assertEqual(nodes["Root.matrix[].item[]"].node_type, NodeType.MUTABLE)

    def test_flag_and_custom_types_expose_actual_runtime_tags(self) -> None:
        @Flags(Int8, endian="big")
        class PackedFlags(Schema):
            active = Bit[1]()
            reserved = Bit[7](fixed(0))

        MqttVarInt = ExtendedType[int]("MqttVarInt")

        class Packet(Schema):
            flags = PackedFlags()
            length = MqttVarInt()

        nodes = self.nodes(Packet)
        self.assertEqual(nodes["Root.flags"].kind, "Flags")
        self.assertEqual(nodes["Root.flags"].runtime_type, "Flags")
        self.assertEqual(nodes["Root.flags.active"].runtime_type, "Flag")
        self.assertEqual(nodes["Root.flags.reserved"].node_type, NodeType.FIXED)
        self.assertEqual(nodes["Root.length"].runtime_type, "MqttVarInt")

    def test_single_packet_compiler_duplicate_subtree_is_excluded(self) -> None:
        class Packet(Schema):
            value = Int8()

        @PacketUnion
        class Packets(Schema):
            packet_union = Union(packet=Packet, compiler_single_packet_duplicate=Packet)

        nodes = self.nodes(Packets)
        self.assertEqual(set(nodes), {
            "Root.packet_union", "Root.packet_union.packet", "Root.packet_union.packet.value",
        })


if __name__ == "__main__":
    unittest.main()
