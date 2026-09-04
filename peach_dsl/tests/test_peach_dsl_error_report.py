from pathlib import Path
import tempfile
import unittest

from peach_dsl.error_report import (
    attach_report,
    convert_reports_subprocess,
    evaluate_with_report,
    format_dsl_error_reports,
    parse_peach_report,
)
from peach_dsl import (
    Array,
    Default,
    Int8,
    Occurs,
    Optional,
    PacketUnion,
    Schema,
    Union,
    evaluate_schema,
    fixed,
)


def make_test_schema() -> type[Schema]:
    class ChannelRequestPayload(Schema):
        want_reply = Int8(0)

    class ChannelRequestRecord(Schema):
        packet_length_target = ChannelRequestPayload()

    class OtherPacket(Schema):
        msg_type = Int8(1)

    @PacketUnion
    class TestPacket(Schema):
        packet_union = Union(
            ssh_msg_channel_request=ChannelRequestRecord,
            other=OtherPacket,
        )

    @Default(endian="big", signed=False)
    class TestPacketArray(Schema):
        packets = Array[TestPacket, Occurs(0, 100)]()

    return TestPacketArray


SAMPLE_REPORT = """-+ DataModel 'ssh_packet_array', Bytes: 0/24, Bits: 0/192
 | Size: ??? (Deterministic)
 |-+ Array 'packets', Bytes: 0/24, Bits: 0/192
 | | Min: 0, Max: 100
 | |-+ DataModel 'packets_0', Bytes: 0/24, Bits: 0/192
 | | |-+ Choice 'packet_union', Bytes: 0/24, Bits: 0/192
 | | | |-+ DataModel 'ssh_msg_channel_request', Bytes: 0/24, Bits: 0/192
 | | | | |-+ DataModel 'packet_length_target', Bytes: 4/24, Bits: 32/192
 | | | | | |-+ DataModel 'payload', Bytes: 1/20, Bits: 8/160
 | | | | | | |-- Number 'want_reply', Bytes: 15/20, Bits: 120/160
 | | | | | | |   Size: 1 bytes | 8 bits (Has Length)
 | | | | | | |   Value: 1 (0x1)
 | | | | | | |   Failed: Token did not match '1' vs. '0'.
 | | | | | | X
 | | | | | X
 | | | | X
 | | | X (No valid children were found.)
 | | X
 | X
 /
Parsed bytes do not match original file for 'ssh_channel_shell.raw'
Original Bytes:
00 00 00 14 04 62 00 00 00 00 00 00 00 05 73 68 65 6C 6C 01 F6 97 76 1C
Parsed   Bytes:
"""

PARSE_FAILURE_REPORT = SAMPLE_REPORT.split(
    "Parsed bytes do not match original file", 1
)[0] + """Failed to parse file 'ssh_channel_shell.raw': buffer exhausted
Bytes:
00 01 02 03
"""


def make_optional_packet_union_schema() -> type[Schema]:
    class ConnectHeader(Schema):
        packet_type = Int8(fixed(1))

    class Connect(Schema):
        header = ConnectHeader()

    class PublishHeader(Schema):
        packet_type = Int8(fixed(3))
        flags = Int8()

    class PublishBody(Schema):
        packet_identifier = Optional[Int8](when=(PublishHeader.flags & 0x06) != 0)

    class Publish(Schema):
        header = PublishHeader()
        body = PublishBody()

    @PacketUnion
    class Packet(Schema):
        packet_union = Union(connect=Connect, publish=Publish)

    class PacketArray(Schema):
        packets = Array[Packet, Occurs(1, 100)]()

    return PacketArray


OPTIONAL_PACKET_UNION_REPORT = """-+ DataModel 'packet_array', Bytes: 0/4, Bits: 0/32
 |-+ Array 'packets', Bytes: 0/4, Bits: 0/32
 | |-+ DataModel 'packets_0', Bytes: 0/4, Bits: 0/32
 | | |-+ Choice 'packet_union', Bytes: 0/4, Bits: 0/32
 | | | |-+ DataModel 'connect', Bytes: 0/4, Bits: 0/32
 | | | | |-+ DataModel 'header', Bytes: 0/4, Bits: 0/32
 | | | | | |-- Number 'packet_type', Bytes: 0/1, Bits: 0/8
 | | | | | |   Value: 3 (0x3)
 | | | | | |   Failed: Token did not match '3' vs. '1'.
 | | | |-+ DataModel 'publish', Bytes: 0/4, Bits: 0/32
 | | | | |-+ DataModel 'header', Bytes: 0/4, Bits: 0/32
 | | | | | |-- Number 'packet_type', Bytes: 0/1, Bits: 0/8
 | | | | | |   Value: 3 (0x3)
 | | | | | |-- Number 'flags', Bytes: 1/4, Bits: 8/32
 | | | | | |   Value: 2 (0x2)
 | | | | |-+ Block 'body', Bytes: 2/4, Bits: 16/32
 | | | | | |-+ Optional 'packet_identifier', Bytes: 2/4, Bits: 16/32
Error evaluating condition for Optional 'Optional 'packet_array.packets.packets_0.packet_union.publish.body.packet_identifier'': Error evaluating Optional expression: Referenced element 'publish.header.flags' not found
 | | | | | | /
 | | | X (No valid children were found.)
 /
Parsed bytes do not match original file for 'publish.raw'
Original Bytes:
30 02 00 01
Parsed   Bytes:
"""


def make_all_packet_types_rejected_schema(
    *, packet_union: bool = True
) -> type[Schema]:
    class Short(Schema):
        packet_type = Int8(fixed(1))

    class Long(Schema):
        prefix = Int8()
        packet_type = Int8(fixed(2))

    class Container(Schema):
        choice = Union(short=Short, long=Long)

    return PacketUnion(Container) if packet_union else Container


FAILED_UNION_REPORT = """-+ DataModel 'container', Bytes: 0/4, Bits: 0/32
 |-+ Choice 'choice', Bytes: 0/4, Bits: 0/32
 | |-+ DataModel 'short', Bytes: 0/4, Bits: 0/32
 | | |-- Number 'packet_type', Bytes: 0/4, Bits: 0/32
 | | |   Size: 1 bytes | 8 bits (Has Length)
 | | |   Value: 9 (0x9)
 | | |   Failed: Token did not match '9' vs. '1'.
 | |-+ DataModel 'long', Bytes: 0/4, Bits: 0/32
 | | |-- Number 'prefix', Bytes: 0/4, Bits: 0/32
 | | |   Size: 1 bytes | 8 bits (Has Length)
 | | |   Value: 9 (0x9)
 | | |-- Number 'packet_type', Bytes: 1/4, Bits: 8/32
 | | |   Size: 1 bytes | 8 bits (Has Length)
 | | |   Value: 9 (0x9)
 | | |   Failed: Token did not match '9' vs. '2'.
 | X (No valid children were found.)
X
"""


class PeachErrorReportTests(unittest.TestCase):
    def test_parses_tree_failures_and_byte_mismatch(self) -> None:
        report = parse_peach_report(SAMPLE_REPORT)

        self.assertEqual(report.roots[0].name, "ssh_packet_array")
        self.assertEqual(len(tuple(report.walk())), 8)
        self.assertEqual(report.roots[0].children[0].min_occurs, 0)
        self.assertEqual(report.roots[0].children[0].max_occurs, 100)
        self.assertTrue(
            any("Token did not match" in error.message for node in report.failures for error in node.errors)
        )
        self.assertIsNotNone(report.byte_mismatch)
        assert report.byte_mismatch is not None
        self.assertEqual(report.byte_mismatch.file_name, "ssh_channel_shell.raw")
        self.assertEqual(len(report.byte_mismatch.original), 24)
        self.assertEqual(report.byte_mismatch.parsed, b"")

    def test_associates_report_nodes_with_dsl_paths(self) -> None:
        evaluated = evaluate_with_report(make_test_schema(), SAMPLE_REPORT)
        path = "ssh_msg_channel_request.packet_length_target.want_reply"
        observations = evaluated.for_path(path)

        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].value, "1 (0x1)")
        self.assertEqual(
            observations[0].errors[0].message,
            "Token did not match '1' vs. '0'.",
        )
        self.assertEqual(observations[0].errors[0].category, "token_mismatch")
        self.assertEqual(observations[0].errors[0].actual, "1")
        self.assertEqual(observations[0].errors[0].expected, "0")

    def test_structures_direct_crack_failure_trailer(self) -> None:
        report = parse_peach_report(PARSE_FAILURE_REPORT)

        self.assertIsNotNone(report.parse_failure)
        assert report.parse_failure is not None
        self.assertEqual(report.parse_failure.file_name, "ssh_channel_shell.raw")
        self.assertEqual(report.parse_failure.message, "buffer exhausted")
        self.assertEqual(report.parse_failure.data, b"\x00\x01\x02\x03")

        converted = format_dsl_error_reports(
            make_test_schema(), {"parse.log": PARSE_FAILURE_REPORT}
        )
        self.assertIn(
            'PARSE seed="ssh_channel_shell.raw" '
            'message="buffer exhausted" input=4B:00010203',
            converted,
        )

    def test_can_attach_to_an_existing_evaluation(self) -> None:
        result = evaluate_schema(make_test_schema())
        evaluated = attach_report(result, SAMPLE_REPORT)
        self.assertIs(evaluated.result, result)

    def test_preserves_tree_in_compact_dsl_format(self) -> None:
        converted = format_dsl_error_reports(
            make_test_schema(), {"channel.log": SAMPLE_REPORT}
        )

        self.assertTrue(converted.startswith("DSL-REPORT v1 logs=1\n"))
        self.assertIn(
            'Schema<TestPacketArray> path="$root" state=PASS',
            converted,
        )
        self.assertIn('  Array name="packets" state=FAIL', converted)
        self.assertIn('      Union name="packet_union" state=FAIL', converted)
        self.assertIn(
            '              Field<int8> '
            'path="ssh_msg_channel_request.packet_length_target.want_reply" '
            'state=FAIL',
            converted,
        )
        self.assertIn("                ! ERROR category=token_mismatch", converted)
        self.assertIn('MISMATCH seed="ssh_channel_shell.raw"', converted)

    def test_reports_optional_condition_errors_and_filters_packet_type_cascades(self) -> None:
        converted = format_dsl_error_reports(
            make_optional_packet_union_schema(),
            {"publish.log": OPTIONAL_PACKET_UNION_REPORT},
        )

        self.assertIn(
            'Optional path="publish.body.packet_identifier" state=FAIL', converted
        )
        self.assertIn("category=unresolved_reference", converted)
        self.assertIn("Referenced element 'publish.header.flags' not found", converted)
        self.assertNotIn("Token did not match '3' vs. '1'", converted)
        self.assertNotIn('path="connect.header.packet_type"', converted)
        self.assertNotIn("No valid children were found", converted)
        self.assertNotIn("UNPARSED", converted)

    def test_all_rejected_packet_types_retain_only_longest_candidate(self) -> None:
        converted = format_dsl_error_reports(
            make_all_packet_types_rejected_schema(),
            {"union.log": FAILED_UNION_REPORT},
        )

        self.assertIn('Schema<long> path="long" state=OPEN', converted)
        self.assertNotIn('Schema<Short> path="short"', converted)
        self.assertNotIn("Token did not match '9' vs. '1'", converted)
        self.assertNotIn("No valid children were found", converted)

    def test_ordinary_union_keeps_all_rejected_alternatives(self) -> None:
        converted = format_dsl_error_reports(
            make_all_packet_types_rejected_schema(packet_union=False),
            {"union.log": FAILED_UNION_REPORT},
        )

        self.assertIn('Schema name="short"', converted)
        self.assertIn('Schema name="long"', converted)

    def test_isolated_report_conversion_writes_compact_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = root / "root.py"
            entry.write_text(
                "from peach_dsl import *\n"
                "class ChannelRequestPayload(Schema):\n"
                "    want_reply = Int8(0)\n"
                "class ChannelRequestRecord(Schema):\n"
                "    packet_length_target = ChannelRequestPayload()\n"
                "class OtherPacket(Schema):\n"
                "    msg_type = Int8(1)\n"
                "@PacketUnion\n"
                "class TestPacket(Schema):\n"
                "    packet_union = Union(ssh_msg_channel_request=ChannelRequestRecord, other=OtherPacket)\n"
                "@Default(endian=\"big\", signed=False)\n"
                "class TestPacketArray(Schema):\n"
                "    packets = Array[TestPacket, Occurs(0, 100)]()\n"
                "ROOT = TestPacketArray\n",
                encoding="utf-8",
            )
            log_dir = root / "logs"
            log_dir.mkdir()
            (log_dir / "channel.log").write_text(SAMPLE_REPORT, encoding="utf-8")
            output = root / "datamodel_error_report.txt"

            result = convert_reports_subprocess(entry, log_dir, output)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            converted = output.read_text(encoding="utf-8")
            self.assertIn(
                'path="ssh_msg_channel_request.packet_length_target.want_reply"',
                converted,
            )


if __name__ == "__main__":
    unittest.main()
