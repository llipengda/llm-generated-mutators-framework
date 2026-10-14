"""C# element predicates for rooted paths and expanded array items."""
from __future__ import annotations

from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest

if __package__ in ("", "tests"):
    __package__ = "peach_dsl.tests"

from .. import (
    Array, Int8, NodeType, Optional, PacketUnion, Schema, Union, fixed,
    get_node_types, get_supported_data_element_expressions,
    supported_data_element_expression,
)


class SupportedDataElementTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="dsl-csharp-predicate-")
        self.addCleanup(self._temporary.cleanup)
        self._compiled: dict[str, Path] = {}

    def matches(self, expression: str, full_name: str | None) -> bool:
        if shutil.which("mcs") is None or shutil.which("mono") is None:
            self.skipTest("C# predicate execution requires mcs and mono")
        root = Path(__file__).resolve().parents[2]
        sdk = root / "peach" / "sdk"
        extension = root.parent / "llm-peach" / "llm" / "Core" / "Extensions.cs"
        if not (sdk / "Peach.Core.dll").exists() or not extension.exists():
            self.skipTest("Native predicate checks require Peach SDK and llm-peach Extensions.cs")
        if expression not in self._compiled:
            folder = Path(self._temporary.name)
            source = folder / f"predicate_{len(self._compiled)}.cs"
            executable = source.with_suffix(".exe")
            # Compile the actual matcher in isolation against the legacy SDK.
            # NUnit tests also exercise the full Peach.LLM project assembly.
            extension_source = extension.read_text()
            start = extension_source.index("        private static readonly char[] InvalidPathCharacters")
            method = extension_source.index("public static bool MatchesPath(", start)
            end = extension_source.index("{", method)
            depth = 1
            while depth:
                end += 1
                depth += (extension_source[end] == "{") - (extension_source[end] == "}")
            matcher = folder / "Extensions.cs"
            matcher.write_text("using System; using Peach.Core.Dom; namespace Peach.LLM.Core { public static class Extensions {\n" + extension_source[start:end + 1] + "\n} }")
            source.write_text("""using System;
using Peach.Core.Dom;
using Peach.LLM.Core;
class Program {
    static bool Predicate(DataElement obj) { return """ + expression + """; }
    static DataElement Build(string path) {
        var parts = path.Split('.');
        DataElementContainer node = new DataModel(parts[0]);
        for (int i = 1; i < parts.Length; ++i) {
            DataElementContainer child;
            if (i + 1 < parts.Length && parts[i + 1].StartsWith(parts[i] + "_", StringComparison.Ordinal)) {
                var array = new Peach.Core.Dom.Array(parts[i]);
                array.OriginalElement = new Block(parts[i]);
                child = array;
            } else child = new Block(parts[i]);
            node.Add(child);
            node = child;
        }
        return node;
    }
    static void Main(string[] args) {
        Console.WriteLine(Predicate(args.Length == 0 ? null : Build(args[0])));
    }
}
""")
            compiled = subprocess.run(["mcs", "-r:" + str(sdk / "Peach.Core.dll"), str(matcher), str(source), "-out:" + str(executable)], capture_output=True, text=True)
            self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
            self._compiled[expression] = executable
        result = subprocess.run(["mono", str(self._compiled[expression]), *([] if full_name is None else [full_name])], capture_output=True, text=True, env={**os.environ, "MONO_PATH": str(sdk)})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip() == "True"

    def test_plain_path_matches_only_the_exact_rooted_node(self) -> None:
        expression = supported_data_element_expression("Root.header.value")
        self.assertEqual(expression, 'obj.MatchesPath(@"Root.header.value")')
        self.assertNotIn("Regex", expression)
        self.assertTrue(self.matches(expression, "Root.header.value"))
        for path in ("Other.header.value", "header.value", "Root.other.value", "Root.header.value.extra"):
            self.assertFalse(self.matches(expression, path), path)

    def test_array_item_indices_are_anchored_and_ascii_digits_only(self) -> None:
        expression = supported_data_element_expression("Root.items[].value")
        self.assertNotIn("Regex", expression)
        self.assertEqual(expression, 'obj.MatchesPath(@"Root.items[].value")')
        self.assertFalse(self.matches(expression, None))
        for path in ("Root.items.items_0.value", "Root.items.items_12.value"):
            self.assertTrue(self.matches(expression, path), path)
        for path in (
            "Root", "Root.items", "Root.items.items.value", "Root.items_0.value",
            "Root.items.items_.value", "Root.items.items_-1.value",
            "Root.items.items_1x.value", "Root.items.items_١.value",
            "Other.items.items_0.value", "Prefix.Root.items.items_0.value",
            "Root.items.items_0.value.child", "Root.items.items_0.value\n",
        ):
            self.assertFalse(self.matches(expression, path), path)

    def test_nested_arrays_and_array_container_inside_an_item(self) -> None:
        item = supported_data_element_expression("Root.matrix[].item[]")
        container = supported_data_element_expression("Root.matrix[].item")
        self.assertTrue(self.matches(item, "Root.matrix.matrix_4.item.item_10"))
        self.assertTrue(self.matches(container, "Root.matrix.matrix_4.item"))
        self.assertFalse(self.matches(item, "Root.matrix.matrix_4.item"))
        self.assertFalse(self.matches(container, "Root.matrix.matrix_4.item.item_10"))

    def test_fixed_sections_are_matched_as_groups_between_arrays(self) -> None:
        expression = supported_data_element_expression("Root.packet.items[].body.value")
        self.assertEqual(expression, 'obj.MatchesPath(@"Root.packet.items[].body.value")')
        self.assertTrue(self.matches(expression, "Root.packet.items.items_2.body.value"))
        for path in ("Root.other.items.items_2.body.value", "Root.packet.items.items_2.other.value", "Root.packet.items.items_2.extra.body.value"):
            self.assertFalse(self.matches(expression, path), path)

        nested = supported_data_element_expression("Root.packet.items[].section.rows[].body.value")
        self.assertTrue(self.matches(nested, "Root.packet.items.items_2.section.rows.rows_3.body.value"))
        for path in (
            "Root.packet.items.items_2.other.rows.rows_3.body.value",
            "Root.packet.items.items_2.section.rows.rows_x.body.value",
            "Root.packet.items.items_2.section.rows.rows_3.body.value.extra",
        ):
            self.assertFalse(self.matches(nested, path), path)

    def test_csharp_quotes_and_special_characters_are_literal(self) -> None:
        for path, concrete in (
            ('Root.header"quoted', 'Root.header"quoted'),
            ('R+oot.items"quoted[].v(a)|b$', 'R+oot.items"quoted.items"quoted_3.v(a)|b$'),
            ('Root.items😀[].value', 'Root.items😀.items😀_12.value'),
        ):
            expression = supported_data_element_expression(path)
            self.assertTrue(self.matches(expression, concrete))
            self.assertFalse(self.matches(expression, concrete.replace("Root", "Other").replace("R+oot", "Other")))
            if '"' in path:
                self.assertIn('""quoted', expression)

    def test_invalid_patterns_are_rejected(self) -> None:
        for path in ("", ".Root", "Root.", "Root..value", "Root/value", "Root\\value", "Root[]", "Root.[]", "Root.items[0]", "Root.items[].[]", "Root.items[][]"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                supported_data_element_expression(path)

    def test_schema_generation_preserves_types_order_and_export_options(self) -> None:
        class Header(Schema):
            tag = Int8(fixed(1))
            tail = Optional[Int8]()

        class Packet(Schema):
            items = Array[Header, 2]()

        @PacketUnion
        class Packets(Schema):
            packet_union = Union(first=Packet, second=Packet)

        for include_header in (False, True):
            with self.subTest(include_header=include_header):
                rows = get_supported_data_element_expressions(Packets, name="Root", include_header=include_header)
                self.assertEqual([(kind, path) for kind, path, _ in rows], get_node_types(Packets, name="Root", include_header=include_header))
                indexed = {path: (kind, expression) for kind, path, expression in rows}
                prefix = "Root.packet_union.first.items"
                self.assertEqual(indexed[prefix][0], NodeType.MUTABLE | NodeType.REPEATABLE)
                self.assertEqual(indexed[prefix + "[].tag"][0], NodeType.FIXED)
                self.assertTrue(self.matches(indexed[prefix + "[].tag"][1], prefix + ".items_7.tag"))
                self.assertFalse(self.matches(indexed[prefix + "[].tag"][1], "Root.packet_union.second.items.items_7.tag"))
                self.assertEqual(indexed[prefix + "[].tail"][0], NodeType.MUTABLE | NodeType.OPTIONAL)


if __name__ == "__main__":
    unittest.main()
