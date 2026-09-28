# coding: utf-8
"""Apply cumulative recognition updates to the application being dictated into."""
from __future__ import annotations

import ctypes
import sys
from dataclasses import dataclass
from typing import Callable, Optional, Tuple


def edit_delta(previous: str, current: str) -> Tuple[int, str]:
    """Return the number of trailing characters to remove and text to append."""
    shared = 0
    for old, new in zip(previous, current):
        if old != new:
            break
        shared += 1
    return len(previous) - shared, current[shared:]


def foreground_window() -> Optional[int]:
    if sys.platform != "win32":
        return None
    return ctypes.windll.user32.GetForegroundWindow() or None


@dataclass
class LiveOutputSession:
    task_id: str
    writer: Callable[[str], None]
    backspace: Callable[[], None]
    foreground: Callable[[], Optional[int]] = foreground_window
    written: str = ""
    target_window: Optional[int] = None
    blocked: bool = False

    def update(self, text: str) -> bool:
        """Write a revision only while the original target remains foreground."""
        if self.blocked:
            return False
        active = self.foreground()
        if not active:
            return False
        if self.target_window is None:
            self.target_window = active
        elif active != self.target_window:
            self.blocked = True
            return False
        remove, append = edit_delta(self.written, text)
        if remove > 200:
            self.blocked = True
            return False
        for _ in range(remove):
            self.backspace()
        if append:
            self.writer(append)
        self.written = text
        return True
