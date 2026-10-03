# coding: utf-8
"""
录音输出静音 owner 单元测试（注入假后端，不触碰真实设备）

覆盖：混合静音原值恢复、采集设备零触碰、嵌套/重复获取、部分失败、
恢复失败不牵连、枚举失败降级、音量永不改动、兜底强制恢复、
平台/开关 no-op。
"""

import importlib.util
import pathlib
import sys
import time
import unittest
from unittest import mock

module_path = pathlib.Path(__file__).resolve().parents[1] / "core/client/audio/output_mute.py"
_spec = importlib.util.spec_from_file_location("output_mute_under_test", module_path)
output_mute = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = output_mute
_spec.loader.exec_module(output_mute)


class FakeDevice:
    def __init__(self, device_id, muted=False, volume=0.5, fail_get=False, fail_set=False,
                 unreachable=False):
        self.device_id = device_id
        self.name = device_id
        self.muted = muted
        self.volume = volume
        self.fail_get = fail_get
        self.fail_set = fail_set
        self.unreachable = unreachable   # True: 端点已拔出（set_mute 抛不可达）


class FakeEndpointBackend(output_mute.MuteBackend):
    """可编程假后端：播放/采集设备分离、记录每次操作、可注入故障。

    recording_probe 为可选外部探针：每次 set_mute 时采样并记录，
    供生命周期测试断言"静音先于录音状态放行"。
    """

    def __init__(self):
        self.render = {}
        self.capture = {}
        self.ops = []  # (op, device_id, value, probe)
        self.fail_enumerate = False
        self.recording_probe = None

    def add_render(self, device_id, **kwargs):
        self.render[device_id] = FakeDevice(device_id, **kwargs)
        return self.render[device_id]

    def add_capture(self, device_id, **kwargs):
        self.capture[device_id] = FakeDevice(device_id, **kwargs)
        return self.capture[device_id]

    def enumerate_render_endpoints(self):
        self.ops.append(('enumerate', None, None, None))
        if self.fail_enumerate:
            raise RuntimeError('枚举失败（模拟）')
        return [output_mute.EndpointInfo(device_id=d.device_id, name=d.name)
                for d in self.render.values()]

    def get_mute(self, device_id):
        self.ops.append(('get_mute', device_id, None, None))
        device = self.render[device_id]
        if device.fail_get:
            raise RuntimeError(f'{device_id} get_mute 失败（模拟）')
        return device.muted

    def set_mute(self, device_id, muted):
        probe = self.recording_probe() if self.recording_probe is not None else None
        self.ops.append(('set_mute', device_id, bool(muted), probe))
        device = self.render[device_id]
        if device.unreachable:
            raise output_mute.DeviceUnavailableError(f'{device_id} 已拔出（模拟）')
        if device.fail_set:
            raise RuntimeError(f'{device_id} set_mute 失败（模拟）')
        device.muted = bool(muted)

    def get_volume_scalar(self, device_id):
        self.ops.append(('get_volume', device_id, None, None))
        return self.render[device_id].volume


class OwnerBehaviorTests(unittest.TestCase):
    """OutputMuteOwner 会话语义（假后端）"""

    def setUp(self):
        self.backend = FakeEndpointBackend()
        self.owner = output_mute.OutputMuteOwner(backend=self.backend)

    def test_mixed_prior_mute_values_restored_individually(self):
        self.backend.add_render('spk-a', muted=False)
        self.backend.add_render('spk-b', muted=True)   # 用户手动静音的设备
        self.backend.add_render('hdmi-c', muted=False)

        lease = self.owner.acquire('test')
        self.assertFalse(lease.failed)
        self.assertTrue(self.backend.render['spk-a'].muted)
        self.assertTrue(self.backend.render['spk-b'].muted)
        self.assertTrue(self.backend.render['hdmi-c'].muted)

        lease.release()
        self.assertFalse(self.backend.render['spk-a'].muted)
        self.assertTrue(self.backend.render['spk-b'].muted)    # 原本静音 → 不得解除
        self.assertFalse(self.backend.render['hdmi-c'].muted)

        set_targets = {op[1] for op in self.backend.ops if op[0] == 'set_mute'}
        self.assertEqual(set_targets, {'spk-a', 'hdmi-c'})     # spk-b 从未被改动

    def test_capture_endpoints_never_touched(self):
        self.backend.add_render('spk-a', muted=False)
        self.backend.add_capture('mic-1', muted=False)
        mic = self.backend.capture['mic-1']

        lease = self.owner.acquire('test')
        lease.release()

        self.assertFalse(mic.muted)
        touched = {op[1] for op in self.backend.ops
                   if op[0] in ('get_mute', 'set_mute', 'get_volume')}
        self.assertNotIn('mic-1', touched)

    def test_nested_acquire_shares_single_session(self):
        self.backend.add_render('spk-a', muted=False)
        lease_parent = self.owner.acquire('parent')
        lease_child = self.owner.acquire('child')
        self.assertEqual(self.owner.refcount, 2)

        lease_child.release()   # 子层结束：不恢复（父层仍在录音）
        self.assertTrue(self.backend.render['spk-a'].muted)

        lease_parent.release()  # 全部结束：恢复
        self.assertFalse(self.backend.render['spk-a'].muted)

    def test_repeat_start_stop_cycles(self):
        self.backend.add_render('spk-a', muted=False)
        for _ in range(3):
            lease = self.owner.acquire('test')
            self.assertTrue(self.backend.render['spk-a'].muted)
            lease.release()
            self.assertFalse(self.backend.render['spk-a'].muted)
        self.assertEqual(self.owner.refcount, 0)

    def test_lease_release_is_idempotent(self):
        self.backend.add_render('spk-a', muted=False)
        lease = self.owner.acquire('test')
        lease.release()
        lease.release()
        lease.release()
        self.assertFalse(self.backend.render['spk-a'].muted)
        self.assertEqual(self.owner.refcount, 0)

    def test_partial_mute_failure_restores_succeeded_others(self):
        self.backend.add_render('spk-a', muted=False)
        self.backend.add_render('spk-b', muted=False, fail_set=True)

        lease = self.owner.acquire('test')
        self.assertFalse(lease.failed)   # 部分成功也算会话成立
        self.assertTrue(self.backend.render['spk-a'].muted)
        self.assertFalse(self.backend.render['spk-b'].muted)  # 失败设备保持原状

        lease.release()
        self.assertFalse(self.backend.render['spk-a'].muted)  # 成功改动的仍要恢复

    def test_restore_failure_does_not_discard_other_restorations(self):
        dev_a = self.backend.add_render('spk-a', muted=False)
        dev_b = self.backend.add_render('spk-b', muted=False)

        lease = self.owner.acquire('test')
        dev_b.fail_set = True   # 恢复阶段才注入失败
        lease.release()         # 不得抛异常

        self.assertFalse(dev_a.muted)   # B 失败不影响 A 恢复
        self.assertTrue(dev_b.muted)    # B 停留在静音态并已记录警告

    def test_enumerate_failure_yields_failed_lease_then_recovers(self):
        self.backend.add_render('spk-a', muted=False)
        self.backend.fail_enumerate = True

        lease = self.owner.acquire('test')
        self.assertTrue(lease.failed)
        self.assertFalse(self.backend.render['spk-a'].muted)  # 不盲目改动
        lease.release()   # no-op，不抛异常
        self.assertEqual(self.owner.refcount, 0)

        self.backend.fail_enumerate = False
        lease2 = self.owner.acquire('retry')
        self.assertFalse(lease2.failed)
        self.assertTrue(self.backend.render['spk-a'].muted)
        lease2.release()
        self.assertFalse(self.backend.render['spk-a'].muted)

    def test_force_release_all_restores_after_leaked_lease(self):
        self.backend.add_render('spk-a', muted=False)
        lease = self.owner.acquire('test')
        self.assertTrue(self.backend.render['spk-a'].muted)

        self.owner.force_release_all('app-stop')
        self.assertFalse(self.backend.render['spk-a'].muted)

        lease.release()   # 泄漏租约事后释放也必须安全
        self.assertFalse(self.backend.render['spk-a'].muted)

    def test_volume_never_modified(self):
        self.backend.add_render('spk-a', muted=False, volume=0.37)
        self.backend.add_render('spk-b', muted=True, volume=0.9)

        lease = self.owner.acquire('test')
        lease.release()

        self.assertEqual(self.backend.render['spk-a'].volume, 0.37)
        self.assertEqual(self.backend.render['spk-b'].volume, 0.9)

    def test_get_mute_failure_skips_device_without_blind_muting(self):
        self.backend.add_render('spk-a', muted=False)
        self.backend.add_render('spk-b', muted=False, fail_get=True)

        lease = self.owner.acquire('test')
        self.assertFalse(lease.failed)
        self.assertTrue(self.backend.render['spk-a'].muted)
        self.assertFalse(self.backend.render['spk-b'].muted)  # 未知原值 → 不静音

        lease.release()
        self.assertFalse(self.backend.render['spk-b'].muted)  # 也从未被恢复操作触碰


class SingletonApiTests(unittest.TestCase):
    """模块级入口：开关、平台 no-op、进程级单例会话共享"""

    def setUp(self):
        self._prev_owner = output_mute._OWNER
        output_mute._OWNER = None

    def tearDown(self):
        output_mute._OWNER = self._prev_owner

    def test_disabled_switch_returns_none(self):
        backend = FakeEndpointBackend()
        lease = output_mute.acquire_output_mute_lease('t', backend=backend, enabled=False)
        self.assertIsNone(lease)
        self.assertEqual(backend.ops, [])

    def test_unsupported_platform_is_noop(self):
        backend = FakeEndpointBackend()
        with mock.patch.object(output_mute, '_SUPPORTED', False):
            lease = output_mute.acquire_output_mute_lease('t', backend=backend)
            self.assertIsNone(lease)
            output_mute.force_release_output_mute('t')  # 不得抛异常
        self.assertEqual(backend.ops, [])

    def test_singleton_shares_session_across_calls(self):
        backend = FakeEndpointBackend()
        backend.add_render('spk-a', muted=False)
        lease1 = output_mute.acquire_output_mute_lease('t1', backend=backend)
        lease2 = output_mute.acquire_output_mute_lease('t2', backend=backend)
        self.assertIsNotNone(lease1)
        self.assertIsNotNone(lease2)

        lease1.release()
        self.assertTrue(backend.render['spk-a'].muted)  # lease2 仍持有
        lease2.release()
        self.assertFalse(backend.render['spk-a'].muted)

    def test_force_release_global_restores(self):
        backend = FakeEndpointBackend()
        backend.add_render('spk-a', muted=False)
        lease = output_mute.acquire_output_mute_lease('t', backend=backend)
        self.assertTrue(backend.render['spk-a'].muted)

        output_mute.force_release_output_mute('app-stop')
        self.assertFalse(backend.render['spk-a'].muted)
        self.assertIsNotNone(lease)  # 泄漏的租约保持可用（release 安全 no-op）


class WatcherTests(unittest.TestCase):
    """录音期热插拔/外部改动守护（bounded 巡检，假后端确定性驱动）"""

    def setUp(self):
        self.backend = FakeEndpointBackend()
        self.owner = output_mute.OutputMuteOwner(backend=self.backend, watch_interval=0.05)

    def test_hotplugged_endpoint_muted_and_restored(self):
        self.backend.add_render('spk-a', muted=False)
        lease = self.owner.acquire('test')
        # 录音期间插入新输出设备（未静音激活）
        self.backend.add_render('hdmi-new', muted=False)

        self.owner._watch_cycle()

        self.assertTrue(self.backend.render['hdmi-new'].muted)   # 新端点被补静音
        lease.release()
        self.assertFalse(self.backend.render['hdmi-new'].muted)  # 恢复到插入时原值
        self.assertFalse(self.backend.render['spk-a'].muted)

    def test_external_unmute_mid_recording_is_remuted_original_restored(self):
        dev = self.backend.add_render('spk-a', muted=False)
        lease = self.owner.acquire('test')
        self.assertTrue(dev.muted)
        dev.muted = False   # 模拟录音期间被外部解除静音

        self.owner._watch_cycle()

        self.assertTrue(dev.muted)   # 被重新静音
        lease.release()
        self.assertFalse(dev.muted)  # 恢复仍用会话前原值

    def test_originally_muted_device_remute_ends_at_muted(self):
        dev = self.backend.add_render('spk-b', muted=True)   # 会话前就被用户静音
        lease = self.owner.acquire('test')
        dev.muted = False   # 录音期间被外部解除静音

        self.owner._watch_cycle()

        self.assertTrue(dev.muted)   # 重新静音（终态=原态）
        lease.release()
        self.assertTrue(dev.muted)   # 原本静音 → 结束时保持静音，绝不解除

    def test_watcher_inactive_after_release(self):
        self.backend.add_render('spk-a', muted=False)
        lease = self.owner.acquire('test')
        lease.release()
        ops_before = list(self.backend.ops)

        self.backend.add_render('hdmi-new', muted=False)
        self.owner._watch_cycle()   # 会话已结束：巡检必须为 no-op

        self.assertFalse(self.backend.render['hdmi-new'].muted)
        self.assertEqual(self.backend.ops, ops_before)

    def test_watcher_thread_runs_during_session(self):
        self.backend.add_render('spk-a', muted=False)
        lease = self.owner.acquire('test')
        try:
            deadline = time.time() + 3
            while time.time() < deadline and self.owner.refcount == 1 and not self.backend.ops:
                time.sleep(0.02)   # 等待守护线程完成首轮巡检（枚举操作入 ops）
            self.assertTrue(any(op[0] == 'enumerate' for op in self.backend.ops),
                            '守护线程未在会话期间执行巡检')
        finally:
            lease.release()
        # 会话结束后守护线程停止（下轮 wait 返回 True 即退出）
        self.assertIsNone(self.owner._watcher_stop)

    def test_stale_lease_cannot_unmute_new_session(self):
        """force_release_all 之后泄漏的旧租约 release 不得影响新会话"""
        self.backend.add_render('spk-a', muted=False)
        stale = self.owner.acquire('old-session')
        self.owner.force_release_all('reset')       # 清场：旧会话结束
        self.assertFalse(self.backend.render['spk-a'].muted)

        current = self.owner.acquire('new-session')  # 新会话重新静音
        self.assertTrue(self.backend.render['spk-a'].muted)

        stale.release()                              # 旧租约必须 no-op
        self.assertTrue(self.backend.render['spk-a'].muted)

        current.release()                            # 新会话正常恢复
        self.assertFalse(self.backend.render['spk-a'].muted)

    def test_hresult_s_false_is_success(self):
        """COM 成功码 S_FALSE(1) 不得被判为失败；失败码必须抛出"""
        with mock.patch.object(output_mute, '_vtbl_call', return_value=1):
            output_mute._hr_vtbl_call(None, 0, None)   # 不抛
        with mock.patch.object(output_mute, '_vtbl_call',
                               return_value=0x80004005):  # E_FAIL
            with self.assertRaises(OSError):
                output_mute._hr_vtbl_call(None, 0, None)

    def test_release_failure_reports_false_and_pending_retried(self):
        """恢复失败不得谎报成功：保留待办，后续 force_release 重试成功"""
        dev = self.backend.add_render('spk-a', muted=False)
        lease = self.owner.acquire('test')
        self.assertTrue(self.backend.render['spk-a'].muted)
        dev.fail_set = True   # 恢复阶段持续失败

        self.assertFalse(lease.release())        # 必须如实报告未恢复
        self.assertEqual(len(self.owner._pending_restore), 1)

        dev.fail_set = False
        self.assertTrue(self.owner.force_release_all('retry'))   # 重试后成功
        self.assertFalse(dev.muted)              # 恢复到原值
        self.assertEqual(self.owner._pending_restore, {})

    def test_unreachable_device_dropped_without_failing_restore(self):
        """已拔出的端点无需恢复，也不得把恢复整体判为失败"""
        self.backend.add_render('spk-a', muted=False)
        gone = self.backend.add_render('usb-b', muted=False)
        gone.unreachable = True

        lease = self.owner.acquire('test')
        self.assertTrue(lease.release())
        self.assertEqual(self.owner._pending_restore, {})
        self.assertFalse(self.backend.render['spk-a'].muted)

    def test_watcher_failure_does_not_break_session(self):
        self.backend.add_render('spk-a', muted=False)
        lease = self.owner.acquire('test')
        self.backend.fail_enumerate = True
        self.owner._watch_cycle()   # 巡检失败不抛出、不影响会话
        self.assertTrue(self.backend.render['spk-a'].muted)
        lease.release()
        self.assertFalse(self.backend.render['spk-a'].muted)


if __name__ == '__main__':
    unittest.main()
