# coding: utf-8
"""
录音生命周期 × 输出静音接线测试

用真实 ShortcutTask / ShortcutEventHandler / ClientState 驱动完整录音
生命周期（与快捷键、UDP START/STOP、桌面按钮共用的唯一漏斗），
输出静音通过注入假后端验证：

- 静音严格发生在录音状态放行音频采集之前；
- 录音真正结束（完成/短按取消）立即恢复，不等识别完成；
- 启动失败、取消、应用退出兜底都不会把系统静音带走；
- 采集设备零触碰；配置开关可关闭。
"""

import asyncio
import pathlib
import sys
import threading
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from core.client.audio import output_mute as output_mute_pkg
from core.client.shortcut.event_handler import ShortcutEventHandler
from core.client.shortcut.shortcut_config import Shortcut
from core.client.shortcut.task import ShortcutTask
from core.client.state import ClientState
from config_client import ClientConfig

from test_output_mute_owner import FakeEndpointBackend


class _QuietStatus:
    """替换 rich Status 动画，测试输出保持干净"""

    def start(self):
        pass

    def stop(self):
        pass


class FakeRecorder:
    """替代 AudioRecorder：消费队列直到 finish，随后模拟仍在进行的识别"""

    last_instance = None

    def __init__(self, app):
        self.app = app
        self.recognition_done = threading.Event()   # 由测试放行"识别完成"
        self.record_loop_exited = threading.Event()  # 录音循环已结束（不再采集）
        self.recognition_finished = False
        FakeRecorder.last_instance = self

    async def record_and_send(self):
        try:
            while True:
                item = await self.app.state.queue_in.get()
                self.app.state.queue_in.task_done()
                if item['type'] == 'finish':
                    break
        finally:
            self.record_loop_exited.set()
        # 模拟 ASR/LLM 在录音结束后继续（可被取消，不阻塞事件循环）
        deadline = time.time() + 5
        while not self.recognition_done.is_set() and time.time() < deadline:
            await asyncio.sleep(0.01)
        self.recognition_finished = self.recognition_done.is_set()


class _FakeApp:
    def __init__(self, state, loop):
        self.state = state
        self.loop = loop


class RecordingMuteLifecycleTests(unittest.TestCase):
    """真实任务漏斗 × 假后端的静音接线"""

    def setUp(self):
        # 独立事件循环线程，模拟客户端 app.loop
        self.loop = asyncio.new_event_loop()
        self.loop_thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.loop_thread.start()

        self.backend = FakeEndpointBackend()
        self.backend.add_render('spk-a', muted=False)
        self.backend.add_render('spk-b', muted=True)   # 用户手动静音
        self.backend.add_capture('mic-1', muted=False)

        # 进程级 owner 指向假后端（ShortcutTask 惰性导入的就是这个模块单例）
        self._prev_owner = output_mute_pkg._OWNER
        output_mute_pkg._OWNER = output_mute_pkg.OutputMuteOwner(backend=self.backend)

        self.state = ClientState()
        self.app = _FakeApp(self.state, self.loop)
        self.state.app = self.app
        self.shortcut = Shortcut(key='ctrl+alt+space', suppress=True, hold_mode=True)
        self.task = ShortcutTask(self.app, self.shortcut)
        self.task._status = _QuietStatus()
        self.task.threshold = 0.3
        self.task._recorder_class = FakeRecorder
        self.task._manager_ref = lambda: None

    def tearDown(self):
        try:
            recorder = FakeRecorder.last_instance
            if recorder is not None:
                recorder.recognition_done.set()   # 放行所有待完成的"识别"
            if self.task.is_recording:
                self.task.cancel()
            future = self.task.task
            if future is not None:
                try:
                    future.result(timeout=2)
                except Exception:
                    pass
        finally:
            output_mute_pkg._OWNER = self._prev_owner
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.loop_thread.join(timeout=2)
            self.loop.close()

    def _recorder(self) -> FakeRecorder:
        recorder = FakeRecorder.last_instance
        self.assertIsNotNone(recorder)
        return recorder

    def test_launch_mutes_before_capture_state_allows_audio(self):
        self.backend.recording_probe = lambda: self.state.recording

        self.task.launch()

        # 每一次静音写入时，录音状态尚未放行采集（原本已静音的设备不产生写入）
        mute_probes = [op[3] for op in self.backend.ops if op[0] == 'set_mute']
        self.assertEqual(mute_probes, [False])
        self.assertTrue(self.state.recording)
        self.assertTrue(self.backend.render['spk-a'].muted)
        self.assertTrue(self.backend.render['spk-b'].muted)
        self.assertFalse(self.backend.capture['mic-1'].muted)

        self.task.finish()
        self.assertFalse(self.state.recording)
        self.assertFalse(self.backend.render['spk-a'].muted)
        self.assertTrue(self.backend.render['spk-b'].muted)  # 原本静音不被解除

    def test_finish_restores_before_recognition_finishes(self):
        self.task.launch()
        self.task.finish()

        recorder = self._recorder()
        self.assertTrue(recorder.record_loop_exited.wait(timeout=2))
        # 录音循环已结束且恢复已完成，而"识别"尚未被放行
        self.assertFalse(self.backend.render['spk-a'].muted)
        self.assertFalse(recorder.recognition_done.is_set())

        recorder.recognition_done.set()
        self.task.task.result(timeout=5)   # 识别协程正常收尾
        self.assertTrue(recorder.recognition_finished)

    def test_short_press_cancel_restores(self):
        self.task.launch()
        future = self.task.task

        self.task.cancel()
        self.assertFalse(self.state.recording)
        self.assertFalse(self.backend.render['spk-a'].muted)
        self.assertTrue(self.backend.render['spk-b'].muted)

        self.assertTrue(future.cancelled())   # 识别协程已随取消结束

    def test_short_press_via_event_handler(self):
        handler = ShortcutEventHandler({}, None, None)

        handler.handle_keydown('ctrl+alt+space', self.task)   # 按下：长按启动
        self.assertTrue(self.state.recording)
        self.assertTrue(self.backend.render['spk-a'].muted)

        time.sleep(0.01)   # 持续时间 < threshold(0.3s)
        handler.handle_keyup('ctrl+alt+space', self.task)     # 快速松开：取消
        self.assertFalse(self.state.recording)
        self.assertFalse(self.backend.render['spk-a'].muted)

    def test_failed_launch_restores_and_resets_state(self):
        class ExplodingRecorder:
            def __init__(self, app):
                raise RuntimeError('录音器启动失败（模拟）')

        self.task._recorder_class = ExplodingRecorder
        with self.assertRaises(RuntimeError):
            self.task.launch()

        self.assertFalse(self.backend.render['spk-a'].muted)  # 静音不泄漏
        self.assertEqual(output_mute_pkg._OWNER.refcount, 0)
        # 启动失败后录音状态标志一并复位（不残留 is_recording/recording）
        self.assertFalse(self.task.is_recording)
        self.assertFalse(self.state.recording)

    def test_repeated_launch_reuses_lease_without_refcount_leak(self):
        lease = self.task._acquire_mute_lease()
        self.assertIsNotNone(lease)
        # 重复获取（对应异常路径下的重复 launch）复用同一租约
        self.assertIs(self.task._acquire_mute_lease(), lease)
        self.assertEqual(output_mute_pkg._OWNER.refcount, 1)

    def test_recorder_crash_releases_lease_and_resets_state(self):
        class CrashingRecorder:
            def __init__(self, app):
                self.app = app

            async def record_and_send(self):
                raise RuntimeError('识别协程崩溃（模拟）')

        self.task._recorder_class = CrashingRecorder
        self.task.launch()
        # 协程立即崩溃：恢复可能先于断言发生，直接验证最终态

        future = self.task.task
        with self.assertRaises(RuntimeError):
            future.result(timeout=5)
        # 兜底回调在协程异常终止后复位状态并恢复输出
        deadline = time.time() + 3
        while self.task.is_recording and time.time() < deadline:
            time.sleep(0.02)
        self.assertFalse(self.task.is_recording)
        self.assertFalse(self.state.recording)
        self.assertFalse(self.backend.render['spk-a'].muted)
        self.assertEqual(output_mute_pkg._OWNER.refcount, 0)

    def test_recorder_internal_error_without_exception_releases_lease(self):
        class SwallowingRecorder:
            """模拟 record_and_send 内部 except Exception 吞掉错误后正常返回"""

            def __init__(self, app):
                self.app = app

            async def record_and_send(self):
                try:
                    raise RuntimeError('内部错误被吞掉（模拟）')
                except RuntimeError:
                    pass   # 与真实 AudioRecorder 行为一致：记日志后正常返回

        self.task._recorder_class = SwallowingRecorder
        self.task.launch()
        # 协程立即结束：恢复可能先于断言发生，直接验证最终态

        future = self.task.task
        future.result(timeout=5)   # 协程"正常"结束
        deadline = time.time() + 3
        while self.task.is_recording and time.time() < deadline:
            time.sleep(0.02)
        self.assertFalse(self.task.is_recording)
        self.assertFalse(self.backend.render['spk-a'].muted)
        self.assertEqual(output_mute_pkg._OWNER.refcount, 0)

    def test_stale_recorder_callback_does_not_end_newer_recording(self):
        # 旧会话回调迟到时，不得复位/结束已更新的录音会话
        self.task.launch()
        first_generation = self.task._mute_generation
        self.assertTrue(self.task.is_recording)

        # 模拟旧 generation 的兜底回调在新会话进行中触发
        self.task._on_recorder_done(None, first_generation - 1)
        self.assertTrue(self.task.is_recording)                       # 新会话不受影响
        self.assertTrue(self.backend.render['spk-a'].muted)           # 输出仍被静音
        self.assertEqual(output_mute_pkg._OWNER.refcount, 1)          # 租约仍持有

        self.task.finish()
        self.assertFalse(self.backend.render['spk-a'].muted)

    def test_duplicate_start_then_finish_restores(self):
        # 复刻评审反例：重复 START 幂等（同一会话/同一协程/不换租约）
        self.task.launch()
        first_future = self.task.task
        self.task.launch()   # 重复 START
        self.assertIs(self.task.task, first_future)
        self.assertTrue(self.backend.render['spk-a'].muted)
        self.task.finish()
        self.assertFalse(self.backend.render['spk-a'].muted)
        self.assertEqual(output_mute_pkg._OWNER.refcount, 0)

    def test_shutdown_latch_blocks_all_start_paths(self):
        # PREPARE_SHUTDOWN 闩锁落下后：快捷键/UDP 等一切路径都不得
        # 建立新的静音会话（确认恢复到终止之间不允许重新静音）
        self.state.shutdown_pending = True

        self.task.launch()

        self.assertFalse(self.task.is_recording)
        self.assertFalse(self.state.recording)
        self.assertFalse(self.backend.render['spk-a'].muted)
        self.assertEqual(output_mute_pkg._OWNER.refcount, 0)
        self.assertIsNone(self.task.task)

    def test_config_switch_off_disables_muting(self):
        original = ClientConfig.mute_output_while_recording
        ClientConfig.mute_output_while_recording = False
        try:
            self.task.launch()
            self.assertTrue(self.state.recording)
            self.assertFalse(self.backend.render['spk-a'].muted)  # 未静音
            self.assertIsNone(self.task._mute_lease)
        finally:
            ClientConfig.mute_output_while_recording = original
            self.task.finish()

    def test_app_exit_safety_restores_unreleased_lease(self):
        # 模拟异常退出：launch 后既不 finish 也不 cancel，app.stop() 兜底生效
        self.task.launch()
        self.assertTrue(self.backend.render['spk-a'].muted)
        self.assertTrue(self.backend.render['spk-b'].muted)

        output_mute_pkg.force_release_output_mute('app-stop')
        self.assertFalse(self.backend.render['spk-a'].muted)
        self.assertTrue(self.backend.render['spk-b'].muted)   # 原本静音不被解除


if __name__ == '__main__':
    unittest.main()
