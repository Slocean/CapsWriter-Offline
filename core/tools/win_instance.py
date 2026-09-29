# coding: utf-8
"""
Windows 进程实例登记工具

- 命名互斥锁：防止 ASR 模型被并发启动成两个实例；
- PID 文件：记录 pid 与进程创建时间，供管理进程核对（防 PID 复用误杀）。

非 Windows 平台上函数安全降级（互斥锁退化为 PID 文件检查）。
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import time
from pathlib import Path

MUTEX_NAME = r'Local\CapsWriterOfflineASR'

_MUTEX_ALREADY_EXISTS = 183  # ERROR_ALREADY_EXISTS


class InstanceMutex:
    """进程生命周期内的命名互斥锁；with 语句使用"""

    def __init__(self):
        self._handle = None
        self.acquired = False

    def __enter__(self) -> 'InstanceMutex':
        if sys.platform == 'win32':
            self._handle = ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
            if not self._handle:
                raise OSError('CreateMutexW failed')
            if ctypes.windll.kernel32.GetLastError() == _MUTEX_ALREADY_EXISTS:
                self.acquired = False
            else:
                self.acquired = True
        else:
            self.acquired = True
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()

    def release(self) -> None:
        if self._handle:
            ctypes.windll.kernel32.ReleaseMutex(self._handle)
            ctypes.windll.kernel32.CloseHandle(self._handle)
            self._handle = None


def write_pid_file(path: os.PathLike | str) -> None:
    """写入 {pid, created_at, exe} 供管理进程核对进程身份"""
    data = {
        'pid': os.getpid(),
        'created_at': time.time(),
        'exe': sys.executable,
        'cmd': 'start_server',
    }
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + '.tmp')
    tmp.write_text(json.dumps(data), encoding='utf-8')
    os.replace(tmp, p)


def read_pid_file(path: os.PathLike | str) -> dict | None:
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except Exception:
        return None


def clear_pid_file(path: os.PathLike | str) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


def process_creation_time(pid: int) -> float | None:
    """读取进程创建时间（UTC 秒）；进程不存在返回 None（Windows）"""
    if sys.platform != 'win32':
        return None
    import ctypes.wintypes as wt

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        create = wt.FILETIME()
        exit_ = wt.FILETIME()
        kernel_t = wt.FILETIME()
        user_t = wt.FILETIME()
        if not kernel32.GetProcessTimes(handle,
                                        ctypes.byref(create),
                                        ctypes.byref(exit_),
                                        ctypes.byref(kernel_t),
                                        ctypes.byref(user_t)):
            return None
        # 1601-01-01 起的 100ns → Unix 秒
        stamp = (create.dwHighDateTime << 32) | create.dwLowDateTime
        return stamp / 10_000_000 - 11644473600.0
    finally:
        kernel32.CloseHandle(handle)


def pid_matches(pid: int, expected_created_at: float, tolerance: float = 2.0) -> bool:
    """核对 PID 对应进程的创建时间，防止 PID 被系统复用后误杀新进程"""
    actual = process_creation_time(pid)
    if actual is None:
        return False
    return abs(actual - expected_created_at) <= tolerance
