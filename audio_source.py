"""audio_source.py -- Real-time Windows audio loopback & FFT spectrum analyzer.

Provides system audio capture via sounddevice (WASAPI input/loopback and DirectSound fallback)
and FFT frequency decomposition (default 16 frequency bands) mapped across physical visual
columns for the 75% layout.
"""

from __future__ import annotations

import logging
import math
import sys
import threading
from typing import Optional, Protocol

import numpy as np

logger = logging.getLogger(__name__)


class AudioSource(Protocol):
    """Protocol for audio spectrum providers."""

    def levels(self, bands: int) -> list[float]:
        ...

    def close(self) -> None:
        ...


class WasapiLoopbackSource:
    """Real-time audio capture using sounddevice and FFT spectrum decomposition.

    Captures live audio from Windows WASAPI input or loopback channels, transforms
    time-domain PCM frames via Fast Fourier Transform (FFT) with Hanning windowing,
    and returns normalized magnitude levels per frequency band in [0.0, 1.0].
    """

    def __init__(
        self,
        samplerate: int = 48000,
        blocksize: int = 1024,
        device: Optional[int | str] = None,
        gain: float = 1.8,
    ) -> None:
        import sounddevice as sd

        self._sd = sd
        self._samplerate = samplerate
        self._blocksize = blocksize
        self._gain = max(0.1, gain)
        self._lock = threading.Lock()
        self._block = np.zeros(blocksize, dtype=np.float32)

        selected_device = device
        channels = 1

        if selected_device is None:
            # Prefer WASAPI input device if available, otherwise default
            try:
                for idx, d in enumerate(sd.query_devices()):
                    if d.get("max_input_channels", 0) > 0 and d.get("hostapi") == 2:  # WASAPI
                        selected_device = idx
                        channels = min(2, d.get("max_input_channels", 1))
                        break
            except Exception:
                selected_device = None

        if selected_device is None:
            # Fallback to default input
            selected_device = sd.default.device[0] if sd.default.device[0] is not None else None

        self._device = selected_device

        def _callback(indata, frames, time_info, status):
            if status:
                pass
            with self._lock:
                if indata.ndim > 1 and indata.shape[1] > 1:
                    # Average channels to mono
                    self._block = np.mean(indata, axis=1).astype(np.float32)
                else:
                    self._block = indata[:, 0].copy() if indata.ndim > 1 else indata.copy()

        extra_settings = None
        if sys.platform == "win32":
            try:
                extra_settings = sd.WasapiSettings(auto_convert=True)
            except Exception:
                extra_settings = None

        try:
            self._stream = sd.InputStream(
                device=self._device,
                channels=channels,
                samplerate=self._samplerate,
                blocksize=self._blocksize,
                extra_settings=extra_settings,
                callback=_callback,
            )
        except Exception:
            # Try without extra_settings or default settings
            self._stream = sd.InputStream(
                device=self._device,
                channels=1,
                samplerate=44100,
                blocksize=self._blocksize,
                callback=_callback,
            )
            self._samplerate = 44100

        self._stream.start()

    def levels(self, bands: int) -> list[float]:
        """Compute FFT frequency band levels normalized to [0.0, 1.0]."""
        with self._lock:
            block = self._block.copy()

        if not block.size or np.all(block == 0):
            return [0.0] * bands

        # Apply Hanning window to reduce spectral leakage
        window = np.hanning(len(block))
        windowed = block * window
        fft_vals = np.abs(np.fft.rfft(windowed))
        freqs = np.fft.rfftfreq(len(block), 1.0 / self._samplerate)

        # Logarithmic band spacing between 50 Hz and 12 kHz (human musical range)
        min_freq = 50.0
        max_freq = min(12000.0, self._samplerate * 0.48)
        band_edges = np.geomspace(min_freq, max_freq, bands + 1)

        result: list[float] = []
        for low, high in zip(band_edges[:-1], band_edges[1:]):
            mask = (freqs >= low) & (freqs < high)
            if mask.any():
                # Root mean square energy in this band
                mag = float(np.sqrt(np.mean(fft_vals[mask] ** 2)))
                # Scale magnitude logarithmically/perceptually
                scaled = (mag * self._gain) / 15.0
                result.append(max(0.0, min(1.0, scaled)))
            else:
                result.append(0.0)

        return result

    def close(self) -> None:
        """Stop and release audio streams."""
        try:
            if hasattr(self, "_stream") and self._stream is not None:
                self._stream.stop()
                self._stream.close()
                self._stream = None
        except Exception:
            pass


def get_audio_source(kind: str = "wasapi", **kwargs) -> AudioSource:
    """Factory function for audio sources."""
    kind = kind.lower().strip()
    if kind in ("wasapi", "real", "real-audio", "loopback", "microphone"):
        return WasapiLoopbackSource(**kwargs)
    elif kind == "synthetic":
        from animation_engine import SyntheticAudioSource
        return SyntheticAudioSource()
    else:
        raise ValueError(f"Unknown audio source kind: {kind}")
