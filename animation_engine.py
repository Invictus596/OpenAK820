"""animation_engine.py -- host-driven lighting effects for the AJAZZ AK820.

The keyboard exposes no on-device animation API for custom content, so effects
are rendered on the host: every frame is a full 118-key RGB table pushed with
``0xF6``..``0xFB`` plus the ``0x0A`` commit (see ``PROTOCOL.md`` section 4).

A background heartbeat thread emits the vendor's ``0x0E`` keep-alive every 5 s
while an effect is running, matching the traffic pattern of ``DeviceDriver.exe``.

Included effects
----------------
``rainbow``
    Smooth hue wave, constant speed, full board.
``cpu``
    System CPU utilisation as a left-to-right bar (green -> red).  Uses
    ``GetSystemTimes`` on Windows, so no ``psutil`` dependency.
``audio``
    Spectrum visualiser.  ``--audio-source synthetic`` needs nothing;
    ``--audio-source microphone`` needs ``numpy`` + ``sounddevice`` and is a
    hook for you to wire up a real capture device.

Commands
--------
    python animation_engine.py run --effect rainbow --duration 10
    python animation_engine.py run --effect cpu --fps 20
    python animation_engine.py run --effect audio --audio-source microphone

A single writer lock serialises frames and heartbeats, because the HID handle is
not safe for concurrent writers.
"""

from __future__ import annotations

import argparse
import colorsys
import ctypes
import json
import math
import os
import random
import signal
import subprocess
import sys
import threading
import time
import warnings
from ctypes import wintypes
from pathlib import Path
from typing import Callable, Optional, Protocol, Sequence

import ajazz_hid as h
import layout_75pct

BASE_DIR = Path(__file__).resolve().parent

#: Daemon bookkeeping (all under the project directory).
STATE_FILE = BASE_DIR / ".ajazz_anim.json"
STOP_FILE = BASE_DIR / ".ajazz_anim.stop"
LOG_FILE = BASE_DIR / ".ajazz_anim.log"

HEARTBEAT_INTERVAL = 5.0
DEFAULT_FPS = 30
MIN_FPS = 1
MAX_FPS = 60

#: Teardown state: the board is always returned to Static White at UI level 4,
#: regardless of the animation's brightness, so a dim or bright effect can
#: never leave the keyboard in a surprising state (P4-T1).
RESTORE_BRIGHTNESS = 4
RESTORE_SPEED = 4

#: Below this fraction of mapped LED indices, ``--spatial`` effects warn loudly
#: and leave unmapped keys unlit instead of guessing (P4-S2).
SPATIAL_MIN_COVERAGE = 0.90

UNLIT = (0, 0, 18)


def _log(message: str) -> None:
    print(message, flush=True)


def _rgb(hue: float, saturation: float = 1.0, value: float = 1.0) -> h.RGB:
    r, g, b = colorsys.hsv_to_rgb(hue % 1.0, saturation, value)
    return (round(r * 255), round(g * 255), round(b * 255))


def _partition(total: int, parts: int) -> list[tuple[int, int]]:
    """Split ``total`` indices into ``parts`` contiguous ranges, no gaps."""
    return [
        (round(i * total / parts), round((i + 1) * total / parts))
        for i in range(parts)
    ]


# --------------------------------------------------------------------------
# Effects
# --------------------------------------------------------------------------


class Effect:
    """Base class.  ``frame(t)`` returns the 354-byte table for time ``t``."""

    name = "effect"

    def frame(self, t: float) -> bytes:  # pragma: no cover - interface
        raise NotImplementedError

    def close(self) -> None:
        """Release any resources (audio streams, etc.)."""


class RainbowWave(Effect):
    """Smooth rainbow wave travelling along the LED chain."""

    name = "rainbow"

    def __init__(
        self,
        speed: float = 0.35,
        cycles: float = 2.0,
        direction: int = 1,
        saturation: float = 1.0,
        value: float = 1.0,
    ) -> None:
        self.speed = speed
        self.cycles = cycles
        self.direction = 1 if direction >= 0 else -1
        self.saturation = saturation
        self.value = value

    def frame(self, t: float) -> bytes:
        keys = []
        for i in range(h.KEY_COUNT):
            hue = i / h.KEY_COUNT * self.cycles + t * self.speed * self.direction
            keys.append(_rgb(hue, self.saturation, self.value))
        return h.pack_key_table(keys)


class CpuMeter(Effect):
    """System-wide CPU load as a bar that fills from index 0 upward."""

    name = "cpu"

    def __init__(
        self,
        sampler: Optional["_CpuSampler"] = None,
        smoothing: float = 0.25,
        sample_interval: float = 0.25,
        unlit: h.RGB = UNLIT,
    ) -> None:
        self._sampler = sampler or _CpuSampler()
        self._smoothing = max(0.0, min(1.0, smoothing))
        self._sample_interval = max(0.01, sample_interval)
        self._next_sample = 0.0
        self._unlit = unlit
        self._load = 0.0

    @property
    def load(self) -> float:
        return self._load

    def frame(self, t: float) -> bytes:
        # GetSystemTimes only advances every ~15 ms, so polling it at 60 fps
        # would produce zero deltas and a flickering bar.  Sample 4x/second.
        if t >= self._next_sample:
            level = self._sampler.sample()
            self._next_sample = t + self._sample_interval
            if self._smoothing:
                self._load += (level - self._load) * self._smoothing
            else:
                self._load = level

        lit = round(self._load * h.KEY_COUNT)
        keys: list[h.RGB] = [self._unlit] * h.KEY_COUNT
        for i in range(lit):
            # green at the low end -> red at the high end
            pos = i / max(1, h.KEY_COUNT - 1)
            keys[i] = _rgb((1.0 - pos) * 0.33)
        return h.pack_key_table(keys)


class _CpuSampler:
    """CPU load 0..1.  Windows: ``GetSystemTimes``; elsewhere: load average."""

    def __init__(self) -> None:
        self._prev: Optional[tuple[int, int, int]] = None
        self._get = None

        if sys.platform == "win32":
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self._get = k32.GetSystemTimes
            self._get.argtypes = [
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
            ]
            self._get.restype = wintypes.BOOL

    @staticmethod
    def _as_int(value: wintypes.FILETIME) -> int:
        return (value.dwHighDateTime << 32) | value.dwLowDateTime

    def sample(self) -> float:
        if self._get is None:
            try:
                return max(0.0, min(1.0, os.getloadavg()[0] / (os.cpu_count() or 1)))
            except (AttributeError, OSError):
                return 0.0

        idle = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not self._get(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            return 0.0

        now = (self._as_int(idle), self._as_int(kernel), self._as_int(user))
        if self._prev is None:
            self._prev = now
            return 0.0

        idle_delta = now[0] - self._prev[0]
        total_delta = (now[1] - self._prev[1]) + (now[2] - self._prev[2])
        self._prev = now

        if total_delta <= 0:
            return 0.0
        load = 1.0 - idle_delta / total_delta
        return max(0.0, min(1.0, load))


class AudioSource(Protocol):
    """Hook for anything that can report spectrum magnitudes."""

    def levels(self, bands: int) -> list[float]:
        ...  # pragma: no cover - interface

    def close(self) -> None:
        ...  # pragma: no cover - interface


class SyntheticAudioSource:
    """Deterministic pseudo-spectrum, so the visualiser runs with no extras.

    This is the reference implementation of the :class:`AudioSource` hook and is
    also useful for testing a layout before wiring up real audio.
    """

    def __init__(self, seed: int = 7) -> None:
        rng = random.Random(seed)
        self._phase = [rng.uniform(0, math.tau) for _ in range(32)]
        self._rate = [rng.uniform(0.8, 3.4) for _ in range(32)]
        self._t = 0.0

    def levels(self, bands: int) -> list[float]:
        self._t += 1.0 / DEFAULT_FPS
        beat = max(0.0, math.sin(self._t * 8.0)) ** 8
        out = []
        for b in range(bands):
            i = b % len(self._phase)
            value = 0.35 + 0.3 * math.sin(self._t * self._rate[i] + self._phase[i])
            value += 0.15 * math.sin(self._t * 1.1 + b * 0.6)
            if b < max(1, bands // 4):          # bass pulse
                value += 0.5 * beat
            # higher bands are quieter, like real music
            value *= 1.0 - 0.5 * (b / max(1, bands - 1))
            out.append(max(0.0, min(1.0, value)))
        return out

    def close(self) -> None:
        return None


class MicrophoneAudioSource:
    """Optional real capture.  Requires ``numpy`` and ``sounddevice``.

    This is a working hook, not a dependency: the rest of the engine runs
    without it.  Install with ``pip install numpy sounddevice``.
    """

    def __init__(self, samplerate: int = 44100, blocksize: int = 1024) -> None:
        try:
            import numpy as np
            import sounddevice as sd
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise h.AjazzError(
                "the microphone source needs numpy and sounddevice "
                "(pip install numpy sounddevice); "
                "use --audio-source synthetic instead"
            ) from exc

        self._np = np
        self._blocksize = blocksize
        self._samplerate = samplerate
        self._lock = threading.Lock()
        self._block = np.zeros(blocksize, dtype=np.float32)

        def _callback(indata, frames, time_info, status) -> None:  # pragma: no cover
            with self._lock:
                self._block = indata[:, 0].copy()

        self._stream = sd.InputStream(  # pragma: no cover
            channels=1,
            samplerate=samplerate,
            blocksize=blocksize,
            callback=_callback,
        )
        self._stream.start()  # pragma: no cover

    def levels(self, bands: int) -> list[float]:  # pragma: no cover - needs hardware
        np = self._np
        with self._lock:
            block = self._block.copy()

        if not block.size:
            return [0.0] * bands

        spectrum = np.abs(np.fft.rfft(block * np.hanning(block.size)))
        freqs = np.fft.rfftfreq(block.size, 1.0 / self._samplerate)
        edges = np.geomspace(40.0, self._samplerate * 0.45, bands + 1)

        out = []
        for low, high in zip(edges[:-1], edges[1:]):
            mask = (freqs >= low) & (freqs < high)
            magnitude = float(spectrum[mask].mean()) if mask.any() else 0.0
            out.append(max(0.0, min(1.0, magnitude / 60.0)))
        return out

    def close(self) -> None:  # pragma: no cover - needs hardware
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            pass


def open_audio_source(kind: str) -> AudioSource:
    kind = kind.lower()
    if kind == "synthetic":
        return SyntheticAudioSource()
    if kind in ("wasapi", "real", "real-audio", "loopback", "microphone"):
        try:
            import audio_source
            return audio_source.get_audio_source("wasapi")
        except Exception:
            return MicrophoneAudioSource()
    raise h.AjazzError(
        f"unknown audio source {kind!r}; choose synthetic, wasapi, or microphone"
    )


class AudioVisualizer(Effect):
    """Spectrum bars: one contiguous block of keys per frequency band."""

    name = "audio"

    def __init__(
        self,
        source: AudioSource,
        bands: int = 16,
        smoothing: float = 0.45,
        unlit: h.RGB = UNLIT,
    ) -> None:
        self._source = source
        self._bands = max(1, min(bands, h.KEY_COUNT))
        self._smoothing = max(0.0, min(1.0, smoothing))
        self._unlit = unlit
        self._levels = [0.0] * self._bands

    def frame(self, t: float) -> bytes:
        raw = self._source.levels(self._bands)
        if len(raw) < self._bands:  # tolerate short sources instead of shrinking
            raw = raw + [0.0] * (self._bands - len(raw))
        self._levels = [
            old + (new - old) * (1.0 - self._smoothing)
            for old, new in zip(self._levels, raw)
        ]

        keys: list[h.RGB] = [self._unlit] * h.KEY_COUNT
        for band, (start, end) in enumerate(_partition(h.KEY_COUNT, self._bands)):
            height = round(self._levels[band] * (end - start))
            for step in range(height):
                # green at the base of the bar, red at the top
                pos = step / max(1, (end - start) - 1)
                keys[start + step] = _rgb((1.0 - pos) * 0.33)
        return h.pack_key_table(keys)

    def close(self) -> None:
        self._source.close()


def _spatial_positions(
    keymap: dict[int, str] | None,
) -> tuple[dict[int, tuple[float, float]], float]:
    """Resolve LED indices to normalised positions and report coverage.

    Returns ``(positions, coverage)``.  Coverage is measured against the
    83 layout keys, NOT all 118 LED slots: the board has physical keys plus
    underglow/indicator slots that no layout key can ever occupy, so a
    slot-based ratio could never reach 100 %.  Warns when coverage falls
    below :data:`SPATIAL_MIN_COVERAGE`; callers must leave unmapped keys
    unlit rather than guessing (P4-S2) -- a chain-order fallback mixes
    coordinate systems and renders visual nonsense.
    """
    if keymap is None:
        keymap = h.load_keymap_matrix()
    positions = layout_75pct.index_positions(keymap)
    total = len(layout_75pct.LAYOUT_75PCT)
    coverage = len(positions) / total
    if coverage < SPATIAL_MIN_COVERAGE:
        warnings.warn(
            f"spatial coverage is {coverage:.0%} ({len(positions)}/{total} layout "
            f"keys, {len(positions)}/{h.KEY_COUNT} LED slots) -- unmapped keys "
            "stay unlit. Run 'python auto_matrix.py' or 'python matrix_mapper.py' "
            "to complete the map.",
            UserWarning,
            stacklevel=3,
        )
    return positions, coverage


class SpatialRainbow(Effect):
    """Rainbow wave driven by physical key position rather than LED chain order.

    When ``diagonal`` is False the hue is based on the normalised X coordinate
    alone, producing vertical colour stripes that sweep left-to-right.  When
    ``diagonal`` is True the hue uses ``x + y``, giving a diagonal wave.

    Keys without a spatial position stay unlit (never chain-order fallback).
    """

    name = "rainbow"

    def __init__(
        self,
        keymap: dict[int, str] | None = None,
        speed: float = 0.35,
        cycles: float = 2.0,
        direction: int = 1,
        diagonal: bool = False,
        saturation: float = 1.0,
        value: float = 1.0,
        unlit: h.RGB = UNLIT,
    ) -> None:
        self.speed = speed
        self.cycles = cycles
        self.direction = 1 if direction >= 0 else -1
        self.diagonal = diagonal
        self.saturation = saturation
        self.value = value
        self._unlit = unlit

        self._positions, self.coverage = _spatial_positions(keymap)

    def frame(self, t: float) -> bytes:
        keys = []
        for i in range(h.KEY_COUNT):
            pos = self._positions.get(i)
            if pos is None:
                keys.append(self._unlit)
                continue
            nx, ny = pos
            coord = (nx + ny) if self.diagonal else nx
            hue = coord * self.cycles + t * self.speed * self.direction
            keys.append(_rgb(hue, self.saturation, self.value))
        return h.pack_key_table(keys)


class SpatialCpuMeter(Effect):
    """CPU load as a horizontal bar filling left-to-right across physical columns.

    Keys are grouped by their normalised X coordinate.  At 50 % load the bar
    fills the left half of the keyboard; at 100 % every column is lit.  Colour
    transitions from green (low load, left edge) to red (high load, right edge).

    Keys without a spatial position are left at ``unlit``.
    """

    name = "cpu"

    def __init__(
        self,
        keymap: dict[int, str] | None = None,
        sampler: Optional["_CpuSampler"] = None,
        smoothing: float = 0.25,
        sample_interval: float = 0.25,
        unlit: h.RGB = UNLIT,
    ) -> None:
        self._sampler = sampler or _CpuSampler()
        self._smoothing = max(0.0, min(1.0, smoothing))
        self._sample_interval = max(0.01, sample_interval)
        self._next_sample = 0.0
        self._unlit = unlit
        self._load = 0.0

        self._positions, self.coverage = _spatial_positions(keymap)

    @property
    def load(self) -> float:
        return self._load

    def frame(self, t: float) -> bytes:
        if t >= self._next_sample:
            level = self._sampler.sample()
            self._next_sample = t + self._sample_interval
            if self._smoothing:
                self._load += (level - self._load) * self._smoothing
            else:
                self._load = level

        keys: list[h.RGB] = [self._unlit] * h.KEY_COUNT
        for idx, (nx, _ny) in self._positions.items():
            if nx <= self._load:
                # green at the left edge -> red at the threshold
                pos = nx / max(self._load, 0.001)
                keys[idx] = _rgb((1.0 - pos) * 0.33)
        return h.pack_key_table(keys)


class SpatialAudioVisualizer(Effect):
    """Spectrum visualizer mapped across physical visual columns x in [0..21].

    The 16 frequency bands correspond directly to visual column groups from left
    (low bass) to right (treble). Keys in each column light up vertically from
    bottom row (row 5) up to the function row (row 0) based on band intensity.
    """

    name = "audio"

    def __init__(
        self,
        source: AudioSource,
        keymap: dict[int, str] | None = None,
        bands: int = 16,
        smoothing: float = 0.45,
        unlit: h.RGB = UNLIT,
    ) -> None:
        self._source = source
        self._bands = max(1, min(bands, h.KEY_COUNT))
        self._smoothing = max(0.0, min(1.0, smoothing))
        self._unlit = unlit
        self._levels = [0.0] * self._bands

        self._positions, self.coverage = _spatial_positions(keymap)

    def frame(self, t: float) -> bytes:
        raw = self._source.levels(self._bands)
        if len(raw) < self._bands:  # tolerate short sources instead of shrinking
            raw = raw + [0.0] * (self._bands - len(raw))
        self._levels = [
            old + (new - old) * (1.0 - self._smoothing)
            for old, new in zip(self._levels, raw)
        ]

        keys: list[h.RGB] = [self._unlit] * h.KEY_COUNT
        for idx, (nx, ny) in self._positions.items():
            # Map normalized nx (0.0..1.0) to band index
            band_idx = min(self._bands - 1, int(nx * self._bands))
            band_val = self._levels[band_idx]

            # In visual grid: y=0 is top row (F-row), y=1.0 is bottom row
            # Height fills from bottom (ny=1.0) upwards towards ny=0.0
            height_threshold = 1.0 - band_val
            if ny >= height_threshold:
                # Vertical gradient: green at bottom (ny=1.0) -> red at top (ny=0.0)
                vert_pos = 1.0 - ny
                keys[idx] = _rgb((1.0 - vert_pos) * 0.33)

        return h.pack_key_table(keys)

    def close(self) -> None:
        self._source.close()


EFFECTS = ("rainbow", "cpu", "audio")


def create_effect(
    name: str,
    *,
    audio_source: str = "synthetic",
    spatial: bool = False,
    diagonal: bool = False,
) -> Effect:
    name = name.lower()
    if name == "rainbow":
        if spatial:
            return SpatialRainbow(diagonal=diagonal)
        return RainbowWave()
    if name == "cpu":
        if spatial:
            return SpatialCpuMeter()
        return CpuMeter()
    if name == "audio":
        src = open_audio_source(audio_source)
        if spatial:
            return SpatialAudioVisualizer(src)
        return AudioVisualizer(src)
    raise h.AjazzError(f"unknown effect {name!r}; choose from {', '.join(EFFECTS)}")


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------


class AnimationEngine:
    """Runs one effect against the keyboard until stopped.

    A heartbeat thread keeps sending ``0x0E`` while the render loop pushes
    frames.  Both writers share a lock, because a HID handle must not be used
    from two threads at once.
    """

    def __init__(
        self,
        effect: Effect,
        fps: int = DEFAULT_FPS,
        brightness: int = h.LEVEL_MAX,
        device_path: Optional[str] = None,
        heartbeat_interval: float = HEARTBEAT_INTERVAL,
        final: str = "static",
        stop_file: Optional[Path] = None,
        log: Callable[[str], None] = _log,
    ) -> None:
        if not MIN_FPS <= fps <= MAX_FPS:
            raise ValueError(f"fps must be {MIN_FPS}..{MAX_FPS}, got {fps}")
        if not h.LEVEL_MIN <= brightness <= h.LEVEL_MAX:
            raise ValueError(
                f"brightness must be {h.LEVEL_MIN}..{h.LEVEL_MAX}, got {brightness}"
            )

        self.effect = effect
        self.fps = fps
        self.brightness = brightness
        self.final = final
        self.stop_file = Path(stop_file) if stop_file else None
        self.heartbeat_interval = heartbeat_interval

        self._device_path = device_path
        self._log = log
        self._lock = threading.Lock()
        self._stop = threading.Event()

        self.frames = 0
        self.achieved_fps = 0.0

    # -- control --------------------------------------------------------
    def stop(self) -> None:
        self._stop.set()

    def _should_stop(self) -> bool:
        if self._stop.is_set():
            return True
        return self.stop_file is not None and self.stop_file.exists()

    # -- heartbeat ------------------------------------------------------
    def _heartbeat(self, kb: h.AjazzKeyboard) -> None:
        packet = h.build_keepalive_packet()
        while not self._stop.wait(self.heartbeat_interval):
            try:
                with self._lock:
                    kb.send(packet)
            except Exception as exc:  # keep the render loop alive
                self._log(f"heartbeat failed: {exc}")
                return

    # -- main loop ------------------------------------------------------
    def run(self, duration: Optional[float] = None) -> None:
        started = time.perf_counter()
        deadline = started + duration if duration else None

        with h.open_device(path=self._device_path) as kb:
            kb.enter_custom_mode(brightness=self.brightness)
            self._log(
                f"effect '{self.effect.name}' at {self.fps} fps, "
                f"heartbeat every {self.heartbeat_interval:.0f}s"
            )

            heartbeat = threading.Thread(
                target=self._heartbeat, args=(kb,), name="ajazz-heartbeat", daemon=True
            )
            heartbeat.start()
            try:
                self._loop(kb, started, deadline)
            finally:
                self._stop.set()
                heartbeat.join(timeout=self.heartbeat_interval + 2.0)
                self._restore(kb)

    def _loop(
        self,
        kb: h.AjazzKeyboard,
        started: float,
        deadline: Optional[float],
    ) -> None:
        interval = 1.0 / self.fps
        next_frame = time.perf_counter()
        last_report = next_frame

        while not self._should_stop():
            now = time.perf_counter()
            if deadline is not None and now >= deadline:
                break

            if now < next_frame:
                time.sleep(min(next_frame - now, 0.02))
                continue

            table = self.effect.frame(now - started)
            with self._lock:
                kb.send_keymap(table, delay=0.0)

            self.frames += 1
            next_frame += interval
            if next_frame < now:
                # Fell behind: drop the backlog instead of bursting to catch up.
                next_frame = now + interval

            if now - last_report >= 2.0:
                elapsed = now - started
                self._log(
                    f"{self.frames / elapsed:5.1f} fps  "
                    f"({self.frames} frames, {elapsed:5.1f}s)"
                )
                last_report = now

        elapsed = time.perf_counter() - started
        if elapsed > 0:
            self.achieved_fps = self.frames / elapsed

    def _restore(self, kb: h.AjazzKeyboard) -> None:
        """Teardown: send a static-white frame, sleep, then release the handle.

        The static white frame (``0x07``, mode=STATIC, color=WHITE) switches
        the keyboard out of Custom Mode so it does not stay frozen on the last
        animation frame.  Brightness is a fixed UI level 4 (:data:`RESTORE_BRIGHTNESS`),
        NOT ``self.brightness`` -- teardown must not inherit the animation's
        state.  The 100 ms sleep gives the firmware time to apply the mode
        change before the handle is closed.
        """
        try:
            if self.final == "static":
                with self._lock:
                    kb.set_lighting(
                        h.Mode.STATIC, RESTORE_BRIGHTNESS, RESTORE_SPEED,
                        h.Color.WHITE,
                    )
                time.sleep(0.1)
                self._log("restored Static white lighting")
            elif self.final == "off":
                with self._lock:
                    kb.send_keymap(
                        h.pack_key_table([(0, 0, 0)] * h.KEY_COUNT), delay=0.0
                    )
                time.sleep(0.1)
                self._log("cleared the per-key table")
            elif self.final == "keep":
                return
        except Exception as exc:
            self._log(f"could not restore lighting: {exc}")


def install_stop_handlers(engine: "AnimationEngine") -> None:
    """Trap SIGINT/SIGTERM/SIGBREAK so ``_restore()`` always completes.

    Without this, Ctrl+Break (SIGBREAK) or a SIGTERM would kill the process
    mid-frame and leave the keyboard stuck on the last animation frame.  The
    handler only flags the engine's stop event; ``run()``'s ``finally`` block
    does the actual restore, including the 100 ms hold before the handle
    closes.
    """
    def _graceful(signum, frame) -> None:  # noqa: ANN001 - signal API
        engine.stop()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _graceful)
        except (OSError, ValueError):
            # Some environments (e.g. non-main threads) refuse to install
            # handlers; the stop-file mechanism remains the fallback.
            pass


def restore_white_reset(log: Callable[[str], None] = _log) -> None:
    """Send one Static White frame from *this* process and hold 100 ms.

    Used by the daemon supervisor before force-killing a hung child
    (TerminateProcess is uncatchable, so the child cannot restore itself) and
    as stale-state recovery.  Mirrors :data:`RESTORE_BRIGHTNESS`/SPEED.
    """
    try:
        with h.open_device() as kb:
            kb.send(
                h.build_lighting_packet(
                    h.Mode.STATIC, RESTORE_BRIGHTNESS, RESTORE_SPEED,
                    h.Color.WHITE,
                )
            )
            time.sleep(0.1)
        log("sent Static white reset frame")
    except Exception as exc:
        log(f"hardware reset failed: {exc}")


# --------------------------------------------------------------------------
# Daemon control (start / stop / status)
# --------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.GetExitCodeProcess.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL

    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


def _pid_is_python(pid: int) -> bool:
    """Guard against a recycled PID: only signal processes named python*."""
    if sys.platform != "win32":
        return True

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    k32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    k32.QueryFullProcessImageNameW.restype = wintypes.BOOL

    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        buffer = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buffer))
        if not k32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return False
        return Path(buffer.value).name.lower().startswith("python")
    finally:
        k32.CloseHandle(handle)


def _terminate(pid: int) -> None:
    if sys.platform != "win32":
        os.kill(pid, 15)
        return

    PROCESS_TERMINATE = 0x0001
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k32.TerminateProcess.restype = wintypes.BOOL
    handle = k32.OpenProcess(PROCESS_TERMINATE, False, pid)
    if not handle:
        return
    try:
        k32.TerminateProcess(handle, 1)
    finally:
        k32.CloseHandle(handle)


def read_state() -> Optional[dict]:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def daemon_running() -> tuple[bool, Optional[dict]]:
    state = read_state()
    if not state:
        return False, None
    pid = state.get("pid")
    if isinstance(pid, int) and _pid_alive(pid) and _pid_is_python(pid):
        return True, state
    return False, state


def _cleanup_files() -> None:
    for path in (STATE_FILE, STOP_FILE):
        try:
            path.unlink()
        except OSError:
            pass


def build_daemon_command(
    effect: str,
    fps: int = DEFAULT_FPS,
    brightness: int = h.LEVEL_MAX,
    audio_source: str = "synthetic",
    spatial: bool = False,
    diagonal: bool = False,
) -> list[str]:
    """Command line for the detached animation child process."""
    command = [
        sys.executable,
        str(BASE_DIR / "animation_engine.py"),
        "run",
        "--effect", effect,
        "--fps", str(fps),
        "--brightness", str(brightness),
        "--audio-source", str(audio_source),
        "--stop-file", str(STOP_FILE),
    ]
    if spatial:
        command.append("--spatial")
    if diagonal:
        command.append("--diagonal")
    return command


def start_daemon(
    effect: str,
    fps: int = DEFAULT_FPS,
    brightness: int = h.LEVEL_MAX,
    audio_source: str = "synthetic",
    spatial: bool = False,
    diagonal: bool = False,
    log: Callable[[str], None] = _log,
) -> int:
    running, state = daemon_running()
    if running and state:
        raise h.AjazzError(
            f"an animation daemon is already running (pid {state['pid']}); "
            "use 'cli.py stop' first"
        )

    _cleanup_files()

    command = build_daemon_command(
        effect, fps, brightness, audio_source, spatial=spatial, diagonal=diagonal,
    )

    creationflags = 0
    if sys.platform == "win32":
        creationflags = (
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
        )

    with open(LOG_FILE, "ab") as logfile:
        process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            command,
            cwd=str(BASE_DIR),
            stdin=subprocess.DEVNULL,
            stdout=logfile,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
            close_fds=True,
        )

    state = {
        "pid": process.pid,
        "effect": effect,
        "fps": fps,
        "brightness": brightness,
        "audio_source": audio_source,
        "spatial": spatial,
        "diagonal": diagonal,
        "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "log": str(LOG_FILE),
    }
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

    time.sleep(0.6)
    if process.poll() is not None:
        _cleanup_files()
        raise h.AjazzError(
            f"daemon exited immediately (code {process.returncode}); see {LOG_FILE}"
        )

    log(f"started {effect} at {fps} fps (pid {process.pid})")
    return process.pid


def stop_daemon(timeout: float = 6.0, log: Callable[[str], None] = _log) -> bool:
    running, state = daemon_running()
    if not running or not state:
        _cleanup_files()
        log("no animation daemon is running")
        return False

    pid = int(state["pid"])
    STOP_FILE.write_text("stop\n", encoding="utf-8")

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            break
        time.sleep(0.15)
    else:
        # The child is hung and will never run its own teardown, and
        # TerminateProcess is uncatchable -- restore the board from here
        # BEFORE killing it (P4-T3).
        log(f"pid {pid} did not stop within {timeout:.0f}s; terminating")
        restore_white_reset(log)
        _terminate(pid)

    _cleanup_files()
    log(f"stopped animation daemon (pid {pid})")
    return True


def daemon_status() -> str:
    running, state = daemon_running()
    if not state:
        return "no animation daemon is running"
    if not running:
        return f"stale state for pid {state.get('pid')} (process not running)"
    return (
        f"running: {state.get('effect')} at {state.get('fps')} fps "
        f"(pid {state['pid']}, started {state.get('started')})\n"
        f"log: {state.get('log')}"
    )


# --------------------------------------------------------------------------
# Child-process CLI
# --------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="AJAZZ AK820 animation engine")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="run an effect until stopped")
    run.add_argument("--effect", default="rainbow", choices=EFFECTS)
    run.add_argument("--fps", type=int, default=DEFAULT_FPS, choices=range(MIN_FPS, MAX_FPS + 1))
    run.add_argument("--brightness", type=int, default=h.LEVEL_MAX,
                     choices=range(h.LEVEL_MIN, h.LEVEL_MAX + 1))
    run.add_argument("--audio-source", default="synthetic",
                     choices=("synthetic", "microphone"))
    run.add_argument("--duration", type=float, default=None,
                     help="stop after N seconds (default: until Ctrl+C)")
    run.add_argument("--final", default="static", choices=("static", "off", "keep"),
                     help="lighting state on exit")
    run.add_argument("--stop-file", default=None,
                     help="exit when this file appears (daemon control)")
    run.add_argument("--path", default=None, help="explicit HID interface path")
    run.add_argument("--spatial", action="store_true",
                     help="use physical key positions for spatial effects")
    run.add_argument("--diagonal", action="store_true",
                     help="with --spatial, use x+y diagonal instead of x only")

    args = parser.parse_args(argv)

    if args.cmd != "run":  # pragma: no cover - argparse enforces this
        parser.error("unknown command")

    try:
        effect = create_effect(
            args.effect,
            audio_source=args.audio_source,
            spatial=args.spatial,
            diagonal=args.diagonal,
        )
    except h.AjazzError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    engine = AnimationEngine(
        effect,
        fps=args.fps,
        brightness=args.brightness,
        device_path=args.path,
        final=args.final,
        stop_file=Path(args.stop_file) if args.stop_file else None,
    )
    install_stop_handlers(engine)

    try:
        engine.run(duration=args.duration)
    except h.AjazzError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        engine.stop()
    finally:
        effect.close()
        # Only a daemonised child owns the shared state files; a foreground run
        # must not delete a daemon's bookkeeping.
        if args.stop_file:
            try:
                Path(args.stop_file).unlink()
            except OSError:
                pass
            _cleanup_files()

    print(f"stopped after {engine.frames} frames ({engine.achieved_fps:.1f} fps)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
