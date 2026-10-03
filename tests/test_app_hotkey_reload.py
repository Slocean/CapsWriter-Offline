# coding: utf-8
"""
桌面端快捷键设置修复的回归测试

覆盖评审要求的三个断言面（无真实用户键盘/鼠标，无 OS 级钩子）：

1. 注释布尔解析：默认 config_client.py 的 `'enabled': True  # 注释` 必须
   解析为 True（旧桌面端正则把注释一起吃进导致误判 False）；禁用项
   （现场用户配置 caps_lock enabled=False）必须保持禁用，不得被盲目启用；
2. 新绑定注册/旧绑定移除：用真实的 ShortcutManager.create_keyboard_filter()
   过滤器函数配合成合成事件对象（data.vkCode）驱动——等效"受控假键盘钩子"，
   不安装任何 OS 钩子；热重载后旧键不再触发、新键生效；
3. 静音生命周期：经快捷键路径 launch/finish 仍走输出静音租约
   （假后端断言），且 mouse 绑定与无关绑定在重载后保留。
"""

import ast
import asyncio
import pathlib
import re
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from core.client.audio import output_mute as output_mute_pkg
from core.client.shortcut.shortcut_config import Shortcut
from core.client.shortcut.shortcut_manager import ShortcutManager
from core.client.state import ClientState

from test_output_mute_owner import FakeEndpointBackend
from test_recording_mute_lifecycle import FakeRecorder, _QuietStatus


def entry_value(entry: str, name: str, fallback: str = '') -> str:
    """C# EntryValue 的语义镜像（桌面端解析器的回归基准）"""
    m = re.search(r"'" + re.escape(name) + r"'\s*:\s*", entry)
    if not m:
        return fallback
    rest = entry[m.end():]
    if rest[:1] in ('"', "'"):
        quote = rest[0]
        end = rest.find(quote, 1)
        return rest[1:end] if end > 0 else fallback
    token = re.split(r'[,}#\r\n]', rest, maxsplit=1)[0].strip()
    return token or fallback


def parse_shortcuts_block(content: str):
    """解析 config_client.py 的 shortcuts 块 → [{'key':..., 'enabled':...}]"""
    block = re.search(r'(?ms)^\s{4}shortcuts\s*=\s*\[(.*?)^\s{4}\]', content)
    if not block:
        return []
    out = []
    for entry in re.findall(r'\{[^{}]*\}', block.group(1)):
        out.append({
            'key': entry_value(entry, 'key'),
            'type': entry_value(entry, 'type', 'keyboard'),
            'enabled': entry_value(entry, 'enabled') == 'True',
            'hold_mode': entry_value(entry, 'hold_mode', 'True') == 'True',
        })
    return out


class CommentedBoolParsingTests(unittest.TestCase):
    """EntryValue 语义回归：行内注释不得污染布尔/键名解析"""

    def test_default_config_inline_commented_true_parses_true(self):
        content = (ROOT / 'config_client.py').read_text(encoding='utf-8')
        entries = parse_shortcuts_block(content)
        by_key = {e['key']: e for e in entries}
        # 仓库默认配置：caps_lock 行内注释紧跟 True——必须解析为启用
        self.assertTrue(by_key['caps_lock']['enabled'],
                        "默认 'enabled': True # 注释 被误判为 False")
        self.assertTrue(by_key['ctrl+alt+space']['enabled'])
        self.assertFalse(by_key['caps_lock']['hold_mode'] is None)
        self.assertEqual(by_key['x2']['type'], 'mouse')
        self.assertTrue(by_key['x2']['enabled'])

    def test_disabled_entry_stays_disabled(self):
        # 现场用户配置形状（work/continuation-20261004-live-shortcuts.json）：
        # caps_lock enabled=False 必须保持禁用
        entry = "{'key': 'caps_lock',     # 监听大写锁定键\n" \
                "  'type': 'keyboard',     # 是键盘快捷键\n" \
                "  'suppress': True,      # 阻塞按键（短按会补发）\n" \
                "  'hold_mode': True,      # 长按模式\n" \
                "  'enabled': False        # 启用此快捷键\n" \
                "}"
        self.assertFalse(entry_value(entry, 'enabled') == 'True')
        self.assertEqual(entry_value(entry, 'key'), 'caps_lock')

    def test_commented_true_is_true_and_quoted_key_unquoted(self):
        entry = "{'key': 'ctrl+alt+space',  # 单击开始\n  'enabled': True    # 注释\n}"
        self.assertEqual(entry_value(entry, 'key'), 'ctrl+alt+space')
        self.assertTrue(entry_value(entry, 'enabled') == 'True')
        self.assertEqual(entry_value(entry, 'missing', 'fb'), 'fb')

    def test_synthetic_shortcuts_roundtrip_via_shortcut_ctor(self):
        # 解析结果可直接构造 Shortcut（与 UDP 热重载同一路径）
        entries = [
            {'key': 'caps_lock', 'type': 'keyboard', 'suppress': True,
             'hold_mode': True, 'enabled': False},
            {'key': 'x2', 'type': 'mouse', 'suppress': True,
             'hold_mode': True, 'enabled': True},
        ]
        shortcuts = [Shortcut(**e) for e in entries]
        self.assertFalse(shortcuts[0].enabled)
        self.assertEqual(shortcuts[1].key, 'x2')


class _FakeListener:
    """受控假监听器：记录 start/stop，绝不安装 OS 级钩子"""

    fail = False

    def __init__(self, **kwargs):
        self.alive = False

    def start(self):
        if _FakeListener.fail:
            raise RuntimeError('假钩子注册失败（模拟）')
        self.alive = True

    def stop(self):
        self.alive = False

    def is_alive(self):
        return self.alive


class _FakeHookRegistry:
    """显式假钩子注册表适配器：与真实 _SystemHookRegistry 相同的
    lookup 契约（按监听器线程 ident 查非零句柄）；存活监听器视为
    已注册（readiness 就此退化为存活判定），绝不安装 OS 级钩子"""

    def lookup(self, listener):
        if listener is not None and listener.is_alive():
            return 0x1234
        return None


class _FakeNativeListener:
    """带线程 ident 的假监听器：用于注册表契约测试（非 pynput 实例）"""

    def __init__(self, ident, alive=True):
        self.ident = ident
        self._alive = alive

    def is_alive(self):
        return self._alive


class _ManagerHarness:
    """ShortcutManager + 静音假后端 + 合成键盘事件（无 OS 钩子）"""

    VK = {'caps_lock': 0x14, 'ctrl': 0x11, 'alt': 0x12, 'space': 0x20}

    def __init__(self, shortcuts):
        from core.client.shortcut import shortcut_manager as sm_module
        self.owner_prev = output_mute_pkg._OWNER
        self.backend = FakeEndpointBackend()
        self.backend.add_render('spk-a', muted=False)
        output_mute_pkg._OWNER = output_mute_pkg.OutputMuteOwner(backend=self.backend)

        self.state = ClientState()
        self.loop = asyncio.new_event_loop()
        self.loop_thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.loop_thread.start()
        self.app = SimpleNamespace(state=self.state, loop=self.loop)
        self.state.app = self.app
        self.manager = ShortcutManager(self.app, shortcuts,
                                       hook_registry=_FakeHookRegistry())
        # 受控假钩子：不调用 listener.start()，不安装任何 OS 钩子；
        # 事件经真实 create_keyboard_filter() 过滤器驱动
        self.manager.start = lambda: None
        # 替换 rich Status 动画，测试输出保持干净
        for task in list(self.manager.tasks.values()) + [self.manager.control_task]:
            task._status = _QuietStatus()
            task.threshold = 0.3
        self._recorder_patch()
        self.filter = self.manager.create_keyboard_filter()

    def _recorder_patch(self):
        self.manager.control_task._recorder_class = FakeRecorder
        for task in self.manager.tasks.values():
            task._recorder_class = FakeRecorder

    def key(self, name, is_up=False):
        msg = 0x0101 if is_up else 0x0100   # WM_KEYUP / WM_KEYDOWN
        self.filter(msg, SimpleNamespace(vkCode=self.VK[name]))

    def close(self):
        try:
            for task in list(self.manager.tasks.values()) + [self.manager.control_task]:
                task.is_recording = False
            future = getattr(self.manager.control_task, 'task', None)
            if future is not None:
                future.cancel()
            # 排空事件循环上残留的协程（FakeRecorder），避免 "Task destroyed" 噪音
            async def _drain():
                pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
                for t in pending:
                    t.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
            asyncio.run_coroutine_threadsafe(_drain(), self.loop).result(timeout=3)
        finally:
            output_mute_pkg._OWNER = self.owner_prev
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.loop_thread.join(timeout=2)
            self.loop.close()


class FakeHookBindingTests(unittest.TestCase):
    """新绑定注册 / 旧绑定移除 / 禁用不触发 / 静音生命周期（假钩子）"""

    def test_default_binding_launches_and_restores_mute(self):
        h = _ManagerHarness([Shortcut(key='caps_lock', hold_mode=True)])
        try:
            h.key('caps_lock')
            self.assertTrue(h.state.recording)
            self.assertTrue(h.backend.render['spk-a'].muted)   # 静音生命周期生效

            h.key('caps_lock', is_up=True)   # 短按松开 → cancel → 恢复
            self.assertFalse(h.state.recording)
            self.assertFalse(h.backend.render['spk-a'].muted)
        finally:
            h.close()

    def test_reload_registers_new_and_removes_old_binding(self):
        h = _ManagerHarness([Shortcut(key='caps_lock', hold_mode=True),
                             Shortcut(key='x2', type='mouse', hold_mode=True)])
        try:
            self.assertIn('caps_lock', h.manager.tasks)
            h.key('caps_lock')
            self.assertTrue(h.state.recording)
            # 受控假钩子：restart 直接创建监听器，测试中替换为假实现，
            # 绝不安装 OS 级钩子
            with mock.patch('core.client.shortcut.shortcut_manager.keyboard.Listener',
                            _FakeListener),                  mock.patch('core.client.shortcut.shortcut_manager.mouse.Listener',
                            _FakeListener):
                self.assertTrue(h.manager.restart([Shortcut(key='ctrl+alt+space', hold_mode=True),
                                                   Shortcut(key='x2', type='mouse', hold_mode=True)]))
            h._recorder_patch()
            for task in list(h.manager.tasks.values()) + [h.manager.control_task]:
                task._status = _QuietStatus()
                task.threshold = 0.3
            self.assertFalse(h.state.recording)   # restart 结束了进行中的录音

            # 旧绑定移除：caps_lock 事件不再触发任何录音
            h.key('caps_lock')
            self.assertFalse(h.state.recording)
            # 新绑定注册：ctrl+alt+space 组合键触发
            h.key('ctrl')
            h.key('alt')
            h.key('space')
            self.assertTrue(h.state.recording)
            self.assertTrue(h.backend.render['spk-a'].muted)
            h.key('ctrl', is_up=True)   # 松开修饰键结束长按
            self.assertFalse(h.state.recording)
            self.assertFalse(h.backend.render['spk-a'].muted)
            # 无关 mouse 绑定保留
            self.assertIn('x2', h.manager.tasks)
        finally:
            h.close()

    def test_disabled_binding_never_launches(self):
        h = _ManagerHarness([Shortcut(key='caps_lock', hold_mode=True, enabled=False)])
        try:
            self.assertEqual(
                [k for k, t in h.manager.tasks.items() if t.shortcut.enabled], [])
            h.key('caps_lock')
            self.assertFalse(h.state.recording)
            self.assertFalse(h.backend.render['spk-a'].muted)
        finally:
            h.close()


class TransactionalRestartTests(unittest.TestCase):
    """事务式热重载（评审 continuation-20261004-hotkey-transition-review 永久回归）

    用真实 ShortcutManager + 真实 ShortcutTask + 假钩子/假音频后端验证：
    restart 等待在途旧开始落地并按静音生命周期清流后才替换；新监听器
    启动失败整体回滚且旧绑定保持可用；退役任务晚到的启动是 no-op。
    """

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        self.loop_thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.loop_thread.start()
        self.owner_prev = output_mute_pkg._OWNER
        self.backend = FakeEndpointBackend()
        self.backend.add_render('spk-a', muted=False)
        output_mute_pkg._OWNER = output_mute_pkg.OutputMuteOwner(backend=self.backend)
        self.state = ClientState()
        self.app = SimpleNamespace(state=self.state, loop=self.loop)
        self.state.app = self.app
        self.patches = [
            mock.patch('core.client.shortcut.shortcut_manager.keyboard.Listener', _FakeListener),
            mock.patch('core.client.shortcut.shortcut_manager.mouse.Listener', _FakeListener),
            mock.patch('core.client.shortcut.shortcut_manager.ShortcutEmulator',
                       return_value=SimpleNamespace()),
        ]
        for patcher in self.patches:
            patcher.start()
        self.manager = ShortcutManager(self.app, [Shortcut(key='f6', hold_mode=False)],
                                       hook_registry=_FakeHookRegistry())
        self.manager.start()
        self._patch_task_statuses()

    def tearDown(self):
        try:
            self.manager.stop()
        except Exception:
            pass
        if FakeRecorder.last_instance is not None:
            FakeRecorder.last_instance.recognition_done.set()

        async def drain():
            pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for pending_task in pending:
                pending_task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        asyncio.run_coroutine_threadsafe(drain(), self.loop).result(timeout=3)
        for patcher in reversed(self.patches):
            patcher.stop()
        output_mute_pkg._OWNER = self.owner_prev
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.loop_thread.join(timeout=2)
        self.loop.close()

    def _patch_task_statuses(self):
        for task in list(self.manager.tasks.values()) + [self.manager.control_task]:
            task._status = _QuietStatus()
            task.threshold = 0.3
            task._recorder_class = FakeRecorder

    def test_restart_waits_for_inflight_start_then_drains(self):
        old = self.manager.tasks['f6']
        entered = threading.Event()
        gate = threading.Event()
        orig = old._acquire_mute_lease

        def acquire():
            entered.set()
            gate.wait(3)
            return orig()
        old._acquire_mute_lease = acquire
        starter = threading.Thread(target=old.launch)
        starter.start()
        self.assertTrue(entered.wait(2))

        returned = threading.Event()
        result = {}

        def restart():
            result['ok'] = self.manager.restart([Shortcut(key='f7', hold_mode=False)])
            returned.set()
        r = threading.Thread(target=restart)
        r.start()
        time.sleep(0.2)
        self.assertFalse(returned.is_set(), 'restart 必须等待在途旧开始落地后再返回')
        gate.set()
        starter.join(3)
        r.join(3)

        self.assertTrue(result['ok'])
        self.assertFalse(old.is_recording)
        self.assertFalse(self.state.recording)
        self.assertFalse(self.backend.render['spk-a'].muted)   # 清流完成、恢复完成
        self.assertIn('f7', self.manager.tasks)
        self.assertNotIn('f6', self.manager.tasks)
        self._patch_task_statuses()

    def test_failed_listener_start_rolls_back_to_usable_bindings(self):
        self.assertTrue(self.manager.restart([Shortcut(key='f7', hold_mode=False)]))
        self._patch_task_statuses()
        prev_keys = list(self.manager.tasks)
        prev_listener = self.manager.keyboard_listener

        _FakeListener.fail = True
        try:
            self.assertFalse(self.manager.restart([Shortcut(key='f8', hold_mode=False)]))
        finally:
            _FakeListener.fail = False

        self.assertEqual(list(self.manager.tasks), prev_keys)
        self.assertIs(self.manager.keyboard_listener, prev_listener)
        self.assertTrue(prev_listener.is_alive())
        # 旧绑定仍然可用（超出反例的映射断言：实际能再次启动并走静音生命周期）
        task = self.manager.tasks['f7']
        task.launch()
        self.assertTrue(self.state.recording)
        self.assertTrue(self.backend.render['spk-a'].muted)
        task.finish()
        self.assertFalse(self.state.recording)
        self.assertFalse(self.backend.render['spk-a'].muted)

    def test_retired_old_task_cannot_relaunch_after_replacement(self):
        old = self.manager.tasks['f6']
        self.assertTrue(self.manager.restart([Shortcut(key='f7', hold_mode=False)]))
        self._patch_task_statuses()
        self.assertTrue(old._retired)

        old.launch()   # 池化回调/迟到线程对已替换任务的启动必须 no-op
        self.assertFalse(old.is_recording)
        self.assertFalse(self.state.recording)
        self.assertFalse(self.backend.render['spk-a'].muted)

    def test_duplicate_enabled_key_rejected_without_state_change(self):
        prev_keys = list(self.manager.tasks)
        prev_listener = self.manager.keyboard_listener
        self.assertFalse(self.manager.restart([
            Shortcut(key='f9', hold_mode=False), Shortcut(key='f9', hold_mode=False)]))
        self.assertEqual(list(self.manager.tasks), prev_keys)
        self.assertIs(self.manager.keyboard_listener, prev_listener)


class ReloadBoundaryTests(unittest.TestCase):
    """热重载边界（评审 publication_window 反例的永久回归）

    事务期间：过滤器分发被拒绝（不得穿过新映射）；事务开始时的现役
    任务允许完成入场启动并由退役清流；新任务直连启动被拒绝发布；
    监听器未就绪整体回滚；重载后普通录音照常可用。
    """

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        self.loop_thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.loop_thread.start()
        self.owner_prev = output_mute_pkg._OWNER
        self.backend = FakeEndpointBackend()
        self.backend.add_render('spk-a', muted=False)
        output_mute_pkg._OWNER = output_mute_pkg.OutputMuteOwner(backend=self.backend)
        self.state = ClientState()
        self.app = SimpleNamespace(state=self.state, loop=self.loop)
        self.state.app = self.app
        self.patches = [
            mock.patch('core.client.shortcut.shortcut_manager.keyboard.Listener', _FakeListener),
            mock.patch('core.client.shortcut.shortcut_manager.mouse.Listener', _FakeListener),
            mock.patch('core.client.shortcut.shortcut_manager.ShortcutEmulator',
                       return_value=SimpleNamespace()),
        ]
        for patcher in self.patches:
            patcher.start()
        self.manager = ShortcutManager(self.app, [Shortcut(key='f7', hold_mode=False)],
                                       hook_registry=_FakeHookRegistry())
        self.manager.start()
        self._patch_task_statuses()

    def tearDown(self):
        try:
            self.manager.stop()
        except Exception:
            pass
        if FakeRecorder.last_instance is not None:
            FakeRecorder.last_instance.recognition_done.set()

        async def drain():
            pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for pending_task in pending:
                pending_task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        asyncio.run_coroutine_threadsafe(drain(), self.loop).result(timeout=3)
        for patcher in reversed(self.patches):
            patcher.stop()
        output_mute_pkg._OWNER = self.owner_prev
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.loop_thread.join(timeout=2)
        self.loop.close()

    def _patch_task_statuses(self):
        for task in list(self.manager.tasks.values()) + [self.manager.control_task]:
            task._status = _QuietStatus()
            task.threshold = 0.3
            task._recorder_class = FakeRecorder

    def test_publication_window_filter_dispatch_refused_and_old_drained(self):
        """复刻评审反例：phase1 清流后、retire 前的窗口内——旧任务入场
        启动被放行（随后清流），经真实过滤器分发的新映射任务被拒绝，
        全局录音状态不被清掉"""
        old = self.manager.tasks['f7']
        entered = threading.Event()
        gate = threading.Event()
        orig = old._acquire_mute_lease

        def paused():
            entered.set()
            gate.wait(3)
            return orig()
        old._acquire_mute_lease = paused

        build = self.manager._build_task_maps
        retire = self.manager._set_retired
        slipped = []

        def build_with_late_start(shortcuts):
            maps = build(shortcuts)
            t2 = threading.Thread(target=old.launch)
            slipped.append(t2)
            t2.start()
            self.assertTrue(entered.wait(2))   # 旧任务入场（发布准入放行）
            return maps

        def retire_with_actual_filter(tasks, flag):
            if flag and 'f9' in self.manager.tasks:
                # 事务窗口内经真实过滤器分发新映射任务：必须被边界拒绝
                with mock.patch.object(self.manager, '_check_emulating', return_value=False),                      mock.patch.object(self.manager, '_check_restoring', return_value=False):
                    self.manager.create_keyboard_filter()(
                        0x0100, SimpleNamespace(vkCode=0x78))
                self.assertFalse(self.manager.tasks['f9'].is_recording,
                                 '事务窗口内过滤器分发不得启动新映射任务')
                gate.set()
                for t2 in slipped:
                    t2.join(3)
            return retire(tasks, flag)

        self.manager._build_task_maps = build_with_late_start
        self.manager._set_retired = retire_with_actual_filter
        try:
            self.assertTrue(self.manager.restart([Shortcut(key='f9', hold_mode=False)]))
        finally:
            self.manager._build_task_maps = build
            self.manager._set_retired = retire

        self._patch_task_statuses()
        self.assertFalse(self.manager.tasks['f9'].is_recording)
        self.assertFalse(old.is_recording)                     # 入场旧任务已被清流
        self.assertFalse(self.state.recording)
        self.assertFalse(self.backend.render['spk-a'].muted)   # 输出恢复完成
        # 重载后普通录音照常可用
        self.manager.tasks['f9'].launch()
        self.assertTrue(self.state.recording)
        self.assertTrue(self.backend.render['spk-a'].muted)
        self.manager.tasks['f9'].finish()
        self.assertFalse(self.state.recording)
        self.assertFalse(self.backend.render['spk-a'].muted)

    def test_new_task_direct_launch_refused_mid_transaction(self):
        """事务窗口内对新任务（如 UDP 控制任务直连 launch）拒绝发布"""
        new_task_box = {}
        build = self.manager._build_task_maps

        def build_with_direct_launch(shortcuts):
            maps = build(shortcuts)
            new_task_box['task'] = maps[0]['f9']
            maps[0]['f9']._recorder_class = FakeRecorder
            maps[0]['f9']._status = _QuietStatus()
            maps[0]['f9'].threshold = 0.3
            maps[0]['f9'].launch()   # 事务进行中：发布准入必须拒绝
            return maps

        self.manager._build_task_maps = build_with_direct_launch
        try:
            self.assertTrue(self.manager.restart([Shortcut(key='f9', hold_mode=False)]))
        finally:
            self.manager._build_task_maps = build

        self.assertFalse(new_task_box['task'].is_recording)
        self.assertFalse(self.state.recording)
        self.assertFalse(self.backend.render['spk-a'].muted)

    def test_listener_not_ready_rolls_back(self):
        """新监听器启动但未就绪（线程存活即视为就绪的假象被识破）：
        整体回滚，旧绑定保持可用"""
        class DyingListener(_FakeListener):
            def is_alive(self):
                return False   # start 后立刻判死：模拟未就绪

        prev_keys = list(self.manager.tasks)
        prev_listener = self.manager.keyboard_listener
        with mock.patch('core.client.shortcut.shortcut_manager.keyboard.Listener',
                        DyingListener):
            self.assertFalse(self.manager.restart([Shortcut(key='f9', hold_mode=False)]))
        self.assertEqual(list(self.manager.tasks), prev_keys)
        self.assertIs(self.manager.keyboard_listener, prev_listener)
        self.assertTrue(prev_listener.is_alive())


class NativeHookRegistryContractTests(unittest.TestCase):
    """原生钩子注册表契约（受控假注册表，无 OS 级用户钩子）

    实测库源契约：SystemHook._HOOKS[线程 ident] → 实例（其 _hook 为
    SetWindowsHookEx 句柄；__exit__ 注销移除）。readiness 必须：
    - 已登记非零句柄 → 就绪；
    - 未登记 / 句柄为零 / 监听器死亡 → bounded 等待后判未就绪；
    - ident 不可得（测试替身）→ 退化路径（存活+宽限期）。
    """

    def _manager_with(self, registry):
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
        from core.client.shortcut.shortcut_manager import ShortcutManager
        return ShortcutManager(SimpleNamespace(state=ClientState()), [],
                               hook_registry=registry)

    def _fake_cls(self, hooks):
        import types
        return types.SimpleNamespace(_HOOKS=hooks)

    def test_registered_nonzero_handle_is_ready(self):
        from core.client.shortcut.shortcut_manager import _SystemHookRegistry
        reg = _SystemHookRegistry(self._fake_cls({4321: SimpleNamespace(_hook=0xBEEF)}))
        listener = _FakeNativeListener(4321)
        self.assertTrue(reg.lookup(listener))
        self.assertTrue(self._manager_with(reg)._listener_ready(listener, timeout=0.3))

    def test_unregistered_times_out_not_ready(self):
        from core.client.shortcut.shortcut_manager import _SystemHookRegistry
        reg = _SystemHookRegistry(self._fake_cls({}))   # 钩子未安装
        listener = _FakeNativeListener(9999)
        self.assertIsNone(reg.lookup(listener))
        self.assertFalse(self._manager_with(reg)._listener_ready(listener, timeout=0.1))

    def test_zero_handle_is_not_ready(self):
        from core.client.shortcut.shortcut_manager import _SystemHookRegistry
        reg = _SystemHookRegistry(self._fake_cls({4321: SimpleNamespace(_hook=0)}))
        listener = _FakeNativeListener(4321)
        self.assertIsNone(reg.lookup(listener))   # 零句柄不算已安装
        self.assertFalse(self._manager_with(reg)._listener_ready(listener, timeout=0.1))

    def test_dead_listener_fails_fast(self):
        from core.client.shortcut.shortcut_manager import _SystemHookRegistry
        reg = _SystemHookRegistry(self._fake_cls({4321: SimpleNamespace(_hook=0xBEEF)}))
        listener = _FakeNativeListener(4321, alive=False)
        self.assertFalse(self._manager_with(reg)._listener_ready(listener, timeout=5.0))

    def test_default_registry_probe_and_identless_listener(self):
        from core.client.shortcut.shortcut_manager import _default_hook_registry
        reg = _default_hook_registry()
        # 本机为 Windows + pynput：注册表可探测（非 Windows 环境为 None）
        if reg is not None:
            self.assertIsNone(reg.lookup(_FakeNativeListener(None)))   # ident 不可得
            self.assertIsNone(reg.lookup(object()))                    # 无 ident 属性

    def test_fake_adapter_readiness_follows_liveness(self):
        """显式假钩子适配器（测试替身路径）：就绪随存活判定"""
        manager = self._manager_with(_FakeHookRegistry())
        listener = _FakeListener()
        self.assertFalse(manager._listener_ready(listener, timeout=0.3))   # 未启动
        listener.start()
        self.assertTrue(manager._listener_ready(listener, timeout=0.3))    # 存活即就绪
        listener.stop()
        self.assertFalse(manager._listener_ready(listener, timeout=0.3))   # 已停止


if __name__ == '__main__':
    unittest.main()
