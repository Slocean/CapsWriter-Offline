"""Low-cost pause detector for desktop dictation.

The detector never sends silent gaps to ASR. It keeps a short onset buffer,
requires sustained speech energy, and closes a phrase after a configurable
pause. A long-phrase cap only prevents unbounded memory use.
"""
from __future__ import annotations

from collections import deque
from typing import Optional
import numpy as np


class PauseSegmenter:
    SAMPLE_RATE = 48000

    def __init__(
        self,
        pause_seconds: float = 0.75,
        pre_roll_seconds: float = 0.18,
        tail_seconds: float = 0.12,
        min_voice_seconds: float = 0.22,
        max_phrase_seconds: float = 45.0,
    ):
        self.pause_samples = int(pause_seconds * self.SAMPLE_RATE)
        self.pre_roll_samples = int(pre_roll_seconds * self.SAMPLE_RATE)
        self.tail_samples = int(tail_seconds * self.SAMPLE_RATE)
        self.min_voice_samples = int(min_voice_seconds * self.SAMPLE_RATE)
        self.max_phrase_samples = int(max_phrase_seconds * self.SAMPLE_RATE)
        self.noise_floor = 0.0015
        self._pre = deque()
        self._pre_samples = 0
        self._frames = []
        self._samples = 0
        self._voice_samples = 0
        self._candidate_samples = 0
        self._silence_samples = 0

    def _is_voice(self, frame: np.ndarray) -> bool:
        mono = frame.mean(axis=1) if frame.ndim == 2 else frame
        rms = float(np.sqrt(np.mean(np.square(mono, dtype=np.float64))))
        threshold = max(0.004, self.noise_floor * 3.0)
        voiced = rms >= threshold
        if not voiced:
            self.noise_floor = min(0.02, max(0.0005, self.noise_floor * 0.97 + rms * 0.03))
        return voiced

    def _remember(self, frame: np.ndarray) -> None:
        self._pre.append(frame)
        self._pre_samples += len(frame)
        while len(self._pre) > 1 and self._pre_samples - len(self._pre[0]) >= self.pre_roll_samples:
            self._pre_samples -= len(self._pre.popleft())

    def _take(self, trailing_silence: int) -> Optional[np.ndarray]:
        if self._voice_samples < self.min_voice_samples:
            result = None
        else:
            audio = np.concatenate(self._frames)
            trim = max(0, trailing_silence - self.tail_samples)
            result = audio[:-trim] if trim else audio
        self._frames = []
        self._samples = 0
        self._voice_samples = 0
        self._candidate_samples = 0
        self._silence_samples = 0
        self._pre.clear()
        self._pre_samples = 0
        return result

    def feed(self, frame: np.ndarray) -> Optional[np.ndarray]:
        if frame.size == 0:
            return None
        voiced = self._is_voice(frame)
        if not self._frames:
            self._remember(frame)
            self._candidate_samples = self._candidate_samples + len(frame) if voiced else 0
            if self._candidate_samples < min(self.min_voice_samples // 2, 2 * len(frame)):
                return None
            self._frames = list(self._pre)
            self._samples = sum(map(len, self._frames))
            self._voice_samples = self._candidate_samples
            self._pre.clear()
            self._pre_samples = 0
            return None

        self._frames.append(frame)
        self._samples += len(frame)
        if voiced:
            self._voice_samples += len(frame)
            self._silence_samples = 0
        else:
            self._silence_samples += len(frame)
        if self._silence_samples >= self.pause_samples:
            return self._take(self._silence_samples)
        if self._samples >= self.max_phrase_samples:
            return self._take(0)
        return None

    def flush(self) -> Optional[np.ndarray]:
        if not self._frames:
            return None
        return self._take(self._silence_samples)
