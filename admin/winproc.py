# coding: utf-8
"""
Windows 进程工具（管理进程侧，ctypes 实现、零依赖）

- 进程存活与创建时间核对（防 PID 复用误杀）；
- 进程树工作集内存统计（Toolhelp32 快照遍历父子链）；
- ASR 单实例互斥锁探测。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import sys

from core.tools.win_instance import MUTEX_NAME, pid_matches, process_creation_time  # noqa: F401 re-export

__all__ = ['pid_alive', 'tree_working_set', 'asr_mutex_exists', 'pid_matches']

TH32CS_SNAPPROCESS = 0x2


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ('dwSize', wt.DWORD),
        ('cntUsage', wt.DWORD),
        ('th32ProcessID', wt.DWORD),
        ('th32DefaultHeapID', ctypes.POINTER(ctypes.c_ulong)),
        ('th32ModuleID', wt.DWORD),
        ('cntThreads', wt.DWORD),
        ('th32ParentProcessID', wt.DWORD),
        ('pcPriClassBase', ctypes.c_long),
        ('dwFlags', wt.DWORD),
        ('szExeFile', ctypes.c_wchar * 260),
    ]


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform != 'win32':
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    kernel32.CloseHandle(handle)
    return True


def _working_set(pid: int) -> int:
    """单个进程的工作集字节数；失败返回 0"""
    if sys.platform != 'win32':
        return 0
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return 0

    class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [
            ('cb', wt.DWORD),
            ('PageFaultCount', wt.DWORD),
            ('PeakWorkingSetSize', ctypes.c_size_t),
            ('WorkingSetSize', ctypes.c_size_t),
            ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
            ('QuotaPagedPoolUsage', ctypes.c_size_t),
            ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
            ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
            ('PagefileUsage', ctypes.c_size_t),
            ('PeakPagefileUsage', ctypes.c_size_t),
        ]

    counters = PROCESS_MEMORY_COUNTERS()
    counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
    ok = kernel32.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
    kernel32.CloseHandle(handle)
    return counters.WorkingSetSize if ok else 0


def tree_working_set(root_pid: int) -> int:
    """统计根进程及其全部子孙进程的工作集总和（识别 worker 是其子进程）"""
    if sys.platform != 'win32':
        return _working_set(root_pid)
    kernel32 = ctypes.windll.kernel32
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == ctypes.c_void_p(-1).value or not snapshot:
        return _working_set(root_pid)
    parents: dict[int, int] = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            parents[entry.th32ProcessID] = entry.th32ParentProcessID
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)

    wanted = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, ppid in parents.items():
            if ppid in wanted and pid not in wanted:
                wanted.add(pid)
                changed = True

    return sum(_working_set(pid) for pid in wanted if pid_alive(pid))


def asr_mutex_exists() -> bool:
    """探测 ASR 单实例命名互斥锁是否存在（存在即说明 ASR 正在运行）"""
    if sys.platform != 'win32':
        return False
    MUTEX_ALL_ACCESS = 0x1F0001
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenMutexW(MUTEX_ALL_ACCESS, False, MUTEX_NAME)
    if handle:
        kernel32.CloseHandle(handle)
        return True
    return False
