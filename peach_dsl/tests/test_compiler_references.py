"""Regression tests for resolving DSL references to concrete Peach members."""

from __future__ import annotations

import unittest
from xml.etree import ElementTree as ET

if __package__ in ("", "tests"):
    __package__ = "peach_dsl.tests"

from .. import (
    Array,
    Blob,
    Block,
    Default,
    Int8,
    Int16,
    Optional,
    PacketUnion,
    Schema,
    String,
    Union,
    fixed,
    to_peach_data_model,
)


NS = {"p": "http://peachfuzzer.com/2012/Peach"}


class CompilerReferenceTests(unittest.TestCase):
    def test_relation_rejects_unresolvable_model_name_shadow(self) -> None:
        class Header(Schema):
            length = Int8(3)
            body = Block(data=Blob[1]())
            Packet = Block(body=Block(data=Blob[1]()))

        class Packet(Schema):
            header = Header()
            body = Block(data=Blob[header.length]())

        with self.assertRaisesRegex(ValueError, "relation.*ambiguous.*Packet"):
            to_peach_data_model(Packet, include_header=False)
        root = ET.fromstring(to_peach_data_model(Packet, name="Renamed", include_header=False))
        self.assertEqual(root.find("Block[@name='header']/Number/Relation").get("of"), "Renamed.body.data")

    def test_model_name_shadow_uses_safe_shorter_relation_path(self) -> None:
        class Packet(Schema):
            length = Int8(3)
            header = Block(body=Block(data=Blob[1]()), Packet=Block(body=Block(data=Blob[1]())))
            body = Block(data=Blob[length]())

        root = ET.fromstring(to_peach_data_model(Packet, include_header=False))
        self.assertEqual(root.find("Number/Relation").get("of"), "body.data")

    def test_fixed_padded_string_conditions_use_runtime_text(self) -> None:
        for terminated in (False, True):
            expected = "AXX\0" if terminated else "AXXX"

            class Header(Schema):
                tag = String[4](fixed("A"), type="ascii", pad_character="X", null_terminated=terminated)

            for nested in (False, True):
                class Packet(Schema):
                    if nested:
                        header = Header()
                        source = header.tag
                    else:
                        tag = String[4](fixed("A"), type="ascii", pad_character="X", null_terminated=terminated)
                        source = tag
                    padded = Optional[Int8](when=source == expected)
                    original = Optional[Int8](when=source == "A")
                    del source

                variants = (Packet, Packet(header=Header(tag=fixed("A")))) if nested else (Packet, Packet(tag=fixed("A")))
                for packet in variants:
                    with self.subTest(terminated=terminated, nested=nested, packet=packet):
                        root = ET.fromstring(to_peach_data_model(packet, include_header=False))
                        padded = root.find("Optional[@name='padded']")
                        original = root.find("Optional[@name='original']")
                        self.assertEqual(padded.get("src"), "header.tag" if nested else "tag")
                        self.assertTrue(eval(padded.get("expression"), {"value": expected}))
                        self.assertFalse(eval(original.get("expression"), {"value": expected}))

    def test_relations_qualify_targets_shadowed_by_inner_paths(self) -> None:
        for counted in (False, True):
            class Header(Schema):
                length = Int8(3)
                body = Block(data=Array[Int8, 1]() if counted else Blob[1]())

            class Packet(Schema):
                header = Header()
                body = Block(data=Array[Int8, header.length]() if counted else Blob[header.length]())

            for name in ("Packet", "Renamed"):
                with self.subTest(counted=counted, name=name):
                    root = ET.fromstring(to_peach_data_model(Packet, name=name, include_header=False))
                    relation = root.find("Block[@name='header']/Number/Relation")
                    self.assertEqual(relation.get("of"), name + ".body.data")
                    self.assertEqual(relation.get("type"), "count" if counted else "size")

            @PacketUnion
            class Packets(Schema):
                packet_union = Union(first=Packet, second=Packet)

            library = ET.fromstring(to_peach_data_model(Packets))
            packet = library.find("p:DataModel[@name='Packet']", NS)
            relation = packet.find("p:Block[@name='header']/p:Number/p:Relation", NS)
            self.assertEqual(relation.get("of"), "Packet.body.data")

    def test_optional_rejects_unresolvable_model_name_shadow(self) -> None:
        class Header(Schema):
            length = Int8()

        class Packet(Schema):
            header = Header()
            body = Block(
                header=Header(),
                Packet=Block(header=Header()),
                tail=Optional[Int8](when=header.length != 0),
            )

        with self.assertRaisesRegex(ValueError, "Optional.*ambiguous.*rename"):
            to_peach_data_model(Packet, include_header=False)
        root = ET.fromstring(to_peach_data_model(Packet, name="Renamed", include_header=False))
        self.assertEqual(root.find("Block/Optional").get("src"), "Renamed.header.length")

    def test_optional_parent_lookup_ignores_own_source_descendant(self) -> None:
        class Payload(Schema):
            source = Int8(9)

        class Packet(Schema):
            source = Int8(3)
            payload = Optional[Payload](when=source == 3)

        root = ET.fromstring(to_peach_data_model(Packet, include_header=False))
        self.assertEqual(root.find("Optional").get("src"), "source")
        for node in root.iter():
            for attribute in ("src", "of", "ref"):
                value = node.get(attribute)
                if value is not None:
                    self.assertNotIn("/", value)

    def test_outer_optional_source_uses_model_qualification(self) -> None:
        class Header(Schema):
            length = Int8()

        class Packet(Schema):
            header = Header()
            body = Block(
                header=Header(),
                tail=Optional[Int8](when=header.length == 3),
            )

        @PacketUnion
        class Packets(Schema):
            packet_union = Union(first=Packet, second=Packet)

        root = ET.fromstring(to_peach_data_model(Packets))
        optionals = root.findall(".//p:Optional", NS)
        self.assertTrue(optionals)
        self.assertEqual({node.get("src") for node in optionals}, {"Packet.header.length"})
        renamed = ET.fromstring(to_peach_data_model(Packet, name="Renamed", include_header=False))
        self.assertEqual(renamed.find("Block[@name='body']/Optional").get("src"), "Renamed.header.length")

    def test_inline_block_member_references_use_outer_instance_overrides(self) -> None:
        class Header(Schema):
            length = Int8(2)

        for nested in (False, True):
            class Packet(Schema):
                header = Header()
                content = Block(
                    header=Header(length=fixed(9)),
                    data=Blob[header.length + 1](),
                    items=Array[Int8, header.length](),
                    tail=Optional[Int8](when=header.length == 4),
                )
                if nested:
                    body = Block(inner=content)
                else:
                    body = content
                del content

            with self.subTest(nested=nested):
                root = ET.fromstring(to_peach_data_model(
                    Packet(header=Header(length=fixed(4))), include_header=False,
                ))
                self.assertEqual(root.findall(".//Relation"), [])
                self.assertEqual(root.find(".//Blob[@name='data']").get("length"), "5")
                self.assertEqual(root.find(".//Number[@name='items']").get("occurs"), "4")
                self.assertTrue(eval(root.find(".//Optional").get("expression"), {}))
                dynamic = ET.fromstring(to_peach_data_model(
                    Packet(header=Header(length=Int16(6))), include_header=False,
                ))
                prefix = "body.inner" if nested else "body"
                relations = dynamic.findall("Block[@name='header']/Number/Relation")
                self.assertEqual({r.get("of") for r in relations}, {prefix + ".data", prefix + ".items"})
                self.assertEqual(dynamic.find(".//Optional").get("src"),
                                 "Packet.header.length")
                with self.assertRaisesRegex(TypeError, "integer field.*string"):
                    to_peach_data_model(Packet(header=Header(length=String())), include_header=False)
                with self.assertRaisesRegex(ValueError, "replaced by a scalar"):
                    to_peach_data_model(Packet(header=Int8()), include_header=False)

    def test_inline_blocks_resolve_outer_fields_by_identity(self) -> None:
        for nested in (False, True):
            class Packet(Schema):
                length = Int8(2)
                if nested:
                    body = Block(inner=Block(length=Int8(9), data=Blob[length + 1]()))
                else:
                    body = Block(length=Int8(9), data=Blob[length + 1]())

            target = "body.inner.data" if nested else "body.data"
            for packet in (Packet, Packet(length=5), Packet(length=Int16(5))):
                with self.subTest(nested=nested, packet=packet):
                    root = ET.fromstring(to_peach_data_model(packet, include_header=False))
                    relation = root.find("Number/Relation")
                    self.assertIsNotNone(relation)
                    self.assertEqual(relation.get("of"), target)
                    self.assertEqual(eval(relation.get('expressionGet'), {'size': 3, 'count': 3}), eval(('(size + 1)') or "size", {'size': 3, 'count': 3}))
                    self.assertIsNone(root.find(".//Block/Number/Relation"))
            fixed_root = ET.fromstring(to_peach_data_model(Packet(length=fixed(4)), include_header=False))
            self.assertEqual(fixed_root.findall(".//Relation"), [])
            self.assertEqual(fixed_root.find(".//Blob").get("length"), "5")

    def test_inline_block_outer_conditions_and_counts_follow_overrides(self) -> None:
        class Packet(Schema):
            count = Int8(2)
            body = Block(
                count=Int8(9),
                items=Array[Int8, count](),
                tail=Optional[Int8](when=count == 3),
            )

        root = ET.fromstring(to_peach_data_model(Packet, include_header=False))
        self.assertEqual(root.find("Number/Relation").get("of"), "body.items")
        self.assertEqual(root.find("Block/Optional").get("src"), "Packet.count")
        fixed_root = ET.fromstring(to_peach_data_model(Packet(count=fixed(3)), include_header=False))
        self.assertEqual(fixed_root.find("Block/Number[@name='items']").get("occurs"), "3")

    def test_union_inline_block_keeps_boundary_and_outer_references(self) -> None:
        class Packet(Schema):
            size = Int8(2)
            choice = Union(a=Block[size](data=Blob[size]()), b=Int8())

        root = ET.fromstring(to_peach_data_model(Packet, include_header=False))
        self.assertEqual(
            {relation.get("of") for relation in root.findall("Number/Relation")},
            {"choice.a", "choice.a.data"},
        )
        fixed_root = ET.fromstring(to_peach_data_model(Packet(size=fixed(4)), include_header=False))
        self.assertEqual(fixed_root.find("Choice/Block").get("length"), "4")
        self.assertEqual(fixed_root.find("Choice/Block/Blob").get("length"), "4")

    def test_non_token_defaults_keep_runtime_references(self) -> None:
        class Header(Schema):
            length = Int8(2, token=False)

        for nested in (False, True):
            class Packet(Schema):
                if nested:
                    header = Header()
                    source = header.length
                else:
                    length = Int8(2, token=False)
                    source = length
                payload = Blob[source]()
                tail = Optional[Int8](when=source == 3)
                del source

            root = ET.fromstring(to_peach_data_model(Packet, include_header=False))
            source = root.find("Block/Number" if nested else "Number")
            self.assertEqual(source.get("token"), "false")
            self.assertEqual(source.find("Relation").get("of"), "payload")
            self.assertIsNone(root.find("Blob").get("length"))
            self.assertEqual(root.find("Optional").get("src"), "header.length" if nested else "length")
            invalid = Packet(header=Header(length=fixed(4))) if nested else Packet(length=fixed(4))
            with self.assertRaisesRegex(ValueError, "fixed.*token=False"):
                to_peach_data_model(invalid, include_header=False)

    def test_encoded_fixed_length_keeps_runtime_reference(self) -> None:
        for encoding in ("hex", "HEX", "literal"):
            for arithmetic in (False, True):
                class Packet(Schema):
                    size = Int8(fixed(12), value_type=encoding)
                    payload = Blob[size + 1 if arithmetic else size]()

                for packet in (Packet, Packet(size=fixed(14)),
                               Packet(size=Int8(fixed(16), value_type=encoding))):
                    with self.subTest(encoding=encoding, arithmetic=arithmetic, packet=packet):
                        root = ET.fromstring(to_peach_data_model(packet, include_header=False))
                        self.assertIsNone(root.find("Blob").get("length"))
                        relation = root.find("Number/Relation")
                        self.assertIsNotNone(relation)
                        self.assertEqual(relation.get("of"), "payload")
                        self.assertEqual(eval(relation.get('expressionGet'), {'size': 3, 'count': 3}), eval(('(size + 1)' if arithmetic else None) or "size", {'size': 3, 'count': 3}))

    def test_nested_encoded_fixed_length_keeps_runtime_reference(self) -> None:
        class Header(Schema):
            size = Int8(fixed(12), value_type="hex")

        for header in (Header(), Header(size=fixed(14)),
                       Header(size=Int8(fixed(16), value_type="hex"))):
            for arithmetic in (False, True):
                packet_header = header

                class Packet(Schema):
                    header = packet_header
                    payload = Blob[header.size + 1 if arithmetic else header.size]()

                with self.subTest(header=header, arithmetic=arithmetic):
                    root = ET.fromstring(to_peach_data_model(Packet, include_header=False))
                    self.assertIsNone(root.find("Blob").get("length"))
                    relation = root.find("Block/Number/Relation")
                    self.assertIsNotNone(relation)
                    self.assertEqual(relation.get("of"), "payload")
                    self.assertEqual(eval(relation.get('expressionGet'), {'size': 3, 'count': 3}), eval(('(size + 1)' if arithmetic else None) or "size", {'size': 3, 'count': 3}))

    def test_shared_model_keeps_each_parents_numeric_defaults(self) -> None:
        class Header(Schema):
            value = Int16()

        class Envelope(Schema):
            header = Header()

        @Default(endian="big", signed=True)
        class Big(Schema):
            envelope = Envelope()

        @Default(endian="little", signed=False)
        class Little(Schema):
            envelope = Envelope()

        for reverse in (False, True):
            @PacketUnion
            class Packet(Schema):
                packet_union = (
                    Union(little=Little, big=Big) if reverse else Union(big=Big, little=Little)
                )

            with self.subTest(reverse=reverse):
                root = ET.fromstring(to_peach_data_model(Packet))
                models = {m.get("name"): m for m in root.findall("p:DataModel", NS)}

                def child(element: ET.Element, name: str) -> ET.Element:
                    direct = element.find(f"*[@name='{name}']")
                    if direct is not None:
                        return direct
                    reference = element.get("ref")
                    assert reference is not None
                    return child(models[reference], name)

                for model_name, endian, signed in (
                    ("Big", "big", "true"), ("Little", "little", "false")
                ):
                    envelope = child(models[model_name], "envelope")
                    header = child(envelope, "header")
                    value = child(header, "value")
                    self.assertEqual(value.get("endian"), endian)
                    self.assertEqual(value.get("signed"), signed)

    def test_plain_override_replaces_fixed_value_and_restores_dynamic_relations(self) -> None:
        class Header(Schema):
            length = Int8(fixed(2))
            data = Blob[length]()
            items = Array[Int8, length + 1]()

        class Packet(Schema):
            header = Header(length=5)
            extra = Blob[header.length]()

        for target, prefix in ((Header(length=5), ""), (Packet, "header.")):
            with self.subTest(prefix=prefix):
                root = ET.fromstring(to_peach_data_model(target, include_header=False))
                container = root.find("Block[@name='header']") if prefix else root
                assert container is not None
                source = container.find("Number[@name='length']")
                data = container.find("Blob[@name='data']")
                assert source is not None and data is not None
                self.assertEqual(source.get("value"), "5")
                self.assertEqual(source.get("token"), "false")
                self.assertIsNone(data.get("length"))
                targets = {r.get("of") for r in source.findall("Relation")}
                self.assertIn(prefix + "data", targets)
                self.assertIn(prefix + "items", targets)
                if prefix:
                    self.assertIn("extra", targets)
                    extra = root.find("Blob[@name='extra']")
                    assert extra is not None
                    self.assertIsNone(extra.get("length"))

    def test_root_union_with_packet_union_honors_document_mode(self) -> None:
        class Request(Schema):
            length = Int8()
            data = Blob[length]()

        class Ack(Schema):
            kind = Int8(fixed(1))

        @PacketUnion
        class Packet(Schema):
            packet_union = Union(request=Request, ack=Ack)

        class Other(Schema):
            value = Int8()

        for target in (Union(packet=Packet, other=Other), Packet | Other):
            for include_header in (False, True):
                with self.subTest(target=target, include_header=include_header):
                    xml = to_peach_data_model(target, name="Root", include_header=include_header)
                    root = ET.fromstring(xml)
                    if include_header:
                        self.assertTrue(xml.startswith("<?xml"))
                        self.assertEqual(root.tag, "{http://peachfuzzer.com/2012/Peach}Peach")
                        model = root.find("p:DataModel[@name='Root']", NS)
                        assert model is not None
                        self.assertIsNotNone(model.find("p:Choice[@name='value']", NS))
                        relations = model.findall(".//p:Relation", NS)
                    else:
                        self.assertEqual(root.tag, "DataModel")
                        self.assertEqual(root.get("name"), "Root")
                        self.assertIsNotNone(root.find("Choice[@name='value']"))
                        relations = root.findall(".//Relation")
                    self.assertEqual(len(relations), 1)
                    self.assertEqual(relations[0].get("type"), "size")
                    self.assertEqual(relations[0].get("of"), "request.data")

    def test_direct_block_replacement_resolves_colliding_model_names(self) -> None:
        def make_element(wide: bool) -> type[Schema]:
            class Element(Schema):
                value = Int16() if wide else Int8()
            return Element

        old = make_element(False)
        new = make_element(True)
        reusable = Block(child=old())

        class Request(Schema):
            original = reusable
            replaced = reusable(child=new())

        class Ack(Schema):
            kind = Int8(fixed(1))

        @PacketUnion
        class Packet(Schema):
            packet_union = Union(request=Request, ack=Ack)

        xml = to_peach_data_model(Packet)
        self.assertEqual(xml, to_peach_data_model(Packet))
        root = ET.fromstring(xml)
        models = {
            model.get("name"): model for model in root.findall("p:DataModel", NS)
        }
        request = models["Request"]
        references: list[str] = []
        for block_name, width in (("original", "8"), ("replaced", "16")):
            child = request.find(f"p:Block[@name='{block_name}']/p:Block[@name='child']", NS)
            assert child is not None
            reference = child.get("ref")
            assert reference is not None
            references.append(reference)
            self.assertIn(reference, models)
            field = models[reference].find("p:Number[@name='value']", NS)
            assert field is not None
            self.assertEqual(field.get("size"), width)
        self.assertNotEqual(*references)
        order = list(models)
        for reference in references:
            self.assertLess(order.index(reference), order.index("Request"))

    def test_replaced_models_keep_baseline_reference_dependencies(self) -> None:
        class Leaf(Schema):
            value = Int8()

        class Old(Schema):
            leaf = Leaf()

        class New(Schema):
            value = Int16()

        class Part(Schema):
            child = Old()

        class Ack(Schema):
            kind = Int8(fixed(1))

        for replacement in (New(), Array[New, 2](), Union(new=New, ack=Ack)):
            class Request(Schema):
                part = Part(child=replacement)

            @PacketUnion
            class Packet(Schema):
                packet_union = Union(request=Request, ack=Ack)

            with self.subTest(replacement=replacement):
                root = ET.fromstring(to_peach_data_model(Packet))
                defined: set[str] = set()
                for model in root.findall("p:DataModel", NS):
                    for element in model.iter():
                        reference = element.get("ref")
                        if reference is not None:
                            self.assertIn(reference, defined, f"undefined or forward ref: {reference}")
                    model_name = model.get("name")
                    assert model_name is not None
                    defined.add(model_name)
                self.assertTrue({"Leaf", "Old", "New", "Part", "Request", "Packet"} <= defined)
                effective = root.find(
                    "p:DataModel[@name='Request']/p:Block[@name='part']/*[@name='child']", NS
                )
                assert effective is not None
                self.assertIsNone(effective.get("ref"))

    def test_model_library_preserves_root_instance_overrides_and_block_boundary(self) -> None:
        class Request(Schema):
            kind = Int8(fixed(1))

        class Ack(Schema):
            kind = Int8(fixed(2))

        @PacketUnion
        class Packet(Schema):
            length = Int8(fixed(2))
            data = Blob[length]()

            @Block[4]
            class body(Schema):
                value = Int8()

            packet_union = Union(request=Request, ack=Ack)

        target = Packet(length=fixed(5), body=Packet.body(value=fixed(9)))
        root = ET.fromstring(to_peach_data_model(target))
        model = root.find("p:DataModel[@name='Packet']", NS)
        assert model is not None
        length = model.find("p:Number[@name='length']", NS)
        data = model.find("p:Blob[@name='data']", NS)
        body = model.find("p:Block[@name='body']", NS)
        assert length is not None and data is not None and body is not None
        self.assertEqual(length.get("value"), "5")
        self.assertEqual(data.get("length"), "5")
        self.assertEqual(body.get("length"), "4")
        value = body.find("p:Number[@name='value']", NS)
        assert value is not None
        self.assertEqual(value.get("value"), "9")

    def test_root_union_override_collects_new_models_and_paths(self) -> None:
        class Old(Schema):
            kind = Int8(fixed(1))

        class Ack(Schema):
            kind = Int8(fixed(2))

        class New(Schema):
            length = Int8()
            data = Blob[length]()

        @PacketUnion
        class Packet(Schema):
            packet_union = Union(old=Old, ack=Ack)

        root = ET.fromstring(to_peach_data_model(
            Packet(packet_union=Union(replacement=New, ack=Ack))
        ))
        self.assertIsNone(root.find("p:DataModel[@name='Old']", NS))
        branch = root.find(
            "p:DataModel[@name='Packet']/p:Choice/p:Block[@name='replacement']", NS
        )
        assert branch is not None
        self.assertEqual(branch.get("ref"), "New")
        relation = root.find("p:DataModel[@name='New']/p:Number/p:Relation", NS)
        assert relation is not None
        self.assertEqual(relation.get("of"), "replacement.data")

    def test_library_overrides_refresh_dependent_lengths_and_counts(self) -> None:
        for fixed_base in (False, True):
            class Part(Schema):
                length = Int8(fixed(2)) if fixed_base else Int8()
                data = Blob[length]()
                items = Array[Int8, length + 1]()

            for is_fixed, override in ((True, fixed(5)), (False, Int8())):
                class Request(Schema):
                    part = Part(length=override)

                class Ack(Schema):
                    kind = Int8(fixed(1))

                @PacketUnion
                class Packet(Schema):
                    packet_union = Union(request=Request, ack=Ack)

                with self.subTest(fixed_base=fixed_base, override=override):
                    root = ET.fromstring(to_peach_data_model(Packet))
                    part = root.find("p:DataModel[@name='Request']/p:Block[@name='part']", NS)
                    assert part is not None
                    self.assertEqual(part.get("ref"), "Part")
                    data = part.find("p:Blob[@name='data']", NS)
                    items = part.find("p:Number[@name='items']", NS)
                    source = part.find("p:Number[@name='length']", NS)
                    assert data is not None and items is not None and source is not None
                    if is_fixed:
                        self.assertEqual(data.get("length"), "5")
                        self.assertEqual(items.get("occurs"), "6")
                        self.assertEqual(source.findall("p:Relation", NS), [])
                    else:
                        self.assertIsNone(data.get("length"))
                        self.assertEqual(
                            {r.get("type"): r.get("of") for r in source.findall("p:Relation", NS)},
                            {"size": "request.part.data", "count": "request.part.items"},
                        )

    def test_non_field_attribute_shadows_inherited_field_consistently(self) -> None:
        class Base(Schema):
            first = Int8()
            value = Int8()
            last = Int8()

        class Child(Base):
            value = None

        class Grandchild(Child):
            pass

        class Restored(Grandchild):
            value = Int16()

        for schema in (Child, Grandchild):
            with self.subTest(schema=schema.__name__):
                root = ET.fromstring(to_peach_data_model(schema, include_header=False))
                self.assertEqual([e.get("name") for e in root], ["first", "last"])
                with self.assertRaisesRegex(TypeError, "has no field.*value"):
                    schema(value=fixed(1))
        root = ET.fromstring(to_peach_data_model(Restored, include_header=False))
        value = root.find("Number[@name='value']")
        assert value is not None
        self.assertEqual(value.get("size"), "16")

    def test_shadowed_scalar_source_cannot_supply_old_constant(self) -> None:
        class Base(Schema):
            length = Int8(fixed(2))
            payload = Blob[length]()

        class Child(Base):
            length = None

        class Grandchild(Child):
            pass

        for schema in (Child, Grandchild):
            with self.subTest(schema=schema.__name__):
                with self.assertRaisesRegex(ValueError, "cannot resolve referenced field 'length'"):
                    to_peach_data_model(schema, include_header=False)

    def test_shadowed_nested_source_cannot_supply_old_constant(self) -> None:
        class Header(Schema):
            length = Int8(fixed(2))

        class Base(Schema):
            header = Header()
            payload = Blob[header.length + 1]()

        class Child(Base):
            header = None

        class Grandchild(Child):
            pass

        for schema in (Child, Grandchild):
            with self.subTest(schema=schema.__name__):
                with self.assertRaisesRegex(ValueError, "header.length.*absent"):
                    to_peach_data_model(schema, include_header=False)

    def test_nested_reference_rejects_scalar_replacing_root(self) -> None:
        for constant in (False, True):
            class Header(Schema):
                length = Int8(fixed(2)) if constant else Int8()

            class Base(Schema):
                header = Header()
                payload = Blob[header.length]()

            class Child(Base):
                header = Int8()

            for schema in (Child, Base(header=Int8())):
                with self.subTest(constant=constant, schema=schema):
                    with self.assertRaisesRegex(ValueError, "header.length.*replaced by a scalar"):
                        to_peach_data_model(schema, include_header=False)

    def test_nested_reference_rejects_scalar_replacing_intermediate_schema(self) -> None:
        class Size(Schema):
            length = Int8(fixed(2))

        class Header(Schema):
            size = Size()

        class Base(Schema):
            header = Header()
            payload = Blob[header.size.length + 1]()

        class Child(Base):
            header = Header(size=Int8())

        class Direct(Schema):
            header = Header(size=Int8())
            payload = Blob[header.size.length + 1]()

        for schema in (Child, Base(header=Header(size=Int8())), Direct):
            with self.subTest(schema=schema):
                with self.assertRaisesRegex(ValueError, "header.size.length.*replaced by a scalar"):
                    to_peach_data_model(schema, include_header=False)

    def test_scalar_reference_rejects_container_override_before_constant_folding(self) -> None:
        class Part(Schema):
            value = Int8()

        for expression in (False, True):
            for constant in (False, True):
                class Base(Schema):
                    length = Int8(fixed(2)) if constant else Int8()
                    payload = Blob[length + 1 if expression else length]()

                class Child(Base):
                    length = Part()

                replacements = (Part(), Array[Int8, 2](), Optional[Int8]())
                for schema in (Child, *(Base(length=r) for r in replacements)):
                    with self.subTest(expression=expression, constant=constant, schema=schema):
                        with self.assertRaisesRegex(ValueError, "length.*not a scalar"):
                            to_peach_data_model(schema, include_header=False)

    def test_bound_scalar_reference_rejects_container_override(self) -> None:
        class Part(Schema):
            value = Int8()

        class Header(Schema):
            length = Int8(fixed(2))

        class Packet(Schema):
            header = Header(length=Part())
            payload = Blob[header.length]()

        with self.assertRaisesRegex(ValueError, "header.length.*not a scalar"):
            to_peach_data_model(Packet, include_header=False)

    def test_inherited_nested_references_follow_replacement_schema(self) -> None:
        class Header(Schema):
            length = Int8()

        class WideHeader(Schema):
            length = Int16()

        class Base(Schema):
            header = Header()
            payload = Blob[header.length]()
            values = Array[Int8, header.length + 1]()
            tail = Optional[Int8](when=header.length != 0)

        class Child(Base):
            header = WideHeader()

        for schema, width in ((Base, "8"), (Child, "16")):
            with self.subTest(schema=schema.__name__):
                root = ET.fromstring(to_peach_data_model(schema, include_header=False))
                source = root.find("Block[@name='header']/Number[@name='length']")
                assert source is not None
                self.assertEqual(source.get("size"), width)
                self.assertEqual(
                    {r.get("type"): r.get("of") for r in source.findall("Relation")},
                    {"size": "payload", "count": "values"},
                )
                tail = root.find("Optional[@name='tail']")
                assert tail is not None
                self.assertEqual(tail.get("src"), "header.length")

    def test_inherited_deep_reference_uses_replacement_constants(self) -> None:
        class Size(Schema):
            length = Int8(fixed(2))

        class Header(Schema):
            size = Size()

        class Base(Schema):
            header = Header()
            payload = Blob[header.size.length + 1]()

        class Child(Base):
            header = Header(size=Size(length=fixed(7)))

        for schema, length in (
            (Base, 3),
            (Child, 8),
            (Child(header=Header(size=Size(length=fixed(10)))), 11),
        ):
            with self.subTest(length=length):
                root = ET.fromstring(to_peach_data_model(schema, include_header=False))
                payload = root.find("Blob[@name='payload']")
                assert payload is not None
                self.assertEqual(payload.get("length"), str(length))

    def test_inherited_nested_reference_rejects_removed_field(self) -> None:
        class Header(Schema):
            length = Int8(fixed(2))

        class Replacement(Schema):
            other = Int8()

        class Base(Schema):
            header = Header()
            payload = Blob[header.length]()

        class Child(Base):
            header = Replacement()

        with self.assertRaisesRegex(ValueError, "header.length.*absent"):
            to_peach_data_model(Child, include_header=False)

    def test_nested_reference_rejects_container_replacing_scalar(self) -> None:
        class Header(Schema):
            length = Int8(fixed(2))

        class Replacement(Schema):
            length = Header()

        class Base(Schema):
            header = Header()
            payload = Blob[header.length]()

        class Child(Base):
            header = Replacement()

        for schema in (Child, Base(header=Replacement())):
            with self.subTest(schema=schema):
                with self.assertRaisesRegex(ValueError, "header.length.*not a scalar"):
                    to_peach_data_model(schema, include_header=False)

    def test_inherited_references_follow_redefined_source(self) -> None:
        class Base(Schema):
            length = Int8()
            payload = Blob[length]()
            values = Array[Int8, length + 1]()
            tail = Optional[Int8](when=length != 0)

        class Child(Base):
            length = Int16()

        for schema, width in ((Child, "16"), (Base, "8")):
            with self.subTest(schema=schema.__name__):
                root = ET.fromstring(to_peach_data_model(schema, include_header=False))
                source = root.find("Number[@name='length']")
                assert source is not None
                self.assertEqual(source.get("size"), width)
                relations = {r.get("type"): r for r in source.findall("Relation")}
                self.assertEqual(relations["size"].get("of"), "payload")
                self.assertEqual(relations["count"].get("of"), "values")
                self.assertEqual(eval(relations['count'].get('expressionGet'), {'size': 3, 'count': 3}), eval(('(count + 1)') or "size", {'size': 3, 'count': 3}))
                tail = root.find("Optional[@name='tail']")
                assert tail is not None
                self.assertEqual(tail.get("src"), "length")

    def test_inherited_lengths_use_redefined_constants_and_instance_overrides(self) -> None:
        class Base(Schema):
            length = Int8(fixed(2))
            payload = Blob[length]()
            extra = Blob[length + 1]()

        class Child(Base):
            length = Int16(fixed(7))

        for schema, length in ((Base, 2), (Child, 7), (Child(length=fixed(9)), 9)):
            with self.subTest(length=length):
                root = ET.fromstring(to_peach_data_model(schema, include_header=False))
                payload = root.find("Blob[@name='payload']")
                extra = root.find("Blob[@name='extra']")
                assert payload is not None and extra is not None
                self.assertEqual(payload.get("length"), str(length))
                self.assertEqual(extra.get("length"), str(length + 1))

    def test_inherited_reference_does_not_rebind_unrelated_field(self) -> None:
        class Other(Schema):
            length = Int8()

        class Base(Schema):
            length = Int8()
            payload = Blob[Other.length]()

        class Child(Base):
            length = Int16(fixed(7))

        with self.assertRaisesRegex(ValueError, "cannot resolve relation source"):
            to_peach_data_model(Child, include_header=False)

    def test_diamond_inherited_reference_uses_mro_source(self) -> None:
        class Base(Schema):
            length = Int8(fixed(2))
            payload = Blob[length]()

        class Left(Base):
            pass

        class Right(Base):
            length = Int16()

        class Child(Left, Right):
            pass

        class Packet(Schema):
            first = Child()
            second = Child(length=fixed(5))

        root = ET.fromstring(to_peach_data_model(Packet, include_header=False))
        relation = root.find(
            "Block[@name='first']/Number[@name='length']/Relation[@type='size']"
        )
        assert relation is not None
        self.assertEqual(relation.get("of"), "first.payload")
        payload = root.find("Block[@name='second']/Blob[@name='payload']")
        assert payload is not None
        self.assertEqual(payload.get("length"), "5")

    def test_schema_defaults_survive_inheritance_and_partial_override(self) -> None:
        @Default(endian="big", signed=True)
        class Base(Schema):
            value = Int16()

        class Inherited(Base):
            extra = Int16()

        @Default(signed=False)
        class Changed(Base):
            extra = Int16()

        for schema, signed in ((Inherited, "true"), (Changed, "false"), (Base, "true")):
            with self.subTest(schema=schema.__name__):
                root = ET.fromstring(to_peach_data_model(schema, include_header=False))
                for number in root.findall("Number"):
                    self.assertEqual(number.get("endian"), "big")
                    self.assertEqual(number.get("signed"), signed)

    def test_stacked_default_decorators_preserve_other_options(self) -> None:
        @Default(signed=True)
        @Default(endian="big")
        class Packet(Schema):
            value = Int16()

        root = ET.fromstring(to_peach_data_model(Packet, include_header=False))
        self.assertIsNotNone(root.find(
            "Number[@name='value'][@endian='big'][@signed='true']"
        ))

    def test_multiple_inheritance_uses_python_field_precedence(self) -> None:
        class First(Schema):
            value = Int8(fixed(1))

        class Second(Schema):
            value = Int16(fixed(2))

        class Combined(First, Second):
            pass

        self.assertIs(Combined.value, First.value)
        root = ET.fromstring(to_peach_data_model(Combined, include_header=False))
        number = root.find("Number[@name='value']")
        self.assertIsNotNone(number)
        assert number is not None
        self.assertEqual(number.get("size"), "8")
        self.assertEqual(number.get("value"), "1")

    def test_diamond_inheritance_uses_nearest_declared_field(self) -> None:
        class Base(Schema):
            value = Int8(fixed(1))

        class Left(Base):
            pass

        class Right(Base):
            value = Int16(fixed(2))

        class Combined(Left, Right):
            pass

        self.assertIs(Combined.value, Right.value)
        root = ET.fromstring(to_peach_data_model(Combined, include_header=False))
        number = root.find("Number[@name='value']")
        self.assertIsNotNone(number)
        assert number is not None
        self.assertEqual(number.get("size"), "16")
        self.assertEqual(number.get("value"), "2")

    def test_union_blob_branch_resolves_enclosing_length(self) -> None:
        class Packet(Schema):
            length = Int8()
            payload = Union(data=Blob[length + 1](), empty=Block())

        root = ET.fromstring(to_peach_data_model(Packet, include_header=False))
        relation = root.find("Number[@name='length']/Relation[@type='size']")
        self.assertIsNotNone(relation)
        assert relation is not None
        self.assertEqual(relation.get("of"), "payload.data")
        self.assertEqual(eval(relation.get('expressionGet'), {'size': 3, 'count': 3}), eval(('(size + 1)') or "size", {'size': 3, 'count': 3}))

        fixed_root = ET.fromstring(to_peach_data_model(
            Packet(length=fixed(3)), include_header=False
        ))
        data = fixed_root.find("Choice[@name='payload']/Blob[@name='data']")
        self.assertIsNotNone(data)
        assert data is not None
        self.assertEqual(data.get("length"), "4")
        self.assertEqual(fixed_root.findall(".//Relation"), [])

    def test_container_blob_length_uses_enclosing_fixed_override(self) -> None:
        for kind in ("array", "optional", "conditional", "nested_array"):
            with self.subTest(kind=kind):
                class Packet(Schema):
                    length = Int8()
                    item = Int8()
                    if kind == "array":
                        payload = Array[Blob[length + 1], 2]()
                    elif kind == "nested_array":
                        payload = Array[Array[Blob[length + 1], 2](), 3]()
                    else:
                        payload = Optional[Blob[length + 1]](
                            when=length != 0 if kind == "conditional" else None
                        )

                dynamic = ET.fromstring(to_peach_data_model(Packet, include_header=False))
                relation = dynamic.find("Number[@name='length']/Relation[@type='size']")
                self.assertIsNotNone(relation)
                assert relation is not None
                target = {
                    "array": "payload", "optional": "payload",
                    "conditional": "payload.value", "nested_array": "payload.item",
                }[kind]
                self.assertEqual(relation.get("of"), target)
                self.assertEqual(eval(relation.get('expressionGet'), {'size': 3, 'count': 3}), eval(('(size + 1)') or "size", {'size': 3, 'count': 3}))

                root = ET.fromstring(to_peach_data_model(
                    Packet(length=fixed(3), item=fixed(99)), include_header=False
                ))
                blobs = root.findall(".//Blob")
                self.assertEqual(len(blobs), 1)
                self.assertEqual(blobs[0].get("length"), "4")
                self.assertIsNone(blobs[0].get("value"))
                self.assertEqual(root.findall(".//Relation"), [])

    def test_optional_array_count_uses_outer_scope_and_inner_target(self) -> None:
        for conditional in (False, True):
            with self.subTest(conditional=conditional):
                class Packet(Schema):
                    count = Int8()
                    values = Optional[Array[Int8, count]()](
                        when=count != 0 if conditional else None
                    )

                root = ET.fromstring(to_peach_data_model(Packet, include_header=False))
                relation = root.find("Number[@name='count']/Relation[@type='count']")
                self.assertIsNotNone(relation)
                assert relation is not None
                self.assertEqual(relation.get("of"), "values.value")

                fixed_root = ET.fromstring(to_peach_data_model(
                    Packet(count=fixed(3)), include_header=False
                ))
                tag = "Optional" if conditional else "Choice"
                item = fixed_root.find(f"{tag}[@name='values']/Number[@name='value']")
                self.assertIsNotNone(item)
                assert item is not None
                self.assertEqual(item.get("occurs"), "3")
                self.assertEqual(fixed_root.findall(".//Relation"), [])

                class Ack(Schema):
                    kind = Int8(fixed(2))

                @PacketUnion
                class Packets(Schema):
                    packet_union = Union(data=Packet, ack=Ack)

                library = ET.fromstring(to_peach_data_model(Packets))
                model = library.find("p:DataModel[@name='Packet']", NS)
                self.assertIsNotNone(model)
                assert model is not None
                emitted_relation = model.find(
                    "p:Number[@name='count']/p:Relation[@type='count']", NS
                )
                self.assertIsNotNone(emitted_relation)
                assert emitted_relation is not None
                self.assertEqual(emitted_relation.get("of"), "data.values.value")

    def test_nested_array_count_overrides_apply_to_both_dimensions(self) -> None:
        class Matrix(Schema):
            rows = Int8()
            columns = Int8()
            values = Array[Array[Int8, columns](), rows]()

        root = ET.fromstring(to_peach_data_model(
            Matrix(rows=fixed(3), columns=fixed(2)), include_header=False
        ))
        rows = root.find("Block[@name='values']")
        self.assertIsNotNone(rows)
        assert rows is not None
        self.assertEqual(rows.get("occurs"), "3")
        item = rows.find("Number[@name='item']")
        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(item.get("occurs"), "2")
        self.assertEqual(root.findall(".//Relation"), [])

    def test_nested_array_internal_reference_keeps_inner_item_path(self) -> None:
        class Header(Schema):
            length = Int8()

        class Holder(Schema):
            matrix = Array[Array[Header, 2](), 3]()

        class Packet(Schema):
            source = Holder()
            data = Blob[source.matrix.internal().internal().length]()

        root = ET.fromstring(to_peach_data_model(Packet, include_header=False))
        relation = root.find(
            "Block[@name='source']/Block[@name='matrix']/Block[@name='item']/"
            "Number[@name='length']/Relation[@type='size']"
        )
        self.assertIsNotNone(relation)
        assert relation is not None
        self.assertEqual(relation.get("of"), "data")

    def test_nested_array_dynamic_counts_target_separate_dimensions(self) -> None:
        class Matrix(Schema):
            rows = Int8()
            columns = Int8()
            values = Array[Array[Int8, columns](), rows]()

        class Ack(Schema):
            kind = Int8(fixed(2))

        @PacketUnion
        class Packet(Schema):
            packet_union = Union(matrix=Matrix, ack=Ack)

        root = ET.fromstring(to_peach_data_model(Packet))
        matrix = root.find("p:DataModel[@name='Matrix']", NS)
        self.assertIsNotNone(matrix)
        assert matrix is not None
        for source, target in (("rows", "matrix.values"), ("columns", "matrix.values.item")):
            relation = matrix.find(f"p:Number[@name='{source}']/p:Relation", NS)
            self.assertIsNotNone(relation)
            assert relation is not None
            self.assertEqual(relation.get("type"), "count")
            self.assertEqual(relation.get("of"), target)
        self.assertIsNotNone(matrix.find("p:Block[@name='values']/p:Number[@name='item']", NS))

    def test_nested_array_keeps_both_repeat_counts(self) -> None:
        class Matrix(Schema):
            values = Array[Array[Int8, 2](), 3]()

        root = ET.fromstring(to_peach_data_model(Matrix, include_header=False))
        rows = root.find("Block[@name='values']")
        self.assertIsNotNone(rows)
        assert rows is not None
        self.assertEqual(rows.get("occurs"), "3")
        items = rows.find("Number[@name='item']")
        self.assertIsNotNone(items)
        assert items is not None
        self.assertEqual(items.get("occurs"), "2")

    def test_dynamic_schema_override_removes_stale_constant_folding(self) -> None:
        class Header(Schema):
            length = Int8()

        class Packet(Schema):
            header = Header(length=fixed(3))
            data = Blob[header.length]()
            items = Array[Int8, header.length + 1]()
            tail = Optional[Int8](when=header.length != 0)

        root = ET.fromstring(to_peach_data_model(
            Packet(header=Header()), include_header=False
        ))
        data = root.find("Blob[@name='data']")
        self.assertIsNotNone(data)
        assert data is not None
        self.assertIsNone(data.get("length"))
        relations = root.findall("Block[@name='header']/Number/Relation")
        self.assertEqual(
            {(item.get("type"), item.get("of")) for item in relations},
            {("size", "data"), ("count", "items")},
        )
        tail = root.find("Optional[@name='tail']")
        self.assertIsNotNone(tail)
        assert tail is not None
        self.assertEqual(tail.get("src"), "header.length")
        original = ET.fromstring(to_peach_data_model(Packet, include_header=False))
        original_data = original.find("Blob[@name='data']")
        self.assertIsNotNone(original_data)
        assert original_data is not None
        self.assertEqual(original_data.get("length"), "3")

    def test_outer_schema_override_rebinds_existing_member_reference(self) -> None:
        class Header(Schema):
            length = Int8()

        class Packet(Schema):
            header = Header(length=fixed(3))
            data = Blob[header.length]()

        root = ET.fromstring(to_peach_data_model(
            Packet(header=Header(length=fixed(7))), include_header=False
        ))
        header = root.find("Block[@name='header']/Number[@name='length']")
        data = root.find("Blob[@name='data']")
        self.assertIsNotNone(header)
        self.assertIsNotNone(data)
        assert header is not None and data is not None
        self.assertEqual(header.get("value"), "7")
        self.assertEqual(data.get("length"), "7")

    def test_optional_scalar_initial_value_stays_dynamic(self) -> None:
        class Holder(Schema):
            length = Optional[Int8](6)

        class Packet(Schema):
            source = Holder()
            data = Blob[source.length]()

        root = ET.fromstring(to_peach_data_model(Packet, include_header=False))
        data = root.find("Blob[@name='data']")
        number = root.find("Block/Choice[@name='length']/Number[@name='value']")
        self.assertIsNotNone(data)
        self.assertIsNotNone(number)
        assert data is not None and number is not None
        self.assertIsNone(data.get("length"))
        self.assertEqual(number.get("value"), "6")
        self.assertIsNotNone(number.find("Relation[@type='size'][@of='data']"))

    def test_fixed_optional_scalar_reference_matches_emitted_value(self) -> None:
        class Holder(Schema):
            length = Optional[Int8](fixed(6))

        for label, holder, expected in (
            ("declared", Holder(), "6"),
            ("replaced", Holder(length=Optional[Int8](fixed(9))), "9"),
        ):
            with self.subTest(label=label):
                class Packet(Schema):
                    source = holder
                    data = Blob[source.length]()

                root = ET.fromstring(to_peach_data_model(Packet, include_header=False))
                data = root.find("Blob[@name='data']")
                number = root.find("Block/Choice[@name='length']/Number[@name='value']")
                self.assertIsNotNone(data)
                self.assertIsNotNone(number)
                assert data is not None and number is not None
                self.assertEqual(number.get("value"), expected)
                self.assertEqual(data.get("length"), expected)
                self.assertEqual(root.findall(".//Relation"), [])

    def test_optional_array_dynamic_reference_emits_relation(self) -> None:
        class Header(Schema):
            length = Int8()

        class Holder(Schema):
            headers = Optional[Array[Header, 1]()]()

        class Packet(Schema):
            source = Holder()
            data = Blob[source.headers.internal().length]()

        root = ET.fromstring(to_peach_data_model(Packet, include_header=False))
        relation = root.find(
            "Block[@name='source']/Choice[@name='headers']/Block[@name='value']/"
            "Number[@name='length']/Relation[@type='size']"
        )
        self.assertIsNotNone(relation)
        assert relation is not None
        self.assertEqual(relation.get("of"), "data")

    def test_optional_array_internal_reference_keeps_container_path(self) -> None:
        class Header(Schema):
            length = Int8(fixed(4))

        class Holder(Schema):
            headers = Optional[Array[Header, 1]()]()

        class Packet(Schema):
            source = Holder()
            data = Blob[source.headers.internal().length]()

        root = ET.fromstring(to_peach_data_model(Packet, include_header=False))
        data = root.find("Blob[@name='data']")
        self.assertIsNotNone(data)
        assert data is not None
        self.assertEqual(data.get("length"), "4")

    def test_union_scalar_reference_does_not_take_parent_override(self) -> None:
        @Union
        class Choice(Schema):
            length = Int8(fixed(7))
            empty = Block()

        class Holder(Schema):
            length = Int8()
            choice = Choice()

        class Packet(Schema):
            source = Holder(length=fixed(3))
            data = Blob[source.choice.length]()

        root = ET.fromstring(to_peach_data_model(Packet, include_header=False))
        data = root.find("Blob[@name='data']")
        self.assertIsNotNone(data)
        assert data is not None
        self.assertEqual(data.get("length"), "7")

    def test_replaced_union_branch_reference_uses_effective_schema(self) -> None:
        class Header(Schema):
            length = Int8()

        @Union
        class Choice(Schema):
            header = Header()
            empty = Block()

        class Holder(Schema):
            choice = Choice()

        class Packet(Schema):
            source = Holder(choice=Choice(header=Header(length=fixed(9))))
            data = Blob[source.choice.header.length]()

        root = ET.fromstring(to_peach_data_model(Packet, include_header=False))
        data = root.find("Blob[@name='data']")
        self.assertIsNotNone(data)
        assert data is not None
        self.assertEqual(data.get("length"), "9")
        self.assertEqual(root.findall(".//Relation"), [])

    def test_decorated_block_replacement_exposes_effective_fields(self) -> None:
        class Holder(Schema):
            @Block
            class header(Schema):
                length = Int8()

        class Packet(Schema):
            source = Holder(header=Holder.header(length=fixed(6)))
            data = Blob[source.header.length]()

        root = ET.fromstring(to_peach_data_model(Packet, include_header=False))
        data = root.find("Blob[@name='data']")
        self.assertIsNotNone(data)
        assert data is not None
        self.assertEqual(data.get("length"), "6")
        self.assertEqual(root.findall(".//Relation"), [])

    def test_optional_and_array_replacements_expose_their_effective_element(self) -> None:
        class Header(Schema):
            length = Int8()

        for kind in ("optional", "array"):
            with self.subTest(kind=kind):
                default = Optional[Header]() if kind == "optional" else Array[Header, 1]()
                replacement = (
                    Optional[Header(length=fixed(4))]()
                    if kind == "optional" else Array[Header(length=fixed(4)), 1]()
                )

                class Holder(Schema):
                    header = default

                holder = Holder(header=replacement)
                reference = (
                    holder.header.length if kind == "optional"
                    else holder.header.internal().length
                )

                class Packet(Schema):
                    source = holder
                    data = Blob[reference]()

                root = ET.fromstring(to_peach_data_model(Packet, include_header=False))
                data = root.find("Blob[@name='data']")
                self.assertIsNotNone(data)
                assert data is not None
                self.assertEqual(data.get("length"), "4")
                self.assertEqual(root.findall(".//Relation"), [])

    def test_nested_instance_overrides_fold_each_reference_independently(self) -> None:
        class Header(Schema):
            length = Int8()

        class Envelope(Schema):
            header = Header()

        class Packet(Schema):
            left = Envelope(header=Header(length=fixed(3)))
            right = Envelope(header=Header(length=fixed(5)))
            left_data = Blob[left.header.length]()
            right_data = Blob[right.header.length]()
            values = Array[Int8, right.header.length + 1]()
            tail = Optional[Int8](when=left.header.length == 3)

        root = ET.fromstring(to_peach_data_model(Packet, include_header=False))
        for name, expected in (("left_data", "3"), ("right_data", "5")):
            blob = root.find(f"Blob[@name='{name}']")
            self.assertIsNotNone(blob)
            assert blob is not None
            self.assertEqual(blob.get("length"), expected)
        self.assertEqual(root.findall(".//Relation"), [])
        values = root.find("Number[@name='values']")
        self.assertIsNotNone(values)
        assert values is not None
        self.assertEqual(values.get("occurs"), "6")
        tail = root.find("Optional[@name='tail']")
        self.assertIsNotNone(tail)
        assert tail is not None
        self.assertIsNone(tail.get("src"))
        self.assertEqual(tail.get("expression"), "(3 == 3)")

    def test_fixed_override_only_folds_references_to_its_declaration(self) -> None:
        class Constants(Schema):
            length = Int8(fixed(7))

        class Packet(Schema):
            length = Int8()
            local = Blob[length]()
            external = Blob[Constants.length]()
            values = Array[Int8, length + 1]()

        root = ET.fromstring(to_peach_data_model(
            Packet(length=fixed(3)), include_header=False
        ))
        for name, expected in (("local", "3"), ("external", "7")):
            blob = root.find(f"Blob[@name='{name}']")
            self.assertIsNotNone(blob)
            assert blob is not None
            self.assertEqual(blob.get("length"), expected)
        values = root.find("Number[@name='values']")
        self.assertIsNotNone(values)
        assert values is not None
        self.assertEqual(values.get("occurs"), "4")

    def test_fixed_override_cannot_hide_detached_reference(self) -> None:
        class Other(Schema):
            length = Int8()

        for kind, member in (
            ("size", Blob[Other.length]()),
            ("count", Array[Int8, Other.length + 1]()),
            ("condition", Optional[Int8](when=Other.length != 0)),
        ):
            with self.subTest(kind=kind):
                class Packet(Schema):
                    length = Int8()
                    value = member

                with self.assertRaisesRegex(ValueError, "cannot resolve"):
                    to_peach_data_model(Packet(length=fixed(3)), include_header=False)

    def test_replacing_length_field_preserves_existing_reference(self) -> None:
        class Packet(Schema):
            length = Int8()
            payload = Blob[length]()

        root = ET.fromstring(
            to_peach_data_model(Packet(length=Int16()), include_header=False)
        )
        length = root.find("Number[@name='length']")
        self.assertIsNotNone(length)
        assert length is not None
        self.assertEqual(length.get("size"), "16")
        self.assertIsNotNone(length.find("Relation[@type='size'][@of='payload']"))

    def test_replacing_count_and_condition_fields_preserves_references(self) -> None:
        class Packet(Schema):
            count = Int8()
            flags = Int8()
            items = Array[Int8, count + 1]()
            tail = Optional[Int8](when=(flags & 1) != 0)

        root = ET.fromstring(to_peach_data_model(
            Packet(count=Int16(), flags=Int16()), include_header=False
        ))
        relation = root.find("Number[@name='count']/Relation[@type='count']")
        self.assertIsNotNone(relation)
        assert relation is not None
        self.assertEqual(relation.get("of"), "items")
        self.assertEqual(eval(relation.get('expressionGet'), {'size': 3, 'count': 3}), eval(('(count + 1)') or "size", {'size': 3, 'count': 3}))
        optional = root.find("Optional[@name='tail']")
        self.assertIsNotNone(optional)
        assert optional is not None
        self.assertEqual(optional.get("src"), "flags")

    def test_length_reference_to_detached_field_is_rejected(self) -> None:
        class Other(Schema):
            length = Int8()

        class Packet(Schema):
            payload = Blob[Other.length]()

        with self.assertRaisesRegex(ValueError, "reference|resolve|field"):
            to_peach_data_model(Packet, include_header=False)

    def test_detached_same_named_field_does_not_bind_to_local_field(self) -> None:
        class Other(Schema):
            length = Int8()

        class Packet(Schema):
            length = Int8()
            payload = Blob[Other.length]()

        with self.assertRaisesRegex(ValueError, "cannot resolve relation source"):
            to_peach_data_model(Packet, include_header=False)

    def test_detached_same_named_count_expression_is_rejected(self) -> None:
        class Other(Schema):
            count = Int8()

        class Packet(Schema):
            count = Int8()
            values = Array[Int8, Other.count + 1]()

        with self.assertRaisesRegex(ValueError, "cannot resolve relation source"):
            to_peach_data_model(Packet, include_header=False)

    def test_detached_same_named_optional_source_is_rejected(self) -> None:
        class Other(Schema):
            flags = Int8()

        detached = Other()

        class Packet(Schema):
            flags = Int8()
            value = Optional[Int8](when=detached.flags != 0)

        with self.assertRaisesRegex(ValueError, "cannot resolve condition field"):
            to_peach_data_model(Packet, include_header=False)

    def test_detached_reference_is_rejected_beside_packet_union(self) -> None:
        class Other(Schema):
            length = Int8()

        class Request(Schema):
            kind = Int8(fixed(1))

        class Ack(Schema):
            kind = Int8(fixed(2))

        @PacketUnion
        class Packet(Schema):
            packet_union = Union(request=Request, ack=Ack)

        class Stream(Schema):
            packets = Array[Packet]()
            payload = Blob[Other.length]()

        with self.assertRaisesRegex(ValueError, "cannot resolve relation source"):
            to_peach_data_model(Stream)

    def test_detached_same_named_reference_is_rejected_in_packet_union(self) -> None:
        class Other(Schema):
            length = Int8()

        class Request(Schema):
            length = Int8()
            payload = Blob[Other.length]()

        class Ack(Schema):
            kind = Int8(fixed(2))

        @PacketUnion
        class Packet(Schema):
            packet_union = Union(request=Request, ack=Ack)

        with self.assertRaisesRegex(ValueError, "cannot resolve relation source"):
            to_peach_data_model(Packet, include_header=False)

    def test_same_named_length_fields_bind_to_their_own_scope(self) -> None:
        class Packet(Schema):
            length = Int8()

            @Block
            class body(Schema):
                length = Int8()
                data = Blob[length]()

            trailer = Blob[length]()

        root = ET.fromstring(to_peach_data_model(Packet, include_header=False))
        outer = root.find("Number[@name='length']/Relation[@type='size']")
        inner = root.find(
            "Block[@name='body']/Number[@name='length']/Relation[@type='size']"
        )
        self.assertIsNotNone(outer)
        self.assertIsNotNone(inner)
        assert outer is not None and inner is not None
        self.assertEqual(outer.get("of"), "trailer")
        self.assertEqual(inner.get("of"), "body.data")

    def test_reused_schema_reference_targets_one_instance(self) -> None:
        class Segment(Schema):
            length = Int8()
            data = Blob[length]()

        class Request(Schema):
            first = Segment()
            second = Segment()
            extra = Blob[second.length]()

        class Ack(Schema):
            kind = Int8(fixed(2))

        @PacketUnion
        class Packet(Schema):
            packet_union = Union(request=Request, ack=Ack)

        root = ET.fromstring(to_peach_data_model(Packet))
        request = root.find("p:DataModel[@name='Request']", NS)
        self.assertIsNotNone(request)
        assert request is not None
        first = request.find("p:Block[@name='first']", NS)
        second = request.find("p:Block[@name='second']", NS)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        assert first is not None and second is not None
        self.assertEqual(first.get("ref"), "Segment")
        self.assertEqual(len(first), 0)
        relation = second.find(
            "p:Number[@name='length']/p:Relation[@type='size']", NS
        )
        self.assertIsNotNone(relation)
        assert relation is not None
        self.assertEqual(relation.get("of"), "request.extra")
        segment = root.find("p:DataModel[@name='Segment']", NS)
        self.assertIsNotNone(segment)
        assert segment is not None
        local = segment.find(
            "p:Number[@name='length']/p:Relation[@type='size']", NS
        )
        self.assertIsNotNone(local)
        assert local is not None
        self.assertEqual(local.get("of"), "data")

    def test_array_element_reference_stays_inside_element_model(self) -> None:
        class Element(Schema):
            count = Int8()
            flags = Int8()
            values = Array[Int8, count]()
            tail = Optional[Int8](when=(flags & 1) != 0)

        class Request(Schema):
            kind = Int8(fixed(1))
            count = Int8()
            elements = Array[Element, count]()

        class Ack(Schema):
            kind = Int8(fixed(2))

        @PacketUnion
        class Packet(Schema):
            packet_union = Union(request=Request, ack=Ack)

        root = ET.fromstring(to_peach_data_model(Packet))
        request = root.find("p:DataModel[@name='Request']", NS)
        element = root.find("p:DataModel[@name='Element']", NS)
        self.assertIsNotNone(request)
        self.assertIsNotNone(element)
        assert request is not None and element is not None
        request_relation = request.find(
            "p:Number[@name='count']/p:Relation[@type='count']", NS
        )
        element_relation = element.find(
            "p:Number[@name='count']/p:Relation[@type='count']", NS
        )
        self.assertIsNotNone(request_relation)
        self.assertIsNotNone(element_relation)
        assert request_relation is not None and element_relation is not None
        self.assertEqual(request_relation.get("of"), "request.elements")
        self.assertEqual(element_relation.get("of"), "values")
        optional = element.find("p:Optional[@name='tail']", NS)
        self.assertIsNotNone(optional)
        assert optional is not None
        self.assertEqual(optional.get("src"), "flags")
