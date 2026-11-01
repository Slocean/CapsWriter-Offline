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

from core.tools.win_instance import (MUTEX_NAME, pid_matches, process_creation_time,
                                     process_image_path, _kernel32)  # noqa: F401 re-export

__all__ = ['pid_alive', 'tree_working_set', 'asr_mutex_exists', 'pid_matches',
           'process_creation_time', 'process_image_path', 'child_processes']

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


STILL_ACTIVE = 259  # GetExitCodeProcess：进程仍在运行的哨兵退出码

ERROR_INVALID_PARAMETER = 87   # OpenProcess：无此 PID（证实不存在）
ERROR_ACCESS_DENIED = 5        # OpenProcess：存在但无权限查询

# 三态进程状态（A01-read）
PID_STATE_ALIVE = 'alive'      # 确认存活
PID_STATE_EXITED = 'exited'    # 证实退出（句柄可开且退出码非 STILL_ACTIVE，或系统明确无此 PID）
PID_STATE_UNKNOWN = 'unknown'  # 无法核实（OpenProcess 拒绝访问 / 退出码读取失败）


def pid_state(pid: int) -> str:
    """
    三态进程状态（A01-read）：

    - alive：句柄可开且退出码 == STILL_ACTIVE；
    - exited：证实退出——句柄可开且退出码非 STILL_ACTIVE（进程对象仍被句柄
      持有但已终止），或系统明确无此 PID（ERROR_INVALID_PARAMETER）；
    - unknown：无法核实——OpenProcess 拒绝访问或 GetExitCodeProcess 读取
      失败。不得当作已退出（会错误放行 done），也不得据此终止。
    """
    if pid <= 0:
        return PID_STATE_EXITED
    if sys.platform != 'win32':
        try:
            os.kill(pid, 0)
            return PID_STATE_ALIVE
        except ProcessLookupError:
            return PID_STATE_EXITED
        except PermissionError:
            return PID_STATE_ALIVE   # POSIX：EPERM 说明进程存在
        except OSError:
            return PID_STATE_UNKNOWN
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = _kernel32()
    kernel32.GetExitCodeProcess.restype = wt.BOOL
    kernel32.GetExitCodeProcess.argtypes = (wt.HANDLE, ctypes.POINTER(wt.DWORD))
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        kernel32.GetLastError.restype = wt.DWORD
        err = kernel32.GetLastError()
        # 只有系统明确「无此 PID」才可证实退出；权限/其他失败一律无法核实
        return PID_STATE_EXITED if err == ERROR_INVALID_PARAMETER else PID_STATE_UNKNOWN
    try:
        exit_code = wt.DWORD(0)
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return PID_STATE_UNKNOWN   # 读取失败 ≠ 已退出（A01-read）
        return PID_STATE_ALIVE if exit_code.value == STILL_ACTIVE else PID_STATE_EXITED
    finally:
        kernel32.CloseHandle(handle)


def pid_alive(pid: int) -> bool:
    """
    进程是否确认存活（A01 兼容封装）。

    unknown / exited 都返回 False；调用方需要区分「证实退出」与「无法
    核实」（终态核验 fail-closed）时必须用 pid_state，不能用本函数把
    权限/读取失败当成已退出。
    """
    return pid_state(pid) == PID_STATE_ALIVE


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


def child_processes(root_pid: int):
    """
    记录根进程的全部后代（A01）：返回 [(pid, 创建时间), ...]，创建时间与
    PID 一起构成可核对的身份——父进程退出后，避免按复用 PID 误杀其他程序。

    快照时根进程必须存活：根进程不在快照中（已退出）时仍收集 PPID 指向
    root 的后代（A01-root：父在核身与快照之间退出不得丢后代），快照无 root
    且无其后代才返回 []（合法空）；快照本身失败返回 None（**枚举失败 ≠
    空树**，调用方必须当作无法核实）。

    A01 补充：后代创建时间读取失败记为 (pid, None) 不确定身份——不能当作
    不存在（它可能仍存活且不监听任何端口），也绝不据此终止；OS 枚举中途
    失败（错误码非 ERROR_NO_MORE_FILES/0）同样返回 None，部分清单不得
    被当作完整已核实清单。
    """
    if sys.platform != 'win32':
        return []
    kernel32 = ctypes.windll.kernel32
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == ctypes.c_void_p(-1).value or not snapshot:
        return None
    parents: dict[int, int] = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        if not ok:
            return None
        while ok:
            parents[entry.th32ProcessID] = entry.th32ParentProcessID
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        # Process32NextW 返回 FALSE 时只有 ERROR_NO_MORE_FILES（18）/0
        # 是正常遍历结束；其余错误码 = 枚举中断，部分清单不可核实
        kernel32.GetLastError.restype = wt.DWORD
        if kernel32.GetLastError() not in (0, 18):
            return None
    finally:
        kernel32.CloseHandle(snapshot)

    # A01-root：快照无 root 不等于无后代——父进程在核身与快照之间退出时，
    # 后代的 PPID 仍指向已退出的 root，必须继续收集
    wanted: set[int] = set()
    changed = True
    while changed:
        changed = False
        for pid, ppid in parents.items():
            if ppid in wanted or ppid == root_pid:
                if pid not in wanted:
                    wanted.add(pid)
                    changed = True
    if not wanted and root_pid not in parents:
        return []            # 快照无 root 且无 PPID 指向 root 的后代（合法空）
    out = []
    for pid in wanted:
        created = process_creation_time(pid)
        if created is not None:
            out.append((pid, created))
        else:
            # 身份读取失败 ≠ 已退出：记为不确定身份，控制器不得当作
            # 无后代、不得终止未知 PID
            out.append((pid, None))
    return out


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
