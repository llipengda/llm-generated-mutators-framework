"""Fixed values always require token checking, including instance overrides."""
from __future__ import annotations

import unittest

if __package__ in ("", "tests"):
    __package__ = "peach_dsl.tests"

from .. import Bit, Blob, DecimalString, Double, ExtendedType, Int8, Schema, String, evaluate_schema, fixed, get_node_types, to_peach_data_model


class FixedTokenTests(unittest.TestCase):
    def test_all_scalar_families_reject_disabled_fixed_tokens(self) -> None:
        factories = (
            lambda: Int8(fixed(1), token=False),
            lambda: Bit[1](fixed(1), token=False),
            lambda: Double(fixed(1.0), token=False),
            lambda: String(fixed("A"), token=False),
            lambda: DecimalString[2](fixed(1), token=False),
            lambda: Blob[1](fixed(b"A"), token=False),
        )
        for index, factory in enumerate(factories):
            with self.subTest(index=index):
                with self.assertRaisesRegex(ValueError, "fixed.*token=False"):
                    factory()

    def test_extended_attributes_cannot_bypass_fixed_token_validation(self) -> None:
        custom = ExtendedType[int]("Custom")
        for token in (False, "false", "FALSE", " false "):
            with self.subTest(token=token):
                with self.assertRaisesRegex(ValueError, "fixed.*token=False"):
                    custom(fixed(1), token=token)

    def test_fixed_overrides_of_non_token_defaults_are_rejected(self) -> None:
        class Packet(Schema):
            value = Int8(1, token=False)

        for operation in (evaluate_schema, get_node_types, to_peach_data_model):
            with self.subTest(operation=operation.__name__):
                with self.assertRaisesRegex(ValueError, "value.*fixed.*token=False"):
                    operation(Packet(value=fixed(2)), include_header=False)
        self.assertIn('token="false"', to_peach_data_model(Packet, include_header=False))
        self.assertIn('token="true"', to_peach_data_model(Packet(value=Int8(fixed(2))), include_header=False))


if __name__ == "__main__":
    unittest.main()
