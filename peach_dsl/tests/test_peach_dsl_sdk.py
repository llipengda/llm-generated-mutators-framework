# pyright: basic, reportAttributeAccessIssue=false, reportOptionalMemberAccess=false
from __future__ import annotations

from xml.etree import ElementTree as ET
import unittest

import peach_dsl
from peach_dsl import *


@Flags(Int16, endian="big")
class HeaderFlags(Schema):
    ack = Bit[1]()
    reserved = Bit[15](fixed(0))


class Header(Schema):
    kind = Int8()
    flags = HeaderFlags()
    body_length = Int32()


class NameTLV(Schema):
    length = Int16()
    value = String[length]()


class Hello(Schema):
    header = Header(kind=fixed(1))

    @Block[header.body_length]
    class body(Schema):
        name = NameTLV()


class Entry(Schema):
    value = Int8()


class ListPacket(Schema):
    entries = Array[Entry, Occurs(0, 4)]()


class EmptyPacket(Schema):
    value = Int8()


@PacketUnion
class Packets(Schema):
    packet_union = Union(hello=Hello, list=ListPacket)


@Default(endian="big", signed=False)
class PacketEnvelope(Schema):
    packets = Array[Packets, Occurs(1, 8)]()


class SDKTests(unittest.TestCase):
    def test_fixed_replaces_const_without_compatibility_alias(self) -> None:
        self.assertIsInstance(fixed(1), Fixed)
        self.assertFalse(hasattr(peach_dsl, "const"))
        self.assertFalse(hasattr(peach_dsl, "Const"))

    def test_schema_member_references_and_block_length(self) -> None:
        result = evaluate_schema(Hello)
        self.assertIsInstance(result, SchemaResult)
        header = result.fields["header"]
        body = result.fields["body"]
        self.assertIsInstance(header, SchemaResult)
        self.assertIsInstance(body, SchemaResult)
        self.assertEqual(body.length, FieldReference("header.body_length"))
        kind = header.fields["kind"]
        self.assertIsInstance(kind, FieldResult)
        self.assertIsNotNone(kind.fixed)
        self.assertEqual(kind.fixed.value, 1)

    def test_factories_accept_bare_constant_values(self) -> None:
        Custom = ExtendedType[str]("Custom")

        class LiteralValues(Schema):
            number = Int8(1)
            text = String("ok")
            payload = Blob(b"x")
            custom = Custom("value")
            optional = Optional[Int8](2, when=number == 1)

        result = evaluate_schema(LiteralValues)
        self.assertIsInstance(result, SchemaResult)
        for name, value in {
            "number": 1,
            "text": "ok",
            "payload": b"x",
            "custom": "value",
        }.items():
            field = result.fields[name]
            self.assertIsInstance(field, FieldResult)
            self.assertIsNone(field.fixed)
            self.assertEqual(field.value, value)
        optional = result.fields["optional"]
        self.assertIsInstance(optional, OptionalResult)
        self.assertIsInstance(optional.element, FieldResult)
        self.assertIsNone(optional.element.fixed)
        self.assertEqual(optional.element.value, 2)

        root = ET.fromstring(to_peach_data_model(LiteralValues, include_header=False))
        self.assertEqual(root.find("Number[@name='number']").attrib["token"], "false")
        self.assertEqual(root.find("String[@name='text']").attrib["token"], "false")

    def test_blob_bytes_values_are_emitted_as_hex(self) -> None:
        class ByteValues(Schema):
            fixed_value = Blob[2](fixed(b"\x00\xff"))
            default_value = Blob[2](b"\x12\x34")
            explicit_value_type = Blob[2](
                fixed(b"\xab\xcd"), value_type="string"
            )

        root = ET.fromstring(to_peach_data_model(ByteValues, include_header=False))
        fixed_value = root.find("Blob[@name='fixed_value']")
        default_value = root.find("Blob[@name='default_value']")
        explicit_value_type = root.find("Blob[@name='explicit_value_type']")

        self.assertIsNotNone(fixed_value)
        self.assertIsNotNone(default_value)
        self.assertIsNotNone(explicit_value_type)
        assert fixed_value is not None
        assert default_value is not None
        assert explicit_value_type is not None
        self.assertEqual(fixed_value.attrib["value"], "00ff")
        self.assertEqual(fixed_value.attrib["valueType"], "hex")
        self.assertEqual(fixed_value.attrib["token"], "true")
        self.assertEqual(default_value.attrib["value"], "1234")
        self.assertEqual(default_value.attrib["valueType"], "hex")
        self.assertEqual(default_value.attrib["token"], "false")
        self.assertEqual(explicit_value_type.attrib["valueType"], "string")

    def test_union_accepts_inline_scalar_fields(self) -> None:
        class TextChoice(Schema):
            @Union
            class value(Schema):
                first = String("first")
                second = String(fixed("second"))

        result = evaluate_schema(TextChoice)
        self.assertIsInstance(result, SchemaResult)
        choice = result.fields["value"]
        self.assertIsInstance(choice, UnionResult)
        self.assertTrue(
            all(isinstance(alternative, FieldResult) for alternative in choice.alternatives)
        )

        root = ET.fromstring(to_peach_data_model(TextChoice, include_header=False))
        choice_xml = root.find("Choice[@name='value']")
        self.assertIsNotNone(choice_xml)
        self.assertEqual(choice_xml.find("String[@name='first']").attrib["token"], "false")
        self.assertEqual(choice_xml.find("String[@name='second']").attrib["token"], "true")

    def test_nested_schema_member_can_be_overridden(self) -> None:
        class Inner(Schema):
            kind = Int8()

        class Outer(Schema):
            inner = Inner()

        customized = Outer(inner=Inner(kind=Int8(fixed(7))))
        result = evaluate_schema(customized)
        self.assertIsInstance(result, SchemaResult)
        inner = result.fields["inner"]
        self.assertIsInstance(inner, SchemaResult)
        kind = inner.fields["kind"]
        self.assertIsInstance(kind, FieldResult)
        self.assertEqual(kind.fixed.value, 7)

        @PacketUnion
        class Variants(Schema):
            packet = Union(customized=customized, plain=Outer)

        class Envelope(Schema):
            packet = Variants()

        root = ET.fromstring(to_peach_data_model(Envelope))
        peach = {"p": "http://peachfuzzer.com/2012/Peach"}
        override = root.find(
            "p:DataModel[@name='Variants']/p:Choice[@name='packet']/"
            "p:Block[@name='customized']/p:Block[@name='inner']/"
            "p:Number[@name='kind']",
            peach,
        )
        self.assertIsNotNone(override)
        self.assertEqual(override.attrib["value"], "7")

    def test_scalar_member_can_be_overridden_with_block(self) -> None:
        class Message(Schema):
            payload = Blob()

        customized = Message(
            payload=Block(kind=Int8(fixed(7)), contents=Blob())
        )
        result = evaluate_schema(customized)
        self.assertIsInstance(result, SchemaResult)
        payload = result.fields["payload"]
        self.assertIsInstance(payload, SchemaResult)
        self.assertEqual(payload.fields["kind"].fixed.value, 7)

        @PacketUnion
        class Variants(Schema):
            packet = Union(customized=customized, plain=Message)

        root = ET.fromstring(to_peach_data_model(Variants))
        peach = {"p": "http://peachfuzzer.com/2012/Peach"}
        block = root.find(
            "p:DataModel[@name='Variants']/p:Choice[@name='packet']/"
            "p:Block[@name='customized']/p:Block[@name='payload']",
            peach,
        )
        self.assertIsNotNone(block)
        assert block is not None
        self.assertIsNotNone(block.find("p:Number[@name='kind']", peach))
        self.assertIsNotNone(block.find("p:Blob[@name='contents']", peach))

    def test_every_member_kind_can_be_replaced_by_every_member_kind(self) -> None:
        class Nested(Schema):
            value = Int8()

        class Alternative(Schema):
            value = Int16()

        class Template(Schema):
            gate = Int8()
            value = Int8()

        def replacements() -> tuple[SchemaMember, ...]:
            return (
                Int32(),
                Nested(),
                Nested | Alternative,
                Union(nested=Nested, alternative=Alternative),
                Array[Nested, 2](),
                Optional[Nested](when=Template.gate == 1),
                Block(value=String()),
            )

        originals = replacements()
        expected_types = (
            FieldResult,
            SchemaResult,
            UnionResult,
            UnionResult,
            ArrayResult,
            OptionalResult,
            SchemaResult,
        )
        for original_index, original in enumerate(originals):
            class Container(Schema):
                gate = Int8()
                value = original

            for replacement_index, (replacement, expected_type) in enumerate(
                zip(replacements(), expected_types)
            ):
                with self.subTest(
                    original=original_index,
                    replacement=replacement_index,
                ):
                    result = evaluate_schema(Container(value=replacement))
                    self.assertIsInstance(result.fields["value"], expected_type)

    def test_cross_kind_overrides_are_emitted_for_referenced_models(self) -> None:
        class Nested(Schema):
            value = Int8()

        class Alternative(Schema):
            value = Int16()

        class Base(Schema):
            scalar = Int8()
            schema = Nested()
            union = Union(nested=Nested, alternative=Alternative)
            array = Array[Nested, 1]()
            optional = Optional[Nested]()
            block = Block(value=Blob())

        customized = Base(
            scalar=Array[Nested, 2](),
            schema=Int32(),
            union=Optional[Nested](),
            array=Block(value=String()),
            optional=Union(nested=Nested, alternative=Alternative),
            block=Alternative(),
        )

        @PacketUnion
        class Variants(Schema):
            packet = Union(customized=customized, plain=Base)

        root = ET.fromstring(to_peach_data_model(Variants))
        peach = {"p": "http://peachfuzzer.com/2012/Peach"}
        customized_xml = root.find(
            "p:DataModel[@name='Variants']/p:Choice[@name='packet']/"
            "p:Block[@name='customized']",
            peach,
        )
        self.assertIsNotNone(customized_xml)
        assert customized_xml is not None
        self.assertIsNotNone(customized_xml.find("p:Block[@name='scalar']", peach))
        self.assertIsNotNone(customized_xml.find("p:Number[@name='schema']", peach))
        self.assertIsNotNone(customized_xml.find("p:Block[@name='union']", peach))
        self.assertIsNotNone(customized_xml.find("p:Block[@name='array']", peach))
        self.assertIsNotNone(customized_xml.find("p:Choice[@name='optional']", peach))
        self.assertIsNotNone(customized_xml.find("p:Block[@name='block']", peach))

    def test_flags_and_inherited_defaults(self) -> None:
        result = evaluate_schema(PacketEnvelope)
        self.assertIsInstance(result, SchemaResult)
        packets = result.fields["packets"]
        self.assertIsInstance(packets, ArrayResult)
        self.assertIsInstance(packets.element, SchemaResult)
        union = packets.element.fields["packet_union"]
        self.assertIsInstance(union, UnionResult)
        hello = union.alternatives[0]
        header = hello.fields["header"]
        self.assertIsInstance(header, SchemaResult)
        flags = header.fields["flags"]
        self.assertIsInstance(flags, SchemaResult)
        self.assertIsNotNone(flags.flags_layout)
        ack = flags.fields["ack"]
        reserved = flags.fields["reserved"]
        self.assertIsInstance(ack, FieldResult)
        self.assertIsInstance(reserved, FieldResult)
        self.assertEqual(ack.position, 0)
        self.assertEqual(reserved.position, 1)
        kind = header.fields["kind"]
        self.assertIsInstance(kind, FieldResult)
        self.assertEqual(kind.endian, "big")
        self.assertFalse(kind.signed)

    def test_flags_positions_follow_declaration_order_independent_of_endian(self) -> None:
        @Flags(Int8, endian="little")
        class LittleEndianFlags(Schema):
            first = Bit[3]()
            second = Bit[5]()

        result = evaluate_schema(LittleEndianFlags)
        self.assertIsInstance(result, SchemaResult)
        first = result.fields["first"]
        second = result.fields["second"]
        self.assertIsInstance(first, FieldResult)
        self.assertIsInstance(second, FieldResult)
        self.assertEqual(first.position, 0)
        self.assertEqual(second.position, 3)

        class Envelope(Schema):
            flags = LittleEndianFlags()

        root = ET.fromstring(to_peach_data_model(Envelope, include_header=False))
        flags = root.find("Flags[@name='flags']")
        self.assertIsNotNone(flags)
        assert flags is not None
        self.assertEqual(flags.attrib["endian"], "little")
        self.assertEqual(flags.find("Flag[@name='first']").attrib["position"], "0")
        self.assertEqual(flags.find("Flag[@name='second']").attrib["position"], "3")

    def test_bit_outside_flags_exports_as_number(self) -> None:
        class BitFields(Schema):
            enabled = Bit[1](fixed(1))
            sequence = Bit[12]()

        root = ET.fromstring(to_peach_data_model(BitFields, include_header=False))
        enabled = root.find("Number[@name='enabled']")
        sequence = root.find("Number[@name='sequence']")
        self.assertIsNotNone(enabled)
        self.assertIsNotNone(sequence)
        assert enabled is not None
        assert sequence is not None
        self.assertEqual(enabled.attrib["size"], "1")
        self.assertEqual(enabled.attrib["value"], "1")
        self.assertEqual(sequence.attrib["size"], "12")
        self.assertIsNone(root.find("Flag[@name='enabled']"))

    def test_expr_rejects_two_field_references(self) -> None:
        class Sized(Schema):
            left = Int16()
            right = Int16()

        with self.assertRaisesRegex(ValueError, "at most one Field"):
            _ = Sized.left + Sized.right
        expression = Sized.left + 2
        self.assertIsInstance(expression, Expr)
        self.assertEqual(expression.operation, "+")

    def test_optional_and_occurs(self) -> None:
        class Conditional(Schema):
            count = Int8()
            value = Optional[Int16](when=count == 2)
            values = Array[Int8, Occurs(0, 3)]()

        result = evaluate_schema(Conditional)
        self.assertIsInstance(result, SchemaResult)
        optional = result.fields["value"]
        array = result.fields["values"]
        self.assertIsInstance(optional, OptionalResult)
        self.assertIsInstance(array, ArrayResult)
        self.assertEqual(array.count, Occurs(0, 3))
        self.assertEqual(optional.condition.operation, "==")

    def test_array_without_count_is_unbounded(self) -> None:
        class Item(Schema):
            value = Int8()

        class Container(Schema):
            items = Array[Item]()

        result = evaluate_schema(Container)
        items = result.fields["items"]
        self.assertIsInstance(items, ArrayResult)
        assert isinstance(items, ArrayResult)
        self.assertIsNone(items.count)
        self.assertIn("Array[unbounded]", str(items))

        root = ET.fromstring(to_peach_data_model(Container, include_header=False))
        array = root.find("Block[@name='items']")
        self.assertIsNotNone(array)
        assert array is not None
        self.assertEqual(array.get("minOccurs"), "0")
        self.assertEqual(array.get("maxOccurs"), "-1")

    def test_wrapped_members_are_accessed_explicitly(self) -> None:
        class LengthHeader(Schema):
            value = Int16()

        class First(Schema):
            value = Int8()

        class Second(Schema):
            value = Int8()

        @Union
        class ElementChoice(Schema):
            first = First()
            second = Second()

        class Container(Schema):
            optional = Optional[LengthHeader]()
            entries = Array[LengthHeader, Occurs(0, 2)]()
            union_entries = Array[ElementChoice, Occurs(0, 2)]()

            @Union
            class named_choice(Schema):
                first = First()
                second = Second()

            @Union
            class choice(Schema):
                first = First()
                second = Second()

        class SizedPacket(Schema):
            container = Container()
            optional_length: MemberRef[int] = container.optional.internal().value
            array_length: MemberRef[int] = container.entries.internal().value
            union_array_length: MemberRef[int] = (
                container.union_entries.internal().first.value
            )
            named_union_length: MemberRef[int] = container.named_choice.first.value
            union_length: MemberRef[int] = container.choice.first.value

            @Block[optional_length]
            class optional_body(Schema):
                payload = Blob()

            @Block[array_length]
            class array_body(Schema):
                payload = Blob()

            @Block[union_array_length]
            class union_array_body(Schema):
                payload = Blob()

            @Block[named_union_length]
            class named_union_body(Schema):
                payload = Blob()

            @Block[union_length]
            class union_body(Schema):
                payload = Blob()

        result = evaluate_schema(SizedPacket)
        self.assertIsInstance(result, SchemaResult)
        self.assertEqual(
            result.fields["optional_body"].length,
            FieldReference("container.optional.value"),
        )
        self.assertEqual(
            result.fields["array_body"].length,
            FieldReference("container.entries.value"),
        )
        self.assertEqual(
            result.fields["union_array_body"].length,
            FieldReference("container.union_entries.first.value"),
        )
        self.assertEqual(
            result.fields["named_union_body"].length,
            FieldReference("container.named_choice.first.value"),
        )
        self.assertEqual(
            result.fields["union_body"].length,
            FieldReference("container.choice.first.value"),
        )

    def test_block_class_decorator_has_local_reference_scope(self) -> None:
        class DecoratedBlockPacket(Schema):
            length = Int8()

            @Block[length]
            class body(Schema):
                flags = Int8()
                value = Optional[Int8](when=(flags & 0x01) != 0)

        xml = to_peach_data_model(DecoratedBlockPacket, include_header=False)
        root = ET.fromstring(xml)
        optional = root.find("Block[@name='body']/Optional[@name='value']")
        self.assertIsNotNone(optional)
        assert optional is not None
        self.assertEqual(optional.attrib["src"], "body.flags")

    def test_decorated_block_preserves_its_internal_schema_type(self) -> None:
        class Envelope(Schema):
            @Block
            class body(Schema):
                length = Int8()

        class Packet(Schema):
            envelope = Envelope()
            payload_length: MemberRef[int] = envelope.body.length

            @Block[payload_length]
            class payload(Schema):
                contents = Blob()

        result = evaluate_schema(Packet)
        self.assertIsInstance(result, SchemaResult)
        self.assertEqual(
            result.fields["payload"].length,
            FieldReference("envelope.body.length"),
        )

    def test_decorated_block_constructs_nested_overrides(self) -> None:
        class Packet(Schema):
            @Block
            class body(Schema):
                payload = Block(data=Blob())

            @classmethod
            def build(cls, payload: Override) -> Packet:
                return cls(body=cls.body(payload=payload))

        packet = Packet.build(Block(kind=Int8(fixed(7)), data=Blob()))
        result = evaluate_schema(packet)
        body = result.fields["body"]
        self.assertIsInstance(body, SchemaResult)
        payload = body.fields["payload"]
        self.assertIsInstance(payload, SchemaResult)
        self.assertEqual(payload.fields["kind"].fixed.value, 7)

    def test_anonymous_block_constructs_nested_overrides(self) -> None:
        class Packet(Schema):
            payload = Block(kind=Int8(), contents=Blob())

        customized = Packet(payload=Packet.payload(kind=fixed(7)))
        result = evaluate_schema(customized)
        payload = result.fields["payload"]

        self.assertIsInstance(payload, SchemaResult)
        self.assertEqual(payload.fields["kind"].fixed.value, 7)
        self.assertIn("contents", payload.fields)

    def test_anonymous_block_member_accepts_cross_kind_override(self) -> None:
        class Packet(Schema):
            payload = Block(value=Int8())

        customized = Packet(payload=Packet.payload(value=Array[Int16, 2]()))
        result = evaluate_schema(customized)
        payload = result.fields["payload"]
        self.assertIsInstance(payload, SchemaResult)
        self.assertIsInstance(payload.fields["value"], ArrayResult)

        root = ET.fromstring(to_peach_data_model(customized, include_header=False))
        value = root.find("Block[@name='payload']/Number[@name='value']")
        self.assertIsNotNone(value)
        assert value is not None
        self.assertEqual(value.attrib["occurs"], "2")

    def test_anonymous_block_override_rejects_unknown_fields(self) -> None:
        class Packet(Schema):
            payload = Block(data=Blob())

        with self.assertRaisesRegex(TypeError, "Packet.payload has no field.*missing"):
            Packet.payload(missing=Blob())

    def test_nested_schema_member_constructs_overrides(self) -> None:
        class Inner(Schema):
            a = Int8()
            b = Int16()

        class Outer(Schema):
            c = Inner(a=fixed(1))

            @classmethod
            def build(
                cls,
                a: FieldOverride[int],
                b: FieldOverride[int],
            ) -> Outer:
                return cls(c=cls.c(a=a, b=b))

        result = evaluate_schema(Outer.build(fixed(7), fixed(9)))
        inner = result.fields["c"]
        self.assertIsInstance(inner, SchemaResult)
        self.assertEqual(inner.fields["a"].fixed.value, 7)
        self.assertEqual(inner.fields["b"].fixed.value, 9)

    def test_direct_block_call_is_still_supported(self) -> None:
        class DirectBlockPacket(Schema):
            length = Int8()
            body = Block[length](value=Int8())

        result = evaluate_schema(DirectBlockPacket)
        self.assertIsInstance(result, SchemaResult)
        body = result.fields["body"]
        self.assertIsInstance(body, SchemaResult)
        self.assertEqual(body.length, FieldReference("length"))

    def test_named_union_and_legacy_union(self) -> None:
        class First(Schema):
            value = Int8(fixed(1))

        class Second(Schema):
            value = Int8(fixed(2))

        class Named(Schema):
            choice = Union(first=First, second=Second)

        class Legacy(Schema):
            choice = First | Second

        named = evaluate_schema(Named)
        legacy = evaluate_schema(Legacy)
        self.assertIsInstance(named, SchemaResult)
        self.assertIsInstance(legacy, SchemaResult)
        named_choice = named.fields["choice"]
        legacy_choice = legacy.fields["choice"]
        self.assertIsInstance(named_choice, UnionResult)
        self.assertIsInstance(legacy_choice, UnionResult)
        self.assertEqual(
            tuple(item.name for item in named_choice.alternatives),
            ("first", "second"),
        )
        self.assertEqual(
            tuple(item.name for item in legacy_choice.alternatives),
            ("First", "Second"),
        )

    def test_decorated_union(self) -> None:
        class First(Schema):
            value = Int8(fixed(1))

        class Second(Schema):
            value = Int8(fixed(2))

        class Decorated(Schema):
            @Union
            class choice:
                first = First
                second = Second

        result = evaluate_schema(Decorated)
        self.assertIsInstance(result, SchemaResult)
        choice = result.fields["choice"]
        self.assertIsInstance(choice, UnionResult)
        self.assertEqual(
            tuple(item.name for item in choice.alternatives),
            ("first", "second"),
        )

    def test_decorated_union_exposes_bound_alternatives(self) -> None:
        class A(Schema):
            @Union
            class union(Schema):
                a = Int8()
                b = Int16()

        a: MemberRef[int] = A().union.a
        b: MemberRef[int] = A().union.b
        self.assertEqual(a.name, "union.a")
        self.assertEqual(b.name, "union.b")

    def test_anonymous_block_does_not_expose_internal_field_paths(self) -> None:
        class Packet(Schema):
            body = Block(length=Int8(), data=Blob())

        with self.assertRaisesRegex(AttributeError, "use the @Block class form"):
            Packet().body.length

    def test_anonymous_unions_do_not_expose_internal_field_paths(self) -> None:
        class First(Schema):
            value = Int8()

        class Second(Schema):
            value = Int8()

        class Packet(Schema):
            named = Union(first=First, second=Second)
            unnamed = First | Second

        with self.assertRaisesRegex(AttributeError, "use the @Union class form"):
            Packet().named.first
        with self.assertRaisesRegex(AttributeError, "use the @Union class form"):
            Packet().unnamed.First

    def test_ordinary_array_element_paths_restart_inside_first_element(self) -> None:
        result = evaluate_schema(PacketEnvelope)
        self.assertIsInstance(result, SchemaResult)
        packets = result.fields["packets"]
        self.assertIsInstance(packets, ArrayResult)
        self.assertIsNone(packets.path)
        self.assertIsInstance(packets.element, SchemaResult)
        union = packets.element.fields["packet_union"]
        self.assertIsInstance(union, UnionResult)
        hello, list_packet = union.alternatives
        self.assertEqual(hello.path, "hello")
        header = hello.fields["header"]
        self.assertIsInstance(header, SchemaResult)
        magic = header.fields["kind"]
        self.assertIsInstance(magic, FieldResult)
        self.assertEqual(magic.path, "hello.header.kind")
        entries = list_packet.fields["entries"]
        self.assertIsInstance(entries, ArrayResult)
        self.assertEqual(entries.path, "list.entries")
        self.assertIsInstance(entries.element, SchemaResult)
        self.assertEqual(entries.element.path, "")
        value = entries.element.fields["value"]
        self.assertIsInstance(value, FieldResult)
        self.assertEqual(value.path, "value")

    def test_ordinary_array_relation_paths_restart_inside_first_element(self) -> None:
        class Text(Schema):
            length = Int8()
            value = Blob[length]()

        @Union
        class Item(Schema):
            server_reference = Text()
            reason_string = Text()

        class Properties(Schema):
            items = Array[Item]()

        root = ET.fromstring(to_peach_data_model(Properties, include_header=False))
        relation = root.find(
            "Choice[@name='items']/Block[@name='server_reference']/"
            "Number[@name='length']/Relation"
        )
        self.assertIsNotNone(relation)
        assert relation is not None
        self.assertEqual(relation.attrib["of"], "server_reference.value")

    def test_packet_union_validation(self) -> None:
        with self.assertRaisesRegex(TypeError, "exactly one Union"):

            @PacketUnion
            class Invalid(Schema):
                value = Int8()

    def test_model_name_collisions_merge_equal_and_rename_unequal_schemas(self) -> None:
        def duplicate(value: int) -> type[Schema]:
            class Duplicate(Schema):
                kind = Int8(fixed(value))

            return Duplicate

        first = duplicate(1)
        equal = duplicate(1)
        different = duplicate(2)

        @PacketUnion
        class Root(Schema):
            packet = Union(first=first, equal=equal, different=different)

        root = ET.fromstring(to_peach_data_model(Root))
        peach = "http://peachfuzzer.com/2012/Peach"
        models = root.findall(f"{{{peach}}}DataModel")
        duplicate_models = [
            model.attrib["name"]
            for model in models
            if model.attrib["name"].startswith("Duplicate")
        ]
        self.assertEqual(len(duplicate_models), 2)
        self.assertEqual(duplicate_models[0], "Duplicate")
        self.assertRegex(
            duplicate_models[1],
            r"^Duplicate__variant_[0-9a-f]{8}$",
        )

        root_model = root.find(f"{{{peach}}}DataModel[@name='Root']")
        self.assertIsNotNone(root_model)
        assert root_model is not None
        refs = {
            block.attrib["name"]: block.attrib["ref"]
            for block in root_model.findall(
                f"{{{peach}}}Choice[@name='packet']/{{{peach}}}Block"
            )
        }
        self.assertEqual(
            refs,
            {
                "first": "Duplicate",
                "equal": "Duplicate",
                "different": duplicate_models[1],
            },
        )

    def test_peach_document_headers_and_relations(self) -> None:
        xml = to_peach_data_model(PacketEnvelope, name="packets")
        self.assertTrue(xml.startswith("<?xml version='1.0' encoding='utf-8'?>"))
        root = ET.fromstring(xml)
        peach = "http://peachfuzzer.com/2012/Peach"
        xsi = "http://www.w3.org/2001/XMLSchema-instance"
        self.assertEqual(root.tag, f"{{{peach}}}Peach")
        self.assertEqual(
            root.attrib[f"{{{xsi}}}schemaLocation"],
            f"{peach} /peach/peach.xsd",
        )
        models = root.findall(f"{{{peach}}}DataModel")
        self.assertEqual(
            [model.attrib["name"] for model in models],
            ["Header", "NameTLV", "Hello", "Entry", "ListPacket", "Packets", "packets"],
        )
        packets = root.find(f"{{{peach}}}DataModel[@name='Packets']")
        self.assertIsNotNone(packets)
        hello_ref = packets.find(
            f"{{{peach}}}Choice[@name='packet_union']/"
            f"{{{peach}}}Block[@name='hello'][@ref='Hello']"
        )
        self.assertIsNotNone(hello_ref)
        assert hello_ref is not None
        self.assertEqual(len(hello_ref), 0)
        hello = root.find(f"{{{peach}}}DataModel[@name='Hello']")
        self.assertIsNotNone(hello)
        header_ref = hello.find(f"{{{peach}}}Block[@name='header'][@ref='Header']")
        self.assertIsNotNone(header_ref)
        relation = header_ref.find(
            f"{{{peach}}}Number[@name='body_length']/"
            f"{{{peach}}}Relation[@type='size'][@of='hello.body']"
        )
        self.assertIsNotNone(relation)
        name_ref = hello.find(
            f"{{{peach}}}Block[@name='body']/"
            f"{{{peach}}}Block[@name='name'][@ref='NameTLV']"
        )
        self.assertIsNotNone(name_ref)
        nested_relation = name_ref.find(
            f"{{{peach}}}Number[@name='length']/"
            f"{{{peach}}}Relation[@type='size'][@of='hello.body.name.value']"
        )
        self.assertIsNone(nested_relation)
        name_model = root.find(f"{{{peach}}}DataModel[@name='NameTLV']")
        self.assertIsNotNone(name_model)
        assert name_model is not None
        canonical_relation = name_model.find(
            f"{{{peach}}}Number[@name='length']/"
            f"{{{peach}}}Relation[@type='size'][@of='value']"
        )
        self.assertIsNotNone(canonical_relation)
        packet_array = root.find(f"{{{peach}}}DataModel[@name='packets']")
        self.assertIsNotNone(packet_array)
        assert packet_array is not None
        packets_ref = packet_array.find(
            f"{{{peach}}}Block[@name='packets'][@ref='Packets']"
        )
        self.assertIsNotNone(packets_ref)
        assert packets_ref is not None
        self.assertEqual(len(packets_ref), 0)
        relation_targets = {
            relation.attrib["of"]
            for relation in root.findall(f".//{{{peach}}}Relation[@of]")
        }
        self.assertFalse(
            any(
                "packet_union" in target or target.startswith("packets.")
                for target in relation_targets
            )
        )

    def test_peach_bare_data_model(self) -> None:
        xml = to_peach_data_model(Hello, include_header=False)
        self.assertTrue(xml.startswith('<DataModel name="Hello">'))
        self.assertNotIn("<Peach", xml)
        root = ET.fromstring(xml)
        self.assertEqual(root.tag, "DataModel")

    def test_peach_array_count_relation(self) -> None:
        class Counted(Schema):
            count = Int8()
            values = Array[Int16, count]()

        xml = to_peach_data_model(Counted, include_header=False)
        root = ET.fromstring(xml)
        relation = root.find(
            "Number[@name='count']/Relation[@type='count'][@of='values']"
        )
        self.assertIsNotNone(relation)

    def test_peach_array_count_relation_with_expr_length(self) -> None:
        class Counted(Schema):
            encoded_count = Int8()
            values = Array[Int16, encoded_count + 1]()

        xml = to_peach_data_model(Counted, include_header=False)
        root = ET.fromstring(xml)
        relation = root.find(
            "Number[@name='encoded_count']/Relation"
            "[@type='count'][@of='values']"
        )
        self.assertIsNotNone(relation)
        assert relation is not None
        self.assertEqual(relation.attrib["expressionGet"], "(count + 1)")
        self.assertEqual(relation.attrib["expressionSet"], "(count - 1)")

    def test_outer_references_survive_nested_container_binding(self) -> None:
        class Header(Schema):
            payload_size = Int8()
            short_size = Int8()
            long_size = Int8()
            item_count = Int8()
            flags = Int8()

        class Packet(Schema):
            header = Header()
            payload = Blob[header.payload_size]()
            choice = Union(
                short=Blob[header.short_size](),
                long=Blob[header.long_size](),
            )
            items = Array[Int8, header.item_count]()
            conditional = Union(
                present=Block(
                    item=Optional[Int8](when=(header.flags & 1) != 0),
                ),
                empty=Block(),
            )

        root = ET.fromstring(to_peach_data_model(Packet, include_header=False))
        header = root.find("Block[@name='header']")
        self.assertIsNotNone(header)
        assert header is not None
        expected_relations = {
            "payload_size": ("size", "payload"),
            "short_size": ("size", "choice.short"),
            "long_size": ("size", "choice.long"),
            "item_count": ("count", "items"),
        }
        for field_name, (relation_type, target) in expected_relations.items():
            relation = header.find(
                f"Number[@name='{field_name}']/Relation"
                f"[@type='{relation_type}'][@of='{target}']"
            )
            self.assertIsNotNone(relation, field_name)

        optional = root.find(
            "Choice[@name='conditional']/Block[@name='present']/"
            "Optional[@name='item']"
        )
        self.assertIsNotNone(optional)
        assert optional is not None
        self.assertEqual(optional.attrib["src"], "header.flags")

    def test_deep_external_relation_is_emitted_through_model_references(self) -> None:
        class LengthValue(Schema):
            value = Int16()

        class Headers(Schema):
            content_length = Optional[LengthValue]()

        class BodyData(Schema):
            data = Blob()

        @Union
        class Body(Schema):
            framed = BodyData()
            empty = Block()

        class Request(Schema):
            headers = Headers()
            body = Body(
                framed=BodyData(
                    data=Blob[headers.content_length.internal().value](),
                ),
            )

        class Other(Schema):
            kind = Int8(fixed(0))

        @PacketUnion
        class Packets(Schema):
            packet_union = Union(request=Request, other=Other)

        class Envelope(Schema):
            packets = Array[Packets, Occurs(1, 4)]()

        root = ET.fromstring(to_peach_data_model(Envelope))
        peach = "http://peachfuzzer.com/2012/Peach"
        request = root.find(f"{{{peach}}}DataModel[@name='Request']")
        self.assertIsNotNone(request)
        assert request is not None
        relation = request.find(
            f"{{{peach}}}Block[@name='headers'][@ref='Headers']/"
            f"{{{peach}}}Block[@name='content_length'][@ref='LengthValue']/"
            f"{{{peach}}}Number[@name='value']/"
            f"{{{peach}}}Relation[@type='size']"
            "[@of='request.body.framed.data']"
        )
        self.assertIsNotNone(relation)

        envelope = root.find(f"{{{peach}}}DataModel[@name='Envelope']")
        self.assertIsNotNone(envelope)
        assert envelope is not None
        packets_ref = envelope.find(
            f"{{{peach}}}Block[@name='packets'][@ref='Packets']"
        )
        self.assertIsNotNone(packets_ref)
        assert packets_ref is not None
        self.assertEqual(len(packets_ref), 0)

    def test_peach_size_relation_with_expr_length(self) -> None:
        class Sized(Schema):
            encoded_size = Int16()
            payload = Blob[encoded_size + 2]()

        xml = to_peach_data_model(Sized, include_header=False)
        root = ET.fromstring(xml)
        relation = root.find(
            "Number[@name='encoded_size']/Relation"
            "[@type='size'][@of='payload']"
        )
        self.assertIsNotNone(relation)
        assert relation is not None
        self.assertEqual(relation.attrib["expressionGet"], "(size + 2)")
        self.assertEqual(relation.attrib["expressionSet"], "(size - 2)")

    def test_peach_optional_src_uses_packet_relative_path(self) -> None:
        class ConditionalPacket(Schema):
            count = Int8()
            value = Optional[Int8](when=count == 1)

        @PacketUnion
        class ConditionalPackets(Schema):
            packet_union = Union(conditional=ConditionalPacket, empty=EmptyPacket)

        xml = to_peach_data_model(ConditionalPackets)
        root = ET.fromstring(xml)
        peach = "http://peachfuzzer.com/2012/Peach"
        optional = root.find(
            f"{{{peach}}}DataModel[@name='ConditionalPacket']/"
            f"{{{peach}}}Optional[@name='value'][@src='conditional.count']"
        )
        self.assertIsNotNone(optional)

    def test_peach_optional_src_resolves_deep_packet_sibling(self) -> None:
        class TypeFlags(Schema):
            packet_type = Int8(fixed(3))
            flags = Int8()

        class Header(Schema):
            type_flags = TypeFlags()

        class PublishPacket(Schema):
            header = Header()
            body = Block(
                packet_identifier=Optional[Int8](
                    when=((header.type_flags.flags >> 1) & 0x03) != 0
                )
            )

        @PacketUnion
        class Packets(Schema):
            packet_union = Union(publish=PublishPacket, empty=EmptyPacket)

        xml = to_peach_data_model(Packets)
        root = ET.fromstring(xml)
        peach = "http://peachfuzzer.com/2012/Peach"
        optional = root.find(
            f"{{{peach}}}DataModel[@name='PublishPacket']/"
            f"{{{peach}}}Block[@name='body']/"
            f"{{{peach}}}Optional[@name='packet_identifier']"
        )
        self.assertIsNotNone(optional)
        assert optional is not None
        self.assertEqual(optional.attrib["src"], "publish.header.type_flags.flags")

    def test_peach_optional_internal_block_name_and_relation_path(self) -> None:
        class Properties(Schema):
            property_length = Int8()
            properties = Blob[property_length]()

        class Packet(Schema):
            remaining_length = Int8()
            props_optional = Optional[Properties](when=remaining_length >= 4)

        @PacketUnion
        class Packets(Schema):
            packet_union = Union(packet=Packet, empty=EmptyPacket)

        evaluated = evaluate_schema(Packets)
        packet_union = evaluated.fields["packet_union"]
        self.assertIsInstance(packet_union, UnionResult)
        props = packet_union.alternatives[0].fields["props_optional"]
        self.assertIsInstance(props, OptionalResult)
        self.assertIsInstance(props.element, SchemaResult)
        self.assertEqual(
            props.element.path,
            "packet.props_optional.props_optional_internal",
        )
        property_length = props.element.fields["property_length"]
        self.assertIsInstance(property_length, FieldResult)
        self.assertEqual(
            property_length.path,
            "packet.props_optional.props_optional_internal.property_length",
        )

        xml = to_peach_data_model(Packet, include_header=False)
        root = ET.fromstring(xml)
        optional = root.find("Optional[@name='props_optional']")
        self.assertIsNotNone(optional)
        assert optional is not None
        self.assertEqual(optional.attrib["src"], "remaining_length")
        properties = optional.find("Block[@name='props_optional_internal']")
        self.assertIsNotNone(properties)
        assert properties is not None
        relation = properties.find(
            "Number[@name='property_length']/Relation"
            "[@type='size'][@of='props_optional.props_optional_internal.properties']"
        )
        self.assertIsNotNone(relation)

    def test_typed_constraints_and_extra_peach_types(self) -> None:
        class Extra(Schema):
            count = Int16(
                constraint=lambda value: value >= 0 and value <= 10
            )
            ratio = Double(constraint=lambda value: value > 0.0)
            title = String[16](
                fixed("ok"),
                type="utf16",
                null_terminated=True,
                pad_character=" ",
                constraint=lambda value: value != "",
            )

        xml = to_peach_data_model(Extra, include_header=False)
        root = ET.fromstring(xml)
        count = root.find("Number[@name='count']")
        ratio = root.find("Double[@name='ratio']")
        title = root.find("String[@name='title']")
        self.assertIsNotNone(count)
        self.assertIsNotNone(ratio)
        self.assertIsNotNone(title)
        self.assertEqual(
            count.attrib["constraint"], "int(value) >= 0 and int(value) <= 10"
        )
        self.assertEqual(ratio.attrib["constraint"], "float(value) > 0.0")
        self.assertEqual(title.attrib["constraint"], "value != ''")
        self.assertEqual(title.attrib["type"], "utf16")
        self.assertEqual(title.attrib["nullTerminated"], "true")
        self.assertEqual(title.attrib["padCharacter"], " ")

        class RawConstraint(Schema):
            flags = Int8(constraint="(value & 1) == 0")

        raw_xml = to_peach_data_model(RawConstraint, include_header=False)
        raw_flags = ET.fromstring(raw_xml).find("Number[@name='flags']")
        self.assertIsNotNone(raw_flags)
        assert raw_flags is not None
        self.assertEqual(raw_flags.attrib["constraint"], "int(value) & 1 == 0")

        with self.assertRaisesRegex(ValueError, "unsupported Peach string type"):
            String(type="utf-8")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            String(encoding="utf8")  # type: ignore[call-arg]
        with self.assertRaises(TypeError):
            Bit[1](position=0)  # type: ignore[call-arg]

    def test_bool_is_not_a_dsl_field_type(self) -> None:
        self.assertFalse(hasattr(peach_dsl, "Bool"))
        with self.assertRaisesRegex(TypeError, "not a built-in Peach DSL field type"):
            Optional[bool]()
        BoolExtension = ExtendedType[bool]("BoolExtension")
        with self.assertRaisesRegex(TypeError, "not a supported Peach DSL scalar"):
            BoolExtension()
        with self.assertRaisesRegex(TypeError, "use 0 or 1"):
            Int8(True)
        with self.assertRaisesRegex(TypeError, "use 0 or 1"):
            Int8(fixed(False))

        class NumericBool(Schema):
            enabled = Int8(fixed(1))

        xml = to_peach_data_model(NumericBool, include_header=False)
        enabled = ET.fromstring(xml).find("Number[@name='enabled']")
        self.assertIsNotNone(enabled)
        assert enabled is not None
        self.assertEqual(enabled.attrib["value"], "1")

    def test_string_constraint_supports_len_and_indexing(self) -> None:
        class Header(Schema):
            name = String(
                fixed("X-Header"),
                constraint=lambda value: len(value) > 0
                and value[0] != "\r"
                and value != "Content-Length",
            )

        xml = to_peach_data_model(Header, include_header=False)
        root = ET.fromstring(xml)
        name = root.find("String[@name='name']")
        self.assertIsNotNone(name)
        assert name is not None
        self.assertEqual(
            name.attrib["constraint"],
            "len(value) > 0 and value[0] != '\\r' and (value != 'Content-Length')",
        )

    def test_string_constraint_supports_real_string_methods(self) -> None:
        class Reply(Schema):
            code = String(
                type="ascii",
                constraint=lambda value: 1 <= len(value) <= 4
                and value.isalpha(),
            )

        xml = to_peach_data_model(Reply, include_header=False)
        code = ET.fromstring(xml).find("String[@name='code']")
        self.assertIsNotNone(code)
        assert code is not None
        self.assertEqual(
            code.attrib["constraint"],
            "1 <= len(value) <= 4 and value.isalpha()",
        )

    def test_string_fields_support_conditional_expressions(self) -> None:
        class Header(Schema):
            code = String[1]()

        class Packet(Schema):
            header = Header()
            is_l_type = Optional[Int8](when=header.code == fixed("L"))

        xml = to_peach_data_model(Packet, include_header=False)
        root = ET.fromstring(xml)
        optional = root.find("Optional[@name='is_l_type']")
        self.assertIsNotNone(optional)
        assert optional is not None
        self.assertEqual(optional.attrib["src"], "header.code")
        self.assertEqual(optional.attrib["expression"], "(value == 'L')")

    def test_decimal_string_size_relation_preserves_numeric_expressions(self) -> None:
        class Packet(Schema):
            length = DecimalString[3](
                type="ascii",
                pad_character="0",
                constraint=lambda value: 0 <= value <= 999,
            )
            payload = Blob[length + 2]()

        root = ET.fromstring(to_peach_data_model(Packet, include_header=False))
        length = root.find("String[@name='length']")
        self.assertIsNotNone(length)
        assert length is not None
        self.assertEqual(length.attrib["length"], "3")
        self.assertEqual(length.attrib["type"], "ascii")
        self.assertEqual(length.attrib["padCharacter"], "0")
        self.assertEqual(
            length.attrib["constraint"],
            "0 <= int(value) <= 999",
        )
        relation = length.find("Relation[@type='size'][@of='payload']")
        self.assertIsNotNone(relation)
        assert relation is not None
        self.assertEqual(relation.attrib["expressionGet"], "(size + 2)")
        self.assertEqual(relation.attrib["expressionSet"], "(size - 2)")
        self.assertNotIn("int(", relation.attrib["expressionGet"])
        self.assertNotIn("str(", relation.attrib["expressionSet"])

    def test_decimal_string_count_relation_and_optional_condition(self) -> None:
        class Packet(Schema):
            count = DecimalString(type="ascii")
            separator = String(fixed("\r\n"), type="ascii")
            values = Array[Int8, count]()
            trailer = Optional[Int8](when=count > 0)

        root = ET.fromstring(to_peach_data_model(Packet, include_header=False))
        count = root.find("String[@name='count']")
        self.assertIsNotNone(count)
        assert count is not None
        relation = count.find("Relation[@type='count'][@of='values']")
        self.assertIsNotNone(relation)
        assert relation is not None
        self.assertNotIn("expressionGet", relation.attrib)
        self.assertNotIn("expressionSet", relation.attrib)
        optional = root.find("Optional[@name='trailer']")
        self.assertIsNotNone(optional)
        assert optional is not None
        self.assertEqual(optional.attrib["src"], "count")
        self.assertEqual(optional.attrib["expression"], "(int(value) > 0)")

    def test_extended_type_preserves_custom_attributes(self) -> None:
        Asn1 = ExtendedType[str]("Asn1")

        class Extension(Schema):
            payload = Asn1(fixed("hello"), encoding="ber", strict=True, tag=3)

        xml = to_peach_data_model(Extension, include_header=False)
        root = ET.fromstring(xml)
        payload = root.find("Asn1[@name='payload']")
        self.assertIsNotNone(payload)
        self.assertEqual(payload.attrib["value"], "hello")
        self.assertEqual(payload.attrib["token"], "true")
        self.assertEqual(payload.attrib["encoding"], "ber")
        self.assertEqual(payload.attrib["strict"], "true")
        self.assertEqual(payload.attrib["tag"], "3")

    def test_format_schema_result_includes_paths(self) -> None:
        rendered = str(evaluate_schema(PacketEnvelope))
        self.assertIn("[path=hello.header.kind]", rendered)
        self.assertIn("[path=value]", rendered)

    def test_evaluation_result_walk_visits_every_member_in_preorder(self) -> None:
        result = evaluate_schema(PacketEnvelope)

        visited = list(result.walk())

        self.assertIs(visited[0], result)
        self.assertEqual(
            [
                node.name if isinstance(node, (FieldResult, SchemaResult, ArrayResult, OptionalResult))
                else "one of"
                for node in visited
            ],
            [
                "PacketEnvelope",
                "packets",
                "Packets",
                "one of",
                "hello",
                "Header",
                "kind",
                "HeaderFlags",
                "ack",
                "reserved",
                "body_length",
                "Block",
                "NameTLV",
                "length",
                "value",
                "list",
                "entries",
                "Entry",
                "value",
            ],
        )
        union = next(node for node in visited if isinstance(node, UnionResult))
        self.assertIs(next(union.walk()), union)


if __name__ == "__main__":
    unittest.main()
