"""profile_manager.py -- Profile Management System for AJAZZ AK820 Max Plus.

Provides schema validation, default profiles, and persistent storage for user
lighting configurations (mode, brightness, speed, palette color, custom key colors,
and animated effect parameters).

Profiles are persisted in ``profiles.json``.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

import ajazz_hid as h

PROFILES_FILE = Path(__file__).resolve().parent / "profiles.json"


@dataclass
class LightingProfile:
    """Represents a saved lighting configuration or effect preset."""

    name: str
    description: str = ""
    # "built-in" (0x07 mode), "custom" (per-key static RGB), or "effect" (host animation engine)
    kind: str = "built-in"
    # Mode index (0..18) or mode name
    mode: str = "static"
    # UI levels 1..5
    brightness: int = h.LEVEL_MAX
    speed: int = 4
    # Palette color name or index (for built-in modes)
    color: str = "white"
    # Optional variant byte (e.g. 0x01 for Kaleidoscope)
    variant: int = 0
    # For kind == "custom": sparse mapping {int_index_or_label: [r, g, b]}
    custom_keys: dict[str, list[int]] = field(default_factory=dict)
    # For kind == "effect": name of effect ("rainbow", "cpu", "audio")
    effect_name: Optional[str] = None
    effect_fps: int = 30
    spatial: bool = False
    diagonal: bool = False
    audio_source: str = "wasapi"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # Drop None or empty fields for clean JSON
        return {k: v for k, v in d.items() if v is not None}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> LightingProfile:
        name = str(data.get("name", "Unnamed")).strip()
        kind = str(data.get("kind", "built-in")).strip().lower()
        mode = str(data.get("mode", "static")).strip().lower()
        brightness = int(data.get("brightness", h.LEVEL_MAX))
        brightness = max(h.LEVEL_MIN, min(h.LEVEL_MAX, brightness))
        speed = int(data.get("speed", 4))
        speed = max(h.LEVEL_MIN, min(h.LEVEL_MAX, speed))
        color = str(data.get("color", "white")).strip().lower()
        variant = int(data.get("variant", 0))

        custom_keys_raw = data.get("custom_keys", {})
        custom_keys: dict[str, list[int]] = {}
        if isinstance(custom_keys_raw, dict):
            for k, v in custom_keys_raw.items():
                if isinstance(v, (list, tuple)) and len(v) == 3:
                    custom_keys[str(k)] = [max(0, min(255, int(c))) for c in v]

        effect_name = data.get("effect_name")
        if effect_name is not None:
            effect_name = str(effect_name).strip().lower()

        effect_fps = int(data.get("effect_fps", 30))
        spatial = bool(data.get("spatial", False))
        diagonal = bool(data.get("diagonal", False))
        audio_source = str(data.get("audio_source", "wasapi"))

        return cls(
            name=name,
            description=str(data.get("description", "")),
            kind=kind,
            mode=mode,
            brightness=brightness,
            speed=speed,
            color=color,
            variant=variant,
            custom_keys=custom_keys,
            effect_name=effect_name,
            effect_fps=effect_fps,
            spatial=spatial,
            diagonal=diagonal,
            audio_source=audio_source,
        )


#: Default built-in factory presets
DEFAULT_PROFILES: list[LightingProfile] = [
    LightingProfile(
        name="Static White",
        description="Clean static white backlighting across all keys (default safe mode).",
        kind="built-in",
        mode="static",
        brightness=5,
        speed=4,
        color="white",
    ),
    LightingProfile(
        name="WASD Gaming",
        description="Red WASD cluster, blue directional arrow keys, all other keys unlit.",
        kind="custom",
        mode="custom",
        brightness=5,
        speed=4,
        color="custom",
        custom_keys={
            "W": [255, 0, 0],
            "A": [255, 0, 0],
            "S": [255, 0, 0],
            "D": [255, 0, 0],
            "Up": [0, 0, 255],
            "Down": [0, 0, 255],
            "Left": [0, 0, 255],
            "Right": [0, 0, 255],
        },
    ),
    LightingProfile(
        name="Spatial Rainbow Wave",
        description="Smooth real-time rainbow wave traveling across physical columns x in [0..21].",
        kind="effect",
        effect_name="rainbow",
        effect_fps=30,
        brightness=5,
        spatial=True,
        diagonal=False,
    ),
    LightingProfile(
        name="Audio Visualizer",
        description="Real-time WASAPI audio frequency spectrum equalizer mapped across visual columns.",
        kind="effect",
        effect_name="audio",
        effect_fps=30,
        brightness=5,
        spatial=True,
        audio_source="wasapi",
    ),
    LightingProfile(
        name="CPU Load Monitor",
        description="System CPU utilization as a left-to-right fill bar (green to red).",
        kind="effect",
        effect_name="cpu",
        effect_fps=20,
        brightness=5,
        spatial=True,
    ),
]


class ProfileManager:
    """Manages profile loading, saving, deletion, and schema validation."""

    def __init__(self, path: Path | str = PROFILES_FILE) -> None:
        self.path = Path(path)
        self._profiles: dict[str, LightingProfile] = {}
        self.load()

    def _init_defaults(self) -> None:
        """Seed initial default profiles."""
        self._profiles = {p.name: p for p in DEFAULT_PROFILES}

    def load(self) -> dict[str, LightingProfile]:
        """Load profiles from disk or create defaults if file does not exist."""
        if not self.path.exists():
            self._init_defaults()
            self.save_all()
            return self._profiles

        try:
            content = self.path.read_text(encoding="utf-8")
            raw = json.loads(content)
            profiles_data = raw.get("profiles", [])
            loaded: dict[str, LightingProfile] = {}
            for item in profiles_data:
                prof = LightingProfile.from_dict(item)
                loaded[prof.name] = prof

            if not loaded:
                self._init_defaults()
                self.save_all()
            else:
                self._profiles = loaded
        except (OSError, json.JSONDecodeError):
            self._init_defaults()
            self.save_all()

        return self._profiles

    def save_all(self) -> None:
        """Atomically persist all profiles to JSON."""
        doc = {
            "format": 1,
            "profiles": [p.to_dict() for p in self._profiles.values()],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    def list_profiles(self) -> list[LightingProfile]:
        """Return list of all registered profiles."""
        return list(self._profiles.values())

    def get(self, name: str) -> Optional[LightingProfile]:
        """Lookup profile by name (case-insensitive)."""
        key = name.strip().casefold()
        for p in self._profiles.values():
            if p.name.casefold() == key:
                return p
        return None

    def save_profile(self, profile: LightingProfile) -> None:
        """Add or update a profile."""
        self._profiles[profile.name] = profile
        self.save_all()

    def delete_profile(self, name: str) -> bool:
        """Delete a profile by name. Returns True if removed, False if not found."""
        target = None
        key = name.strip().casefold()
        for k, p in self._profiles.items():
            if p.name.casefold() == key:
                target = k
                break
        if target:
            del self._profiles[target]
            self.save_all()
            return True
        return False
