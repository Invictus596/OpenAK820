# TODO — AJAZZ AK820 Max Plus

Canonical task list. Architecture and rationale live in [DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md);
wire format lives in [PROTOCOL.md](PROTOCOL.md).

**Legend:** `[x]` done and verified · `[~]` implemented, not yet verified on hardware ·
`[ ]` not started · `[!]` blocked

**Last audit:** 2026-10-27 · 82 tests passing · device VID `0x1A2C` / PID `0xA036` / `MI_00` usage page `0xFF02`

---

## Phase 4 — Spatial effects & teardown cleanup (Verification & Polish)

Status: **code complete, data-blocked, hardware verification pending.**

### 4.1 Teardown

- [x] `AnimationEngine._restore()` sends a `0x07` Static frame and sleeps 100 ms before
      the `with h.open_device()` block releases the handle.
- [x] `final="static" | "off" | "keep"` modes; `off` also sleeps 100 ms.
- [x] `tray_service.exit_clean()` stops the engine thread, writes Static White, sleeps 100 ms.
- [x] Unit tests assert opcode/mode/colour and the ≥80 ms delay.
- [ ] **P4-T1 — pin teardown brightness to level 4.** `_restore()` currently reuses
      `self.brightness`, so an engine started at level 1 restores *dim* white and level 5
      restores wire `0x04`. `tray_service` already uses 4; the engine must match.
      Accept: `AnimationEngine(..., brightness=1)._restore()` still emits brightness `0x03`.
- [ ] **P4-T2 — retry the restore frame.** One frame occasionally loses the race with
      handle close (observed on this board). Send up to 3 frames 150 ms apart, then hold
      100 ms. Accept: stuck-rainbow incident cannot recur in a 20-cycle start/stop soak.
- [ ] **P4-T3 — restore on abnormal exit.** `atexit` + `SIGINT`/`SIGBREAK` handler in the
      foreground path. The daemon's 6 s force-kill path (`TerminateProcess`) bypasses
      `_restore()`; before terminating, the *parent* must open the device and send the
      reset frame itself, then wait 100 ms. Accept: `cli.py stop` after a hung child leaves
      the board white, never on the last animation frame.
- [ ] **P4-T4 — stale-state recovery.** On daemon/tray start, if `.ajazz_anim.json` points
      at a dead PID, send a reset frame before accepting new work.

### 4.2 2D layout

- [x] `layout_75pct.py` — 83 key labels, integer grid `x∈[0..21]`, `y∈[0..5]`
      (Esc `(0,0)`, F1 `(2,0)`, Space `(7,5)`), plus `normalised_grid()` (`nx=x/21`, `ny=y/5`).
- [x] `index_positions()` joins a keymap to the grid; unknown labels are skipped.
- [x] `populate_keymap_json()` writes `positions` atomically and preserves other fields.
- [x] 15 layout tests in `tests/test_layout.py` + grid tests in `tests/test_animation.py`.
- [ ] **P4-L1 — reconcile Space and unused columns.** Space is `(7,5)`; the original spec
      said `(6,5)`. Columns 18, 20, 21 are unused. Decide against real keycap geometry and
      record the answer in the module docstring.
- [ ] **P4-L2 — single source of truth for the layout.** The same table will be needed by
      TypeScript (Phase 5). Export `layout_75pct.json` and have both languages load it;
      keep the Python module as a typed wrapper.

### 4.3 Spatial effects

- [x] `SpatialRainbow` — hue from `nx`, or `nx + ny` when `--diagonal`.
- [x] `SpatialCpuMeter` — lights keys with `nx <= load`, green→red across the threshold.
- [x] `SpatialAudioVisualizer` — 16 bands across `nx`, vertical fill from `ny=1.0` up.
- [x] `create_effect(name, spatial=, diagonal=)` and `--spatial` / `--diagonal` on `cli.py effect`.
- [x] `--dry-run` renders one spatial frame and prints chunk headers without writing.
- [ ] **P4-S1 — [!] populate the index→key map (BLOCKER).** `keymap_matrix.json` maps
      **2 of 118** indices (`14:"F1"`, `15:"Esc"`). Every spatial effect therefore lights at
      most 2 keys; `SpatialRainbow` fills the other 116 from LED-chain order, which mixes two
      coordinate systems and looks like a chain effect, not a spatial one. There is no way to
      derive the map from the layout — it must be measured.
      Accept: ≥116/118 indices resolved; every `LAYOUT_75PCT` label appears exactly once;
      remaining indices documented as underglow/indicator.
- [ ] **P4-S2 — spatial coverage guard.** Warn (and optionally refuse) when coverage is
      below e.g. 90 %, printing `mapped N/118 — run matrix_mapper.py`. Never silently fall
      back to chain order inside a `--spatial` effect.
- [ ] **P4-S3 — unmapped-key policy.** For partial maps, either leave unmapped slots unlit or
      snap them to the nearest mapped neighbour. Pick one and test it.
- [ ] **P4-S4 — `--diagonal` on non-rainbow effects.** CPU/audio currently accept the flag and
      ignore it. Reject with a clear message, or define diagonal semantics for them.
- [ ] **P4-S5 — band clamping in `SpatialAudioVisualizer`.** It does not clamp `bands` the way
      `AudioVisualizer` does; `bands=0` divides by zero. Clamp to `1..KEY_COUNT`.
- [ ] **P4-S6 — daemon parity.** `cli.py start` has no `--spatial`, `--diagonal` or
      `--real-audio`, and `start_daemon()` does not forward them, so the background daemon
      can never run a spatial effect. Forward and persist them in `.ajazz_anim.json`.
- [ ] **P4-S7 — hardware verification.** Run `python cli.py effect rainbow --spatial` and
      confirm the gradient sweeps left→right in desk space (not Esc→Del chain order), then
      repeat with `--diagonal`. **Cannot be judged until P4-S1 is done.**
- [ ] **P4-S8 — soak test.** 10 min at 30 fps with `--spatial`, then Ctrl+C; board must
      return to white and no handle must leak.
- [ ] **P4-S9 — document results** in `PROTOCOL.md` §4 (spatial verification subsection).

### 4.4 Test baseline

- [x] 82 tests green. The stale `test_audio_ignores_spatial` (asserted audio ignores
      `--spatial`, contradicting the new `SpatialAudioVisualizer`) was replaced with
      `test_spatial_audio` + `test_non_spatial_audio`.

---

## Phase 5 — WebHID browser driver (pure client-side, no daemon)

Goal: configure the board from Chrome/Edge/Brave/Helium with `navigator.hid` only.
No Python, no local service, no install.

Proposed layout:

```
webhid/
  package.json
  src/protocol.ts        # packets, chunking, commit, enums, CRC-less validation
  src/device.ts          # permission, open, send, reconnect, capability probe
  src/layout.ts          # 75% grid (generated from layout_75pct.json)
  src/matrix.ts          # index <-> label <-> (x,y) joins
  src/profile.ts         # schema v1 import/export
  src/ui/                # matrix painter, presets, colour picker
  tests/                 # vitest: byte-for-byte vectors from PROTOCOL.md
```

- [ ] **P5-1 — protocol core.** `buildLightingFrame`, `buildKeymapChunks`,
      `buildCommitFrame`, `buildKeepalive`, `Level`/`Mode`/`Color` enums.
      Accept: byte vectors identical to the Python builders (`tests/test_ajazz_hid.py`).
- [ ] **P5-2 — device layer.** `requestDevice` filter
      `{vendorId:0x1A2C, productId:0xA036, usagePage:0xFF02}`, pick the collection that
      accepts 65-byte output reports, `sendReport(0, bytes)`, `getDevices()` reconnect.
      Accept: `0x07` Static Cyan works with no Python running.
- [ ] **P5-3 — permission UX.** Handle "no device", "permission revoked", "unplugged",
      "another tab owns it"; re-request on user gesture only.
- [ ] **P5-4 — matrix upload.** 6 chunks + `0x0A`, 1 ms gap (or `await` between reports),
      `0x07 mode=18` entry. Accept: full-board gradient identical to Python output.
- [ ] **P5-5 — interactive 75% painter.** SVG grid from the shared layout; click/drag paint,
      fill, eraser, colour picker, per-key hex, undo/redo, copy/paste, "select all".
- [ ] **P5-6 — live preview + send.** Preview in-page, explicit **Apply** button; show
      write progress and report count. Optional 30 fps preview loop behind a warning
      (tab throttling and 7 awaits/frame are unmeasured — treat 30 fps as ❓ until benchmarked).
- [ ] **P5-7 — profiles.** Import/export `.ak820.json` using the shared schema; browser
      `localStorage` for recents. Must round-trip a desktop-exported profile.
- [ ] **P5-8 — presets.** Static palette modes (no custom table) + sample per-key presets.
- [ ] **P5-9 — keymap resolution.** Load `keymap_matrix.json`; show coverage banner and
      disable spatial features when coverage is low (same rule as P4-S2).
- [ ] **P5-10 — packaging.** Static build deployable to GitHub Pages/Netlify; works from
      `localhost` and HTTPS; document that Firefox/Safari do not implement WebHID.
- [ ] **P5-11 — browser matrix.** Manual pass on Chrome, Edge, Brave, Helium; record
      version + result in `docs/WEBHID.md`.

Out of scope for Phase 5: CPU/audio reactive effects (browser cannot read system CPU or
loopback audio without extra permissions), background operation (tab must stay open).

---

## Phase 6 — Windows tray daemon & real-time host animation

Goal: always-on background app for effects a browser cannot do: system audio loopback,
CPU/GPU/RAM telemetry, spatial reactive effects, clean teardown.

Already present: `tray_service.py` (pystray + Pillow), `audio_source.py` (WASAPI loopback +
FFT), `profile_manager.py`, `animation_engine.py`, `cli.py tray|profile`.

- [ ] **P6-1 — teardown hardening.** Finish P4-T1..T4; add a 20-cycle start/stop stress test.
- [ ] **P6-2 — single instance.** Named mutex / lock file; second launch focuses the first.
- [ ] **P6-3 — autostart + tray polish.** Start with Windows (HKCU Run or Task Scheduler),
      tooltip shows active profile, "Pause animations", "Reload device".
- [ ] **P6-4 — audio loopback.** Promote `WasapiLoopbackSource` to the tray's default audio
      path; device picker, gain, band count, graceful fallback to synthetic when no loopback
      device exists. Accept: music playback drives the equalizer with no console window.
- [ ] **P6-5 — telemetry.** CPU (already), plus RAM and GPU if a dependency-free path exists;
      configurable bar orientation/colour ramp; 4 Hz sampling ceiling.
- [ ] **P6-6 — local IPC.** WebSocket (or REST) bridge on `127.0.0.1` with an origin check and
      a per-session token, so the Phase 7 UI can drive the daemon. No open CORS.
- [ ] **P6-7 — profile hot-swap.** Apply profile without restarting the daemon; atomic engine
      swap, no frame gap longer than ~100 ms.
- [ ] **P6-8 — crash recovery.** Watchdog thread; on engine exception restore white and
      surface a tray notification instead of dying silently.
- [ ] **P6-9 — packaging.** PyInstaller one-file build, icon, version resource, no console.
      Accept: runs on a clean Windows VM with no Python installed.
- [ ] **P6-10 — logging.** Rotating log, `--verbose`, no unbounded `.ajazz_anim.log`.

---

## Phase 7 — Shared profile schema & unified UI

Goal: one profile format and one UI codebase across browser and desktop.

- [ ] **P7-1 — schema v1.** `schema/profile.schema.json` (JSON Schema 2020-12):
      `format`, `name`, `kind` (`built-in|custom|effect`), `brightness`, `speed`, `color`,
      `variant`, `custom_keys` (`label|index -> [r,g,b]`), `effect {name, fps, spatial,
      diagonal, audio_source}`, `layout: "75pct-ak820"`, `keymap_ref`, `metadata`.
- [ ] **P7-2 — validators.** Python (`jsonschema` or hand-rolled) + TypeScript (zod/ajv)
      must accept/reject the same fixtures. Accept: shared fixture suite passes on both.
- [ ] **P7-3 — migration.** Import legacy `profiles.json` and bare `keymap_matrix.json`;
      write a migration note and bump `format`.
- [ ] **P7-4 — export/import.** `.ak820.json`, stable key ordering, embedded keymap optional
      (small file vs portable file).
- [ ] **P7-5 — unified UI.** Share the painter/preset components between the WebHID app and a
      desktop webview shell (pywebview or Tauri) talking to the Phase 6 IPC bridge.
      Decision required: pywebview (Python-native, bigger bundle) vs Tauri (Rust toolchain).
- [ ] **P7-6 — capability negotiation.** UI hides/disables features the target cannot do:
      no CPU/audio in browser, no background animation in browser, no HID in webview until
      the bridge is connected.
- [ ] **P7-7 — visual parity.** Same layout JSON, same spatial maths, same colour ramp in
      both targets; snapshot-test rendered key colours.
- [ ] **P7-8 — docs.** `README.md` with install/use for both targets, screenshots,
      troubleshooting (device busy, permission, stale daemon).

---

## Cross-cutting

- [ ] **X1 — CI.** GitHub Actions: Python 3.11–3.13 tests; Node typecheck + vitest;
      no hardware required (fake HID backend).
- [ ] **X2 — protocol versioning.** `PROTOCOL.md` gets a version + changelog; code asserts the
      values it was written against.
- [ ] **X3 — licence + attribution.** Pick a licence, note that protocol facts were derived by
      observation, ship no vendor binaries.
- [ ] **X4 — safety.** Document that the vendor app and this project must not write
      simultaneously; shared `CreateFile` access masks the conflict.
- [ ] **X5 — accessibility.** Keyboard-navigable painter, colour-blind-safe ramps, contrast.

---

## Immediate next actions

1. **P4-S1** — run `matrix_mapper.py` and map the remaining 116 indices. Everything spatial
   depends on it.
2. **P4-T1/T2** — fixed level-4 teardown + 3× retry; soak test.
3. **P4-S2** — coverage guard so `--spatial` can never mislead.
4. **P5-1/P5-2** — protocol core + WebHID device layer against byte vectors.
5. **P6-1** — finish teardown before adding more daemon features.
