# coding: utf-8
"""Apply cumulative recognition updates to the application being dictated into."""
from __future__ import annotations

import ctypes
import sys
from difflib import SequenceMatcher
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


MAX_BACKSPACES = 16


def bounded_edit(previous: str, current: str, final: bool = False) -> Tuple[int, str, bool]:
    """Update at most a short tail; preserve already typed paragraphs.

    Final formatting can insert spaces or punctuation near the beginning.
    In that case a longest-common-prefix diff would erase the whole utterance.
    """
    remove, append = edit_delta(previous, current)
    if final and len(current) < len(previous) and remove:
        return 0, "", True
    if remove <= MAX_BACKSPACES:
        return remove, append, False
    blocks = SequenceMatcher(None, previous, current, autojunk=False).get_matching_blocks()
    for match in reversed(blocks[:-1]):
        old_tail = len(previous) - match.a - match.size
        if match.size >= 3 and old_tail <= MAX_BACKSPACES:
            suffix = current[match.b + match.size:]
            if old_tail and not suffix:
                return 0, "", True
            return old_tail, suffix, True
    return 0, "", True


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
    revision_skipped: bool = False

    def update(self, text: str, final: bool = False) -> bool:
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
        remove, append, self.revision_skipped = bounded_edit(self.written, text, final=final)
        for _ in range(remove):
            self.backspace()
        if append:
            self.writer(append)
        if not self.revision_skipped:
            self.written = text
        else:
            self.written = self.written[:-remove] + append if remove else self.written + append
        return True
