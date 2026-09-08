import os
import unittest
from unittest.mock import patch

from core.peach_sdk import peach_image, peach_sdk_dir, peach_sdk_from_environment


class PeachSdkSelectionTests(unittest.TestCase):
    def test_defaults_to_legacy(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(peach_sdk_from_environment(), "legacy")
            self.assertEqual(peach_sdk_dir().as_posix(), "peach/sdk")
            self.assertEqual(peach_image(), "pdli/llm-peach:sdk")

    def test_accepts_modern_sdk_alias(self) -> None:
        with patch.dict(os.environ, {"PEACH_SDK": "modern-sdk"}, clear=True):
            self.assertEqual(peach_sdk_from_environment(), "modern")
            self.assertEqual(peach_sdk_dir().as_posix(), "peach/modern-sdk")
            self.assertEqual(peach_image(), "pdli/llm-peach:modern-sdk")

    def test_rejects_unknown_variant(self) -> None:
        with patch.dict(os.environ, {"PEACH_SDK": "future"}, clear=True):
            with self.assertRaisesRegex(ValueError, "PEACH_SDK"):
                peach_sdk_from_environment()


if __name__ == "__main__":
    unittest.main()
