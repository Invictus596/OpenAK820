#!/usr/bin/env python3
"""mapper_common.py -- shared session plumbing for the two matrix mappers.

``matrix_mapper.py`` (types the label) and ``auto_matrix.py`` (identifies the
key from the keypress) both need the same three guarantees:

**One writer at a time.**
    A running animation daemon owns the board and re-uploads effect frames.
    Mapping while it runs makes each probe flash for a frame before the
    daemon's next write replaces it.  :func:`preflight` stops the daemon, and
    aborts with explicit instructions if it cannot.

**A stable Custom Mode session.**
    The board only displays the uploaded per-key table after the measured
    vendor entry sequence -- ``0x07 mode=0x17``, then ``0x0B``, then ``0x09``
    (see :meth:`ajazz_hid.AjazzKeyboard.enter_custom_mode`).  The firmware also
    leaves that mode after an idle period, which is why the vendor app sends
    ``0x0E`` heartbeats: without them a mapper that enters Custom Mode only
    once at session start begins to fail part-way through, and each later
    probe blinks for a frame before the stored profile returns.
    :func:`probe` therefore re-asserts the whole entry sequence immediately
    before every table upload, and :class:`Keepalive` emits ``0x0E`` every few
    seconds while the mapper waits for a keypress.

**Serialised writes.**
    Every write takes the same :class:`threading.Lock`, so a heartbeat can
    never land between the six ``0xF6``..``0xFB`` chunks of a probe or between
    the last chunk and the ``0x0A`` commit.

This module never opens its own device handle: the caller keeps one handle
open for the whole session (both mappers already do this).
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional

import ajazz_hid as h

#: Seconds between ``0x0E`` heartbeats.  Comfortably below the board's Custom
#: Mode idle window (the vendor app uses 5.02 s; 3 s leaves margin for a
#: missed beat) and far above the time a probe needs.
KEEPALIVE_INTERVAL = 3.0

#: Fixed teardown level, mirroring ``animation_engine.RESTORE_BRIGHTNESS`` /
#: ``RESTORE_SPEED``.  Pinned to 4 so a mapper started at brightness 5 cannot
#: leave the board brighter than the engine's teardown state (P4-T1).
RESTORE_BRIGHTNESS = 4
RESTORE_SPEED = 4

#: How long the board needs to apply a lighting-mode change before the handle
#: may close, matching ``AnimationEngine._restore()``.
RESTORE_HOLD = 0.1

#: Vendor processes that also write lighting state.  Best-effort detection:
#: a warning, not a hard failure (they are sometimes services).
VENDOR_WRITERS = ("devicedriver.exe", "ajazz.exe", "ajazzdriver.exe")

_Log = Callable[[str], None]


class MapperPreflightError(RuntimeError):
    """The keyboard is owned by another writer and mapping must not start."""


def _log(message: str) -> None:  # pragma: no cover - trivial
    print(message)


def _vendor_writers() -> list[str]:
    """Names of running vendor lighting processes (best effort, "" on failure)."""
    import subprocess
    import sys

    if sys.platform != "win32":  # pragma: no cover - Windows-only feature
        return []
    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, errors="replace", timeout=5,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - env
        return []
    found: list[str] = []
    for line in out.stdout.splitlines():
        name = line.split(",", 1)[0].strip().strip('"').lower()
        if name in VENDOR_WRITERS:
            found.append(name)
    return found


def preflight(
    *,
    log: _Log = _log,
    stop_running: bool = True,
    timeout: float = 3.0,
) -> None:
    """Make sure no background writer owns the keyboard before mapping.

    Stops a running animation daemon (the documented conflict: it re-uploads
    effect frames, so probes flash and revert).  If the daemon survives the
    normal stop path it is force-killed by ``stop_daemon`` itself; if it is
    still alive afterwards we refuse to start.

    Raises :class:`MapperPreflightError` with actionable text on failure.
    """
    import animation_engine as ae

    if _vendor_writers():
        log(
            "warning: the AJAZZ vendor driver is running; it can overwrite the "
            "lighting state mid-session. Close DeviceDriver.exe for reliable "
            "mapping."
        )

    running, state = ae.daemon_running()
    if not running:
        if state:
            log(
                f"note: ignoring stale daemon state for pid {state.get('pid')} "
                "(process is not running)"
            )
        return

    pid = state.get("pid") if state else "?"
    if not stop_running:
        raise MapperPreflightError(
            f"an animation daemon is already running (pid {pid}) and owns the "
            "keyboard. Run 'python cli.py stop' and close DeviceDriver.exe, "
            "then try again."
        )

    log(f"stopping animation daemon (pid {pid}) before mapping ...")
    ae.stop_daemon(timeout=timeout, log=log)

    running, _state = ae.daemon_running()
    if running:
        raise MapperPreflightError(
            f"the animation daemon (pid {pid}) did not stop. Run "
            "'python cli.py stop' and close DeviceDriver.exe, then try again."
        )


def probe(
    kb: h.AjazzKeyboard,
    table: bytes,
    lock: threading.Lock,
    *,
    brightness: int = h.LEVEL_MAX,
    delay: float = h.KEYMAP_WRITE_DELAY,
    enter_custom: bool = True,
) -> None:
    """Upload one probe table, re-asserting Custom Mode first.

    The measured entry sequence (``0x07 mode=0x17`` + ``0x0B`` + ``0x09``) is
    deliberately re-sent for *every* probe: the firmware drops Custom Mode
    after an idle period, and once that happens a table upload only flashes for
    a frame before the stored profile returns.  Re-asserting costs three
    65-byte writes and makes the probe independent of how long the user takes
    to answer the previous slot.
    """
    with lock:
        if enter_custom:
            kb.enter_custom_mode(brightness=brightness)
        kb.send_keymap(table, delay=delay)


def restore_static_white(
    kb: h.AjazzKeyboard,
    lock: Optional[threading.Lock] = None,
    *,
    hold: float = RESTORE_HOLD,
) -> None:
    """Return the board to Static White at fixed level 4, then hold.

    The hold matters: closing the handle immediately after the ``0x07`` report
    can drop the last write, leaving the board on the final probe frame.
    """
    if lock is not None:
        with lock:
            kb.set_lighting(
                h.Mode.STATIC, RESTORE_BRIGHTNESS, RESTORE_SPEED, h.Color.WHITE,
            )
    else:
        kb.set_lighting(
            h.Mode.STATIC, RESTORE_BRIGHTNESS, RESTORE_SPEED, h.Color.WHITE,
        )
    time.sleep(hold)


class Keepalive:
    """Background ``0x0E`` heartbeat that keeps the session in Custom Mode.

    Used as a context manager around the whole mapping loop::

        with Keepalive(kb, lock):
            ...probe and wait for keypresses...
    """

    def __init__(
        self,
        kb: h.AjazzKeyboard,
        lock: threading.Lock,
        interval: float = KEEPALIVE_INTERVAL,
        log: _Log = _log,
    ) -> None:
        self._kb = kb
        self._lock = lock
        self._interval = max(0.05, float(interval))
        self._log = log
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.beats = 0

    def start(self) -> "Keepalive":
        if self._thread is not None:
            return self
        self._thread = threading.Thread(
            target=self._run, name="ajazz-keepalive", daemon=True,
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=self._interval + 1.0)
        self._thread = None

    def _run(self) -> None:
        packet = h.build_keepalive_packet()
        while not self._stop.wait(self._interval):
            try:
                with self._lock:
                    self._kb.send(packet)
                self.beats += 1
            except Exception as exc:  # device unplugged: let the probe path report
                self._log(f"keepalive stopped: {exc}")
                return

    def __enter__(self) -> "Keepalive":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()
