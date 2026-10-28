#!/usr/bin/env python3
"""layout_75pct.py -- 2D spatial grid for the AJAZZ AK820 Max Plus 75% layout.

The keyboard's per-key table is indexed in LED chain order, which bears no
relationship to the physical position of each key.  This module provides a
canonical (x, y) grid for every mapped key so that spatial effects (rainbow
gradients along the X axis, diagonal waves, CPU bars that fill left-to-right)
can work in visual space instead of chain order.

Grid coordinate system
----------------------

The raw grid uses integer positions::

    x in [0 .. 21]    (columns, left to right)
    y in [0 ..  5]    (rows, top to bottom: F-row = 0, bottom row = 5)

Normalised coordinates (as stored in ``keymap_matrix.json``) are floating
point in [0.0, 1.0] per axis::

    nx = x / 21.0
    ny = y / 5.0

The grid intentionally has *more columns than physical keys in any row* so
that staggered rows (1.5 u offsets, 0.25 u steps) land on integer x values
without fractional positions.

Usage
-----
    python layout_75pct.py                  # prints the grid and writes JSON
    python layout_75pct.py --dry-run        # prints without writing
    python layout_75pct.py --keymap my.json # write to a custom path
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

import ajazz_hid as h

# --------------------------------------------------------------------------
# Physical layout table
# --------------------------------------------------------------------------
# Each entry: (label, grid_x, grid_y)
#
# x spans 0..21 (22 half-unit columns) to accommodate the 75 % stagger.
# y spans 0..5  (6 rows: F-row through bottom row).
#
# Labels MUST match the labels used in keymap_matrix.json (case-sensitive).
# If a key has not been mapped yet in keymap_matrix.json it will be skipped
# when populating coordinates.

LAYOUT_75PCT: list[tuple[str, int, int]] = [
    # --- Row 0: Function row -------------------------------------------
    ("Esc",   0,  0),
    ("F1",    2,  0),
    ("F2",    3,  0),
    ("F3",    4,  0),
    ("F4",    5,  0),
    ("F5",    7,  0),
    ("F6",    8,  0),
    ("F7",    9,  0),
    ("F8",   10,  0),
    ("F9",   12,  0),
    ("F10",  13,  0),
    ("F11",  14,  0),
    ("F12",  15,  0),
    ("Del",  17,  0),
    ("Knob", 19,  0),

    # --- Row 1: Number row ---------------------------------------------
    ("`",     0,  1),
    ("1",     1,  1),
    ("2",     2,  1),
    ("3",     3,  1),
    ("4",     4,  1),
    ("5",     5,  1),
    ("6",     6,  1),
    ("7",     7,  1),
    ("8",     8,  1),
    ("9",     9,  1),
    ("0",    10,  1),
    ("-",    11,  1),
    ("=",    12,  1),
    ("Backspace", 14, 1),
    ("Home", 17,  1),

    # --- Row 2: QWERTY -------------------------------------------------
    ("Tab",   0,  2),
    ("Q",     2,  2),
    ("W",     3,  2),
    ("E",     4,  2),
    ("R",     5,  2),
    ("T",     6,  2),
    ("Y",     7,  2),
    ("U",     8,  2),
    ("I",     9,  2),
    ("O",    10,  2),
    ("P",    11,  2),
    ("[",    12,  2),
    ("]",    13,  2),
    ("\\",   15,  2),
    ("PgUp", 17,  2),

    # --- Row 3: Home row ------------------------------------------------
    ("Caps",  0,  3),
    ("A",     2,  3),
    ("S",     3,  3),
    ("D",     4,  3),
    ("F",     5,  3),
    ("G",     6,  3),
    ("H",     7,  3),
    ("J",     8,  3),
    ("K",     9,  3),
    ("L",    10,  3),
    (";",    11,  3),
    ("'",    12,  3),
    ("Enter",14,  3),
    ("PgDn", 17,  3),

    # --- Row 4: Shift row -----------------------------------------------
    ("LShift", 0, 4),
    ("Z",      2, 4),
    ("X",      3, 4),
    ("C",      4, 4),
    ("V",      5, 4),
    ("B",      6, 4),
    ("N",      7, 4),
    ("M",      8, 4),
    (",",      9, 4),
    (".",     10, 4),
    ("/",     11, 4),
    ("RShift",13, 4),
    ("Up",    16, 4),
    ("End",   17, 4),

    # --- Row 5: Bottom row -----------------------------------------------
    ("LCtrl", 0, 5),
    ("Win",   2, 5),
    ("LAlt",  3, 5),
    ("Space", 7, 5),
    ("RAlt", 11, 5),
    ("Fn",   12, 5),
    ("RCtrl",13, 5),
    ("Left", 15, 5),
    ("Down", 16, 5),
    ("Right",17, 5),
]

#: Maximum raw grid x.
GRID_X_MAX = 21
#: Maximum raw grid y.
GRID_Y_MAX = 5


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def raw_grid() -> dict[str, tuple[int, int]]:
    """Return ``{label: (x, y)}`` for every key in the 75 % layout."""
    return {label: (x, y) for label, x, y in LAYOUT_75PCT}


def normalised_grid() -> dict[str, tuple[float, float]]:
    """Return ``{label: (nx, ny)}`` with coordinates in [0.0, 1.0].

    ``nx = x / GRID_X_MAX``, ``ny = y / GRID_Y_MAX``.
    """
    return {
        label: (x / GRID_X_MAX, y / GRID_Y_MAX)
        for label, x, y in LAYOUT_75PCT
    }


def index_positions(
    keymap: dict[int, str],
) -> dict[int, tuple[float, float]]:
    """Map LED chain indices to normalised (nx, ny) using a keymap.

    Only indices whose label appears in both ``keymap`` and the layout are
    included.  Keys present in the keymap but absent from the layout are
    silently skipped (they may be underglow or indicator LEDs with no
    physical position).
    """
    grid = normalised_grid()
    result: dict[int, tuple[float, float]] = {}
    for idx, label in keymap.items():
        pos = grid.get(label)
        if pos is not None:
            result[idx] = pos
    return result


def populate_keymap_json(
    path: str | Path = h.KEYMAP_MATRIX_FILE,
    *,
    dry_run: bool = False,
) -> dict[int, tuple[float, float]]:
    """Read keymap_matrix.json, compute (nx, ny) for each mapped key, and
    write the coordinates back into the JSON under a ``"positions"`` key.

    Returns the ``{index: (nx, ny)}`` dict.
    """
    p = Path(path)
    keymap = h.load_keymap_matrix(p)
    if not keymap:
        print(f"warning: {p} not found or empty; no positions to compute")
        return {}

    positions = index_positions(keymap)

    if not dry_run:
        # Re-read the full JSON so we preserve all existing fields.
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            doc = {}

        doc["positions"] = {
            str(idx): list(pos) for idx, pos in sorted(positions.items())
        }

        import os
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(
            json.dumps(doc, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, p)
        print(f"wrote {len(positions)} positions to {p}")

    return positions


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _print_grid() -> None:
    """Pretty-print the visual grid."""
    by_row: dict[int, list[tuple[str, int]]] = {}
    for label, x, y in LAYOUT_75PCT:
        by_row.setdefault(y, []).append((label, x))

    print(f"AJAZZ AK820 Max Plus 75% layout  (x: 0..{GRID_X_MAX}, y: 0..{GRID_Y_MAX})")
    print("=" * 72)

    for row in sorted(by_row):
        keys = sorted(by_row[row], key=lambda kx: kx[1])
        cells = "  ".join(f"{label}@{x}" for label, x in keys)
        print(f"  row {row}: {cells}")

    print(f"\n{len(LAYOUT_75PCT)} keys defined in the layout.")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="2D spatial grid for the AJAZZ AK820 Max Plus 75% layout",
    )
    parser.add_argument(
        "--keymap", default=h.KEYMAP_MATRIX_FILE,
        help=f"keymap file to annotate with positions (default: {h.KEYMAP_MATRIX_FILE})",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="print the grid but do not write positions to the keymap file",
    )
    args = parser.parse_args(argv)

    _print_grid()
    print()

    positions = populate_keymap_json(args.keymap, dry_run=args.dry_run)

    if positions:
        print(f"\nNormalised positions ({len(positions)} keys):")
        grid = normalised_grid()
        keymap = h.load_keymap_matrix(args.keymap)
        for idx in sorted(positions):
            label = keymap.get(idx, "?")
            nx, ny = positions[idx]
            print(f"  idx {idx:3d}  {label:8s}  ({nx:.3f}, {ny:.3f})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
