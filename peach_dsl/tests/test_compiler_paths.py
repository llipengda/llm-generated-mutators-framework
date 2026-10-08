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
        self.assertEqual(choice.path, "request.payload")
        self.assertIsInstance(choice.element, SchemaResult)
        self.assertEqual(
            choice.element.path, "request.payload.payload_internal"
        )
        tail = choice.element.fields["tail"]
        self.assertIsInstance(tail, OptionalResult)
        self.assertEqual(
            tail.source_path, "request.payload.payload_internal.flags"
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
        self.assertEqual(value.path, "body.value")
        self.assertEqual(value.source_path, "body.gates.enabled")

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

    def test_array_element_paths_restart_with_local_optional_condition(self) -> None:
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
        self.assertEqual(request.path, "request")
        items = request.fields["items"]
        self.assertIsInstance(items, ArrayResult)
        self.assertEqual(items.path, "request.items")
        self.assertIsInstance(items.element, SchemaResult)
        self.assertEqual(items.element.path, "")
        length = items.element.fields["length"]
        self.assertIsInstance(length, FieldResult)
        self.assertEqual(length.path, "length")
        trailer = items.element.fields["trailer"]
        self.assertIsInstance(trailer, OptionalResult)
        self.assertEqual(trailer.path, "trailer")
        self.assertEqual(trailer.source_path, "flags")

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

    def test_nested_optionals_keep_packet_paths_and_condition_sources(self) -> None:
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
        self.assertEqual(request.path, "request")
        header = request.fields["header"]
        self.assertIsInstance(header, SchemaResult)
        self.assertEqual(header.path, "request.header")
        outer = header.fields["payload"]
        self.assertIsInstance(outer, OptionalResult)
        self.assertEqual(outer.path, "request.header.payload")
        self.assertEqual(outer.source_path, "request.header.present")
        self.assertIsInstance(outer.element, SchemaResult)
        self.assertEqual(
            outer.element.path, "request.header.payload.payload_internal"
        )
        length = outer.element.fields["body_length"]
        self.assertIsInstance(length, FieldResult)
        self.assertEqual(
            length.path, "request.header.payload.payload_internal.body_length"
        )
        inner = outer.element.fields["body"]
        self.assertIsInstance(inner, OptionalResult)
        self.assertEqual(
            inner.path, "request.header.payload.payload_internal.body"
        )
        self.assertEqual(
            inner.source_path, "request.header.payload.payload_internal.flags"
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
        self.assertEqual(outer_xml.get("src"), "request.header.present")
        length_relation = packet.find(
            "p:Block[@name='header']/p:Optional[@name='payload']/"
            "p:Block[@name='payload_internal']/p:Number[@name='body_length']/"
            "p:Relation[@type='size']", NS
        )
        self.assertIsNotNone(length_relation)
        assert length_relation is not None
        self.assertEqual(length_relation.get("of"), "request.footer")
