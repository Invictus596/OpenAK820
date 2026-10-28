#!/usr/bin/env python3
"""auto_matrix.py -- semi-automatic LED index -> key-label mapper.

The keyboard's per-key table is indexed in LED chain order, which does not
match the visual layout (Esc is index 15, F1 is index 14).  This tool walks
the 118 slots one at a time, lights exactly one key, and identifies the key
from the keypress itself: you press the physically lit key, the mapper reads
the scancode, and records the label.  No typing, no typos.

How to use
----------
1. Close ``DeviceDriver.exe`` and stop any animation daemon
   (``python cli.py stop``) so nothing else writes to the board.
2. Run ``python auto_matrix.py`` and keep the mapper console focused.
3. For each probe: press the key that lights up.  If nothing on the board
   lights up (underglow / indicator / knob-ring slot), simply wait -- the
   slot is recorded as ``LED<index>`` when the press window expires.
4. Press keys WITHOUT modifiers unless the lit key IS that modifier.
   Ctrl+C saves progress and restores Static White; rerun any time --
   the mapper resumes at the first unmapped slot.

Modifier pairs (LShift/RShift, LCtrl/RCtrl, LAlt/RAlt, Win) are resolved
without console focus by polling ``GetAsyncKeyState``, so press them normally.

The map is written to ``keymap_matrix.json`` after every entry and the
normalised (x, y) positions are refreshed from ``layout_75pct.py`` at exit.
"""

from __future__ import annotations

import argparse
import ctypes
import sys
import time
from pathlib import Path
from typing import Optional

import ajazz_hid as h

try:
    import msvcrt  # noqa: F401 - Windows only; guarded for imports elsewhere
    HAS_MSVCRT = True
except ImportError:  # pragma: no cover - non-Windows
    HAS_MSVCRT = False

#: Probe colour: white at full board brightness is easiest to spot.
PROBE_COLOURS = {
    "white": (255, 255, 255),
    "red": (255, 0, 0),
    "green": (0, 255, 0),
    "blue": (0, 0, 255),
}

# --- VK / scancode tables ----------------------------------------------------
# Single getwch() characters whose ord() is a virtual-key code:
VK_CHARS = {
    0x08: "Backspace",
    0x09: "Tab",
    0x0D: "Enter",
    0x1B: "Esc",
    0x20: "Space",
}

#: Modifier virtual keys resolved via GetAsyncKeyState side detection.
#: (The Win key, VK 0x5B/0x5C, is polled focus-free instead -- its char code
#: collides with the printable '[' / '\\' keys, and consoles never receive it.)
VK_MODIFIERS = {0x10, 0x11, 0x12}

#: Second byte after a 0x00/0xE0 prefix from msvcrt.getwch() -> label.
#: These are scancodes, not VK codes (0x47='G' as VK, 'Home' as scancode).
EXTENDED_CODES = {
    0x3B: "F1", 0x3C: "F2", 0x3D: "F3", 0x3E: "F4", 0x3F: "F5",
    0x40: "F6", 0x41: "F7", 0x42: "F8", 0x43: "F9", 0x44: "F10",
    0x47: "Home", 0x48: "Up", 0x49: "PgUp",
    0x4B: "Left", 0x4D: "Right",
    0x4F: "End", 0x50: "Down", 0x51: "PgDn",
    0x52: "Ins", 0x53: "Del",
    0x85: "F11", 0x86: "F12",
}


def decode_keypress(
    first: str,
    second: Optional[str],
    held_modifiers: set[str],
) -> Optional[str]:
    """Translate one console key event into a layout label (or None).

    ``first``/``second`` are the characters from ``msvcrt.getwch()``: extended
    keys arrive as a ``\\x00`` or ``\\xe0`` prefix followed by a scancode.
    ``held_modifiers`` is the set of modifier labels currently held down, used
    to disambiguate the L/R modifier virtual keys.
    """
    if second is not None:
        return EXTENDED_CODES.get(ord(second))

    code = ord(first)
    if 0x21 <= code <= 0x7E:  # printable symbols/letters/digits first: codes
        # in this range collide with VK numbers (e.g. '[' == VK_LWIN 0x5B)
        if first.isascii() and first.isalpha():
            return first.upper()
        return first
    if code in VK_CHARS:
        return VK_CHARS[code]
    if code in VK_MODIFIERS:
        if len(held_modifiers) == 1:
            return next(iter(held_modifiers))
        return None  # ambiguous or too fast; the probe loop retries
    return None


def held_modifiers() -> set[str]:
    """Modifier labels currently held down (focus-independent)."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user32.GetAsyncKeyState.restype = ctypes.c_short
    out: set[str] = set()
    for vk, label in (
        (0xA0, "LShift"), (0xA1, "RShift"),
        (0xA2, "LCtrl"), (0xA3, "RCtrl"),
        (0xA4, "LAlt"), (0xA5, "RAlt"),
        (0x5B, "Win"), (0x5C, "Win"),
    ):
        if user32.GetAsyncKeyState(vk) & 0x8000:
            out.add(label)
    return out


def _probe(kb: h.AjazzKeyboard, index: int, colour: h.RGB, delay: float) -> None:
    """Light one slot and black out the rest."""
    kb.send_keymap(h.build_key_table({index: colour}), delay=delay)


def _banner(index: int, mapping: dict[int, str]) -> None:
    label = mapping.get(index)
    state = f"label: {label}" if label else "unmapped"
    print()
    print("-" * 64)
    print(f"  LED index {index:3d}  of 0..{h.KEY_COUNT - 1}    [{state}]")
    print(f"  progress: {len(mapping)}/{h.KEY_COUNT} mapped")
    print("-" * 64)


def _read_keypress(timeout: float) -> tuple[Optional[str], bool]:
    """Wait up to ``timeout`` seconds for a keypress.

    Returns ``(label_or_None, aborted)``.  ``None`` with ``aborted=False``
    means the press window expired (non-key LED slot).
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        held = held_modifiers()
        # Modifiers are read focus-free; printable keys need console focus.
        if HAS_MSVCRT and msvcrt.kbhit():
            first = msvcrt.getwch()
            second = msvcrt.getwch() if first in ("\x00", "\xe0") else None
            label = decode_keypress(first, second, held)
            if label is not None:
                return label, False
            continue
        if held:
            return sorted(held)[0], False
        time.sleep(0.02)
    return None, False


def _finish(kb: h.AjazzKeyboard, args: argparse.Namespace) -> None:
    """Leave the keyboard in a predictable state (mirrors matrix_mapper)."""
    if args.final == "keep":
        return
    if args.final == "fill":
        kb.send_keymap(h.pack_key_table([(255, 255, 255)] * h.KEY_COUNT),
                       delay=args.delay)
        print("Keyboard left in Custom Mode with all keys lit white.")
    elif args.final == "off":
        kb.send_keymap(h.pack_key_table([(0, 0, 0)] * h.KEY_COUNT), delay=args.delay)
        print("Keyboard left in Custom Mode with all keys off.")
    elif args.final == "static":
        kb.set_lighting(h.Mode.STATIC, 4, 4, h.Color.WHITE)
        time.sleep(0.1)
        print("Keyboard returned to Static White (level 4).")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Semi-automatic LED index mapper for the AJAZZ AK820 Max Plus",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--start", type=int, default=None,
                        help="first index to probe (default: first unmapped slot)")
    parser.add_argument("--output", default=h.KEYMAP_MATRIX_FILE,
                        help=f"mapping file (default: {h.KEYMAP_MATRIX_FILE})")
    parser.add_argument("--color", choices=sorted(PROBE_COLOURS), default="white",
                        help="probe colour (default: white)")
    parser.add_argument("--timeout", type=float, default=2.5,
                        help="seconds to wait per slot before marking LED<i> (default: 2.5)")
    parser.add_argument("--skip", default="",
                        help="comma-separated indices to record as LED<i> without probing")
    parser.add_argument("--brightness", type=int, default=h.LEVEL_MAX,
                        choices=range(h.LEVEL_MIN, h.LEVEL_MAX + 1))
    parser.add_argument("--delay", type=float, default=h.KEYMAP_WRITE_DELAY,
                        help="seconds between reports (default: 0.001)")
    parser.add_argument("--final", choices=("fill", "off", "static", "keep"),
                        default="static", help="keyboard state on exit (default: static)")
    parser.add_argument("--no-enter-custom", dest="enter_custom",
                        action="store_false",
                        help="do not send the 0x07 Custom Mode frame first")
    parser.add_argument("--no-layout-refresh", dest="refresh_layout",
                        action="store_false",
                        help="skip the layout_75pct.py positions refresh on exit")
    parser.add_argument("--path", help="explicit HID interface path")
    parser.add_argument("--list", action="store_true",
                        help="print the saved mapping and exit")
    args = parser.parse_args(argv)

    try:
        mapping = h.load_keymap_matrix(args.output)
    except h.AjazzError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.list:
        for index in sorted(mapping):
            print(f"  {index:3d}  {mapping[index]}")
        unmapped = [i for i in range(h.KEY_COUNT) if i not in mapping]
        print(f"{len(mapping)}/{h.KEY_COUNT} mapped; unmapped: "
              f"{', '.join(map(str, unmapped)) if unmapped else 'none'}")
        return 0

    skip = set()
    if args.skip:
        try:
            skip = {int(part) for part in args.skip.split(",") if part}
        except ValueError:
            print("error: --skip takes comma-separated integers", file=sys.stderr)
            return 2

    start = args.start
    if start is None:
        start = next((i for i in range(h.KEY_COUNT) if i not in mapping), h.KEY_COUNT)
    if not 0 <= start <= h.KEY_COUNT:
        print(f"error: --start must be 0..{h.KEY_COUNT}", file=sys.stderr)
        return 2
    if start == h.KEY_COUNT:
        print(f"All {h.KEY_COUNT} slots are already mapped in {args.output}.")
        return 0

    print(__doc__)
    print(f"Starting at index {start}. Press the LIT key; wait to skip "
          f"non-key slots. Ctrl+C saves and exits.")

    colour = PROBE_COLOURS[args.color]
    saved = 0
    try:
        with h.open_device(path=args.path) as kb:
            if args.enter_custom:
                kb.enter_custom_mode(brightness=args.brightness)
            print(f"Device: {kb.info.path}")

            for index in range(start, h.KEY_COUNT):
                if index in mapping:
                    continue
                if index in skip:
                    mapping[index] = f"LED{index}"
                    h.save_keymap_matrix(mapping, args.output)
                    print(f"  index {index}: skipped -> LED{index}")
                    continue

                _probe(kb, index, colour, args.delay)
                _banner(index, mapping)

                label, _aborted = _read_keypress(args.timeout)
                if label is None:
                    label = f"LED{index}"
                    print(f"  (no keypress) -> recorded as LED{index}")
                else:
                    print(f"  detected key: {label!r}")

                clash = [
                    other for other, value in mapping.items()
                    if other != index and value.casefold() == label.casefold()
                ]
                if clash:
                    print(f"  ! {label!r} is already used by index {clash[0]}; "
                          f"recording {index} as UNCHECKED_{label} - fix it with "
                          "'python matrix_mapper.py --start "
                          f"{min(index, clash[0])}'")
                    label = f"UNCHECKED_{label}"

                mapping[index] = label
                h.save_keymap_matrix(mapping, args.output)
                saved += 1
                if index == h.KEY_COUNT - 1:
                    print("\nReached the end of the table.")

            _finish(kb, args)

    except h.AjazzError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted - progress saved.")

    print(f"\nWrote {saved} new entr{'y' if saved == 1 else 'ies'}; "
          f"{len(mapping)}/{h.KEY_COUNT} slots now mapped to {args.output}.")

    if args.refresh_layout:
        try:
            import layout_75pct
            positions = layout_75pct.populate_keymap_json()
            print(f"layout positions refreshed: {len(positions)} keys placed "
                  "on the 75% grid")
        except Exception as exc:  # layout refresh is best-effort
            print(f"warning: layout refresh failed: {exc}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
