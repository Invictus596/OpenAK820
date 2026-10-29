"""cli.py -- command-line interface for the AJAZZ AK820 Max Plus.

Examples
--------
    python cli.py list
    python cli.py static-cyan
    python cli.py static-cyan --brightness 3
    python cli.py set --mode static --color cyan --brightness 5 --speed 4
    python cli.py set --color red --dry-run
    python cli.py custom-test
    python cli.py custom-test --gradient

``static-cyan`` is the Phase 1 acceptance test.  ``custom-test`` exercises the
Phase 2 per-key table: it pushes a demo pattern through the six ``0xF6``..``0xFB``
reports and commits with ``0x0A``.
"""

from __future__ import annotations

import argparse
import colorsys
import re
import sys

from ajazz_hid import (
    KEY_COUNT,
    KEYMAP_MATRIX_FILE,
    KEYMAP_WRITE_DELAY,
    LEVEL_MAX,
    LEVEL_MIN,
    PRODUCT_ID,
    VENDOR_ID,
    AjazzError,
    Color,
    Mode,
    build_commit_packet,
    build_key_table,
    build_keymap_chunks,
    build_lighting_packet,
    enumerate_devices,
    format_packet,
    load_keymap_matrix,
    open_device,
    pack_key_table,
    pick_device,
    static_cyan_packet,
)
from animation_engine import (
    DEFAULT_FPS,
    EFFECTS,
    LOG_FILE,
    MAX_FPS,
    MIN_FPS,
    AnimationEngine,
    create_effect,
    daemon_status,
    install_stop_handlers,
    start_daemon,
    stop_daemon,
)

EFFECT_DESCRIPTIONS = {
    "rainbow": "smooth rainbow wave travelling along the LED chain (default)",
    "cpu": "system CPU utilisation as a green->red bar",
    "audio": "spectrum visualiser; synthetic source unless --audio-source microphone",
}


def _resolve_enum(enum_cls, value: str):
    """Accept either a numeric value or a case-insensitive member name."""
    try:
        return enum_cls(int(value, 0))
    except ValueError:
        pass
    key = value.strip().upper().replace(" ", "_").replace("-", "_")
    try:
        return enum_cls[key]
    except KeyError:
        names = ", ".join(m.name.lower() for m in enum_cls)
        raise SystemExit(f"unknown {enum_cls.__name__} {value!r}; choose from: {names}")


def _print_devices() -> int:
    devices = enumerate_devices()
    print(f"Searching for VID_{VENDOR_ID:04X} PID_{PRODUCT_ID:04X} (AK820)...\n")
    if not devices:
        print("No matching HID interfaces found.")
        return 1

    chosen = pick_device(devices)
    for i, d in enumerate(devices):
        if d.path == chosen.path:
            marker = "  <-- SELECTED (65-byte output reports)"
        elif d.is_vendor_interface:
            marker = "  (vendor page, not writable)"
        else:
            marker = ""
        print(f"[{i}] {d.describe()}{marker}")
    print(f"\n{len(devices)} interface(s) found.")
    return 0


def _send(packet: bytes, path: str | None, dry_run: bool) -> int:
    print("packet:", format_packet(packet))

    if dry_run:
        print("(dry run -- nothing written)")
        return 0

    with open_device(path=path) as kb:
        print(f"writing to: {kb.info.path}")
        written = kb.send(packet)

    if written != len(packet):
        print(f"WARNING: expected {len(packet)} bytes written, got {written}", file=sys.stderr)
        return 1

    print(f"OK: {written} bytes written.")
    return 0


# --------------------------------------------------------------------------
# custom-test: demo pattern for the chunked per-key table
# --------------------------------------------------------------------------

RED = (255, 0, 0)
BLUE = (0, 0, 255)

#: Requested demo: WASD red, arrow keys blue, everything else off.
DEMO_KEYS = [
    ("W", RED), ("A", RED), ("S", RED), ("D", RED),
    ("Up", BLUE), ("Down", BLUE), ("Left", BLUE), ("Right", BLUE),
]

#: Key labels in the map are free-form, so match them loosely.
KEY_ALIASES = {
    "escape": "esc",
    "return": "enter",
    "spacebar": "space",
    "arrowup": "up", "uparrow": "up",
    "arrowdown": "down", "downarrow": "down",
    "arrowleft": "left", "leftarrow": "left",
    "arrowright": "right", "rightarrow": "right",
}


def _norm_label(name: str) -> str:
    """Casefold and drop separators: 'Arrow Up' -> 'arrowup'."""
    return re.sub(r"[\s_\-]+", "", name).casefold()


def _build_name_lookup(mapping: dict[int, str]) -> dict[str, int]:
    """Index labels by normalised name, registering aliases in both directions."""
    lookup: dict[str, int] = {}
    for index, label in mapping.items():
        key = _norm_label(label)
        lookup.setdefault(key, index)
        alias = KEY_ALIASES.get(key)
        if alias is not None:
            lookup.setdefault(alias, index)
    return lookup


def _lookup_index(lookup: dict[str, int], name: str) -> int | None:
    key = _norm_label(name)
    if key in lookup:
        return lookup[key]
    alias = KEY_ALIASES.get(key)
    if alias is not None:
        return lookup.get(alias)
    return None


def _gradient_table() -> bytes:
    """Rainbow across all 118 slots -- proves every chunk lands correctly."""
    keys = []
    for i in range(KEY_COUNT):
        r, g, b = colorsys.hsv_to_rgb(i / KEY_COUNT, 1.0, 1.0)
        keys.append((round(r * 255), round(g * 255), round(b * 255)))
    return pack_key_table(keys)


def _custom_test(args: argparse.Namespace) -> int:
    table: bytes
    description: str

    if args.gradient:
        table = _gradient_table()
        description = "full-board rainbow gradient across indices 0..117"
    else:
        try:
            mapping = load_keymap_matrix(args.keymap)
        except AjazzError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

        lookup = _build_name_lookup(mapping)
        colours: dict[int, tuple[int, int, int]] = {}
        resolved: list[str] = []
        missing: list[str] = []

        for name, rgb in DEMO_KEYS:
            index = _lookup_index(lookup, name)
            if index is None:
                missing.append(name)
            else:
                colours[index] = rgb
                resolved.append(f"{name}={index}")

        if not colours:
            print(f"No demo keys found in {args.keymap}.")
            print("The index table is LED-chain order, so the pattern needs a map.")
            print("Run `python matrix_mapper.py` first, or use --gradient to")
            print("verify the six-report chunking without a map.\n")
            print("Falling back to --gradient.")
            table = _gradient_table()
            description = "full-board rainbow gradient (no keymap available)"
        else:
            table = build_key_table(colours)
            description = (
                "WASD red, arrows blue, rest off"
                if not missing
                else f"partial demo (unmapped: {', '.join(missing)})"
            )
            print(f"resolved: {', '.join(resolved)}")
        if missing and colours:
            print(f"not in {args.keymap}: {', '.join(missing)}")

    print(f"pattern: {description}")
    print(f"table:   {len(table)} bytes = {KEY_COUNT} keys x 3 channels")

    if args.dry_run:
        chunks = build_keymap_chunks(table)
        for i, packet in enumerate(chunks):
            slot = packet[5]
            first = packet[6:12]
            print(f"  chunk {i}: opcode 0x{slot:02X}  data[0:6]={format_packet(first)}")
        print(f"  commit:  opcode 0x{build_commit_packet()[5]:02X}")
        print("(dry run -- nothing written)")
        return 0

    with open_device(path=args.path) as kb:
        print(f"writing to: {kb.info.path}")
        if args.enter_custom:
            kb.enter_custom_mode(brightness=args.brightness)
            print("sent 0x07 Custom Mode frame")
        written = kb.send_keymap(table, delay=args.delay)

    expected = 7 * 65
    if written != expected:
        print(f"WARNING: expected {expected} bytes written, got {written}", file=sys.stderr)
        return 1

    print(f"OK: 6 chunks + commit = {written} bytes written.")
    return 0


# --------------------------------------------------------------------------
# Real-time effects and the animation daemon
# --------------------------------------------------------------------------


def _list_effects() -> int:
    print("Available effects:\n")
    for name in EFFECTS:
        print(f"  {name:8s} {EFFECT_DESCRIPTIONS.get(name, '')}")
    print(
        "\nRun one in the foreground:\n"
        "  python cli.py effect rainbow --fps 30\n"
        "  python cli.py effect cpu\n"
        "  python cli.py effect audio --audio-source synthetic\n"
        "\nSpatial effects (use physical key positions):\n"
        "  python cli.py effect rainbow --spatial\n"
        "  python cli.py effect rainbow --spatial --diagonal\n"
        "  python cli.py effect cpu --spatial\n"
        "\nRun one in the background:\n"
        "  python cli.py start rainbow\n"
        "  python cli.py status\n"
        "  python cli.py stop"
    )
    return 0


def _run_effect(args: argparse.Namespace) -> int:
    if not args.name:
        return _list_effects()

    if args.name not in EFFECTS:
        print(f"unknown effect {args.name!r}; choose from: {', '.join(EFFECTS)}", file=sys.stderr)
        return 2

    spatial = getattr(args, "spatial", False)
    diagonal = getattr(args, "diagonal", False)
    audio_source_name = getattr(args, "audio_source", "synthetic")
    if getattr(args, "real_audio", False):
        audio_source_name = "wasapi"

    effect = create_effect(
        args.name,
        audio_source=audio_source_name,
        spatial=spatial,
        diagonal=diagonal,
        fps=args.fps if args.name == "snake" else DEFAULT_FPS,
    )

    if args.dry_run:
        table = effect.frame(0.0)
        for i, packet in enumerate(build_keymap_chunks(table)):
            print(f"  chunk {i}: opcode 0x{packet[5]:02X}  data[0:6]={format_packet(packet[6:12])}")
        print(f"  commit:  opcode 0x{build_commit_packet()[5]:02X}")
        print("(dry run -- nothing written)")
        effect.close()
        return 0

    if spatial and diagonal and args.name != "rainbow":
        print("note: --diagonal only applies to the rainbow effect; ignoring it",
              file=sys.stderr)

    engine = AnimationEngine(
        effect,
        fps=args.fps,
        brightness=args.brightness,
        device_path=args.path,
        final=args.final,
    )
    install_stop_handlers(engine)

    print(f"effect: {args.name}  fps: {args.fps}  (Ctrl+C to stop)")
    if spatial:
        print(f"  spatial mode: {'diagonal (x+y)' if diagonal else 'column (x)'}")
    try:
        engine.run(duration=args.duration)
    except KeyboardInterrupt:
        pass
    finally:
        effect.close()

    print(f"stopped after {engine.frames} frames ({engine.achieved_fps:.1f} fps)")
    return 0


def _start_daemon(args: argparse.Namespace) -> int:
    if args.name not in EFFECTS:
        print(f"unknown effect {args.name!r}; choose from: {', '.join(EFFECTS)}", file=sys.stderr)
        return 2

    if args.diagonal and args.name != "rainbow":
        print("note: --diagonal only applies to the rainbow effect; ignoring it",
              file=sys.stderr)

    audio_source = "wasapi" if getattr(args, "real_audio", False) else args.audio_source

    pid = start_daemon(
        args.name,
        fps=args.fps,
        brightness=args.brightness,
        audio_source=audio_source,
        spatial=args.spatial,
        diagonal=args.diagonal,
    )
    print(f"animation daemon running (pid {pid})")
    print(f"log: {LOG_FILE}")
    print("stop with: python cli.py stop")
    return 0


def _stop_daemon() -> int:
    stop_daemon()
    return 0


def _daemon_status() -> int:
    print(daemon_status())
    return 0


def _handle_profile(args: argparse.Namespace) -> int:
    from profile_manager import ProfileManager, LightingProfile
    mgr = ProfileManager()

    action = getattr(args, "profile_action", "list")
    if action == "list" or not action:
        profiles = mgr.list_profiles()
        print(f"Available profiles ({len(profiles)}):\n")
        for p in profiles:
            kind_info = f"kind={p.kind}"
            if p.kind == "effect":
                kind_info += f", effect={p.effect_name}"
            elif p.kind == "custom":
                kind_info += f", keys={len(p.custom_keys)}"
            else:
                kind_info += f", mode={p.mode}, color={p.color}"
            print(f"  * {p.name:<24} [{kind_info}]")
            if p.description:
                print(f"      {p.description}")
        return 0

    if action == "save":
        name = args.profile_name
        if not name:
            print("error: profile name required for save", file=sys.stderr)
            return 2
        # Save a new profile using command-line arguments or defaults
        prof = LightingProfile(
            name=name,
            description=getattr(args, "desc", "") or f"User saved profile '{name}'",
            kind=getattr(args, "kind", "built-in"),
            mode=getattr(args, "mode", "static"),
            color=getattr(args, "color", "white"),
            brightness=getattr(args, "brightness", LEVEL_MAX),
            speed=getattr(args, "speed", 4),
            effect_name=getattr(args, "effect", None),
            spatial=getattr(args, "spatial", False),
        )
        mgr.save_profile(prof)
        print(f"Saved profile '{name}' to {mgr.path}")
        return 0

    if action == "load":
        name = args.profile_name
        if not name:
            print("error: profile name required for load", file=sys.stderr)
            return 2
        prof = mgr.get(name)
        if not prof:
            print(f"error: profile '{name}' not found", file=sys.stderr)
            return 2
        print(f"Applying profile: {prof.name} ({prof.kind})")
        from tray_service import TrayService
        srv = TrayService()
        srv.apply_profile(prof)
        print(f"Profile '{prof.name}' applied successfully.")
        return 0

    if action == "delete":
        name = args.profile_name
        if not name:
            print("error: profile name required for delete", file=sys.stderr)
            return 2
        if mgr.delete_profile(name):
            print(f"Deleted profile '{name}'")
            return 0
        else:
            print(f"Profile '{name}' not found", file=sys.stderr)
            return 2

    return 0


def _run_tray() -> int:
    import tray_service
    return tray_service.main()


def main(argv: list[str] | None = None) -> int:
    # Shared options so they are accepted after the subcommand, e.g.
    #   python cli.py static-cyan --dry-run
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--path", help="explicit HID interface path (overrides discovery)")
    common.add_argument("--dry-run", action="store_true", help="print the packet without writing")

    parser = argparse.ArgumentParser(
        description="AJAZZ AK820 Max Plus lighting CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", parents=[common], help="list matching HID interfaces")

    p_cyan = sub.add_parser(
        "static-cyan", parents=[common],
        help="set Static constant light + cyan (Phase 1 test)",
    )
    p_cyan.add_argument("--brightness", type=int, default=LEVEL_MAX, choices=range(LEVEL_MIN, LEVEL_MAX + 1))
    p_cyan.add_argument("--speed", type=int, default=4, choices=range(LEVEL_MIN, LEVEL_MAX + 1))

    p_set = sub.add_parser("set", parents=[common], help="set an arbitrary lighting state")
    p_set.add_argument("--mode", default="static")
    p_set.add_argument("--color", default="cyan")
    p_set.add_argument("--brightness", type=int, default=LEVEL_MAX, choices=range(LEVEL_MIN, LEVEL_MAX + 1))
    p_set.add_argument("--speed", type=int, default=4, choices=range(LEVEL_MIN, LEVEL_MAX + 1))
    p_set.add_argument("--variant", type=lambda v: int(v, 0), default=None,
                       help="override the mode-specific byte [9] (auto by default)")

    p_custom = sub.add_parser(
        "custom-test", parents=[common],
        help="push a per-key demo pattern through 0xF6..0xFB + 0x0A",
    )
    p_custom.add_argument("--gradient", action="store_true",
                          help="rainbow over all 118 slots instead of the WASD demo")
    p_custom.add_argument("--keymap", default=KEYMAP_MATRIX_FILE,
                          help=f"index map produced by matrix_mapper.py (default: {KEYMAP_MATRIX_FILE})")
    p_custom.add_argument("--brightness", type=int, default=LEVEL_MAX,
                          choices=range(LEVEL_MIN, LEVEL_MAX + 1))
    p_custom.add_argument("--delay", type=float, default=KEYMAP_WRITE_DELAY,
                          help="seconds between reports (default: 0.001)")
    p_custom.add_argument("--no-enter-custom", dest="enter_custom", action="store_false",
                          help="do not send the 0x07 Custom Mode frame first")

    p_effect = sub.add_parser(
        "effect", parents=[common],
        help="run a real-time effect in the foreground (no name: list effects)",
    )
    p_effect.add_argument("name", nargs="?", default=None, metavar="NAME")
    p_effect.add_argument("--fps", type=int, default=DEFAULT_FPS,
                          choices=range(MIN_FPS, MAX_FPS + 1),
                          help=f"target frame rate (default: {DEFAULT_FPS})")
    p_effect.add_argument("--brightness", type=int, default=LEVEL_MAX,
                          choices=range(LEVEL_MIN, LEVEL_MAX + 1))
    p_effect.add_argument("--duration", type=float, default=None,
                          help="stop after N seconds (default: until Ctrl+C)")
    p_effect.add_argument("--audio-source", default="synthetic",
                          choices=("synthetic", "microphone"))
    p_effect.add_argument("--final", default="static", choices=("static", "off", "keep"),
                          help="lighting state on exit (default: static)")
    p_effect.add_argument("--spatial", action="store_true",
                          help="use physical key positions for rainbow/cpu/audio effects")
    p_effect.add_argument("--diagonal", action="store_true",
                          help="with --spatial, use x+y diagonal (rainbow only)")
    p_effect.add_argument("--real-audio", action="store_true",
                          help="use real-time Windows audio loopback capture (WASAPI)")

    p_start = sub.add_parser("start", help="start an effect as a background daemon")
    p_start.add_argument("name", choices=EFFECTS, metavar="NAME")
    p_start.add_argument("--fps", type=int, default=DEFAULT_FPS,
                         choices=range(MIN_FPS, MAX_FPS + 1))
    p_start.add_argument("--brightness", type=int, default=LEVEL_MAX,
                         choices=range(LEVEL_MIN, LEVEL_MAX + 1))
    p_start.add_argument("--audio-source", default="synthetic",
                         choices=("synthetic", "microphone"))
    p_start.add_argument("--spatial", action="store_true",
                         help="use physical key positions for the effect")
    p_start.add_argument("--diagonal", action="store_true",
                         help="with --spatial, use x+y diagonal (rainbow only)")
    p_start.add_argument("--real-audio", action="store_true",
                         help="use real-time Windows audio loopback capture (WASAPI)")

    sub.add_parser("stop", help="stop the background animation daemon")
    sub.add_parser("status", help="show animation daemon status")
    sub.add_parser("tray", help="launch the Windows system tray background service")

    p_prof = sub.add_parser("profile", help="manage lighting profiles (list/save/load/delete)")
    p_prof.add_argument("profile_action", nargs="?", default="list", choices=("list", "save", "load", "delete"),
                        metavar="ACTION")
    p_prof.add_argument("profile_name", nargs="?", default=None, metavar="NAME")
    p_prof.add_argument("--kind", default="built-in", choices=("built-in", "custom", "effect"))
    p_prof.add_argument("--mode", default="static")
    p_prof.add_argument("--color", default="white")
    p_prof.add_argument("--effect", default=None)
    p_prof.add_argument("--brightness", type=int, default=LEVEL_MAX)
    p_prof.add_argument("--speed", type=int, default=4)
    p_prof.add_argument("--spatial", action="store_true")
    p_prof.add_argument("--desc", default="")

    args = parser.parse_args(argv)

    try:
        if args.cmd == "list":
            return _print_devices()

        if args.cmd == "tray":
            return _run_tray()

        if args.cmd == "profile":
            return _handle_profile(args)

        if args.cmd == "static-cyan":
            packet = static_cyan_packet(args.brightness, args.speed)
            return _send(packet, args.path, args.dry_run)

        if args.cmd == "custom-test":
            return _custom_test(args)

        if args.cmd == "effect":
            return _run_effect(args)
        if args.cmd == "start":
            return _start_daemon(args)
        if args.cmd == "stop":
            return _stop_daemon()
        if args.cmd == "status":
            return _daemon_status()

        mode = _resolve_enum(Mode, args.mode)
        color = _resolve_enum(Color, args.color)
        packet = build_lighting_packet(
            mode, args.brightness, args.speed, color, args.variant
        )
        return _send(packet, args.path, args.dry_run)

    except AjazzError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
