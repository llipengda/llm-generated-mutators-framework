"""Long, end-to-end DSL programs exercising interacting compiler features."""

from __future__ import annotations

from pathlib import Path
import tempfile
import textwrap
import unittest
from xml.etree import ElementTree as ET

if __package__ in ("", "tests"):
    __package__ = "peach_dsl.tests"

from ..compiler import compile_entry


NS = {"p": "http://peachfuzzer.com/2012/Peach"}


def write_module(directory: Path, name: str, source: str) -> Path:
    path = directory / name
    path.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    return path


def compile_source(source: str) -> tuple[ET.Element, str]:
    # Keep source and generated files alive only while this helper parses them.
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        entry = write_module(directory, "root.py", source)
        output = directory / "datamodel.xml"
        compile_entry(entry, output)
        root = ET.parse(output).getroot()
        fixup = output.with_name("python_fixup.py")
        return root, fixup.read_text(encoding="utf-8") if fixup.exists() else ""


class LongCompilerScenarios(unittest.TestCase):
    def test_optional_header_drives_nested_block_size_and_item_count(self) -> None:
        # Relations from a reusable optional header must reach the body.
        root, _ = compile_source('''
            from peach_dsl import *

            class Limits(Schema):
                body_size = Int16()
                item_count = Int8()

            class Item(Schema):
                tag = Int8()
                length = Int8()
                data = Blob[length]()

            class RequestHeader(Schema):
                flags = Int8()
                limits = Optional[Limits](when=(flags & 1) != 0)

            class Request(Schema):
                kind = Int8(fixed(1))
                header = RequestHeader()
                body = Block[header.limits.body_size](
                    items=Array[Item, header.limits.item_count](),
                    checksum=Optional[Int16](when=(header.flags & 2) != 0),
                )

            class Response(Schema):
                kind = Int8(fixed(2))
                status = Int16()
                text_size = Int8()
                text = String[text_size](type="utf8")

            @PacketUnion
            class Packet(Schema):
                packet_union = Union(request=Request, response=Response)

            class Session(Schema):
                packets = Array[Packet, Occurs(1, 4)]()

            ROOT = Session
        ''')
        request = root.find("p:DataModel[@name='Request']", NS)
        self.assertIsNotNone(request)
        assert request is not None
        limits = request.find(
            "p:Block[@name='header']/p:Optional[@name='limits']/"
            "p:Block[@name='limits_internal']", NS
        )
        self.assertIsNotNone(limits)
        assert limits is not None
        self.assertEqual(limits.get("ref"), "Limits")
        checksum = request.find("p:Block[@name='body']/p:Optional[@name='checksum']", NS)
        self.assertIsNotNone(checksum)
        self.assertEqual(checksum.get("src"), "../../header.flags")
        missing = []
        for name, relation_type, target in (
            ("body_size", "size", "request.body"),
            ("item_count", "count", "request.body.items"),
        ):
            relation = limits.find(
                f"p:Number[@name='{name}']/p:Relation[@type='{relation_type}']", NS
            )
            if relation is None or relation.get("of") != target:
                missing.append((name, relation_type, target))
        self.assertEqual(missing, [])

    def test_optional_limits_combine_nested_inverse_and_local_relations(self) -> None:
        root, _ = compile_source('''
            from peach_dsl import *

            class Limits(Schema):
                body_size = Int16()
                item_count = Int8()
                local_size = Int8()
                local_data = Blob[local_size]()

            class Header(Schema):
                flags = Int8()
                limits = Optional[Limits](when=(flags & 1) != 0)

            class Entry(Schema):
                kind = Int8()
                value_size = Int8()
                value = Blob[value_size]()

            class Request(Schema):
                tag = Int8(fixed(1))
                header = Header()
                body = Block[(header.limits.body_size + 4) - 1](
                    entries=Array[Entry, 20 - (header.limits.item_count + 2)](),
                    checksum=Optional[Int16](when=(header.flags & 2) != 0),
                )

            class Ack(Schema):
                tag = Int8(fixed(2))
                code = Int8()

            @PacketUnion
            class Packet(Schema):
                packet_union = Union(request=Request, ack=Ack)

            class Session(Schema):
                packets = Array[Packet, Occurs(1, 6)]()

            ROOT = Session
        ''')
        request = root.find("p:DataModel[@name='Request']", NS)
        self.assertIsNotNone(request)
        assert request is not None
        limits = request.find(
            "p:Block[@name='header']/p:Optional[@name='limits']/"
            "p:Block[@name='limits_internal']", NS
        )
        self.assertIsNotNone(limits)
        assert limits is not None
        for field, relation_type, target, expression_get, expression_set in (
            ("body_size", "size", "request.body", "((size + 4) - 1)", "(size - 3)"),
            ("item_count", "count", "request.body.entries",
             "(20 - (count + 2))", "(18 - count)"),
        ):
            relation = limits.find(
                f"p:Number[@name='{field}']/p:Relation[@type='{relation_type}']",
                NS,
            )
            self.assertIsNotNone(relation, field)
            assert relation is not None
            self.assertEqual(relation.get("of"), target)
            self.assertEqual(relation.get("expressionGet"), expression_get)
            self.assertEqual(relation.get("expressionSet"), expression_set)
        limits_model = root.find("p:DataModel[@name='Limits']", NS)
        self.assertIsNotNone(limits_model)
        assert limits_model is not None
        local = limits_model.find(
            "p:Number[@name='local_size']/p:Relation[@type='size']", NS
        )
        self.assertIsNotNone(local)
        assert local is not None
        self.assertEqual(local.get("of"), "local_data")
        checksum = request.find(
            "p:Block[@name='body']/p:Optional[@name='checksum']", NS
        )
        self.assertIsNotNone(checksum)
        assert checksum is not None
        self.assertEqual(checksum.get("src"), "../../header.flags")
        entry = root.find("p:DataModel[@name='Entry']", NS)
        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertIsNotNone(
            entry.find("p:Number[@name='value_size']/p:Relation[@type='size']", NS)
        )

    def test_unconditional_optional_choice_preserves_both_relation_scopes(self) -> None:
        root, _ = compile_source('''
            from peach_dsl import *

            class LengthHeader(Schema):
                width = Int16()
                local_size = Int8()
                local_data = Blob[local_size]()

            class Header(Schema):
                marker = Int8(fixed(0xA5))
                lengths = Optional[LengthHeader]()

            class Data(Schema):
                kind = Int8(fixed(1))
                header = Header()
                payload = Blob[(header.lengths.width + 3) * 2]()

            class Empty(Schema):
                kind = Int8(fixed(0))

            @PacketUnion
            class Packet(Schema):
                packet_union = Union(data=Data, empty=Empty)

            ROOT = Packet
        ''')
        data = root.find("p:DataModel[@name='Data']", NS)
        self.assertIsNotNone(data)
        assert data is not None
        choice = data.find("p:Block[@name='header']/p:Choice[@name='lengths']", NS)
        self.assertIsNotNone(choice)
        assert choice is not None
        self.assertEqual(
            [child.get("name") for child in choice], ["lengths_internal", "empty"]
        )
        present = choice.find("p:Block[@name='lengths_internal']", NS)
        self.assertIsNotNone(present)
        assert present is not None
        self.assertEqual(present.get("ref"), "LengthHeader")
        external = present.find(
            "p:Number[@name='width']/p:Relation[@type='size']", NS
        )
        self.assertIsNotNone(external)
        assert external is not None
        self.assertEqual(external.get("of"), "data.payload")
        self.assertEqual(eval(external.attrib["expressionSet"], {"size": 20}), 7)
        with self.assertRaisesRegex(ValueError, "not divisible"):
            eval(external.attrib["expressionSet"], {"size": 21})
        length_model = root.find("p:DataModel[@name='LengthHeader']", NS)
        self.assertIsNotNone(length_model)
        assert length_model is not None
        local = length_model.find(
            "p:Number[@name='local_size']/p:Relation[@type='size']", NS
        )
        self.assertIsNotNone(local)
        assert local is not None
        self.assertEqual(local.get("of"), "local_data")

    def test_framed_telemetry_batch_with_nested_relations(self) -> None:
        root, _ = compile_source('''
            from peach_dsl import *

            @Default(endian="big", signed=False)
            class FrameHeader(Schema):
                magic = Int16(fixed(0xCAFE))
                version = Int8(fixed(2))
                flags = Int8()
                body_size = Int16()
                item_count = Int8()

            class NumericItem(Schema):
                tag = Int8(fixed(1))
                item_size = Int8()
                value = Blob[item_size]()

            class TextItem(Schema):
                tag = Int8(fixed(2))
                text_size = Int8()
                text = String[text_size](type="utf8")

            @Union
            class TelemetryItem(Schema):
                numeric = NumericItem()
                text = TextItem()

            class TelemetryPacket(Schema):
                header = FrameHeader()
                body = Block[header.body_size](
                    items=Array[TelemetryItem, header.item_count](),
                    checksum=Optional[Int16](when=(header.flags & 1) != 0),
                )

            class HeartbeatPacket(Schema):
                header = FrameHeader(
                    version=fixed(3), body_size=fixed(1), item_count=fixed(0)
                )
                sequence = Int8()

            @PacketUnion
            class Frame(Schema):
                packet_union = Union(telemetry=TelemetryPacket,
                                     heartbeat=HeartbeatPacket)

            class Stream(Schema):
                frames = Array[Frame, Occurs(1, 8)]()

            ROOT = Stream
        ''')
        telemetry = root.find("p:DataModel[@name='TelemetryPacket']", NS)
        self.assertIsNotNone(telemetry)
        assert telemetry is not None
        header = telemetry.find("p:Block[@name='header']", NS)
        self.assertIsNotNone(header)
        assert header is not None
        for name, relation_type, target in (
            ("body_size", "size", "telemetry.body"),
            ("item_count", "count", "telemetry.body.items"),
        ):
            relation = header.find(
                f"p:Number[@name='{name}']/p:Relation[@type='{relation_type}']", NS
            )
            self.assertIsNotNone(relation, name)
            self.assertEqual(relation.get("of"), target)
        optional = telemetry.find("p:Block[@name='body']/p:Optional[@name='checksum']", NS)
        self.assertIsNotNone(optional)
        self.assertEqual(optional.get("src"), "../../header.flags")
        self.assertIsNotNone(root.find(".//p:Choice[@name='items']", NS))
        self.assertIsNotNone(root.find("p:DataModel[@name='Stream']", NS))

    def test_nested_command_payloads_and_reused_schema_overrides(self) -> None:
        root, _ = compile_source('''
            from peach_dsl import *

            class Lengths(Schema):
                command_size = Int16()
                reply_size = Int16()
                item_count = Int8()
                flags = Int8()

            class Entry(Schema):
                size = Int8()
                value = Blob[size]()

            class Command(Schema):
                header = Lengths(reply_size=fixed(0))
                command = Block[header.command_size](
                    opcode=Int8(fixed(0x10)),
                    entries=Array[Entry, header.item_count](),
                    extension=Optional[Blob[2]](when=(header.flags & 0x80) != 0),
                )

            class Reply(Schema):
                header = Lengths(command_size=fixed(0))
                reply = Block[header.reply_size](
                    opcode=Int8(fixed(0x90)),
                    status=Int16(),
                    entries=Array[Entry, header.item_count](),
                    diagnostic=Optional[String](when=(header.flags & 1) != 0),
                )

            @PacketUnion
            class Message(Schema):
                packet_union = Union(command=Command, reply=Reply)

            class Root(Schema):
                messages = Array[Message, Occurs(0, 16)]()

            ROOT = Root
        ''')
        for model_name, length, body, optional, flag in (
            ("Command", "command_size", "command", "extension", "128"),
            ("Reply", "reply_size", "reply", "diagnostic", "1"),
        ):
            model = root.find(f"p:DataModel[@name='{model_name}']", NS)
            self.assertIsNotNone(model)
            assert model is not None
            relation = model.find(
                f"p:Block[@name='header']/p:Number[@name='{length}']/"
                "p:Relation[@type='size']", NS
            )
            self.assertIsNotNone(relation)
            self.assertIn(body, relation.get("of", ""))
            count = model.find(
                "p:Block[@name='header']/p:Number[@name='item_count']/"
                "p:Relation[@type='count']", NS
            )
            self.assertIsNotNone(count)
            self.assertIn("entries", count.get("of", ""))
            condition = model.find(
                f"p:Block[@name='{body}']/p:Optional[@name='{optional}']", NS
            )
            self.assertIsNotNone(condition)
            self.assertIn(flag, condition.get("expression", ""))
            self.assertIn("header.flags", condition.get("src", ""))

    def test_text_protocol_headers_with_decimal_length_and_union(self) -> None:
        root, _ = compile_source(r'''
            from peach_dsl import *

            class LengthLine(Schema):
                name = String(fixed("Content-Length"), type="ascii")
                colon = String(fixed(": "), type="ascii")
                value = DecimalString(type="ascii")
                end = String(fixed("\r\n"), type="ascii")

            class GenericLine(Schema):
                name = String(type="ascii")
                colon = String(fixed(": "), type="ascii")
                value = String(type="utf8")
                end = String(fixed("\r\n"), type="ascii")

            @Union
            class HeaderLine(Schema):
                length = LengthLine()
                generic = GenericLine()

            class Headers(Schema):
                request_line = String(type="ascii")
                line_end = String(fixed("\r\n"), type="ascii")
                lines = Array[HeaderLine, Occurs(1, 32)]()
                end = String(fixed("\r\n"), type="ascii")

            class Request(Schema):
                headers = Headers()
                body = Blob[headers.lines.internal().length.value]()

            class Response(Schema):
                status = String(fixed("HTTP/1.1 204 No Content\r\n\r\n"),
                                type="ascii")

            @PacketUnion
            class Packet(Schema):
                packet_union = Union(request=Request, response=Response)

            ROOT = Packet
        ''')
        request = root.find("p:DataModel[@name='Request']", NS)
        self.assertIsNotNone(request)
        assert request is not None
        relation = request.find(".//p:Relation[@type='size']", NS)
        self.assertIsNotNone(relation)
        self.assertIn("body", relation.get("of", ""))
        self.assertIsNotNone(root.find(".//p:Choice[@name='lines']", NS))
        self.assertIsNotNone(root.find("p:DataModel[@name='Response']", NS))

    def test_multiple_computed_fields_across_nested_blocks(self) -> None:
        root, fixup = compile_source('''
            from peach_dsl import *

            @compute
            def header_crc(header: bytearray, seed: int) -> int:
                return (sum(header) + seed) % 65536

            @compute
            def payload_crc(payload: bytearray) -> int:
                return sum(payload) % 256

            class Header(Schema):
                version = Int8(fixed(1))
                flags = Int8()
                size = Int16()

            class Packet(Schema):
                seed = Int8()
                header = Header()
                payload = Block[header.size](data=Blob())
                header_check = Int16(header_crc(header, seed))
                payload_check = Int8(payload_crc(payload))
                tail = String(fixed("END"), type="ascii")

            class Other(Schema):
                type = Int8(fixed(0))
                body = Blob[2]()

            @PacketUnion
            class PacketSet(Schema):
                packet_union = Union(data=Packet, other=Other)

            ROOT = PacketSet
        ''')
        packet = root.find("p:DataModel[@name='Packet']", NS)
        self.assertIsNotNone(packet)
        assert packet is not None
        for field in ("header_check", "payload_check"):
            node = packet.find(f"p:Number[@name='{field}']/p:Fixup", NS)
            self.assertIsNotNone(node, field)
        self.assertIn("class Fixup_header_crc_", fixup)
        self.assertIn("class Fixup_payload_crc_", fixup)
        self.assertIsNotNone(packet.find("p:Block[@name='header']/p:Number[@name='size']/p:Relation", NS))

    def test_split_modules_compile_deterministically_with_deep_imports(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_module(directory, "primitives.py", '''
                from peach_dsl import *

                class Header(Schema):
                    magic = Int16(fixed(0xABCD))
                    flags = Int8()
                    count = Int8()
                    size = Int16()

                class Item(Schema):
                    length = Int8()
                    data = Blob[length]()
            ''')
            write_module(directory, "commands.py", '''
                from peach_dsl import *
                from primitives import Header, Item

                class DataCommand(Schema):
                    header = Header()
                    body = Block[header.size](
                        items=Array[Item, header.count](),
                        trailer=Optional[Int16](when=(header.flags & 1) != 0),
                    )

                class EmptyCommand(Schema):
                    header = Header(count=fixed(0), size=fixed(0))
            ''')
            entry = write_module(directory, "root.py", '''
                from peach_dsl import *
                from commands import DataCommand, EmptyCommand

                @PacketUnion
                class Command(Schema):
                    packet_union = Union(data=DataCommand, empty=EmptyCommand)

                class Session(Schema):
                    messages = Array[Command, Occurs(1, 10)]()

                ROOT = Session
            ''')
            output = directory / "datamodel.xml"
            compile_entry(entry, output)
            first = output.read_bytes()
            compile_entry(entry, output)
            self.assertEqual(output.read_bytes(), first)
            root = ET.fromstring(first)
            for model in ("Header", "Item", "DataCommand", "EmptyCommand", "Command", "Session"):
                self.assertIsNotNone(root.find(f"p:DataModel[@name='{model}']", NS), model)
            command = root.find("p:DataModel[@name='DataCommand']", NS)
            self.assertIsNotNone(command)
            self.assertIsNotNone(command.find("p:Block[@name='header']/p:Number[@name='size']/p:Relation", NS))

    def test_split_modules_keep_optional_relations_and_inverse_expressions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_module(directory, "models.py", '''
                from peach_dsl import *

                class Limits(Schema):
                    length = Int16()
                    count = Int8()

                class Record(Schema):
                    size = Int8()
                    data = Blob[size]()
            ''')
            write_module(directory, "packets.py", '''
                from peach_dsl import *
                from models import Limits, Record

                class Header(Schema):
                    flags = Int8()
                    limits = Optional[Limits](when=(flags & 1) != 0)

                class DataPacket(Schema):
                    kind = Int8(fixed(1))
                    header = Header()
                    body = Block[(header.limits.length + 2) * 2](
                        records=Array[Record, header.limits.count + 1](),
                    )

                class EmptyPacket(Schema):
                    kind = Int8(fixed(0))
            ''')
            entry = write_module(directory, "root.py", '''
                from peach_dsl import *
                from packets import DataPacket, EmptyPacket

                @PacketUnion
                class Packet(Schema):
                    packet_union = Union(data=DataPacket, empty=EmptyPacket)

                class Stream(Schema):
                    packets = Array[Packet, Occurs(1, 5)]()

                ROOT = Stream
            ''')
            output = directory / "datamodel.xml"
            compile_entry(entry, output)
            first = output.read_bytes()
            compile_entry(entry, output)
            self.assertEqual(output.read_bytes(), first)

            root = ET.fromstring(first)
            data = root.find("p:DataModel[@name='DataPacket']", NS)
            self.assertIsNotNone(data)
            assert data is not None
            limits = data.find(
                "p:Block[@name='header']/p:Optional[@name='limits']/"
                "p:Block[@name='limits_internal']", NS
            )
            self.assertIsNotNone(limits)
            assert limits is not None
            self.assertEqual(limits.get("ref"), "Limits")
            for field, kind, target, inverse in (
                ("length", "size", "data.body", "((size // 2) - 2)"),
                ("count", "count", "data.body.records", "(count - 1)"),
            ):
                relation = limits.find(
                    f"p:Number[@name='{field}']/p:Relation[@type='{kind}']", NS
                )
                self.assertIsNotNone(relation, field)
                assert relation is not None
                self.assertEqual(relation.get("of"), target)
                for measured in (10, 20):
                    self.assertEqual(
                        eval(relation.attrib["expressionSet"], {kind: measured}),
                        eval(inverse, {kind: measured}),
                    )
            base_limits = root.find("p:DataModel[@name='Limits']", NS)
            self.assertIsNotNone(base_limits)
            assert base_limits is not None
            self.assertIsNone(base_limits.find("p:Number/p:Relation", NS))
            record = root.find("p:DataModel[@name='Record']", NS)
            self.assertIsNotNone(record)
            assert record is not None
            self.assertIsNotNone(
                record.find("p:Number[@name='size']/p:Relation[@type='size']", NS)
            )

    def test_ambiguous_deep_condition_rejects_without_replacing_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            output = directory / "datamodel.xml"
            valid = write_module(directory, "valid.py", '''
                from peach_dsl import *

                class Header(Schema):
                    kind = Int8(fixed(1))
                    flags = Int8()
                    size = Int16()

                class Packet(Schema):
                    header = Header()
                    body = Blob[header.size]()

                ROOT = Packet
            ''')
            compile_entry(valid, output)
            previous = output.read_bytes()

            invalid = write_module(directory, "invalid.py", '''
                from peach_dsl import *

                class Header(Schema):
                    flags = Int8()
                    size = Int16()
                    count = Int8()

                class Record(Schema):
                    length = Int8()
                    data = Blob[length]()

                class Request(Schema):
                    first = Header()
                    second = Header()
                    body = Block[first.size](
                        records=Array[Record, first.count](),
                        marker=Optional[Int8](when=(Header.flags & 1) != 0),
                    )

                class Response(Schema):
                    status = Int8(fixed(0))
                    body = Blob[2]()

                @PacketUnion
                class Packets(Schema):
                    packet_union = Union(request=Request, response=Response)

                ROOT = Packets
            ''')
            with self.assertRaisesRegex(ValueError, "ambiguous condition field"):
                compile_entry(invalid, output)
            self.assertEqual(output.read_bytes(), previous)


if __name__ == "__main__":
    unittest.main()
