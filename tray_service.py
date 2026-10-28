"""tray_service.py -- System Tray Background Service for AJAZZ AK820 Max Plus.

Provides a lightweight Windows system tray icon using ``pystray`` and ``Pillow``.

Menu Options:
- Quick Effect Selector (Rainbow, CPU, Audio)
- Profile Preset Switcher (Static White, WASD Gaming, Spatial Rainbow, etc.)
- Brightness Control (Levels 1–5)
- Clean Teardown Exit (sends 0x07 Static White, 100ms flush, and releases handle)
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw
import pystray

import ajazz_hid as h
from animation_engine import AnimationEngine, create_effect
from profile_manager import LightingProfile, ProfileManager

logger = logging.getLogger(__name__)


def create_tray_icon_image(color: tuple[int, int, int] = (0, 200, 255)) -> Image.Image:
    """Generate a clean 64x64 keyboard icon for the system tray."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Keyboard body outline
    draw.rounded_rectangle([4, 12, 60, 52], radius=6, fill=(35, 39, 46, 255), outline=color, width=3)

    # Key rows grid representation
    key_color = color + (220,)
    for row in range(3):
        y = 20 + row * 9
        for col in range(5):
            x = 11 + col * 9
            draw.rectangle([x, y, x + 6, y + 6], fill=key_color)

    # Spacebar
    draw.rectangle([20, 44, 44, 47], fill=key_color)

    return img


class TrayService:
    """Windows system tray daemon for the AJAZZ AK820 Max Plus."""

    def __init__(self) -> None:
        self.profile_mgr = ProfileManager()
        self.brightness = h.LEVEL_MAX
        self.active_profile_name: str = "Static White"
        self.active_effect_name: Optional[str] = None

        self._engine: Optional[AnimationEngine] = None
        self._engine_thread: Optional[threading.Thread] = None
        self._engine_lock = threading.Lock()

        self._tray_icon: Optional[pystray.Icon] = None
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------
    # Engine & Hardware Lighting Control
    # ------------------------------------------------------------------

    def stop_active_engine(self) -> None:
        """Stop any running host animation engine safely."""
        with self._engine_lock:
            if self._engine is not None:
                self._engine.stop()
                if self._engine_thread and self._engine_thread.is_alive():
                    self._engine_thread.join(timeout=2.0)
                self._engine = None
                self._engine_thread = None

    def apply_profile(self, profile: LightingProfile) -> None:
        """Apply a lighting profile (built-in, custom per-key, or host animation effect)."""
        self.stop_active_engine()
        self.active_profile_name = profile.name
        self.active_effect_name = profile.effect_name
        self.brightness = profile.brightness

        if profile.kind == "effect" and profile.effect_name:
            self._start_effect(
                profile.effect_name,
                fps=profile.effect_fps,
                spatial=profile.spatial,
                diagonal=profile.diagonal,
                audio_source=profile.audio_source,
            )
        elif profile.kind == "custom":
            self._apply_custom_keys(profile)
        else:
            self._apply_builtin(profile)

        if self._tray_icon:
            self._tray_icon.update_menu()

    def _start_effect(
        self,
        name: str,
        fps: int = 30,
        spatial: bool = False,
        diagonal: bool = False,
        audio_source: str = "wasapi",
    ) -> None:
        """Launch an animation engine effect in a background thread."""
        try:
            effect = create_effect(
                name,
                audio_source=audio_source,
                spatial=spatial,
                diagonal=diagonal,
            )
        except Exception as exc:
            logger.error("Failed to create effect %s: %s", name, exc)
            return

        engine = AnimationEngine(
            effect,
            fps=fps,
            brightness=self.brightness,
            final="static",
        )

        def _runner():
            try:
                engine.run()
            except Exception as e:
                logger.error("Engine runner exited: %s", e)
            finally:
                effect.close()

        with self._engine_lock:
            self._engine = engine
            self._engine_thread = threading.Thread(target=_runner, daemon=True)
            self._engine_thread.start()

    def _apply_builtin(self, profile: LightingProfile) -> None:
        """Apply hardware-defined lighting mode (e.g. Static White, Breathing)."""
        try:
            mode_val = h.Mode[profile.mode.upper().replace("-", "_")]
        except KeyError:
            mode_val = h.Mode.STATIC

        try:
            color_val = h.Color[profile.color.upper().replace("-", "_")]
        except KeyError:
            color_val = h.Color.WHITE

        try:
            with h.open_device() as kb:
                kb.set_lighting(
                    mode=mode_val,
                    brightness=self.brightness,
                    speed=profile.speed,
                    color=color_val,
                    variant=profile.variant,
                )
        except Exception as exc:
            logger.error("Error setting built-in mode: %s", exc)

    def _apply_custom_keys(self, profile: LightingProfile) -> None:
        """Apply custom per-key table with WASD/arrow colors."""
        keymap = h.load_keymap_matrix()
        # Build inverted label -> index lookup
        label_to_index: dict[str, int] = {}
        for idx, lbl in keymap.items():
            label_to_index[lbl.casefold()] = idx

        table_dict: dict[int, tuple[int, int, int]] = {}
        for k, rgb in profile.custom_keys.items():
            try:
                idx = int(k)
            except ValueError:
                idx = label_to_index.get(k.casefold())

            if idx is not None and 0 <= idx < h.KEY_COUNT:
                table_dict[idx] = (rgb[0], rgb[1], rgb[2])

        table_bytes = h.build_key_table(table_dict)
        try:
            with h.open_device() as kb:
                kb.enter_custom_mode(brightness=self.brightness)
                kb.send_keymap(table_bytes)
        except Exception as exc:
            logger.error("Error setting custom keys: %s", exc)

    def set_brightness(self, level: int) -> None:
        """Update brightness level across active effect or static lighting."""
        self.brightness = max(h.LEVEL_MIN, min(h.LEVEL_MAX, level))
        if self._engine:
            self._engine.brightness = self.brightness
        else:
            prof = self.profile_mgr.get(self.active_profile_name)
            if prof:
                prof.brightness = self.brightness
                if prof.kind != "effect":
                    self.apply_profile(prof)

        if self._tray_icon:
            self._tray_icon.update_menu()

    # ------------------------------------------------------------------
    # Teardown & Exit
    # ------------------------------------------------------------------

    def exit_clean(self) -> None:
        """Perform full teardown: stop effect, restore Static White, 100ms delay, release handle."""
        self._stop_event.set()
        self.stop_active_engine()

        try:
            with h.open_device() as kb:
                kb.set_lighting(h.Mode.STATIC, brightness=4, speed=4, color=h.Color.WHITE)
                time.sleep(0.1)
        except Exception:
            pass

        if self._tray_icon:
            self._tray_icon.stop()

    # ------------------------------------------------------------------
    # Tray Menu Construction
    # ------------------------------------------------------------------

    def _build_menu(self) -> pystray.Menu:
        # Profiles submenu
        profiles = self.profile_mgr.list_profiles()
        profile_items = []
        for p in profiles:
            def _make_prof_cb(prof=p):
                return lambda: self.apply_profile(prof)

            is_active = (p.name == self.active_profile_name)
            profile_items.append(
                pystray.MenuItem(
                    p.name,
                    _make_prof_cb(),
                    checked=lambda item, active=is_active: active,
                )
            )

        # Quick Effects submenu
        def _make_effect_cb(eff_name: str, spatial: bool = True):
            return lambda: self.apply_profile(
                LightingProfile(
                    name=eff_name.capitalize(),
                    kind="effect",
                    effect_name=eff_name,
                    brightness=self.brightness,
                    spatial=spatial,
                )
            )

        effects_items = [
            pystray.MenuItem("Spatial Rainbow Wave", _make_effect_cb("rainbow", spatial=True)),
            pystray.MenuItem("Spatial Rainbow (Diagonal)", lambda: self.apply_profile(
                LightingProfile(
                    name="Rainbow Diagonal",
                    kind="effect",
                    effect_name="rainbow",
                    brightness=self.brightness,
                    spatial=True,
                    diagonal=True,
                )
            )),
            pystray.MenuItem("CPU Load Meter", _make_effect_cb("cpu", spatial=True)),
            pystray.MenuItem("WASAPI Audio Visualizer", _make_effect_cb("audio", spatial=True)),
        ]

        # Brightness submenu
        brightness_items = []
        for lvl in range(h.LEVEL_MAX, h.LEVEL_MIN - 1, -1):
            def _make_bright_cb(b=lvl):
                return lambda: self.set_brightness(b)

            is_cur_b = (lvl == self.brightness)
            brightness_items.append(
                pystray.MenuItem(
                    f"Level {lvl}",
                    _make_bright_cb(),
                    checked=lambda item, cur=is_cur_b: cur,
                )
            )

        menu = pystray.Menu(
            pystray.MenuItem("AJAZZ AK820 Max Plus", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quick Effects", pystray.Menu(*effects_items)),
            pystray.MenuItem("Lighting Profiles", pystray.Menu(*profile_items)),
            pystray.MenuItem("Brightness", pystray.Menu(*brightness_items)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Reset to Static White", lambda: self.apply_profile(
                LightingProfile(name="Static White", kind="built-in", mode="static", color="white", brightness=5)
            )),
            pystray.MenuItem("Exit (Clean Teardown)", lambda: self.exit_clean()),
        )
        return menu

    def run(self) -> None:
        """Run the system tray event loop."""
        icon_img = create_tray_icon_image()
        self._tray_icon = pystray.Icon(
            name="AjazzAK820",
            icon=icon_img,
            title="AJAZZ AK820 Max Plus Lighting Control",
            menu=self._build_menu,
        )

        # Apply initial default profile (Static White)
        default_prof = self.profile_mgr.get("Static White")
        if default_prof:
            self.apply_profile(default_prof)

        self._tray_icon.run()


def main() -> int:
    service = TrayService()
    try:
        service.run()
    except KeyboardInterrupt:
        service.exit_clean()
    return 0


if __name__ == "__main__":
    sys.exit(main())
