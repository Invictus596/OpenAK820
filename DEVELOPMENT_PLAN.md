# DEVELOPMENT PLAN — AJAZZ AK820 Max Plus

**Status:** Phase 3 complete and measured on hardware · Phase 4 code complete, data-blocked
**Last updated:** 2026-10-27
**Companion docs:** [PROTOCOL.md](PROTOCOL.md) (wire format, evidence marks) · [TODO.md](TODO.md) (task list)

---

## 1. Product goal

Replace the vendor `DeviceDriver.exe` with two independent, open clients that speak the
same protocol:

| Target | Runtime | What it is for |
|---|---|---|
| **WebHID app** | Browser (Chrome / Edge / Brave / Helium) | Zero-install configuration: lighting modes, per-key painter, profile import/export. Talks to the keyboard directly, no daemon. |
| **Windows tray daemon** | Python + pystray | Always-on features a browser cannot do: CPU/RAM telemetry, system-audio loopback equalizer, reactive spatial animation, clean teardown. |

Both targets are first-class. Neither depends on the other. They share a **profile schema**
and, from Phase 7, a **UI component set**.

### Non-goals

- No vendor binary is shipped or required. Protocol facts come from observation.
- No kernel driver, no firmware flashing, no macro/key-remap until the lighting path is
  complete and documented (remap is a separate reverse-engineering effort).
- The browser target will not chase features it cannot access (system CPU, loopback audio,
  background execution).

---

## 2. What is already proven

These are measured facts, not assumptions (details and raw captures in `PROTOCOL.md`):

- **Transport:** 65-byte output reports, report ID `0x00`, magic `AA 55 CC 33` at bytes `[1..4]`.
- **Device:** VID `0x1A2C`, PID `0xA036`, product string `AK820`. Composite device — only
  `MI_00`, usage page `0xFF02`, accepts the 65-byte reports. `CreateFile` sharing works, so
  the vendor app may be running at the same time (but see risk R1).
- **`0x07` lighting frame:** `mode` `[6]`, brightness `[7]`, speed `[8]`, variant `[9]`,
  palette colour `[10]`. UI level `1..5` encodes as `0x00..0x04`.
- **Custom Mode:** `0x07` with `mode = 18` enters per-key display. Then six reports
  `0xF6..0xFB`, 59 data bytes each = 354 bytes = 118 keys × RGB, then `0x0A` commit.
  ✅ Reproduced end to end from a host program with no vendor code (Esc→Del hue sweep).
- **Heartbeat:** `0x0E`, empty payload, every 5 s, matching vendor traffic.
- **Throughput:** 59.8 fps sustained at a 60 fps target (420 reports/s) with no write errors;
  30/20/15 fps also hit their targets.
- **Teardown:** a `0x07` Static White frame followed by a ~100 ms hold before closing the
  handle returns the board to white. Closing immediately after the write was the root cause
  of a stuck-rainbow incident.

### The one real data gap

The LED chain order is **not** the visual order (`Esc` is index 15). `keymap_matrix.json`
currently holds **2 of 118** entries. Spatial effects need `index → key label`; the label
then resolves to `(x, y)` through the layout table. There is no algorithmic shortcut — the
map must be measured with `matrix_mapper.py` (or captured from the vendor app).

Until that map exists:
- spatial effects light at most 2 keys,
- `SpatialRainbow` fills the rest from chain order, which is *not* spatial,
- `--spatial` verification on hardware is impossible.

This is **P4-S1** in `TODO.md` and the top priority.

---

## 3. Architecture

```
                 shared contracts
     PROTOCOL.md ──────┬──────────────┬──────────────┐
     layout_75pct.json │              │              │
     profile.schema.json              │              │
                       │              │              │
             ┌─────────▼───────┐  ┌───▼──────────────▼───┐
             │  WebHID client  │  │  Windows daemon      │
             │  TypeScript     │  │  Python              │
             │                 │  │                      │
             │ navigator.hid   │  │ ctypes CreateFile    │
             │  ↓ 65-byte      │  │  ↓ 65-byte WriteFile │
             └─────────┬───────┘  └───┬──────────────────┘
                       │              │
                       └──────┬───────┘
                              ▼
                    AK820 Max Plus (118 LEDs)
```

### 3.1 WebHID client

Pure client-side TypeScript. No server, no daemon, no install.

- **Permission:** `navigator.hid.requestDevice({ filters: [{ vendorId: 0x1A2C,
  productId: 0xA036, usagePage: 0xFF02 }] })`, triggered by a user gesture. Persisted
  grants are re-enumerated with `navigator.hid.getDevices()`.
- **Writes:** `device.sendReport(0, Uint8Array(64))` — the browser prepends the report ID,
  so the payload excludes byte `[0]`. `sendReport` is promise-based; a frame is 7 awaits
  (6 chunks + commit).
- **UI:** SVG 75 % board rendered from the shared layout. Paint / fill / erase, colour
  picker, per-key hex, undo, presets, apply.
- **Profiles:** import/export `.ak820.json` (schema v1), recent profiles in `localStorage`.
- **Constraints:** Chromium-only (Firefox/Safari have no WebHID), HTTPS or `localhost`,
  tab must stay open. A 30 fps browser preview loop is plausible but **unmeasured** — treat
  as an experiment, not a promise.

### 3.2 Windows daemon

Python, py3.11+, no compiled extensions required.

- **Transport:** `ajazz_hid.py` — dependency-free `ctypes` Win32 HID backend (a `hidapi`
  backend exists but has no wheel for the current Python and no compiler is available).
- **Engine:** host renders full 354-byte tables at a target fps, commits each frame, drops
  backlog instead of bursting. Heartbeat thread + one writer lock.
- **Effects:** rainbow (chain / spatial / diagonal), CPU meter (chain / spatial), audio
  visualizer (chain / spatial), synthetic or WASAPI-loopback audio.
- **Daemon:** detached child, state file, graceful stop flag, PID-reuse guard, force-kill
  fallback. **Gap:** force-kill bypasses teardown — the parent must send the reset itself
  (P4-T3).
- **Tray:** pystray + Pillow; quick effects, profile switcher, brightness, clean exit.
- **IPC (Phase 6):** `127.0.0.1` WebSocket with origin check + session token, so the unified
  UI can drive the daemon without duplicating HID code in JavaScript.

### 3.3 Shared contracts

| Contract | Form | Consumers |
|---|---|---|
| Wire protocol | `PROTOCOL.md` + byte-vector tests | Python builders, TS builders |
| Layout grid | `layout_75pct.json` (generated from `layout_75pct.py`) | Python effects, TS painter |
| Index→key map | `keymap_matrix.json` (`index_to_key`, `key_to_index`, `positions`) | both |
| Profile | `schema/profile.schema.json` (Phase 7) | both + validators |
| Effect names | `rainbow`, `cpu`, `audio` + params | both |

The layout must be generated once and consumed by both languages; hand-maintaining two
copies is a guaranteed drift bug (P4-L2).

---

## 4. Repository layout (target)

```
ajazz-ak820/
├── PROTOCOL.md
├── DEVELOPMENT_PLAN.md
├── TODO.md
├── README.md
├── requirements.txt
├── ajazz_hid.py            # transport, packet builders, keymap I/O
├── animation_engine.py     # effects, engine, daemon control
├── audio_source.py         # WASAPI loopback + FFT
├── layout_75pct.py         # grid source of truth
├── layout_75pct.json       # generated, consumed by TS
├── matrix_mapper.py        # interactive index→key mapper
├── profile_manager.py
├── tray_service.py
├── cli.py
├── keymap_matrix.json
├── schema/
│   └── profile.schema.json
├── webhid/
│   ├── package.json
│   ├── src/{protocol,device,layout,matrix,profile}.ts
│   ├── src/ui/
│   └── tests/
├── tests/                  # Python: unittest, hardware-free
└── docs/
    ├── WEBHID.md
    └── TROUBLESHOOTING.md
```

---

## 5. Phase plan

### Phase 4 — Spatial effects & teardown (Verification & Polish)

**Exit criteria**

1. `keymap_matrix.json` resolves ≥116/118 indices; every layout label appears once.
2. `cli.py effect rainbow --spatial` visibly sweeps **left→right in desk space**, and
   `--diagonal` sweeps corner-to-corner. Verified by eye on hardware.
3. CPU and audio spatial effects fill by physical position, not chain order.
4. Stopping any effect — foreground, daemon, tray, or hung child — leaves the board on
   Static White level 4. 20-cycle soak passes.
5. 82+ tests green, including coverage-guard tests.

**Work:** see `TODO.md` §4. The blockers are P4-S1 (map), P4-T1/T2/T3 (teardown),
P4-S2 (coverage guard), P4-S6 (daemon parity).

### Phase 5 — WebHID browser driver

**Exit criteria**

1. On a clean Chrome profile: grant permission, set Static Cyan, paint a full-board
   gradient, apply it, verify on hardware — **with the Python project not running**.
2. Byte-for-byte parity with Python builders, proven by shared vectors.
3. Profile exported in the browser applies correctly from the desktop daemon (and back).
4. Works on Chrome, Edge, Brave, Helium; unsupported browsers get a clear message.
5. Coverage banner prevents spatial features from silently misrepresenting a partial map.

**Work:** `TODO.md` §5.

### Phase 6 — Windows tray daemon & real-time host animation

**Exit criteria**

1. Installed from a one-file build on a clean Windows VM with no Python.
2. WASAPI loopback drives the equalizer during normal music playback.
3. CPU meter tracks Task Manager within a few percent.
4. Tray start/stop, profile switch, brightness, and exit all restore white; no zombie
   processes; single instance enforced.
5. Local IPC rejects requests from foreign origins.

**Work:** `TODO.md` §6.

### Phase 7 — Shared profile schema & unified UI

**Exit criteria**

1. One JSON Schema; Python and TypeScript validators agree on a shared fixture suite.
2. A profile exported from either target imports into the other without loss.
3. The painter component renders identical colours in browser and desktop webview
   (snapshot test).
4. Legacy `profiles.json` migrates automatically.

**Work:** `TODO.md` §7. **Open decision:** desktop webview shell — pywebview (Python-only,
larger bundle, no Rust) vs Tauri (smaller, needs a Rust toolchain). Decide before P7-5.

---

## 6. Verification strategy

- **Hardware-free by default.** Every test runs against a fake HID backend; no test requires
  the keyboard. `tests/` currently has 82 tests covering packets, chunk boundaries,
  effects, grid maths, engine loop, heartbeat, teardown, and profiles.
- **Byte vectors are the contract.** The same expected byte arrays are asserted in Python
  and TypeScript. A protocol change updates `PROTOCOL.md`, both builders, and both test
  suites in one commit.
- **Confidence marks in docs.** ✅ measured/reproduced · ⚠️ inferred · ❓ open. A claim only
  becomes ✅ after a hardware run; code existing is never evidence.
- **Hardware checklists** for anything visual: a human must confirm direction, smoothness,
  and teardown. Automated tests cannot see the LEDs.
- **Soak tests** for lifecycle paths (start/stop/force-kill) because that is where this
  project has already failed once.

---

## 7. Risk register

| ID | Risk | Impact | Mitigation |
|---|---|---|---|
| R1 | Vendor app writes concurrently and overwrites state | Flicker, wrong colours | Document single-writer rule; detect vendor process and warn; never assume state persists |
| R2 | Index→key map incomplete | Spatial features are decorative lies | P4-S1 blocking task; coverage guard; no silent chain fallback |
| R3 | Teardown frame lost on handle close | Board stuck on animation | 3× retry + 100 ms hold; parent-side restore before force-kill; soak test |
| R4 | WebHID rate limits / promise latency | Browser preview stutters | Benchmark before promising fps; static painting is the primary use case |
| R5 | Browser compatibility | Firefox/Safari users blocked | Clear capability detection and messaging; desktop app is the fallback |
| R6 | Two layout tables drift | Painter and effects disagree | Generate JSON from Python; single source of truth (P4-L2) |
| R7 | Python/TS schema drift | Profiles fail to round-trip | Shared JSON Schema + shared fixtures (P7-1/P7-2) |
| R8 | Windows force-kill leaves LEDs on | Bad UX, support noise | Parent restores before terminating (P4-T3); startup recovery (P4-T4) |
| R9 | Vendor protocol changes in a firmware update | Silent breakage | Protocol version in docs; capability probe; report unknown modes rather than writing garbage |

---

## 8. Decisions log

| Date | Decision | Why |
|---|---|---|
| 2026-10-26 | `ctypes` Win32 HID backend, `hidapi` optional | No wheel for Python 3.15, no MSVC toolchain |
| 2026-10-26 | Host-rendered frames, commit every frame | Firmware has no custom animation API; atomic updates |
| 2026-10-26 | Single lock for frames + heartbeat | HID handle is not safe for concurrent writers |
| 2026-10-26 | Drop backlog instead of bursting | Bursting causes visible jumps and write pressure |
| 2026-10-26 | Normalised `(nx, ny)` in `keymap_matrix.json` | Resolution- and layout-independent effects |
| 2026-10-27 | Browser target is daemon-free | Portability is the point; no install, no local service |
| 2026-10-27 | Desktop keeps real-time/telemetry features | Browser cannot access system CPU or loopback audio |
| 2026-10-27 | Profile schema owned by the project, not either client | Both targets must round-trip the same file |
