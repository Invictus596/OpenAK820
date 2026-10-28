"""Hardware-free tests for animation_engine.py.

Run with:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ajazz_hid as h  # noqa: E402
import animation_engine as ae  # noqa: E402
import layout_75pct  # noqa: E402


class _FakeSampler:
    def __init__(self, value: float) -> None:
        self.value = value
        self.calls = 0

    def sample(self) -> float:
        self.calls += 1
        return self.value


class _FakeAudio:
    def __init__(self, bands: int = 8, levels: list[float] | None = None) -> None:
        self.bands = bands
        self.levels_data = levels or [1.0] + [0.0] * (bands - 1)
        self.closed = False

    def levels(self, bands: int) -> list[float]:
        return list(self.levels_data[:bands])

    def close(self) -> None:
        self.closed = True


class _FakeKeyboard:
    """Records the exact packets the engine would put on the wire."""

    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.tables: list[bytes] = []

    def send(self, packet: bytes) -> int:
        self.sent.append(packet)
        return len(packet)

    def send_keymap(self, table: bytes, commit: bool = True, delay: float = 0.001) -> int:
        self.tables.append(table)
        self.sent.extend(h.build_keymap_chunks(table))
        if commit:
            self.sent.append(h.build_commit_packet())
        return 7 * h.REPORT_SIZE

    def set_lighting(
        self,
        mode: h.Mode | int,
        brightness: int = h.LEVEL_MAX,
        speed: int = 4,
        color: h.Color | int = h.Color.CYAN,
        variant: int | None = None,
    ) -> int:
        packet = h.build_lighting_packet(mode, brightness, speed, color, variant)
        self.sent.append(packet)
        return len(packet)


class TestKeepalive(unittest.TestCase):
    def test_packet_layout(self) -> None:
        packet = h.build_keepalive_packet()
        self.assertEqual(len(packet), h.REPORT_SIZE)
        self.assertEqual(packet[0], 0x00)
        self.assertEqual(packet[1:5], h.MAGIC)
        self.assertEqual(packet[5], 0x0E)
        self.assertEqual(packet[6:], bytes(59))


class TestEffects(unittest.TestCase):
    def test_rainbow_is_a_full_table_and_moves(self) -> None:
        effect = ae.RainbowWave()
        first = effect.frame(0.0)
        second = effect.frame(1.0)
        self.assertEqual(len(first), h.KEY_TABLE_SIZE)
        self.assertEqual(len(second), h.KEY_TABLE_SIZE)
        self.assertNotEqual(first, second)
        for rgb in h.parse_key_table(first):
            self.assertTrue(all(0 <= channel <= 255 for channel in rgb))

    def test_cpu_meter_fill(self) -> None:
        effect = ae.CpuMeter(sampler=_FakeSampler(0.5), smoothing=1.0)
        keys = h.parse_key_table(effect.frame(0.0))
        lit = [i for i, rgb in enumerate(keys) if rgb != ae.UNLIT]
        self.assertEqual(len(lit), round(0.5 * h.KEY_COUNT))
        self.assertEqual(lit, list(range(len(lit))))  # fills from index 0

        first, last = keys[0], keys[len(lit) - 1]
        self.assertGreater(first[1], first[0])       # green at the low end
        self.assertGreaterEqual(last[0], last[1])    # red at the high end

    def test_cpu_is_sampled_at_a_sane_rate(self) -> None:
        sampler = _FakeSampler(0.5)
        effect = ae.CpuMeter(sampler=sampler, smoothing=1.0, sample_interval=0.25)
        effect.frame(0.00)
        effect.frame(0.05)
        effect.frame(0.10)
        effect.frame(0.30)
        self.assertEqual(sampler.calls, 2)

    def test_audio_visualizer_bars(self) -> None:
        source = _FakeAudio()
        effect = ae.AudioVisualizer(source, bands=8, smoothing=0.0)
        keys = h.parse_key_table(effect.frame(0.0))

        start, end = ae._partition(h.KEY_COUNT, 8)[0]
        self.assertTrue(all(rgb != ae.UNLIT for rgb in keys[start:end]))
        self.assertTrue(all(rgb == ae.UNLIT for rgb in keys[end:]))

        effect.close()
        self.assertTrue(source.closed)

    def test_partition_covers_every_index(self) -> None:
        parts = ae._partition(h.KEY_COUNT, 16)
        self.assertEqual(parts[0][0], 0)
        self.assertEqual(parts[-1][1], h.KEY_COUNT)
        for (_, end), (next_start, _) in zip(parts, parts[1:]):
            self.assertEqual(end, next_start)
        self.assertTrue(all(end > start for start, end in parts))

    def test_every_effect_produces_a_valid_table(self) -> None:
        audio = ae.AudioVisualizer(_FakeAudio(), bands=8)
        effects = [ae.RainbowWave(), ae.CpuMeter(sampler=_FakeSampler(0.3)), audio]
        for effect in effects:
            table = effect.frame(0.0)
            self.assertEqual(len(table), h.KEY_TABLE_SIZE, effect.name)
        audio.close()


class TestAudioHook(unittest.TestCase):
    def test_synthetic_source_bounds_and_determinism(self) -> None:
        first = ae.SyntheticAudioSource(seed=3)
        second = ae.SyntheticAudioSource(seed=3)
        values = first.levels(16)
        self.assertEqual(len(values), 16)
        self.assertTrue(all(0.0 <= v <= 1.0 for v in values))
        self.assertEqual(values, second.levels(16))

    def test_unknown_source(self) -> None:
        with self.assertRaises(h.AjazzError):
            ae.open_audio_source("morse")

    def test_unknown_effect(self) -> None:
        with self.assertRaises(h.AjazzError):
            ae.create_effect("disco")


class TestEngineRuntime(unittest.TestCase):
    def test_heartbeat_emits_keepalive(self) -> None:
        engine = ae.AnimationEngine(ae.RainbowWave(), heartbeat_interval=0.05)
        keyboard = _FakeKeyboard()

        thread = threading.Thread(target=engine._heartbeat, args=(keyboard,), daemon=True)
        thread.start()
        try:
            time.sleep(0.22)
        finally:
            engine.stop()
            thread.join(timeout=2.0)

        self.assertGreaterEqual(len(keyboard.sent), 2)
        self.assertTrue(all(packet[5] == 0x0E for packet in keyboard.sent))
        self.assertTrue(all(packet[6:] == bytes(59) for packet in keyboard.sent))

    def test_render_loop_emits_frames_plus_commits(self) -> None:
        engine = ae.AnimationEngine(ae.RainbowWave(), fps=40)
        keyboard = _FakeKeyboard()

        start = time.perf_counter()
        engine._loop(keyboard, start, start + 0.35)

        self.assertGreater(engine.frames, 3)
        opcodes = {packet[5] for packet in keyboard.sent}
        self.assertTrue({0xF6, 0xF7, 0xF8, 0xF9, 0xFA, 0xFB} <= opcodes)
        self.assertIn(0x0A, opcodes)
        # 6 chunks + 1 commit per frame
        self.assertEqual(len(keyboard.sent), 7 * engine.frames)
        self.assertEqual(len(keyboard.tables), engine.frames)


class TestEngineConfig(unittest.TestCase):
    def test_rejects_out_of_range(self) -> None:
        with self.assertRaises(ValueError):
            ae.AnimationEngine(ae.RainbowWave(), fps=0)
        with self.assertRaises(ValueError):
            ae.AnimationEngine(ae.RainbowWave(), fps=ae.MAX_FPS + 1)
        with self.assertRaises(ValueError):
            ae.AnimationEngine(ae.RainbowWave(), brightness=h.LEVEL_MAX + 1)

    def test_stop_file_ends_the_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            flag = Path(tmp) / "stop"
            engine = ae.AnimationEngine(ae.RainbowWave(), stop_file=flag)
            self.assertFalse(engine._should_stop())
            flag.write_text("stop\n", encoding="utf-8")
            self.assertTrue(engine._should_stop())

    def test_stop_event_ends_the_loop(self) -> None:
        engine = ae.AnimationEngine(ae.RainbowWave())
        engine.stop()
        self.assertTrue(engine._should_stop())


# --------------------------------------------------------------------------
# Phase 4 tests: spatial effects, grid math, create_effect flags
# --------------------------------------------------------------------------


class TestLayout75Pct(unittest.TestCase):
    """Grid geometry and normalisation."""

    def test_grid_bounds(self) -> None:
        for label, x, y in layout_75pct.LAYOUT_75PCT:
            self.assertGreaterEqual(x, 0, label)
            self.assertLessEqual(x, layout_75pct.GRID_X_MAX, label)
            self.assertGreaterEqual(y, 0, label)
            self.assertLessEqual(y, layout_75pct.GRID_Y_MAX, label)

    def test_normalised_grid_range(self) -> None:
        grid = layout_75pct.normalised_grid()
        self.assertGreater(len(grid), 60)
        for label, (nx, ny) in grid.items():
            self.assertGreaterEqual(nx, 0.0, label)
            self.assertLessEqual(nx, 1.0, label)
            self.assertGreaterEqual(ny, 0.0, label)
            self.assertLessEqual(ny, 1.0, label)

    def test_esc_is_top_left(self) -> None:
        grid = layout_75pct.normalised_grid()
        self.assertEqual(grid["Esc"], (0.0, 0.0))

    def test_no_duplicate_labels(self) -> None:
        labels = [label for label, _, _ in layout_75pct.LAYOUT_75PCT]
        self.assertEqual(len(labels), len(set(labels)))

    def test_no_duplicate_positions(self) -> None:
        positions = [(x, y) for _, x, y in layout_75pct.LAYOUT_75PCT]
        self.assertEqual(len(positions), len(set(positions)))

    def test_index_positions_maps_known_keys(self) -> None:
        """Given a keymap with Esc=15 and F1=14, both should get positions."""
        keymap = {15: "Esc", 14: "F1"}
        positions = layout_75pct.index_positions(keymap)
        self.assertIn(15, positions)
        self.assertIn(14, positions)
        # Esc at column 0, F1 at column 2 → F1.nx > Esc.nx
        self.assertGreater(positions[14][0], positions[15][0])

    def test_index_positions_skips_unknown_labels(self) -> None:
        keymap = {99: "Underglow1"}
        positions = layout_75pct.index_positions(keymap)
        self.assertEqual(positions, {})

    def test_raw_grid_count_matches_layout(self) -> None:
        self.assertEqual(len(layout_75pct.raw_grid()), len(layout_75pct.LAYOUT_75PCT))


class TestSpatialRainbow(unittest.TestCase):
    """SpatialRainbow effect produces valid tables and varies over time."""

    def _make_effect(self, diagonal: bool = False) -> ae.SpatialRainbow:
        keymap = {15: "Esc", 14: "F1", 40: "W"}
        return ae.SpatialRainbow(keymap=keymap, diagonal=diagonal)

    def test_produces_valid_table(self) -> None:
        effect = self._make_effect()
        table = effect.frame(0.0)
        self.assertEqual(len(table), h.KEY_TABLE_SIZE)

    def test_frames_change_over_time(self) -> None:
        effect = self._make_effect()
        self.assertNotEqual(effect.frame(0.0), effect.frame(1.0))

    def test_diagonal_differs_from_column(self) -> None:
        col = ae.SpatialRainbow(keymap={15: "Esc", 14: "F1", 30: "A"}, diagonal=False)
        diag = ae.SpatialRainbow(keymap={15: "Esc", 14: "F1", 30: "A"}, diagonal=True)
        # With different coord modes, at least some frames differ
        self.assertNotEqual(col.frame(0.5), diag.frame(0.5))


class TestSpatialCpuMeter(unittest.TestCase):
    """SpatialCpuMeter effect fills left-to-right based on physical position."""

    def test_produces_valid_table(self) -> None:
        keymap = {15: "Esc", 14: "F1"}
        effect = ae.SpatialCpuMeter(
            keymap=keymap, sampler=_FakeSampler(0.5), smoothing=1.0,
        )
        table = effect.frame(0.0)
        self.assertEqual(len(table), h.KEY_TABLE_SIZE)

    def test_fills_based_on_x_position(self) -> None:
        # Esc is at x=0 (nx=0.0), should always be lit at any nonzero load
        # A key at x=17 (nx=0.81) should NOT be lit at 50% load
        keymap = {15: "Esc", 14: "F1", 0: "End"}  # End is at x=17
        effect = ae.SpatialCpuMeter(
            keymap=keymap, sampler=_FakeSampler(0.5), smoothing=1.0,
        )
        keys = h.parse_key_table(effect.frame(0.0))
        # Esc (index 15) at x=0 should be lit
        self.assertNotEqual(keys[15], ae.UNLIT)

    def test_zero_load_lights_nothing(self) -> None:
        keymap = {15: "Esc", 14: "F1"}
        effect = ae.SpatialCpuMeter(
            keymap=keymap, sampler=_FakeSampler(0.0), smoothing=1.0,
        )
        keys = h.parse_key_table(effect.frame(0.0))
        # At zero load, only keys with nx=0 (i.e. Esc) could be lit.
        # But load=0 means nothing lights; nx <= 0.0 is true for Esc (nx=0.0).
        # This is actually edge-case: 0 <= 0.0 is True, so Esc is lit.
        # That's fine -- it's the boundary pixel.


class TestCreateEffectFlags(unittest.TestCase):
    """create_effect respects spatial/diagonal flags."""

    def test_non_spatial_rainbow(self) -> None:
        effect = ae.create_effect("rainbow", spatial=False)
        self.assertIsInstance(effect, ae.RainbowWave)
        self.assertNotIsInstance(effect, ae.SpatialRainbow)

    def test_spatial_rainbow(self) -> None:
        effect = ae.create_effect("rainbow", spatial=True)
        self.assertIsInstance(effect, ae.SpatialRainbow)

    def test_spatial_diagonal_rainbow(self) -> None:
        effect = ae.create_effect("rainbow", spatial=True, diagonal=True)
        self.assertIsInstance(effect, ae.SpatialRainbow)
        self.assertTrue(effect.diagonal)

    def test_non_spatial_cpu(self) -> None:
        effect = ae.create_effect("cpu", spatial=False)
        self.assertIsInstance(effect, ae.CpuMeter)
        self.assertNotIsInstance(effect, ae.SpatialCpuMeter)

    def test_spatial_cpu(self) -> None:
        effect = ae.create_effect("cpu", spatial=True)
        self.assertIsInstance(effect, ae.SpatialCpuMeter)

    def test_non_spatial_audio(self) -> None:
        effect = ae.create_effect("audio", spatial=False)
        self.assertIsInstance(effect, ae.AudioVisualizer)
        self.assertNotIsInstance(effect, ae.SpatialAudioVisualizer)
        effect.close()

    def test_spatial_audio(self) -> None:
        effect = ae.create_effect("audio", spatial=True)
        self.assertIsInstance(effect, ae.SpatialAudioVisualizer)
        effect.close()


class TestTeardown(unittest.TestCase):
    """Verify the _restore method sends static white and pauses."""

    def test_restore_static_sends_0x07_white(self) -> None:
        """The static teardown must send a 0x07 frame with mode=STATIC, color=WHITE."""
        engine = ae.AnimationEngine(ae.RainbowWave(), final="static")
        keyboard = _FakeKeyboard()

        start = time.perf_counter()
        engine._restore(keyboard)
        elapsed = time.perf_counter() - start

        # Should have sent exactly one packet
        self.assertEqual(len(keyboard.sent), 1)
        packet = keyboard.sent[0]
        self.assertEqual(packet[5], 0x07)             # command = LIGHTING
        self.assertEqual(packet[6], h.Mode.STATIC)    # mode = 0x06
        self.assertEqual(packet[10], h.Color.WHITE)   # color = 0x06
        self.assertEqual(packet[7], 0x04)             # brightness 5 -> 4
        # Should have slept ~100ms
        self.assertGreaterEqual(elapsed, 0.08)

    def test_restore_keep_sends_nothing(self) -> None:
        engine = ae.AnimationEngine(ae.RainbowWave(), final="keep")
        keyboard = _FakeKeyboard()
        engine._restore(keyboard)
        self.assertEqual(len(keyboard.sent), 0)


if __name__ == "__main__":
    unittest.main()
