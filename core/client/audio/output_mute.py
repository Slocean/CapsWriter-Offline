# coding: utf-8
"""
录音输出静音模块

录音开始（快捷键按下/单击触发/桌面按钮 UDP START）时立即静音本机全部
活动播放设备（扬声器/耳机/HDMI 等），录音真正结束（完成/短按取消/
退出清理）时按设备逐台恢复先前的静音状态。

硬性约束：
- 只枚举 eRender（播放）端点；麦克风/采集设备的静音与音量绝不触碰；
- 只改静音位，不碰音量；恢复时恢复每台设备各自的先前值，
  绝不把所有设备一律恢复成"未静音"；
- 进程级唯一 owner + 引用计数：嵌套/重复 acquire 共享同一会话，
  不会把"已被本模块静音"的状态当成原值快照（否则父层先恢复、
  子层后恢复时会把已静音当原值，导致系统永久静音）；
- COM 生命周期自包含：每次操作内部 CoInitializeEx → 创建对象 →
  使用 → Release → CoUninitialize，不跨线程/跨操作缓存 COM 指针，
  键盘钩子线程、UDP 线程、主线程任意线程调用均安全；
- 单台设备失败只记日志：静音失败不阻断录音，恢复失败不影响其余设备；
- 非 Windows 平台整体 no-op。

后端通过构造参数注入，测试用假后端替换（见 tests/test_output_mute_owner.py）。
真实后端用 ctypes 直调 Windows Core Audio（MMDevice/IAudioEndpointVolume），
零第三方依赖，PyInstaller 打包无需额外收集。
"""

from __future__ import annotations

import atexit
import ctypes
import logging
import sys
import threading
import uuid
from ctypes import (POINTER, Structure, byref, c_float, c_int, c_uint,
                    c_void_p, c_wchar_p)
from dataclasses import dataclass
from typing import Dict, List, Optional

# ctypes.HRESULT / ctypes.WINFUNCTYPE 仅 Windows 版 ctypes 提供；
# 非 Windows 平台用等价替身占位（永不参与调用），保证模块可导入、整体 no-op。
try:  # pragma: no cover - 平台相关分支
    from ctypes import HRESULT, WINFUNCTYPE
except ImportError:  # pragma: no cover - 非 Windows
    HRESULT = ctypes.c_long
    WINFUNCTYPE = ctypes.CFUNCTYPE

# 与 core.logger.get_logger('client') 是同一个标准库 logger 对象；
# 本模块保持仅依赖 stdlib，便于测试按文件路径独立加载。
logger = logging.getLogger('client')

_SUPPORTED = sys.platform == 'win32'

# —— Core Audio 常量 ——
_CLSID_MMDeviceEnumerator = 'BCDE0395-E52F-467C-8E3D-C4579291692E'
_IID_IMMDeviceEnumerator = 'A95664D2-9614-4F35-A746-DE8DB63617E6'
_IID_IAudioEndpointVolume = '5CDF2C82-841E-4546-9722-0CF74078229A'
_DATA_FLOW_RENDER = 0          # eRender：播放设备（麦克风是 eCapture=1，天然排除）
_DEVICE_STATE_ACTIVE = 0x1     # 只枚举当前活动端点
_CLSCTX_ALL = 0x17
_COINIT_MULTITHREADED = 0x0
_RPC_E_CHANGED_MODE = 0x80010106


class _GUID(Structure):
    _fields_ = [('Data1', c_uint), ('Data2', ctypes.c_ushort),
                ('Data3', ctypes.c_ushort), ('Data4', ctypes.c_ubyte * 8)]


def _guid(text: str) -> _GUID:
    return _GUID.from_buffer_copy(uuid.UUID(text).bytes_le)


_GUID_ENUMERATOR_CLS = _guid(_CLSID_MMDeviceEnumerator)
_GUID_DEVICE_ENUMERATOR = _guid(_IID_IMMDeviceEnumerator)
_GUID_ENDPOINT_VOLUME = _guid(_IID_IAudioEndpointVolume)

# —— vtable 原型（序号来自 MMDevice API / EndpointVolume API 头文件） ——
_PROTO_RELEASE = WINFUNCTYPE(c_uint, c_void_p)
_PROTO_ENUM_ENDPOINTS = WINFUNCTYPE(HRESULT, c_void_p, c_uint, c_uint, c_void_p)
_PROTO_GET_DEVICE = WINFUNCTYPE(HRESULT, c_void_p, c_wchar_p, c_void_p)
_PROTO_COLLECTION_COUNT = WINFUNCTYPE(HRESULT, c_void_p, POINTER(c_uint))
_PROTO_COLLECTION_ITEM = WINFUNCTYPE(HRESULT, c_void_p, c_uint, c_void_p)
_PROTO_ACTIVATE = WINFUNCTYPE(HRESULT, c_void_p, POINTER(_GUID), c_uint, c_void_p, c_void_p)
_PROTO_GET_ID = WINFUNCTYPE(HRESULT, c_void_p, POINTER(c_void_p))
_PROTO_SET_MUTE = WINFUNCTYPE(HRESULT, c_void_p, c_int, c_void_p)
_PROTO_GET_MUTE = WINFUNCTYPE(HRESULT, c_void_p, POINTER(c_int))
_PROTO_GET_VOLUME = WINFUNCTYPE(HRESULT, c_void_p, POINTER(c_float))

# IUnknown::Release 固定在 vtable 序号 2；以下为各接口业务方法的原始槽位
# （IUnknown 占 0-2，业务方法从 3 起——IAudioEndpointVolume 的头文件方法
# 序号需整体 +3，例如 GetMute 是头文件第 13 个方法 → 槽位 15）
_VTBL_RELEASE = 2
_VTBL_ENUM_ENDPOINTS = 3          # IMMDeviceEnumerator::EnumAudioEndpoints
_VTBL_GET_DEVICE = 5              # IMMDeviceEnumerator::GetDevice
_VTBL_COLLECTION_COUNT = 3        # IMMDeviceCollection::GetCount
_VTBL_COLLECTION_ITEM = 4         # IMMDeviceCollection::Item
_VTBL_ACTIVATE = 3                # IMMDevice::Activate
_VTBL_GET_ID = 5                  # IMMDevice::GetId
_VTBL_SET_MUTE = 14               # IAudioEndpointVolume::SetMute
_VTBL_GET_MUTE = 15               # IAudioEndpointVolume::GetMute
_VTBL_GET_VOLUME = 9              # IAudioEndpointVolume::GetMasterVolumeLevelScalar

_ole32 = ctypes.OleDLL('ole32') if _SUPPORTED else None
if _ole32 is not None:
    # 注意：必须用独立的 OleDLL 实例。oledll.ole32 是进程级缓存单例，
    # 其函数对象的 argtypes 会被本模块的多个加载副本互相覆盖
    # （打包内为同一份，测试按文件路径加载时会有多个副本），
    # 独立实例保证 argtypes 与本副本的 _GUID 类严格配对。
    _ole32.CoInitializeEx.argtypes = [c_void_p, c_uint]
    _ole32.CoTaskMemFree.argtypes = [c_void_p]
    _ole32.CoCreateInstance.argtypes = [POINTER(_GUID), c_void_p, c_uint,
                                        POINTER(_GUID), POINTER(c_void_p)]


@dataclass(frozen=True)
class EndpointInfo:
    """活动播放端点标识（device_id 为 Windows 稳定设备 ID）"""
    device_id: str
    name: str = ''


@dataclass(frozen=True)
class EndpointSnapshot:
    """静音前留存的端点原值；只有被本模块改动过的端点才会进入快照"""
    device_id: str
    name: str
    was_muted: bool


def _vtbl_call(this: c_void_p, index: int, proto, *args):
    """按 vtable 序号直调 COM 接口方法，返回原始返回值"""
    vtbl = ctypes.cast(this, ctypes.POINTER(c_void_p)).contents.value
    addr = ctypes.cast(
        ctypes.c_void_p(vtbl + index * ctypes.sizeof(c_void_p)),
        ctypes.POINTER(c_void_p),
    ).contents.value
    return proto(addr)(this, *args)


def _hr_vtbl_call(this: c_void_p, index: int, proto, *args, what: str = 'COM call'):
    """同 _vtbl_call，但校验 HRESULT：仅失败（最高位为 1）时抛 OSError。

    S_OK(0) 与 S_FALSE(1) 等成功码一律放行。
    """
    hr = _vtbl_call(this, index, proto, *args)
    if hr & 0x80000000:
        raise OSError(f'{what} 失败: hr=0x{hr & 0xFFFFFFFF:08X}')


def _release_com(ptr: Optional[c_void_p]) -> None:
    if ptr:
        try:
            _vtbl_call(ptr, _VTBL_RELEASE, _PROTO_RELEASE)
        except Exception:
            pass


class DeviceUnavailableError(RuntimeError):
    """播放端点已不存在（拔出/禁用）：其静音状态无从也无需恢复"""


class _ComSession:
    """自包含 COM 作用域：进入时 CoInitializeEx，退出时配对 CoUninitialize。

    线程已按其他套间模型初始化（RPC_E_CHANGED_MODE）时沿用现有套间，
    本次不增加引用也就不做 CoUninitialize；接口指针全部在作用域内
    创建并 Release，绝不跨作用域使用。
    """

    def __enter__(self) -> '_ComSession':
        self._uninit = False
        try:
            _ole32.CoInitializeEx(None, _COINIT_MULTITHREADED)
            self._uninit = True   # S_OK / S_FALSE 都增加了引用计数
        except OSError as e:
            # ctypes 报告的 winerror 是带符号 int，必须先归一化到无符号 HRESULT
            winerror = getattr(e, 'winerror', None)
            if winerror is None or (winerror & 0xFFFFFFFF) != _RPC_E_CHANGED_MODE:
                raise
            # 线程已按其他套间模型初始化：沿用现有套间，本次未增加引用，
            # 不配对 CoUninitialize；接口指针仍全部在本作用域内用完即释
        return self

    def __exit__(self, *exc) -> bool:
        if self._uninit:
            try:
                _ole32.CoUninitialize()
            except Exception:
                pass
        return False

    def create_instance(self, clsid: _GUID, iid: _GUID) -> c_void_p:
        out = c_void_p()
        _ole32.CoCreateInstance(byref(clsid), None, _CLSCTX_ALL, byref(iid), byref(out))
        return out


class MuteBackend:
    """播放设备静音后端接口（真实实现为 CoreAudioBackend，测试注入假后端）。

    所有方法各自自包含（内部处理 COM 初始化），可从任意线程调用；
    set_mute 只允许操作静音位，任何实现都不得改动音量或采集设备。
    """

    def enumerate_render_endpoints(self) -> List[EndpointInfo]:
        raise NotImplementedError

    def get_mute(self, device_id: str) -> bool:
        raise NotImplementedError

    def set_mute(self, device_id: str, muted: bool) -> None:
        raise NotImplementedError

    def get_volume_scalar(self, device_id: str) -> float:
        """仅供测试断言音量未被改动；业务代码不得调用"""
        raise NotImplementedError


class CoreAudioBackend(MuteBackend):
    """Windows Core Audio 真实后端（ctypes，零第三方依赖）"""

    def _open_endpoint_volume(self, session: _ComSession, device_id: str) -> c_void_p:
        enumerator = session.create_instance(_GUID_ENUMERATOR_CLS, _GUID_DEVICE_ENUMERATOR)
        device = c_void_p()
        try:
            try:
                _hr_vtbl_call(enumerator, _VTBL_GET_DEVICE, _PROTO_GET_DEVICE,
                              device_id, byref(device), what=f'GetDevice({device_id})')
            except OSError as e:
                raise DeviceUnavailableError(f'播放端点不可达: {device_id}: {e}') from e
        finally:
            _release_com(enumerator)
        endpoint = c_void_p()
        try:
            try:
                _hr_vtbl_call(device, _VTBL_ACTIVATE, _PROTO_ACTIVATE,
                              byref(_GUID_ENDPOINT_VOLUME), _CLSCTX_ALL, None, byref(endpoint),
                              what=f'Activate({device_id})')
            except OSError as e:
                raise DeviceUnavailableError(f'播放端点不可达: {device_id}: {e}') from e
        finally:
            _release_com(device)
        return endpoint

    def enumerate_render_endpoints(self) -> List[EndpointInfo]:
        endpoints: List[EndpointInfo] = []
        with _ComSession() as session:
            enumerator = session.create_instance(_GUID_ENUMERATOR_CLS, _GUID_DEVICE_ENUMERATOR)
            collection = c_void_p()
            try:
                _hr_vtbl_call(enumerator, _VTBL_ENUM_ENDPOINTS, _PROTO_ENUM_ENDPOINTS,
                              _DATA_FLOW_RENDER, _DEVICE_STATE_ACTIVE, byref(collection),
                              what='EnumAudioEndpoints(eRender)')
                count = c_uint()
                _hr_vtbl_call(collection, _VTBL_COLLECTION_COUNT, _PROTO_COLLECTION_COUNT,
                              byref(count), what='GetCount')
                for index in range(count.value):
                    device = c_void_p()
                    try:
                        _hr_vtbl_call(collection, _VTBL_COLLECTION_ITEM, _PROTO_COLLECTION_ITEM,
                                      index, byref(device), what=f'Item({index})')
                        device_id_ptr = c_void_p()
                        try:
                            _hr_vtbl_call(device, _VTBL_GET_ID, _PROTO_GET_ID,
                                          byref(device_id_ptr), what='GetId')
                            device_id = ctypes.wstring_at(device_id_ptr.value) if device_id_ptr.value else ''
                        finally:
                            if device_id_ptr.value:
                                _ole32.CoTaskMemFree(device_id_ptr)
                    finally:
                        _release_com(device)
                    if device_id:
                        endpoints.append(EndpointInfo(device_id=device_id))
            finally:
                _release_com(collection)
                _release_com(enumerator)
        return endpoints

    def get_mute(self, device_id: str) -> bool:
        with _ComSession() as session:
            endpoint = c_void_p()
            try:
                endpoint = self._open_endpoint_volume(session, device_id)
                value = c_int()
                _hr_vtbl_call(endpoint, _VTBL_GET_MUTE, _PROTO_GET_MUTE,
                              byref(value), what=f'GetMute({device_id})')
                return bool(value.value)
            finally:
                _release_com(endpoint)

    def set_mute(self, device_id: str, muted: bool) -> None:
        with _ComSession() as session:
            endpoint = c_void_p()
            try:
                endpoint = self._open_endpoint_volume(session, device_id)
                _hr_vtbl_call(endpoint, _VTBL_SET_MUTE, _PROTO_SET_MUTE,
                              1 if muted else 0, None, what=f'SetMute({device_id}, {muted})')
            finally:
                _release_com(endpoint)

    def get_volume_scalar(self, device_id: str) -> float:
        with _ComSession() as session:
            endpoint = c_void_p()
            try:
                endpoint = self._open_endpoint_volume(session, device_id)
                value = c_float()
                _hr_vtbl_call(endpoint, _VTBL_GET_VOLUME, _PROTO_GET_VOLUME,
                              byref(value), what=f'GetMasterVolumeLevelScalar({device_id})')
                return float(value.value)
            finally:
                _release_com(endpoint)


class MuteLease:
    """一次录音会话持有的静音租约；release 幂等且会话隔离

    租约绑定签发时的会话代号：会话被 force_release_all 结束后，泄漏的
    旧租约再 release 也不会影响之后建立的新会话。
    """

    def __init__(self, owner: 'OutputMuteOwner', reason: str,
                 session_id: int = 0, failed: bool = False):
        self._owner = owner
        self._reason = reason
        self._session_id = session_id
        self._failed = failed
        # failed 租约从未建立会话，视为已释放：release 永远 no-op
        self._released = failed

    @property
    def failed(self) -> bool:
        """True 表示未能建立静音会话（如设备枚举失败），录音不受影响"""
        return self._failed

    @property
    def reason(self) -> str:
        return self._reason

    def release(self) -> bool:
        """录音真正结束时调用（完成/取消/清理），恢复各设备原静音状态。

        返回 False 表示仍有可达设备的静音恢复未成功（已保留待重试）。
        """
        if self._released:
            return True
        self._released = True
        return self._owner._release(self)

    def __enter__(self) -> 'MuteLease':
        return self

    def __exit__(self, *exc) -> bool:
        self.release()
        return False


class OutputMuteOwner:
    """进程级唯一静音 owner（引用计数会话 + 录音期热插拔守护）

    - 计数 0→1 时快照并静音；归零时恢复快照中的设备；
    - 已处于静音状态（含用户自己静音的设备）不进快照、恢复时绝不改动，
      因此永远不会把用户手动静音的设备解除静音；
    - 会话期间由 bounded 守护线程周期巡检（同一把锁、同一份快照）：
      新插入/新激活的播放端点被静音并补入快照；录音期间被外部解除
      静音的设备被重新静音；恢复始终使用录音开始前的原值；
    - 部分设备操作失败：成功改动的设备仍会进快照并在恢复阶段逐一恢复。
    """

    def __init__(self, backend: Optional[MuteBackend] = None, watch_interval: float = 0.25):
        self._backend = backend
        self._lock = threading.Lock()
        self._refcount = 0
        self._session_id = 0   # 每次会话建立/强制清场时递增；旧租约因此失效
        self._snapshot: Dict[str, EndpointSnapshot] = {}
        # 会话开始时本就静音、因此未改动也无需恢复的设备：若录音期间被
        # 外部解除静音，守护只需重新静音（终态=原态），不进恢复快照
        self._originally_muted: Dict[str, bool] = {}
        # 恢复失败的可达设备（保留原值，等待后续重试；拔出设备不入此列）
        self._pending_restore: Dict[str, EndpointSnapshot] = {}
        self._watch_interval = watch_interval
        self._watcher: Optional[threading.Thread] = None
        self._watcher_stop: Optional[threading.Event] = None

    def _ensure_backend(self) -> MuteBackend:
        if self._backend is None:
            self._backend = CoreAudioBackend()
        return self._backend

    @property
    def refcount(self) -> int:
        with self._lock:
            return self._refcount

    def acquire(self, reason: str = '') -> MuteLease:
        """获取租约；重复/嵌套获取共享同一会话，不重复快照"""
        with self._lock:
            failed = False
            session_id = self._session_id
            if self._refcount == 0:
                failed = not self._snapshot_and_mute_locked(reason)
                if not failed:
                    self._session_id += 1   # 新会话：作废一切旧租约
                    session_id = self._session_id
                    self._ensure_watcher_locked()
            if not failed:
                self._refcount += 1
        return MuteLease(self, reason, session_id=session_id, failed=failed)

    def _snapshot_and_mute_locked(self, reason: str) -> bool:
        """快照并静音所有活动播放端点；返回 False 仅当无法建立会话"""
        try:
            backend = self._ensure_backend()
            endpoints = backend.enumerate_render_endpoints()
        except Exception as e:
            logger.warning(f"输出静音：枚举播放设备失败（本次录音不静音，不影响识别）: {e}")
            return False

        snapshot: Dict[str, EndpointSnapshot] = {}
        originally_muted: Dict[str, bool] = {}
        for endpoint in endpoints:
            try:
                was_muted = backend.get_mute(endpoint.device_id)
                if not was_muted:
                    backend.set_mute(endpoint.device_id, True)
                    snapshot[endpoint.device_id] = EndpointSnapshot(
                        endpoint.device_id, endpoint.name, was_muted)
                else:
                    originally_muted[endpoint.device_id] = True
            except Exception as e:
                # 该设备保持原状（未知原值时绝不盲目静音/恢复），其余设备继续
                logger.warning(f"输出静音：设备 {endpoint.device_id} 静音失败（跳过）: {e}")
        self._snapshot = snapshot
        self._originally_muted = originally_muted
        logger.info(
            f"输出静音：已静音 {len(snapshot)}/{len(endpoints)} 台活动播放设备 (reason={reason})")
        return True

    def _release(self, lease: MuteLease) -> bool:
        with self._lock:
            # 会话代号不匹配 = 旧会话的泄漏租约，绝不允许它减掉新会话的计数
            if lease._session_id != self._session_id or self._refcount <= 0:
                return True
            self._refcount -= 1
            if self._refcount > 0:
                return True
            self._stop_watcher_locked()
            snapshot, self._snapshot = self._snapshot, {}
            self._originally_muted = {}
            return self._restore_locked(snapshot, lease._reason)

    def _restore_locked(self, snapshot: Dict[str, EndpointSnapshot], reason: str) -> bool:
        """恢复给定设备并连同历史未决设备一并重试；返回是否全部恢复成功。

        - 成功改动的设备逐一恢复到各自原值；
        - 已拔出/禁用的端点（不可达）视为无需恢复并从待办移除；
        - 仍然可达但恢复调用失败的设备立即重试两次，仍失败则保留在
          _pending_restore 中，由后续恢复/清场路径继续重试；
        - 返回 False 表示存在未完成的可达设备恢复（调用方不得据此
          对外宣称"恢复成功"）。
        """
        pending: Dict[str, EndpointSnapshot] = dict(self._pending_restore)
        pending.update(snapshot)
        remaining: Dict[str, EndpointSnapshot] = {}
        restored = dropped = failed = 0
        for device_id, state in pending.items():
            resolved = False
            last_error: Optional[Exception] = None
            for _attempt in range(3):   # 立即重试两次，规避瞬时 COM 竞态
                try:
                    self._ensure_backend().set_mute(device_id, state.was_muted)
                    restored += 1
                    resolved = True
                    break
                except DeviceUnavailableError as e:
                    # 端点已不存在：无值可恢复，从待办移除
                    logger.info(f"输出静音恢复：端点已不存在，跳过 {device_id}: {e}")
                    dropped += 1
                    resolved = True
                    break
                except Exception as e:
                    last_error = e
            if not resolved:
                failed += 1
                remaining[device_id] = state
                logger.warning(
                    f"输出静音恢复：设备 {device_id} 恢复失败（已重试，原静音="
                    f"{state.was_muted}，保留待重试）: {last_error}")
        self._pending_restore = remaining
        logger.info(
            f"输出静音恢复：{restored} 台已恢复，{dropped} 台已拔出跳过，"
            f"{failed} 台待重试 (reason={reason})")
        return not remaining

    def force_release_all(self, reason: str = '') -> bool:
        """无条件清场（应用退出/重置兜底）：作废全部租约并恢复快照。

        返回 False 表示仍有可达设备的恢复未成功（保留待下一次重试）。
        """
        with self._lock:
            self._stop_watcher_locked()
            self._refcount = 0
            self._session_id += 1   # 在场旧租约全部失效
            snapshot, self._snapshot = self._snapshot, {}
            self._originally_muted = {}
            return self._restore_locked(snapshot, reason)

    # —— 录音期热插拔/外部改动守护（bounded：仅会话存续期间运行） ——

    def _ensure_watcher_locked(self) -> None:
        if self._watcher is not None and self._watcher.is_alive():
            return
        stop = threading.Event()
        self._watcher_stop = stop
        watcher = threading.Thread(
            target=self._watch_loop, args=(stop,),
            name='OutputMuteWatcher', daemon=True)
        self._watcher = watcher
        watcher.start()

    def _stop_watcher_locked(self) -> None:
        stop, self._watcher_stop = self._watcher_stop, None
        self._watcher = None
        if stop is not None:
            stop.set()   # 不 join：巡检与恢复由同一把锁串行化，线程自行退出

    def _watch_loop(self, stop: threading.Event) -> None:
        while not stop.wait(self._watch_interval):
            try:
                self._watch_cycle()
            except Exception as e:
                logger.warning(f"输出静音守护巡检异常（本轮跳过）: {e}")

    def _watch_cycle(self) -> None:
        """单次巡检：补静音新端点与被外部解除静音的端点（测试可直接调用）

        与 acquire/release 共用同一把锁：巡检期间发生的恢复会等待本轮
        结束并使用更新后的完整快照；恢复之后启动的巡检会看到归零的
        引用计数并立即退出，杜绝"恢复之后又被补静音"的竞态。
        """
        with self._lock:
            if self._refcount <= 0:
                return
            backend = self._ensure_backend()
            try:
                endpoints = backend.enumerate_render_endpoints()
            except Exception as e:
                logger.debug(f"输出静音守护：本轮枚举播放设备失败: {e}")
                return
            for endpoint in endpoints:
                existing = self._snapshot.get(endpoint.device_id)
                if existing is not None:
                    # 已跟踪端点在录音期间被外部解除静音：重新静音；
                    # 录音结束仍恢复到会话前的原值
                    try:
                        if not backend.get_mute(endpoint.device_id):
                            backend.set_mute(endpoint.device_id, True)
                            logger.info(
                                f"输出静音守护：端点 {endpoint.device_id} "
                                f"录音期间被解除静音，已重新静音")
                    except Exception as e:
                        logger.warning(
                            f"输出静音守护：端点 {endpoint.device_id} 重新静音失败: {e}")
                elif endpoint.device_id in self._originally_muted:
                    # 会话开始时本就静音的设备被外部解除静音：重新静音即可
                    # （终态=原态，无需进恢复快照）
                    try:
                        if not backend.get_mute(endpoint.device_id):
                            backend.set_mute(endpoint.device_id, True)
                            logger.info(
                                f"输出静音守护：端点 {endpoint.device_id} "
                                f"录音期间被解除静音，已重新静音（原态静音）")
                    except Exception as e:
                        logger.warning(
                            f"输出静音守护：端点 {endpoint.device_id} 重新静音失败: {e}")
                else:
                    # 会话开始后才出现/激活的端点：按当前值补快照并静音
                    try:
                        was_muted = backend.get_mute(endpoint.device_id)
                        if not was_muted:
                            backend.set_mute(endpoint.device_id, True)
                            self._snapshot[endpoint.device_id] = EndpointSnapshot(
                                endpoint.device_id, endpoint.name, was_muted)
                            logger.info(
                                f"输出静音守护：会话内新端点 {endpoint.device_id} 已静音")
                    except Exception as e:
                        logger.warning(
                            f"输出静音守护：端点 {endpoint.device_id} 静音失败（跳过）: {e}")


# —— 进程级单例（一个录音进程只有一个 owner，杜绝多 owner 互相快照污染） ——

_OWNER: Optional[OutputMuteOwner] = None
_OWNER_LOCK = threading.Lock()


def acquire_output_mute_lease(
    reason: str = '',
    backend: Optional[MuteBackend] = None,
    enabled: bool = True,
) -> Optional[MuteLease]:
    """录音开始前调用：静音全部活动播放设备并返回租约。

    返回 None 表示本次无需静音（开关关闭/平台不支持/建立会话失败），
    调用方直接继续录音即可；租约须在录音真正结束时 release。
    任何失败都不抛出——静音失败只记日志，绝不阻断录音。
    """
    if not enabled or not _SUPPORTED:
        return None
    global _OWNER
    with _OWNER_LOCK:
        if _OWNER is None:
            _OWNER = OutputMuteOwner(backend)
    try:
        return _OWNER.acquire(reason)
    except Exception as e:
        logger.warning(f"输出静音：建立静音会话失败（本次录音不静音，不影响识别）: {e}")
        return None


def pending_restore_count() -> int:
    """当前留存的未完成静音恢复设备数（0 = 全部恢复成功或无会话）。

    供热重载等需要"确认清理完成"的调用方核实，不把恢复失败谎报为成功。
    """
    with _OWNER_LOCK:
        owner = _OWNER
    if owner is None:
        return 0
    with owner._lock:
        return len(owner._pending_restore)


def force_release_output_mute(reason: str = 'app-exit') -> bool:
    """应用退出/重置兜底：无条件恢复所有被本模块静音的设备。

    返回 False 表示仍有可达设备的恢复未成功（已保留，可再次调用重试）。
    """
    with _OWNER_LOCK:
        owner = _OWNER
    if owner is None:
        return True
    try:
        return owner.force_release_all(reason)
    except Exception as e:
        logger.warning(f"输出静音兜底恢复失败: {e}")
        return False


if _SUPPORTED:
    # 解释器退出兜底：覆盖启动失败、崩溃前未走正常 stop() 等路径
    atexit.register(force_release_output_mute, 'interpreter-exit')
