"""Hardware-free tests for auto_matrix.py's key decoding.

Run with:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import auto_matrix as am  # noqa: E402


class TestDecodeKeypress(unittest.TestCase):
    """decode_keypress(first, second, held) -> label | None."""

    def test_printable_letters_are_uppercased(self) -> None:
        self.assertEqual(am.decode_keypress("a", None, set()), "A")
        self.assertEqual(am.decode_keypress("z", None, set()), "Z")
        self.assertEqual(am.decode_keypress("Q", None, set()), "Q")

    def test_printable_symbols_use_unshifted_label(self) -> None:
        for char, label in (
            ("`", "`"), ("1", "1"), ("-", "-"), ("=", "="),
            ("[", "["), ("]", "]"), ("\\", "\\"), (";", ";"),
            ("'", "'"), (",", ","), (".", "."), ("/", "/"),
        ):
            self.assertEqual(am.decode_keypress(char, None, set()), label, char)

    def test_named_keys_via_char_codes(self) -> None:
        self.assertEqual(am.decode_keypress("\r", None, set()), "Enter")
        self.assertEqual(am.decode_keypress("\x08", None, set()), "Backspace")
        self.assertEqual(am.decode_keypress("\t", None, set()), "Tab")
        self.assertEqual(am.decode_keypress("\x1b", None, set()), "Esc")
        self.assertEqual(am.decode_keypress(" ", None, set()), "Space")

    def test_extended_prefix_scancodes(self) -> None:
        # Arrows / navigation arrive as \xe0 + scancode.
        self.assertEqual(am.decode_keypress("\xe0", "H", set()), "Up")
        self.assertEqual(am.decode_keypress("\xe0", "P", set()), "Down")
        self.assertEqual(am.decode_keypress("\xe0", "K", set()), "Left")
        self.assertEqual(am.decode_keypress("\xe0", "M", set()), "Right")
        self.assertEqual(am.decode_keypress("\xe0", "S", set()), "Del")
        self.assertEqual(am.decode_keypress("\xe0", "G", set()), "Home")
        self.assertEqual(am.decode_keypress("\xe0", "O", set()), "End")
        # F-keys arrive as \x00 + scancode.
        self.assertEqual(am.decode_keypress("\x00", ";", set()), "F1")
        self.assertEqual(am.decode_keypress("\x00", "<", set()), "F2")
        self.assertEqual(am.decode_keypress("\xe0", "\x86", set()), "F12")

    def test_modifiers_use_held_side_detection(self) -> None:
        self.assertEqual(am.decode_keypress("\x10", None, {"LShift"}), "LShift")
        self.assertEqual(am.decode_keypress("\x10", None, {"RShift"}), "RShift")
        self.assertEqual(am.decode_keypress("\x11", None, {"LCtrl"}), "LCtrl")
        self.assertEqual(am.decode_keypress("\x12", None, {"RAlt"}), "RAlt")
        # '[' (0x5B) is printable and wins over the VK_LWIN collision; the
        # physical Win key is detected focus-free by held_modifiers() instead.
        self.assertEqual(am.decode_keypress("\x5b", None, {"Win"}), "[")
        self.assertEqual(am.decode_keypress("\\", None, set()), "\\")
        # Ambiguous / instantaneous presses yield None (probe retries).
        self.assertIsNone(am.decode_keypress("\x10", None, set()))
        self.assertIsNone(am.decode_keypress("\x10", None, {"LShift", "RShift"}))

    def test_unknown_codes_return_none(self) -> None:
        self.assertIsNone(am.decode_keypress("\x01", None, set()))
        self.assertIsNone(am.decode_keypress("\xe0", "\x01", set()))


if __name__ == "__main__":
    unittest.main()