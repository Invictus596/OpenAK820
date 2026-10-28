# OpenAK820 — Open clients for the AJAZZ AK820 Max Plus keyboard

Replace the vendor `DeviceDriver.exe` with open-source clients for the
AJAZZ AK820 Max Plus (VID `0x1A2C`, PID `0xA036`).

Two targets, same protocol, neither depends on the other:

| Target | Runtime | Purpose |
|--------|---------|---------|
| **CLI / Windows daemon** | Python 3 (dependency-free `ctypes` Win32 HID) | Lighting modes, per-key table upload, spatial effects, background daemon, tray service, audio-reactive visualizer |
| **WebHID browser app** | Chrome / Edge / Brave / Helium (`navigator.hid`) | Zero-install per-key painter, profiles — planned for Phase 5, no Python required |

Protocol facts are **measured from the vendor app**, not assumed — see
[PROTOCOL.md](PROTOCOL.md) for wire format, byte layouts, and evidence marks
(✅ measured / ⚠️ inferred / ❓ open).

---

## What works today

- **Device discovery** — lists matching HID interfaces, selects the writable
  `mi_00` collection (usage page `0xFF02`)
- **`0x07` lighting frames** — Static / Breathing / etc. modes, brightness
  `1..5`, speed, colour palette
- **Custom Mode + per-key table** — measured vendor entry sequence
  (`0x07 mode=0x17`, then `0x0B`, then `0x09`), six `0xF6`–`0xFB` reports
  (354 bytes = 118 keys × RGB), `0x0A` commit — reproduced end-to-end from a
  host program with no vendor code
- **`0x0E` keepalive** — heartbeat matching vendor traffic
- **Teardown** — `0x07` Static White at fixed level 4, ~100 ms hold before the
  handle closes; traps SIGINT/SIGTERM/SIGBREAK; supervisor calls
  `restore_white_reset()` before force-kill
- **Effects** — rainbow chain, CPU bar, audio spectrum (synthetic or WASAPI
  loopback), spatial rainbow / CPU meter / audio visualizer keyed to physical
  key positions (`--spatial`, `--diagonal`)
- **Daemon / tray** — `cli.py start|stop|status|tray`, profiles via
  `cli.py profile list|save|load|delete`
- **Matrix mapper** — `auto_matrix.py` (semi-automatic, press the lit key) and
  `matrix_mapper.py` (interactive label entry); one HID handle per session,
  Custom Mode re-asserted per probe, `0x0E` keepalive while waiting
- **Tests** — 112 tests green: `.venv\Scripts\python.exe -m unittest discover -s tests`

---

## Hardware status

| Item | Status |
|------|--------|
| Device identity | VID `0x1A2C` / PID `0xA036` / product `AK820`, `mi_00`, usage page `0xFF02` |
| Per-key table upload | ✅ measured, end-to-end verified (slot 15 = Esc confirmed green) |
| LED chain → visual map | ⚠️ only **2 of 118** slots mapped (`14:"F1"`, `15:"Esc"`) |
| Spatial effects | run, but light at most 2 keys until the map is complete |

Run the mapper to finish the map (≈2–5 min at the keyboard):

```powershell
.\.venv\Scripts\python.exe auto_matrix.py
```

Press the key that lights up; wait out non-key slots (they become `LED<n>`).
Ctrl+C saves progress; rerunning resumes at the first unmapped slot.

---

## Installation

```powershell
# clone
git clone https://github.com/Invictus596/OpenAK820.git
cd OpenAK820

# create a venv (Python 3.11+)
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# no third-party runtime deps for the CLI/daemon (ctypes Win32 backend)
# Frida is only needed for reverse-engineering captures
```

Close `DeviceDriver.exe` before writing to the board — two writers on the same
handle will conflict.

---

## CLI quick start

List interfaces:

```powershell
python cli.py list
```

Static cyan at level 4:

```powershell
python cli.py static-cyan --brightness 4
```

Arbitrary lighting state (dry-run prints the packet without writing):

```powershell
python cli.py set --mode static --color red --brightness 3 --speed 4 --dry-run
python cli.py set --mode static --color red --brightness 3 --speed 4
```

Push a per-key demo pattern (Custom Mode entry + 6 chunks + commit):

```powershell
python cli.py custom-test              # WASD demo pattern
python cli.py custom-test --gradient   # full-board rainbow gradient
python cli.py custom-test --gradient --no-enter-custom --dry-run
```

Run an effect in the foreground (Ctrl+C stops, restores Static White):

```powershell
python cli.py effect rainbow --fps 30 --duration 10
python cli.py effect cpu --spatial
python cli.py effect audio --spatial --real-audio --audio-source microphone
```

Background daemon:

```powershell
python cli.py start cpu --spatial --fps 30
python cli.py status
python cli.py stop
```

Tray service:

```powershell
python cli.py tray
```

Profiles:

```powershell
python cli.py profile list
python cli.py profile save "Gaming" --kind effect --effect rainbow --brightness 5
python cli.py profile load "Gaming"
```

Full help:

```powershell
python cli.py --help
python cli.py effect --help
```

---

## Mapping tools

`auto_matrix.py` — semi-automatic. Lights one slot at a time; you press the lit
key; it records the label and advances. Non-key slots time out after 2.5 s and
become `LED<n>`.

```powershell
.\.venv\Scripts\python.exe auto_matrix.py          # resume at first unmapped slot
.\.venv\Scripts\python.exe auto_matrix.py --start 16 --timeout 4
.\.venv\Scripts\python.exe auto_matrix.py --list    # show current map
```

`matrix_mapper.py` — interactive. You type the label for the lit slot:

```powershell
.\.venv\Scripts\python.exe matrix_mapper.py --hold 15 --hold-seconds 6  # probe-stability check
```

Progress is written to `keymap_matrix.json` after every entry.

---

## Project layout

```
ajazz_hid.py          # ctypes Win32 HID backend + protocol packet builders
animation_engine.py   # real-time effects, daemon engine, teardown
audio_source.py       # WASAPI loopback + synthetic audio sources
cli.py                # command-line interface (list, set, custom-test, effect,
                      # start/stop/status, tray, profile)
mapper_common.py      # shared mapper plumbing: preflight, probe, keepalive,
                      # restore_static_white
matrix_mapper.py      # interactive LED index -> label mapper
auto_matrix.py        # semi-automatic mapper (press the lit key)
layout_75pct.py       # 83-key 75% physical grid, index<->(x,y) joins
tray_service.py       # pystray background service
profile_manager.py    # lighting profile persistence (.ajazz_anim.json)
keymap_matrix.json    # measured slot -> key label map (2 of 118 populated)
profiles.json         # saved lighting profiles
PROTOCOL.md           # wire format, byte layouts, evidence marks
TODO.md               # phase task list
DEVELOPMENT_PLAN.md   # architecture and phase roadmap
tests/                # 112 unit tests (unittest)
```

---

## Roadmap

Phase 4 (in progress): spatial/teardown polish — finish the 118-slot matrix map,
teardown retry, stale-state recovery, 20-cycle soak.

Phase 5 (planned): WebHID browser driver — `navigator.hid`, VID `0x1A2C` /
PID `0xA036`, per-key painter, profiles, no daemon.

Phase 6 (precursors present): Windows tray + real-time animation — already has
`tray_service.py`, `audio_source.py`, `profile_manager.py`.

Phase 7 (planned): shared profile schema + unified UI across browser and desktop.

See [DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md) and [TODO.md](TODO.md).

---

## Safety

- Do not run `DeviceDriver.exe` and this project at the same time while writing
  lighting state — they share `CreateFile` access, so the last write wins.
- The board's firmware rejects full-board high-intensity per-key tables
  (all-red and all-green revert). Teardown uses the built-in Static White
  effect instead of an all-white table.
- No vendor binaries are shipped or required. Protocol facts come from
  observation of the hardware.

---

## License

To be determined (X3 in TODO.md). No vendor code or binaries are included.