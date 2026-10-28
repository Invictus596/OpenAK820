"""Hardware-free tests for mapper_common.py.

Covers the two things that made probes unusable:

* pre-flight must stop a daemon that owns the board, and abort with actionable
  text when it cannot;
* every probe must re-assert ``0x07 mode=18`` before the table, and a
  keep-alive must emit only ``0x0E`` frames while the mapper waits.

Run with:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ajazz_hid as h  # noqa: E402
import animation_engine as ae  # noqa: E402
import mapper_common as mc  # noqa: E402


class _FakeKeyboard:
    """Records wire traffic in the same shapes the real handle accepts."""

    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.custom_mode_calls = 0

    def send(self, packet: bytes) -> int:
        self.sent.append(packet)
        return len(packet)

    def send_keymap(self, table: bytes, commit: bool = True,
                    delay: float = 0.0) -> int:
        total = 0
        for packet in h.build_keymap_chunks(table):
            total += self.send(packet)
        if commit:
            total += self.send(h.build_commit_packet())
        return total

    def enter_custom_mode(self, brightness: int = h.LEVEL_MAX,
                          speed: int = 4) -> int:
        self.custom_mode_calls += 1
        return self.set_lighting(h.Mode.CUSTOM, brightness, speed, h.Color.CUSTOM)

    def set_lighting(self, mode, brightness=h.LEVEL_MAX, speed=4,
                     color=h.Color.CYAN, variant=None) -> int:
        return self.send(h.build_lighting_packet(mode, brightness, speed, color,
                                                 variant))


class TestPreflight(unittest.TestCase):
    def test_no_daemon_proceeds_without_stopping(self) -> None:
        with mock.patch.object(ae, "daemon_running", return_value=(False, None)), \
             mock.patch.object(ae, "stop_daemon") as stop:
            mc.preflight(log=lambda _m: None)
        stop.assert_not_called()

    def test_stale_state_only_logs(self) -> None:
        logs: list[str] = []
        with mock.patch.object(ae, "daemon_running",
                               return_value=(False, {"pid": 4242})), \
             mock.patch.object(ae, "stop_daemon") as stop:
            mc.preflight(log=logs.append)
        stop.assert_not_called()
        self.assertTrue(any("stale" in line for line in logs), logs)

    def test_running_daemon_is_stopped(self) -> None:
        with mock.patch.object(
            ae, "daemon_running",
            side_effect=[(True, {"pid": 111}), (False, None)],
        ), mock.patch.object(ae, "stop_daemon") as stop:
            mc.preflight(log=lambda _m: None, timeout=1.5)
        self.assertEqual(stop.call_count, 1)
        self.assertEqual(stop.call_args.kwargs["timeout"], 1.5)

    def test_unstoppable_daemon_aborts_with_instructions(self) -> None:
        with mock.patch.object(ae, "daemon_running",
                               return_value=(True, {"pid": 111})), \
             mock.patch.object(ae, "stop_daemon"):
            with self.assertRaises(mc.MapperPreflightError) as ctx:
                mc.preflight(log=lambda _m: None)
        message = str(ctx.exception)
        self.assertIn("cli.py stop", message)
        self.assertIn("DeviceDriver.exe", message)

    def test_abort_without_stopping_when_disabled(self) -> None:
        with mock.patch.object(ae, "daemon_running",
                               return_value=(True, {"pid": 111})), \
             mock.patch.object(ae, "stop_daemon") as stop:
            with self.assertRaises(mc.MapperPreflightError):
                mc.preflight(stop_running=False, log=lambda _m: None)
        stop.assert_not_called()


class TestProbe(unittest.TestCase):
    def test_custom_mode_is_reasserted_before_every_probe(self) -> None:
        kb = _FakeKeyboard()
        lock = threading.Lock()
        table = h.build_key_table({12: (255, 255, 255)})

        mc.probe(kb, table, lock, brightness=h.LEVEL_MAX, delay=0.0)
        mc.probe(kb, table, lock, brightness=h.LEVEL_MAX, delay=0.0)

        # Each probe = 0x07 mode=18, six 0xF6..0xFB chunks, one 0x0A commit.
        self.assertEqual(kb.custom_mode_calls, 2)
        self.assertEqual(len(kb.sent), 2 * 8)
        for start in (0, 8):
            frame = kb.sent[start]
            self.assertEqual(frame[5], 0x07)
            self.assertEqual(frame[6], h.Mode.CUSTOM)
            self.assertEqual(
                [p[5] for p in kb.sent[start + 1:start + 7]],
                [0xF6, 0xF7, 0xF8, 0xF9, 0xFA, 0xFB],
            )
            self.assertEqual(kb.sent[start + 7][5], 0x0A)

    def test_enter_custom_can_be_disabled(self) -> None:
        kb = _FakeKeyboard()
        mc.probe(kb, h.build_key_table({0: (1, 2, 3)}), threading.Lock(),
                 brightness=h.LEVEL_MAX, delay=0.0, enter_custom=False)
        self.assertEqual(kb.custom_mode_calls, 0)
        self.assertEqual(kb.sent[0][5], 0xF6)

    def test_brightness_is_forwarded_to_custom_mode(self) -> None:
        kb = _FakeKeyboard()
        mc.probe(kb, h.build_key_table({0: (1, 2, 3)}), threading.Lock(),
                 brightness=3, delay=0.0)
        self.assertEqual(kb.sent[0][7], h.encode_level(3))


class TestKeepalive(unittest.TestCase):
    def test_emits_only_keepalive_frames(self) -> None:
        kb = _FakeKeyboard()
        keepalive = mc.Keepalive(kb, threading.Lock(), interval=0.05,
                                 log=lambda _m: None)
        with keepalive:
            time.sleep(0.2)
        self.assertGreaterEqual(keepalive.beats, 2)
        self.assertEqual(len(kb.sent), keepalive.beats)
        for packet in kb.sent:
            self.assertEqual(len(packet), h.REPORT_SIZE)
            self.assertEqual(packet[5], 0x0E)
            self.assertEqual(packet[6:], bytes(h.REPORT_SIZE - 6))

    def test_stop_is_idempotent_and_thread_exits(self) -> None:
        kb = _FakeKeyboard()
        keepalive = mc.Keepalive(kb, threading.Lock(), interval=0.05,
                                 log=lambda _m: None).start()
        time.sleep(0.08)
        keepalive.stop()
        keepalive.stop()
        self.assertFalse(keepalive._thread and keepalive._thread.is_alive())

    def test_keepalive_shares_the_write_lock(self) -> None:
        """A heartbeat must never land inside a probe's chunk sequence."""
        kb = _FakeKeyboard()
        lock = threading.Lock()
        probe_active = threading.Event()
        violations: list[str] = []

        original = kb.send

        def guarded(packet: bytes) -> int:
            if packet[5] == 0x0E and probe_active.is_set():
                # 0x0E while a probe holds the lock would mean the two writers
                # are not serialised.
                violations.append("keepalive during probe")
            return original(packet)

        kb.send = guarded  # type: ignore[method-assign]
        with mc.Keepalive(kb, lock, interval=0.02, log=lambda _m: None):
            for _ in range(5):
                with lock:
                    probe_active.set()
                    time.sleep(0.03)
                    probe_active.clear()
        self.assertEqual(violations, [])


class TestRestore(unittest.TestCase):
    def test_constants_match_the_engine(self) -> None:
        self.assertEqual(mc.RESTORE_BRIGHTNESS, ae.RESTORE_BRIGHTNESS)
        self.assertEqual(mc.RESTORE_SPEED, ae.RESTORE_SPEED)

    def test_sends_static_white_level_4_and_holds(self) -> None:
        kb = _FakeKeyboard()
        with mock.patch.object(mc.time, "sleep") as sleep:
            mc.restore_static_white(kb, threading.Lock())
        sleep.assert_called_once_with(mc.RESTORE_HOLD)
        self.assertEqual(len(kb.sent), 1)
        packet = kb.sent[0]
        self.assertEqual(packet[5], 0x07)
        self.assertEqual(packet[6], h.Mode.STATIC)
        self.assertEqual(packet[10], h.Color.WHITE)
        self.assertEqual(packet[7], h.encode_level(4))


if __name__ == "__main__":
    unittest.main()
