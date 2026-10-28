"""Tests for layout_75pct.py -- grid math and normalisation.

Run with:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ajazz_hid as h  # noqa: E402
import layout_75pct  # noqa: E402


class TestRawGrid(unittest.TestCase):
    """Verify the raw integer grid."""

    def test_all_labels_are_strings(self) -> None:
        for label, x, y in layout_75pct.LAYOUT_75PCT:
            self.assertIsInstance(label, str, label)
            self.assertTrue(len(label) > 0, "empty label")

    def test_x_within_bounds(self) -> None:
        for label, x, y in layout_75pct.LAYOUT_75PCT:
            self.assertGreaterEqual(x, 0, label)
            self.assertLessEqual(x, layout_75pct.GRID_X_MAX, label)

    def test_y_within_bounds(self) -> None:
        for label, x, y in layout_75pct.LAYOUT_75PCT:
            self.assertGreaterEqual(y, 0, label)
            self.assertLessEqual(y, layout_75pct.GRID_Y_MAX, label)

    def test_no_duplicate_labels(self) -> None:
        labels = [label for label, _, _ in layout_75pct.LAYOUT_75PCT]
        self.assertEqual(len(labels), len(set(labels)),
                         f"duplicate labels: {[l for l in labels if labels.count(l) > 1]}")

    def test_no_duplicate_positions(self) -> None:
        positions = [(x, y) for _, x, y in layout_75pct.LAYOUT_75PCT]
        self.assertEqual(len(positions), len(set(positions)),
                         "duplicate grid positions")

    def test_six_rows(self) -> None:
        rows = {y for _, _, y in layout_75pct.LAYOUT_75PCT}
        self.assertEqual(rows, {0, 1, 2, 3, 4, 5})

    def test_raw_grid_dict(self) -> None:
        grid = layout_75pct.raw_grid()
        self.assertEqual(len(grid), len(layout_75pct.LAYOUT_75PCT))
        self.assertEqual(grid["Esc"], (0, 0))
        self.assertEqual(grid["Right"], (17, 5))


class TestNormalisedGrid(unittest.TestCase):
    """Verify the normalised [0.0, 1.0] coordinate space."""

    def test_all_values_in_unit_range(self) -> None:
        for label, (nx, ny) in layout_75pct.normalised_grid().items():
            self.assertGreaterEqual(nx, 0.0, label)
            self.assertLessEqual(nx, 1.0, label)
            self.assertGreaterEqual(ny, 0.0, label)
            self.assertLessEqual(ny, 1.0, label)

    def test_esc_is_origin(self) -> None:
        grid = layout_75pct.normalised_grid()
        self.assertEqual(grid["Esc"], (0.0, 0.0))

    def test_normalisation_formula(self) -> None:
        """Spot-check: F1 is at raw (2, 0) → normalised (2/21, 0/5)."""
        grid = layout_75pct.normalised_grid()
        self.assertAlmostEqual(grid["F1"][0], 2.0 / 21.0)
        self.assertAlmostEqual(grid["F1"][1], 0.0)

    def test_right_is_at_column_17(self) -> None:
        grid = layout_75pct.normalised_grid()
        self.assertAlmostEqual(grid["Right"][0], 17.0 / 21.0)
        self.assertAlmostEqual(grid["Right"][1], 1.0)

    def test_space_is_centred(self) -> None:
        grid = layout_75pct.normalised_grid()
        nx, ny = grid["Space"]
        # Space is at raw x=7, y=5 → nx = 7/21 = 0.333...
        self.assertAlmostEqual(nx, 7.0 / 21.0)
        self.assertAlmostEqual(ny, 1.0)


class TestIndexPositions(unittest.TestCase):
    """Mapping LED chain indices to normalised coordinates."""

    def test_maps_known_keys(self) -> None:
        keymap = {15: "Esc", 14: "F1", 40: "W"}
        positions = layout_75pct.index_positions(keymap)
        self.assertEqual(len(positions), 3)
        self.assertIn(15, positions)
        self.assertIn(14, positions)
        self.assertIn(40, positions)

    def test_skips_labels_not_in_layout(self) -> None:
        keymap = {99: "Underglow1", 100: "SideLED2"}
        positions = layout_75pct.index_positions(keymap)
        self.assertEqual(positions, {})

    def test_mixed_known_and_unknown(self) -> None:
        keymap = {15: "Esc", 99: "Underglow1"}
        positions = layout_75pct.index_positions(keymap)
        self.assertEqual(len(positions), 1)
        self.assertIn(15, positions)

    def test_esc_position_values(self) -> None:
        positions = layout_75pct.index_positions({15: "Esc"})
        self.assertEqual(positions[15], (0.0, 0.0))


class TestPopulateKeymapJson(unittest.TestCase):
    """End-to-end: read keymap, compute positions, write back."""

    def test_writes_positions_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "keymap_matrix.json"
            h.save_keymap_matrix({15: "Esc", 14: "F1"}, path)

            positions = layout_75pct.populate_keymap_json(path)
            self.assertEqual(len(positions), 2)

            doc = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("positions", doc)
            self.assertIn("15", doc["positions"])
            self.assertIn("14", doc["positions"])

            # Check the values are lists of two floats
            esc_pos = doc["positions"]["15"]
            self.assertEqual(len(esc_pos), 2)
            self.assertAlmostEqual(esc_pos[0], 0.0)
            self.assertAlmostEqual(esc_pos[1], 0.0)

    def test_dry_run_does_not_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "keymap_matrix.json"
            h.save_keymap_matrix({15: "Esc"}, path)
            original = path.read_text(encoding="utf-8")

            positions = layout_75pct.populate_keymap_json(path, dry_run=True)
            self.assertEqual(len(positions), 1)
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_missing_file_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nope.json"
            positions = layout_75pct.populate_keymap_json(path)
            self.assertEqual(positions, {})

    def test_preserves_existing_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "keymap_matrix.json"
            h.save_keymap_matrix({15: "Esc"}, path)

            # Read, add a custom field, write back
            doc = json.loads(path.read_text(encoding="utf-8"))
            doc["custom_field"] = "test_value"
            path.write_text(json.dumps(doc, indent=2), encoding="utf-8")

            layout_75pct.populate_keymap_json(path)

            doc2 = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(doc2["custom_field"], "test_value")
            self.assertIn("positions", doc2)


if __name__ == "__main__":
    unittest.main()
