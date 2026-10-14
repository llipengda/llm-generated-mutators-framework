"""Exact evaluated and emitted paths through nested DSL containers."""

from __future__ import annotations

import unittest
from xml.etree import ElementTree as ET

if __package__ in ("", "tests"):
    __package__ = "peach_dsl.tests"

from .. import (
    Array,
    ArrayResult,
    Blob,
    Block,
    FieldResult,
    EvaluationResult,
    Bit,
    Flags,
    Int8,
    Int16,
    Occurs,
    Optional,
    OptionalResult,
    PacketUnion,
    Schema,
    SchemaResult,
    Union,
    UnionResult,
    evaluate_schema,
    fixed,
    to_peach_data_model,
)


NS = {"p": "http://peachfuzzer.com/2012/Peach"}


class CompilerPathTests(unittest.TestCase):
    def assert_paths_exist_in_xml(self, result: EvaluationResult, xml: str) -> None:
        root = ET.fromstring(xml)
        models = {node.get("name"): node for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "DataModel"}

        def locate(path: str) -> ET.Element:
            parts = path.split(".")
            node = models[parts[0]]
            for name in parts[1:]:
                item = name.endswith("[]")
                child_name = name[:-2] if item else name
                scope = node
                seen: set[int] = set()
                candidate = None
                while id(scope) not in seen:
                    seen.add(id(scope))
                    candidate = next((child for child in scope if child.get("name") == child_name), None)
                    if candidate is not None or scope.get("ref") is None:
                        break
                    scope = models[scope.get("ref")]
                self.assertIsNotNone(candidate, path)
                node = candidate
                if item:
                    self.assertTrue(any(key in node.attrib for key in ("occurs", "minOccurs", "maxOccurs")), path)
            return node

        for member in result.walk():
            self.assertIsNotNone(member.path)
            self.assertNotIn("/", member.path)
            locate(member.path)
            if isinstance(member, OptionalResult) and member.source_path is not None:
                locate(member.source_path)

    def test_shared_definition_has_distinct_root_instance_paths(self) -> None:
        class Header(Schema):
            value = Int8()

        class Packet(Schema):
            first = Header()
            second = Header()
            overridden = Header(value=Int16())

        @PacketUnion
        class Packets(Schema):
            packet_union = Union(first=Packet, second=Packet)

        result = evaluate_schema(Packets)
        self.assert_paths_exist_in_xml(result, to_peach_data_model(Packets))
        fields = result.fields["packet_union"].alternatives[0].fields
        self.assertEqual(fields["first"].path, "Packets.packet_union.first.first")
        self.assertEqual(fields["second"].path, "Packets.packet_union.first.second")
        self.assertEqual(fields["first"].fields["value"].path, "Packets.packet_union.first.first.value")
        self.assertEqual(fields["second"].fields["value"].path, "Packets.packet_union.first.second.value")
        self.assertEqual(fields["overridden"].fields["value"].path, "Packets.packet_union.first.overridden.value")

    def test_duplicate_inline_xml_paths_are_rejected(self) -> None:
        def first_schema() -> type[Schema]:
            class Payload(Schema):
                value = Int8()
            return Payload

        def second_schema() -> type[Schema]:
            class Payload(Schema):
                value = Int16()
            return Payload

        class Packet(Schema):
            choice = first_schema() | second_schema()

        for operation in (evaluate_schema, to_peach_data_model):
            with self.subTest(operation=operation.__name__):
                with self.assertRaisesRegex(ValueError, "XML path 'Packet.choice.Payload'.*not unique"):
                    operation(Packet, include_header=False)

    def test_xml_names_cannot_make_dotted_paths_ambiguous(self) -> None:
        class Packet(Schema):
            value = Int8()

        for name in ("", "Packet.part", "../Packet", "Packet/part", "Packet\\part"):
            for operation in (evaluate_schema, to_peach_data_model):
                with self.subTest(name=name, operation=operation.__name__):
                    if name == "":
                        # An empty override selects the declaration's model name.
                        result = operation(Packet, name=name, include_header=False)
                        self.assertIsNotNone(result)
                    else:
                        with self.assertRaisesRegex(ValueError, "XML element name.*cannot contain"):
                            operation(Packet, name=name, include_header=False)

    def test_inline_paths_match_every_emitted_node(self) -> None:
        @Flags(Int8, endian="big")
        class Status(Schema):
            ready = Bit[1]()
            rest = Bit[7]()

        class Payload(Schema):
            flags = Int8()
            tail = Optional[Int8](when=flags != 0)

        class Packet(Schema):
            status = Status()
            payload = Optional[Payload]()
            matrix = Array[Array[Int8, 2](), 3]()
            choice = Union(first=Int8(), second=Payload)

        result = evaluate_schema(Packet, name="Renamed", include_header=False)
        self.assert_paths_exist_in_xml(result, to_peach_data_model(Packet, name="Renamed", include_header=False))
        self.assertEqual(result.path, "Renamed")
        self.assertEqual(result.fields["matrix"].element.path, "Renamed.matrix[].item")
        payload = result.fields["payload"].element
        self.assertEqual(payload.fields["tail"].source_path, "Renamed.payload.payload_internal.flags")

    def test_reused_array_sources_follow_explicit_xml_overrides(self) -> None:
        class Element(Schema):
            flags = Int8()
            tail = Optional[Int8](when=flags != 0)

        @PacketUnion
        class Packets(Schema):
            packet_union = Union(first=Element, second=Element)

        class Stream(Schema):
            packets = Array[Packets, 2]()
            first = Array[Element(flags=Int16()), 2]()
            second = Array[Element, 2]()

        result = evaluate_schema(Stream)
        self.assert_paths_exist_in_xml(result, to_peach_data_model(Stream))
        first = result.fields["first"].element
        second = result.fields["second"].element
        self.assertEqual(first.fields["flags"].path, "Stream.first[].flags")
        self.assertEqual(first.fields["tail"].source_path, "Stream.first[].flags")
        self.assertEqual(second.fields["flags"].path, "Stream.second[].flags")
        self.assertEqual(second.fields["tail"].source_path, "Stream.second[].flags")

    def test_root_paths_follow_model_name_collision_resolution(self) -> None:
        class Payload(Schema):
            value = Int8()

        @PacketUnion
        class Packets(Schema):
            packet_union = Union(payload=Payload, other=Payload)

        class Stream(Schema):
            packets = Array[Packets, 2]()

        result = evaluate_schema(Stream, name="Payload")
        self.assertTrue(result.path.startswith("Payload__variant_"))
        self.assert_paths_exist_in_xml(result, to_peach_data_model(Stream, name="Payload"))
        self.assertEqual(result.fields["packets"].path, result.path + ".packets")

    def test_unnamed_union_paths_follow_renamed_alternative_models(self) -> None:
        def first_schema() -> type[Schema]:
            class Payload(Schema):
                value = Int8()
                tail = Optional[Int8](when=value != 0)
            return Payload

        def second_schema() -> type[Schema]:
            class Payload(Schema):
                value = Int16()
                tail = Optional[Int8](when=value != 0)
            return Payload

        first, second = first_schema(), second_schema()

        class Packet(Schema):
            choice = first | second

        @PacketUnion
        class Packets(Schema):
            packet_union = Union(first=Packet, second=Packet)

        result = evaluate_schema(Packets)
        self.assert_paths_exist_in_xml(result, to_peach_data_model(Packets))
        choice = result.fields["packet_union"].alternatives[0].fields["choice"]
        self.assertEqual(choice.alternatives[0].path, "Packets.packet_union.first.choice.Payload")
        self.assertTrue(choice.alternatives[1].path.startswith("Packets.packet_union.first.choice.Payload__variant_"))
        self.assertTrue(choice.alternatives[1].fields["value"].path.startswith("Packets.packet_union.first.choice.Payload__variant_"))
        for alternative in choice.alternatives:
            self.assertEqual(alternative.fields["tail"].source_path, alternative.fields["value"].path)

    def test_top_level_union_and_flags_use_xml_value_wrapper(self) -> None:
        class First(Schema):
            flags = Int8()
            tail = Optional[Int8](when=flags != 0)

        class Second(Schema):
            value = Int16()

        union = First | Second
        result = evaluate_schema(union, name="UnionModel", include_header=False)
        self.assert_paths_exist_in_xml(result, to_peach_data_model(union, name="UnionModel", include_header=False))
        self.assertEqual(result.path, "UnionModel.value")
        self.assertEqual(result.alternatives[0].fields["tail"].source_path, "UnionModel.value.First.flags")

        @Flags(Int8, endian="big")
        class Packed(Schema):
            value = Bit[8]()

        flags = evaluate_schema(Packed, name="FlagsModel", include_header=False)
        self.assert_paths_exist_in_xml(flags, to_peach_data_model(Packed, name="FlagsModel", include_header=False))
        self.assertEqual(flags.path, "FlagsModel.value")
        self.assertEqual(flags.fields["value"].path, "FlagsModel.value.value")

    def test_choice_present_branch_keeps_nested_condition_path(self) -> None:
        class Payload(Schema):
            flags = Int8()
            tail = Optional[Int8](when=(flags & 1) != 0)

        class Request(Schema):
            kind = Int8(fixed(1))
            payload = Optional[Payload]()

        class Ack(Schema):
            kind = Int8(fixed(2))

        @PacketUnion
        class Packet(Schema):
            packet_union = Union(request=Request, ack=Ack)

        result = evaluate_schema(Packet)
        self.assertIsInstance(result, SchemaResult)
        union = result.fields["packet_union"]
        self.assertIsInstance(union, UnionResult)
        request = union.alternatives[0]
        self.assertIsInstance(request, SchemaResult)
        choice = request.fields["payload"]
        self.assertIsInstance(choice, ArrayResult)
        self.assertEqual(choice.path, "Packet.packet_union.request.payload")
        self.assertIsInstance(choice.element, SchemaResult)
        self.assertEqual(
            choice.element.path, "Packet.packet_union.request.payload.payload_internal"
        )
        tail = choice.element.fields["tail"]
        self.assertIsInstance(tail, OptionalResult)
        self.assertEqual(
            tail.source_path, "Packet.packet_union.request.payload.payload_internal.flags"
        )

        root = ET.fromstring(to_peach_data_model(Packet))
        request_model = root.find("p:DataModel[@name='Request']", NS)
        self.assertIsNotNone(request_model)
        assert request_model is not None
        present = request_model.find(
            "p:Choice[@name='payload']/p:Block[@name='payload_internal']", NS
        )
        self.assertIsNotNone(present)
        assert present is not None
        self.assertEqual(present.get("ref"), "Payload")
        optional = root.find(
            "p:DataModel[@name='Payload']/p:Optional[@name='tail']", NS
        )
        self.assertIsNotNone(optional)
        assert optional is not None
        self.assertEqual(optional.get("src"), "flags")

    def test_array_element_nested_condition_uses_local_enclosing_scope(self) -> None:
        class Gates(Schema):
            enabled = Int8()

        class Element(Schema):
            gates = Gates()

            @Block
            class body(Schema):
                gates = Gates()
                value = Optional[Int8](when=gates.enabled != 0)

        class Request(Schema):
            gates = Gates()
            items = Array[Element, Occurs(1, 3)]()

        class Ack(Schema):
            kind = Int8(fixed(2))

        @PacketUnion
        class Packet(Schema):
            packet_union = Union(request=Request, ack=Ack)

        result = evaluate_schema(Packet)
        self.assertIsInstance(result, SchemaResult)
        union = result.fields["packet_union"]
        self.assertIsInstance(union, UnionResult)
        request = union.alternatives[0]
        self.assertIsInstance(request, SchemaResult)
        items = request.fields["items"]
        self.assertIsInstance(items, ArrayResult)
        self.assertIsInstance(items.element, SchemaResult)
        body = items.element.fields["body"]
        self.assertIsInstance(body, SchemaResult)
        value = body.fields["value"]
        self.assertIsInstance(value, OptionalResult)
        self.assertEqual(value.path, "Packet.packet_union.request.items[].body.value")
        self.assertEqual(value.source_path, "Packet.packet_union.request.items[].body.gates.enabled")

        root = ET.fromstring(to_peach_data_model(Packet))
        element = root.find("p:DataModel[@name='Element']", NS)
        self.assertIsNotNone(element)
        assert element is not None
        optional = element.find(
            "p:Block[@name='body']/p:Optional[@name='value']", NS
        )
        self.assertIsNotNone(optional)
        assert optional is not None
        self.assertEqual(optional.get("src"), "body.gates.enabled")

    def test_array_element_paths_include_root_and_item_marker(self) -> None:
        class Element(Schema):
            flags = Int8()
            length = Int8()
            content = Blob[length]()
            trailer = Optional[Int8](when=(flags & 1) != 0)

        class Request(Schema):
            kind = Int8(fixed(1))
            items = Array[Element, Occurs(1, 3)]()

        class Ack(Schema):
            kind = Int8(fixed(2))

        @PacketUnion
        class Packet(Schema):
            packet_union = Union(request=Request, ack=Ack)

        result = evaluate_schema(Packet)
        self.assertIsInstance(result, SchemaResult)
        union = result.fields["packet_union"]
        self.assertIsInstance(union, UnionResult)
        request = union.alternatives[0]
        self.assertIsInstance(request, SchemaResult)
        self.assertEqual(request.path, "Packet.packet_union.request")
        items = request.fields["items"]
        self.assertIsInstance(items, ArrayResult)
        self.assertEqual(items.path, "Packet.packet_union.request.items")
        self.assertIsInstance(items.element, SchemaResult)
        self.assertEqual(items.element.path, "Packet.packet_union.request.items[]")
        length = items.element.fields["length"]
        self.assertIsInstance(length, FieldResult)
        self.assertEqual(length.path, "Packet.packet_union.request.items[].length")
        trailer = items.element.fields["trailer"]
        self.assertIsInstance(trailer, OptionalResult)
        self.assertEqual(trailer.path, "Packet.packet_union.request.items[].trailer")
        self.assertEqual(trailer.source_path, "Packet.packet_union.request.items[].flags")

        root = ET.fromstring(to_peach_data_model(Packet))
        element_model = root.find("p:DataModel[@name='Element']", NS)
        self.assertIsNotNone(element_model)
        assert element_model is not None
        self.assertIsNotNone(
            element_model.find(
                "p:Number[@name='length']/p:Relation[@type='size']"
                "[@of='content']", NS
            )
        )
        optional = element_model.find("p:Optional[@name='trailer']", NS)
        self.assertIsNotNone(optional)
        assert optional is not None
        self.assertEqual(optional.get("src"), "flags")

    def test_nested_optionals_keep_xml_paths_and_condition_sources(self) -> None:
        class Payload(Schema):
            flags = Int8()
            body_length = Int16()
            body = Optional[Blob[2]](when=(flags & 1) != 0)

        class Header(Schema):
            present = Int8()
            payload = Optional[Payload](when=present != 0)

        class Request(Schema):
            kind = Int8(fixed(1))
            header = Header()
            footer = Blob[header.payload.body_length]()

        class Ack(Schema):
            kind = Int8(fixed(2))

        @PacketUnion
        class Packet(Schema):
            packet_union = Union(request=Request, ack=Ack)

        class Stream(Schema):
            packets = Array[Packet, Occurs(1, 4)]()

        stream = evaluate_schema(Stream)
        self.assertIsInstance(stream, SchemaResult)
        packets = stream.fields["packets"]
        self.assertIsInstance(packets.element, SchemaResult)
        union = packets.element.fields["packet_union"]
        self.assertIsInstance(union, UnionResult)
        request = union.alternatives[0]
        self.assertIsInstance(request, SchemaResult)
        self.assertEqual(request.path, 'Stream.packets[].packet_union.request')
        header = request.fields["header"]
        self.assertIsInstance(header, SchemaResult)
        self.assertEqual(header.path, 'Stream.packets[].packet_union.request.header')
        outer = header.fields["payload"]
        self.assertIsInstance(outer, OptionalResult)
        self.assertEqual(outer.path, 'Stream.packets[].packet_union.request.header.payload')
        self.assertEqual(outer.source_path, 'Stream.packets[].packet_union.request.header.present')
        self.assertIsInstance(outer.element, SchemaResult)
        self.assertEqual(
            outer.element.path, 'Stream.packets[].packet_union.request.header.payload.payload_internal'
        )
        length = outer.element.fields["body_length"]
        self.assertIsInstance(length, FieldResult)
        self.assertEqual(
            length.path, 'Stream.packets[].packet_union.request.header.payload.payload_internal.body_length'
        )
        inner = outer.element.fields["body"]
        self.assertIsInstance(inner, OptionalResult)
        self.assertEqual(
            inner.path, 'Stream.packets[].packet_union.request.header.payload.payload_internal.body'
        )
        self.assertEqual(
            inner.source_path, 'Stream.packets[].packet_union.request.header.payload.payload_internal.flags'
        )

        root = ET.fromstring(to_peach_data_model(Stream))
        packet = root.find("p:DataModel[@name='Request']", NS)
        self.assertIsNotNone(packet)
        assert packet is not None
        outer_xml = packet.find(
            "p:Block[@name='header']/p:Optional[@name='payload']", NS
        )
        self.assertIsNotNone(outer_xml)
        assert outer_xml is not None
        self.assertEqual(outer_xml.get("src"), "header.present")
        length_relation = packet.find(
            "p:Block[@name='header']/p:Optional[@name='payload']/"
            "p:Block[@name='payload_internal']/p:Number[@name='body_length']/"
            "p:Relation[@type='size']", NS
        )
        self.assertIsNotNone(length_relation)
        assert length_relation is not None
        self.assertEqual(length_relation.get("of"), "request.footer")
