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

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _kernel32():
    import ctypes.wintypes as wt
    kernel32 = ctypes.windll.kernel32
    # 64 位句柄不能按默认 c_int 截断，必须补全原型
    kernel32.OpenProcess.restype = wt.HANDLE
    kernel32.OpenProcess.argtypes = (wt.DWORD, wt.BOOL, wt.DWORD)
    kernel32.CloseHandle.restype = wt.BOOL
    kernel32.CloseHandle.argtypes = (wt.HANDLE,)
    return kernel32


class InstanceMutex:
    """进程生命周期内的命名互斥锁；with 语句使用"""

    def __init__(self):
        self._handle = None
        self.acquired = False

    def __enter__(self) -> 'InstanceMutex':
        if sys.platform == 'win32':
            kernel32 = _kernel32()
            self._handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
            if not self._handle:
                raise OSError('CreateMutexW failed')
            if kernel32.GetLastError() == _MUTEX_ALREADY_EXISTS:
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
    """
    写入 {pid, created_at, exe, cmd} 供管理进程核对进程身份。

    created_at 记录 OS 返回的真实进程创建时间（R06）——管理进程用它和
    GetProcessTimes 比对；这里若写 time.time()，导入/初始化慢的进程
    （偏差 >2s）会被自己的核对逻辑判成身份不符。
    """
    pid = os.getpid()
    created_at = process_creation_time(pid)
    if created_at is None:
        created_at = time.time()  # 非 Windows 或读取失败时的退化值
    # exe 记 OS 报告的进程镜像路径（R06）：venv 重定向启动时 sys.executable
    # 是 shim 路径，与真实镜像不同，核对比对必须以 OS 报告值为准
    image = process_image_path(pid)
    data = {
        'pid': pid,
        'created_at': created_at,
        'exe': image or sys.executable,
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
    """读取进程创建时间（UTC 秒）；进程不存在或读取失败返回 None（Windows）"""
    if sys.platform != 'win32':
        return None
    import ctypes.wintypes as wt

    kernel32 = _kernel32()
    kernel32.GetProcessTimes.restype = wt.BOOL
    kernel32.GetProcessTimes.argtypes = (wt.HANDLE,
                                         ctypes.POINTER(wt.FILETIME),
                                         ctypes.POINTER(wt.FILETIME),
                                         ctypes.POINTER(wt.FILETIME),
                                         ctypes.POINTER(wt.FILETIME))
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
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


def process_image_path(pid: int) -> str | None:
    """读取进程可执行文件完整路径；进程不存在或读取失败返回 None（Windows）"""
    if sys.platform != 'win32':
        return None
    import ctypes.wintypes as wt

    kernel32 = _kernel32()
    kernel32.QueryFullProcessImageNameW.restype = wt.BOOL
    kernel32.QueryFullProcessImageNameW.argtypes = (wt.HANDLE, wt.DWORD,
                                                    wt.LPWSTR, ctypes.POINTER(wt.DWORD))
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        size = wt.DWORD(1024)
        buf = ctypes.create_unicode_buffer(1024)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return None
        return buf.value
    finally:
        kernel32.CloseHandle(handle)


def _norm_path(p: str) -> str:
    import re
    p = re.sub(r'^\\\\\?\\', '', p or '')
    return p.replace('/', '\\').lower()


def pid_matches(pid: int, expected_created_at: float, tolerance: float = 2.0,
                expected_exe: str | None = None) -> bool:
    """
    核对 PID 对应进程的身份，防止 PID 被系统复用后误杀新进程：

    - 创建时间与 PID 文件记录值比对（R06：记录值是 OS 真实创建时间，
      而不是登记时刻，慢启动进程不会被误判）；
    - 提供 expected_exe 时再核对可执行文件路径（大小写/分隔符不敏感）；
      路径读取失败不参与判定（时间核对仍然有效）。
    """
    actual = process_creation_time(pid)
    if actual is None:
        return False
    if abs(actual - expected_created_at) > tolerance:
        return False
    if expected_exe:
        image = process_image_path(pid)
        if image and _norm_path(image) != _norm_path(expected_exe):
            return False
    return True
