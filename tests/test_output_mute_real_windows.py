# coding: utf-8
"""
真实 Windows 播放设备静音/恢复周期测试

不录音、不模拟键盘鼠标、不做 UI 自动化：直接通过真实 Core Audio
后端（ctypes COM）驱动 OutputMuteOwner 完成"静音→恢复"周期，
并校验：

- 每台活动播放端点的原静音值被逐台恢复（含用户原本静音的设备）；
- 音量标量在前后完全一致（本模块从不改音量）；
- 重复周期结果一致；teardown 兜底恢复，断言失败也不污染系统状态。

本测试会把本机扬声器短暂静音数百毫秒后立即恢复。
"""

import ctypes
import importlib.util
import pathlib
import sys
import threading
import unittest

module_path = pathlib.Path(__file__).resolve().parents[1] / "core/client/audio/output_mute.py"
_spec = importlib.util.spec_from_file_location("output_mute_real", module_path)
output_mute = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = output_mute
_spec.loader.exec_module(output_mute)

_REAL_BACKEND_AVAILABLE = sys.platform == 'win32'


@unittest.skipUnless(_REAL_BACKEND_AVAILABLE, '仅 Windows：需要真实 Core Audio')
class RealWindowsMuteCycleTests(unittest.TestCase):
    def setUp(self):
        if not _REAL_BACKEND_AVAILABLE:
            return
        self.backend = output_mute.CoreAudioBackend()
        self.endpoints = self.backend.enumerate_render_endpoints()
        self._prior = {}  # device_id -> (muted, volume_scalar)
        for endpoint in self.endpoints:
            self._prior[endpoint.device_id] = (
                self.backend.get_mute(endpoint.device_id),
                self.backend.get_volume_scalar(endpoint.device_id),
            )

    def tearDown(self):
        if not _REAL_BACKEND_AVAILABLE:
            return
        # 兜底恢复：即使断言失败也必须把真实设备恢复原状
        owner = getattr(self, '_owner', None)
        if owner is not None:
            owner.force_release_all('test-teardown')
            self._owner = None
        for device_id, (muted, _volume) in self._prior.items():
            try:
                self.backend.set_mute(device_id, muted)
            except Exception:
                pass  # 设备可能已拔出，无法恢复

    def _require_endpoints(self):
        if not self.endpoints:
            self.skipTest('本机无活动播放端点，周期断言无从谈起')

    def test_mute_then_restore_preserves_prior_mute_and_volume(self):
        self._require_endpoints()
        owner = output_mute.OutputMuteOwner(backend=self.backend)
        self._owner = owner

        lease = owner.acquire('real-windows-test')
        self.assertFalse(lease.failed)
        for endpoint in self.endpoints:
            self.assertTrue(self.backend.get_mute(endpoint.device_id),
                            f'{endpoint.device_id} 应被静音')

        lease.release()
        for endpoint in self.endpoints:
            muted, volume = self._prior[endpoint.device_id]
            self.assertEqual(self.backend.get_mute(endpoint.device_id), muted,
                             f'{endpoint.device_id} 的原静音值未被恢复')
            self.assertAlmostEqual(self.backend.get_volume_scalar(endpoint.device_id), volume,
                                   places=6, msg=f'{endpoint.device_id} 音量被改动')

    def test_repeat_real_cycles_are_consistent(self):
        self._require_endpoints()
        owner = output_mute.OutputMuteOwner(backend=self.backend)
        self._owner = owner

        for cycle in range(2):
            lease = owner.acquire(f'real-windows-test-{cycle}')
            self.assertFalse(lease.failed)
            for endpoint in self.endpoints:
                self.assertTrue(self.backend.get_mute(endpoint.device_id))
            lease.release()
            for endpoint in self.endpoints:
                muted, _volume = self._prior[endpoint.device_id]
                self.assertEqual(self.backend.get_mute(endpoint.device_id), muted)

    def test_nested_real_acquires_restore_once(self):
        self._require_endpoints()
        owner = output_mute.OutputMuteOwner(backend=self.backend)
        self._owner = owner

        lease_parent = owner.acquire('real-nested-parent')
        lease_child = owner.acquire('real-nested-child')
        lease_child.release()
        for endpoint in self.endpoints:
            self.assertTrue(self.backend.get_mute(endpoint.device_id))  # 父层仍持有

        lease_parent.release()
        for endpoint in self.endpoints:
            muted, _volume = self._prior[endpoint.device_id]
            self.assertEqual(self.backend.get_mute(endpoint.device_id), muted)

    def test_sta_initialized_thread_full_cycle(self):
        """模拟托盘等已按 STA 初始化 COM 的线程：静音周期必须照常工作"""
        self._require_endpoints()
        errors = []

        def scenario():
            try:
                hr = ctypes.oledll.ole32.CoInitializeEx(None, 2)  # COINIT_APARTMENTTHREADED
                try:
                    owner = output_mute.OutputMuteOwner(backend=self.backend)
                    lease = owner.acquire('real-sta-thread')
                    assert not lease.failed
                    for endpoint in self.endpoints:
                        assert self.backend.get_mute(endpoint.device_id)
                    lease.release()
                    for endpoint in self.endpoints:
                        muted, _volume = self._prior[endpoint.device_id]
                        assert self.backend.get_mute(endpoint.device_id) == muted
                finally:
                    if hr in (0, 1):   # 仅当本次成功初始化才配对释放
                        ctypes.oledll.ole32.CoUninitialize()
            except Exception as e:  # 含 AssertionError，跨线程带回
                errors.append(e)

        thread = threading.Thread(target=scenario, name='sta-mute-test')
        thread.start()
        thread.join(timeout=15)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()
