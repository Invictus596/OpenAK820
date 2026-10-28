#!/usr/bin/env python3
"""matrix_mapper.py -- interactive LED index -> key-label mapper.

The keyboard's per-key table is indexed in **LED chain order**, which does not
match the visual layout (Esc is index 15, F1 is index 14).  This tool walks the
118 slots one at a time, lights exactly one key, and asks you to type its label.

Usage
-----
    python matrix_mapper.py
    python matrix_mapper.py --start 40
    python matrix_mapper.py --color red --final static
    python matrix_mapper.py --list
    python matrix_mapper.py --hold 12 --hold-seconds 5   # probe-stability check

Progress is written to ``keymap_matrix.json`` after every entry, so you can stop
with ``q`` (or Ctrl+C) and resume later -- the default start index is the first
slot that has no label yet.

The mapper holds a single HID handle for the whole session, refuses to start
while an animation daemon owns the board, re-asserts Custom Mode before every
probe, and emits a ``0x0E`` keep-alive while it waits for input.  Without those
last two, the firmware drops out of Custom Mode mid-session and each probe only
flashes for a frame before the board reverts to its stored profile.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from typing import Optional

import ajazz_hid as h
import mapper_common

PROBE_COLOURS = {
    "white": (255, 255, 255),
    "red": (255, 0, 0),
    "green": (0, 255, 0),
    "blue": (0, 0, 255),
    "yellow": (255, 255, 0),
}

HELP = """\
  Commands
    <label>   assign this key name to the lit slot, then advance
    Enter     advance, keeping any existing label
    n         advance to the next index
    p         go back to the previous index
    s         clear this slot and advance (use for unused slots)
    /<label>  force a label that looks like a command (e.g. /n, /q)
    ?         show this help again
    q         save and quit

  Single-letter commands are lowercase only.  To label the N, P, S or Q key,
  type it in UPPERCASE (the label is stored exactly as typed).
"""

INTRO = """\
AJAZZ AK820 Max Plus - LED matrix mapper
========================================
Exactly one key is lit at a time. Type the label of the key you see.

Typical labels: Esc, F1, Tab, Caps, LShift, Space, Up, Down, Left, Right,
                Enter, RAlt, RCtrl, Menu, M1, Knob
""" + HELP


class _Quit(Exception):
    """Raised internally when the user asks to stop."""


def _banner(index: int, mapping: dict[int, str]) -> None:
    label = mapping.get(index)
    state = f"label: {label}" if label else "unmapped"
    mapped = len(mapping)
    print()
    print("-" * 64)
    print(f"  LED index {index:3d}  of 0..{h.KEY_COUNT - 1}    [{state}]")
    print(f"  progress: {mapped}/{h.KEY_COUNT} mapped")
    print("-" * 64)


def _prompt(index: int, mapping: dict[int, str]) -> tuple[str, Optional[str]]:
    """Ask what to do with ``index``.

    Returns ``(action, value)`` where action is one of
    ``label`` / ``next`` / ``prev`` / ``skip`` / ``quit``.
    """
    while True:
        try:
            raw = input("  key name > ").strip()
        except EOFError:
            print()
            return ("quit", None)

        if not raw:
            return ("next", None)

        if raw == "?":
            print(HELP)
            continue

        # A leading slash escapes labels that collide with commands (/n -> "n").
        escaped = raw.startswith("/")
        label = raw[1:].strip() if escaped else raw

        if not escaped and len(raw) == 1:
            command = {"n": "next", "p": "prev", "s": "skip", "q": "quit"}.get(raw)
            if command is not None:
                return (command, None)

        if not label:
            continue

        clash = [
            other
            for other, value in mapping.items()
            if other != index and value.casefold() == label.casefold()
        ]
        if clash:
            print(
                f"  ! '{label}' is already used by index {clash[0]}. "
                "Enter a different label, or press s on that slot first."
            )
            continue

        return ("label", label)


def _probe(
    kb: h.AjazzKeyboard,
    index: int,
    colour: h.RGB,
    delay: float,
    *,
    lock: threading.Lock,
    brightness: int,
    enter_custom: bool,
) -> None:
    """Light one slot and black out the rest.

    Delegates to :func:`mapper_common.probe` so the measured Custom Mode entry
    sequence is re-sent for every slot.  Entering Custom Mode only once per
    session is what made probes flash and revert after the idle window expired.
    """
    mapper_common.probe(
        kb,
        h.build_key_table({index: colour}),
        lock,
        brightness=brightness,
        delay=delay,
        enter_custom=enter_custom,
    )


def _finish(
    kb: h.AjazzKeyboard,
    args: argparse.Namespace,
    lock: Optional[threading.Lock] = None,
) -> None:
    """Leave the keyboard in a predictable state.

    ``fill`` uses the built-in Static White *effect* rather than an all-white
    per-key table: the firmware rejects full-board high-intensity tables (all
    red and all green both revert, measured 2026-10-28), so a table-based fill
    is not a reliable way to light the whole board.  ``off`` stays
    table-based because an all-zero table is accepted.
    """
    if args.final == "keep":
        return
    if args.final == "fill":
        # All keys white via the device's own Static mode -- no 354-byte table.
        kb.set_lighting(
            h.Mode.STATIC, args.brightness, mapper_common.RESTORE_SPEED,
            h.Color.WHITE,
        )
        time.sleep(mapper_common.RESTORE_HOLD)
        print(f"Keyboard left on Static White (level {args.brightness}).")
    elif args.final == "off":
        table = h.pack_key_table([(0, 0, 0)] * h.KEY_COUNT)
        mapper_common.probe(
            kb, table, lock or threading.Lock(), brightness=args.brightness,
            delay=args.delay, enter_custom=args.enter_custom,
        )
        print("Keyboard left in Custom Mode with all keys off.")
    elif args.final == "static":
        # Fixed level 4, matching animation_engine.RESTORE_BRIGHTNESS (P4-T1),
        # plus the 100 ms hold so the mode change survives the handle close.
        mapper_common.restore_static_white(kb, lock)
        print(
            "Keyboard returned to Static White "
            f"(level {mapper_common.RESTORE_BRIGHTNESS})."
        )


def _print_mapping(mapping: dict[int, str], output: str) -> None:
    print(f"{output}: {len(mapping)}/{h.KEY_COUNT} slots mapped")
    for index in sorted(mapping):
        print(f"  {index:3d}  {mapping[index]}")
    missing = [i for i in range(h.KEY_COUNT) if i not in mapping]
    if missing:
        if len(missing) <= 24:
            print("  unmapped:", ", ".join(str(i) for i in missing))
        else:
            print(f"  unmapped: {len(missing)} slots "
                  f"({missing[0]}..{missing[-1]})")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Map AJAZZ AK820 Max Plus LED indices to key labels",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--start", type=int, default=None,
                        help="first index to probe (default: first unmapped slot)")
    parser.add_argument("--output", default=h.KEYMAP_MATRIX_FILE,
                        help=f"mapping file (default: {h.KEYMAP_MATRIX_FILE})")
    parser.add_argument("--color", choices=sorted(PROBE_COLOURS), default="white",
                        help="probe colour (default: white)")
    parser.add_argument("--brightness", type=int, default=h.LEVEL_MAX,
                        choices=range(h.LEVEL_MIN, h.LEVEL_MAX + 1),
                        help="brightness 1..5 while mapping (default: 5)")
    parser.add_argument("--delay", type=float, default=h.KEYMAP_WRITE_DELAY,
                        help="seconds between reports (default: 0.001)")
    parser.add_argument("--final", choices=("fill", "off", "static", "keep"),
                        default="static",
                        help="keyboard state on exit (default: static)")
    parser.add_argument("--no-enter-custom", dest="enter_custom",
                        action="store_false",
                        help="do not send the 0x07 Custom Mode frame first")
    parser.add_argument("--path", help="explicit HID interface path")
    parser.add_argument("--list", action="store_true",
                        help="print the saved mapping and exit")
    parser.add_argument("--hold", type=int, default=None, metavar="INDEX",
                        help="diagnostic: light INDEX and hold it for "
                             "--hold-seconds, then exit (no prompts). Use this "
                             "to confirm a probe stays lit instead of flashing.")
    parser.add_argument("--hold-seconds", type=float, default=5.0,
                        help="how long --hold keeps the probe lit (default: 5)")
    parser.add_argument("--force", action="store_true",
                        help="start even if the animation daemon cannot be stopped")
    args = parser.parse_args(argv)

    try:
        mapping = h.load_keymap_matrix(args.output)
    except h.AjazzError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.list:
        _print_mapping(mapping, args.output)
        return 0

    start = args.start
    if start is None:
        start = next((i for i in range(h.KEY_COUNT) if i not in mapping), h.KEY_COUNT)
    if not 0 <= start <= h.KEY_COUNT:
        print(f"error: --start must be 0..{h.KEY_COUNT}", file=sys.stderr)
        return 2
    if start == h.KEY_COUNT:
        print(f"All {h.KEY_COUNT} slots are already mapped in {args.output}.")
        print("Re-run with --start 0 to review or change them.")
        return 0

    print(INTRO)
    print(f"Starting at index {start}. Resume point is the first unmapped slot.")

    if args.hold is not None and not 0 <= args.hold < h.KEY_COUNT:
        print(f"error: --hold must be 0..{h.KEY_COUNT - 1}", file=sys.stderr)
        return 2

    colour = PROBE_COLOURS[args.color]
    index = start
    quit_requested = False
    lock = threading.Lock()

    try:
        mapper_common.preflight(stop_running=not args.force)
    except mapper_common.MapperPreflightError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3

    try:
        # ONE handle for the whole session: no per-probe open/close, so no
        # teardown frame can be emitted between probes.
        with h.open_device(path=args.path) as kb:
            print(f"Device: {kb.info.path}")

            with mapper_common.Keepalive(kb, lock):
                if args.hold is not None:
                    # Diagnostic path: same write sequence as an interactive
                    # probe, but held so the user can watch for a revert.
                    _probe(kb, args.hold, colour, args.delay, lock=lock,
                           brightness=args.brightness,
                           enter_custom=args.enter_custom)
                    _banner(args.hold, mapping)
                    print(f"  holding probe for {args.hold_seconds:g}s -- "
                          "it must stay lit the whole time")
                    time.sleep(max(0.0, args.hold_seconds))
                    mapper_common.restore_static_white(kb, lock)
                    print("  probe finished; board returned to Static White "
                          f"(level {mapper_common.RESTORE_BRIGHTNESS})")
                    return 0

                if args.enter_custom:
                    kb.enter_custom_mode(brightness=args.brightness)

                while 0 <= index < h.KEY_COUNT:
                    _probe(kb, index, colour, args.delay, lock=lock,
                           brightness=args.brightness,
                           enter_custom=args.enter_custom)
                    _banner(index, mapping)

                    action, value = _prompt(index, mapping)

                    if action == "quit":
                        quit_requested = True
                        break
                    if action == "label":
                        assert value is not None
                        mapping[index] = value
                        h.save_keymap_matrix(mapping, args.output)
                        print(f"  saved: index {index} = {value!r}")
                        index += 1
                    elif action == "skip":
                        if mapping.pop(index, None) is not None:
                            h.save_keymap_matrix(mapping, args.output)
                            print(f"  cleared index {index}")
                        index += 1
                    elif action == "prev":
                        index = max(0, index - 1)
                    else:  # next
                        index += 1

                if not quit_requested and index >= h.KEY_COUNT:
                    print("\nReached the end of the table.")

                _finish(kb, args, lock)

    except h.AjazzError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted.")

    saved = h.save_keymap_matrix(mapping, args.output)
    print(f"\nWrote {len(mapping)}/{h.KEY_COUNT} entries to {saved}")
    if len(mapping) < h.KEY_COUNT:
        print("Resume any time; the mapper starts at the first unmapped slot.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
