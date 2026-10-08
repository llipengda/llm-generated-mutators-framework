"""Repeated DSL loads must observe source changes without leaking modules."""

import os
from pathlib import Path
import py_compile
import sys
import tempfile
from types import ModuleType
import unittest
from xml.etree import ElementTree as ET

if __package__ in ("", "tests"):
    __package__ = "peach_dsl.tests"

from ..compiler import DSLValidationError, _load_entry, compile_entry, validate_dsl_dependencies
from .. import to_peach_data_model


class CompilerImportTests(unittest.TestCase):
    def test_imported_module_type_errors_are_reported_even_if_unused_by_entry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shared = root / "shared_model.py"
            shared.write_text(
                "from peach_dsl import Schema, Int8\n"
                "wrong: int = 'bad'\n"
                "class Item(Schema):\n    value = Int8()\n", encoding="utf-8",
            )
            entry = root / "root.py"
            entry.write_text("from shared_model import Item\nROOT = Item\n", encoding="utf-8")
            with self.assertRaises(DSLValidationError) as raised:
                validate_dsl_dependencies(entry)
            self.assertIn("shared_model.py:2:", str(raised.exception))
            self.assertIn("(reportAssignmentType)", str(raised.exception))

    def test_unimported_local_module_is_not_a_validation_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "unrelated.py").write_text("wrong: int = 'bad'\n", encoding="utf-8")
            entry = root / "root.py"
            entry.write_text(
                "from peach_dsl import Schema, Int8\n"
                "class Packet(Schema):\n    value = Int8()\n"
                "ROOT = Packet\n", encoding="utf-8",
            )
            self.assertEqual(set(validate_dsl_dependencies(entry)), {entry.resolve()})

    def test_plain_import_cannot_bypass_dsl_language_rules(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "shared_model.py").write_text(
                "from peach_dsl import ExtendedType\n"
                "Unsupported = ExtendedType[bool]('Custom')\n", encoding="utf-8",
            )
            entry = root / "root.py"
            entry.write_text(
                "import shared_model as shared\nVALUE = shared.Unsupported\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(DSLValidationError, "ExtendedType\\[bool\\] is forbidden"):
                validate_dsl_dependencies(entry)

    def test_package_relative_imports_are_validated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "models"
            package.mkdir()
            (package / "__init__.py").write_text(
                "from . import wire\nVALUE = wire.VALUE\n", encoding="utf-8"
            )
            (package / "wire.py").write_text(
                "from peach_dsl import ExtendedType\n"
                "VALUE = ExtendedType[bool]('Custom')\n", encoding="utf-8",
            )
            entry = root / "root.py"
            for import_line in ("import models\nVALUE = models.VALUE\n", "from models import VALUE as VALUE\n"):
                entry.write_text(import_line, encoding="utf-8")
                with self.subTest(import_line=import_line):
                    with self.assertRaisesRegex(DSLValidationError, "ExtendedType\\[bool\\] is forbidden"):
                        validate_dsl_dependencies(entry)

    def test_dotted_import_collects_package_and_relative_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "models"
            package.mkdir()
            (package / "__init__.py").write_text("", encoding="utf-8")
            (package / "values.py").write_text("VALUE = 9\n", encoding="utf-8")
            (package / "wire.py").write_text(
                "from .values import VALUE as VALUE\n", encoding="utf-8"
            )
            entry = root / "root.py"
            entry.write_text("import models.wire\nVALUE = models.wire.VALUE\n", encoding="utf-8")
            trees = validate_dsl_dependencies(entry)
            self.assertEqual(
                {str(path.relative_to(root.resolve())) for path in trees},
                {"root.py", "models/__init__.py", "models/wire.py", "models/values.py"},
            )

    def test_recompile_refreshes_computed_function_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = root / "root.py"
            output = root / "datamodel.xml"
            source = (
                "from peach_dsl import Schema, Int8, compute\n"
                "@compute\n"
                "def increment(value: int) -> int:\n"
                "    return value + 1\n"
                "class Packet(Schema):\n"
                "    source = Int8()\n"
                "    result = Int8(increment(source))\n"
                "ROOT = Packet\n"
            )
            entry.write_text(source, encoding="utf-8")
            compile_entry(entry, output)
            original_stat = entry.stat()
            entry.write_text(source.replace("value + 1", "value + 9"), encoding="utf-8")
            os.utime(entry, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
            compile_entry(entry, output)
            script = (root / "python_fixup.py").read_text(encoding="utf-8")
            self.assertIn("return value + 9", script)
            self.assertNotIn("return value + 1", script)

    def test_recompile_reads_changed_sibling_despite_matching_pyc_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = root / "root.py"
            shared = root / "shared_model.py"
            output = root / "datamodel.xml"
            source = (
                "from peach_dsl import Schema, Int8, fixed\n"
                "class Item(Schema):\n"
                "    value = Int8(fixed(1))\n"
            )
            shared.write_text(source, encoding="utf-8")
            entry.write_text("from shared_model import Item\nROOT = Item\n", encoding="utf-8")
            compile_entry(entry, output)
            py_compile.compile(str(shared), doraise=True)
            original_stat = shared.stat()
            shared.write_text(source.replace("fixed(1)", "fixed(9)"), encoding="utf-8")
            os.utime(shared, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
            compile_entry(entry, output)
            xml = ET.fromstring(output.read_text(encoding="utf-8"))
            value = xml.find("{*}DataModel/{*}Number")
            assert value is not None
            self.assertEqual(value.get("value"), "9")

    def test_same_named_sibling_modules_are_isolated_between_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for value in (1, 9):
                root = Path(directory) / str(value)
                root.mkdir()
                (root / "shared_model.py").write_text(
                    "from peach_dsl import Schema, Int8, fixed\n"
                    f"class Item(Schema):\n    value = Int8(fixed({value}))\n",
                    encoding="utf-8",
                )
                entry = root / "root.py"
                entry.write_text("from shared_model import Item\nROOT = Item\n", encoding="utf-8")
                loaded = _load_entry(entry)
                xml = ET.fromstring(to_peach_data_model(loaded.ROOT, include_header=False))
                field = xml.find("Number")
                assert field is not None
                self.assertEqual(field.get("value"), str(value))

    def test_failed_load_restores_import_state(self) -> None:
        sentinel = ModuleType("dsl_import_probe")
        previous = sys.modules.get("dsl_import_probe")
        saved_path = sys.path[:]
        saved_finders = sys.meta_path[:]
        sys.modules["dsl_import_probe"] = sentinel
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "dsl_import_probe.py").write_text("value = 9\n", encoding="utf-8")
                entry = root / "root.py"
                entry.write_text(
                    "import dsl_import_probe\n"
                    "assert dsl_import_probe.value == 9\n"
                    "raise ValueError('load failed')\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ValueError, "load failed"):
                    _load_entry(entry)
            self.assertIs(sys.modules["dsl_import_probe"], sentinel)
            self.assertEqual(sys.path, saved_path)
            self.assertEqual(sys.meta_path, saved_finders)
        finally:
            if previous is None:
                sys.modules.pop("dsl_import_probe", None)
            else:
                sys.modules["dsl_import_probe"] = previous
