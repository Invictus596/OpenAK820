"""Hardware-free tests for the AJAZZ AK820 Max Plus protocol layer.

Run with:  python -m unittest discover -s tests -v

Everything here validates byte layout only -- no device is opened.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ajazz_hid as h  # noqa: E402
import cli  # noqa: E402
import matrix_mapper as mm  # noqa: E402


class TestLightingPacket(unittest.TestCase):
    def test_static_cyan_layout(self) -> None:
        packet = h.static_cyan_packet()
        self.assertEqual(len(packet), h.REPORT_SIZE)
        self.assertEqual(packet[0], 0x00)
        self.assertEqual(packet[1:5], h.MAGIC)
        self.assertEqual(packet[5], 0x07)
        self.assertEqual(packet[6], h.Mode.STATIC)
        self.assertEqual(packet[7], 0x04)   # brightness 5 -> 4
        self.assertEqual(packet[8], 0x03)   # speed 4 -> 3
        self.assertEqual(packet[9], 0x00)
        self.assertEqual(packet[10], h.Color.CYAN)
        self.assertEqual(packet[11:], bytes(54))

    def test_kaleidoscope_variant(self) -> None:
        packet = h.build_lighting_packet(h.Mode.KALEIDOSCOPE, 5, 4, h.Color.RED)
        self.assertEqual(packet[9], 0x01)

    def test_level_bounds(self) -> None:
        with self.assertRaises(ValueError):
            h.build_lighting_packet(h.Mode.STATIC, 0)
        with self.assertRaises(ValueError):
            h.build_lighting_packet(h.Mode.STATIC, 6)


class TestKeyTable(unittest.TestCase):
    def test_geometry_constants(self) -> None:
        self.assertEqual(h.KEY_COUNT, 118)
        self.assertEqual(h.KEY_CHUNKS, 6)
        self.assertEqual(h.CHUNK_LEN, 59)
        self.assertEqual(h.KEY_TABLE_SIZE, 354)
        self.assertEqual(h.KEY_CHUNKS * h.CHUNK_LEN, h.KEY_TABLE_SIZE)

    def test_pack_roundtrip(self) -> None:
        keys = [((i * 7) % 256, (i * 13) % 256, (i * 29) % 256) for i in range(h.KEY_COUNT)]
        table = h.pack_key_table(keys)
        self.assertEqual(len(table), h.KEY_TABLE_SIZE)
        self.assertEqual(h.parse_key_table(table), keys)

    def test_pack_rejects_bad_input(self) -> None:
        with self.assertRaises(ValueError):
            h.pack_key_table([(0, 0, 0)] * (h.KEY_COUNT - 1))
        with self.assertRaises(ValueError):
            h.pack_key_table([(0, 0, 256)] * h.KEY_COUNT)
        with self.assertRaises(ValueError):
            h.pack_key_table([(0, 0)] * h.KEY_COUNT)
        with self.assertRaises(ValueError):
            h.pack_key_table([(0, 0, True)] * h.KEY_COUNT)

    def test_sparse_table(self) -> None:
        table = h.build_key_table({15: (0, 255, 0)})
        self.assertEqual(table[45:48], b"\x00\xff\x00")
        self.assertEqual(table[:45], bytes(45))
        self.assertEqual(table[48:], bytes(h.KEY_TABLE_SIZE - 48))

    def test_build_key_table_rejects_bad_index(self) -> None:
        with self.assertRaises(ValueError):
            h.build_key_table({h.KEY_COUNT: (255, 255, 255)})
        with self.assertRaises(ValueError):
            h.build_key_table({-1: (255, 255, 255)})


class TestChunking(unittest.TestCase):
    def test_opcode_sequence(self) -> None:
        packets = h.build_keymap_chunks(h.pack_key_table([(0, 0, 0)] * h.KEY_COUNT))
        self.assertEqual(len(packets), h.KEY_CHUNKS)
        self.assertEqual([p[5] for p in packets], [0xF6 + i for i in range(h.KEY_CHUNKS)])
        for packet in packets:
            self.assertEqual(len(packet), h.REPORT_SIZE)
            self.assertEqual(packet[0], 0x00)
            self.assertEqual(packet[1:5], h.MAGIC)

    def test_documented_byte_formula(self) -> None:
        """table_byte T -> opcode 0xF6 + T//59, buffer offset 6 + T % 59."""
        keys = [((i * 3) % 256, (i * 5) % 256, (i * 11) % 256) for i in range(h.KEY_COUNT)]
        table = h.pack_key_table(keys)
        packets = h.build_keymap_chunks(table)

        for t, value in enumerate(table):
            chunk, offset = divmod(t, h.CHUNK_LEN)
            self.assertEqual(packets[chunk][5], 0xF6 + chunk)
            self.assertEqual(packets[chunk][6 + offset], value)

    def test_esc_index_15_matches_capture(self) -> None:
        """PROTOCOL.md: Esc=15 -> table 45..47 -> 0xF6 buffer 51..53."""
        table = h.build_key_table({15: (0, 255, 0)})
        packets = h.build_keymap_chunks(table)
        self.assertEqual(packets[0][51:54], b"\x00\xff\x00")
        # Exactly one report changed.
        self.assertEqual(sum(1 for p in packets if any(p[6:])), 1)

    def test_triples_straddle_reports(self) -> None:
        """59 is not a multiple of 3; key 19 (table 57..59) splits across reports."""
        table = h.build_key_table({19: (1, 2, 3)})
        packets = h.build_keymap_chunks(table)
        # buffer offset = 6 + table byte, so 57 -> 63, 58 -> 64, 59 -> next report
        self.assertEqual(packets[0][63:65], b"\x01\x02")
        self.assertEqual(packets[1][6], 0x03)
        self.assertEqual(packets[1][7:], bytes(h.CHUNK_LEN - 1))

    def test_commit_packet(self) -> None:
        commit = h.build_commit_packet()
        self.assertEqual(len(commit), h.REPORT_SIZE)
        self.assertEqual(commit[5], 0x0A)
        self.assertEqual(commit[6:], bytes(59))

    def test_rejects_wrong_length(self) -> None:
        with self.assertRaises(ValueError):
            h.build_keymap_chunks(bytes(353))


class TestKeymapMatrixFile(unittest.TestCase):
    def test_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "keymap_matrix.json"
            h.save_keymap_matrix({15: "Esc", 14: "F1"}, path)

            doc = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(doc["key_count"], h.KEY_COUNT)
            self.assertEqual(doc["key_to_index"], {"Esc": 15, "F1": 14})
            self.assertEqual(h.load_keymap_matrix(path), {14: "F1", 15: "Esc"})

    def test_missing_file_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(h.load_keymap_matrix(Path(tmp) / "nope.json"), {})

    def test_legacy_flat_format(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.json"
            path.write_text('{"3": "Tab"}', encoding="utf-8")
            self.assertEqual(h.load_keymap_matrix(path), {3: "Tab"})


class TestMapperPrompt(unittest.TestCase):
    def _ask(self, answers, mapping=None):
        with mock.patch("builtins.input", side_effect=answers):
            return mm._prompt(10, mapping or {})

    def test_enter_advances(self) -> None:
        self.assertEqual(self._ask([""]), ("next", None))

    def test_single_lowercase_is_command(self) -> None:
        self.assertEqual(self._ask(["n"]), ("next", None))
        self.assertEqual(self._ask(["p"]), ("prev", None))
        self.assertEqual(self._ask(["s"]), ("skip", None))
        self.assertEqual(self._ask(["q"]), ("quit", None))

    def test_uppercase_letter_is_label(self) -> None:
        self.assertEqual(self._ask(["N"]), ("label", "N"))
        self.assertEqual(self._ask(["Q"]), ("label", "Q"))

    def test_slash_escapes_command(self) -> None:
        self.assertEqual(self._ask(["/n"]), ("label", "n"))

    def test_duplicate_label_reprompts(self) -> None:
        result = self._ask(["Esc", "Escape"], {15: "Esc"})
        self.assertEqual(result, ("label", "Escape"))

    def test_help_reprompts(self) -> None:
        self.assertEqual(self._ask(["?", "Tab"]), ("label", "Tab"))


class TestDemoResolution(unittest.TestCase):
    def test_alias_and_normalisation(self) -> None:
        lookup = cli._build_name_lookup({15: "Esc", 14: "F1", 30: "Arrow Up", 40: "W"})
        self.assertEqual(cli._lookup_index(lookup, "Up"), 30)
        self.assertEqual(cli._lookup_index(lookup, "ArrowUp"), 30)
        self.assertEqual(cli._lookup_index(lookup, "Arrow Up"), 30)
        self.assertEqual(cli._lookup_index(lookup, "W"), 40)
        self.assertEqual(cli._lookup_index(lookup, "Escape"), 15)
        self.assertIsNone(cli._lookup_index(lookup, "Arrow Down"))


if __name__ == "__main__":
    unittest.main()
