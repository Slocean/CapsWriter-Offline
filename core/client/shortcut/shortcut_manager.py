# coding: utf-8
"""
快捷键管理器（重构版）

统一管理多个快捷键，处理键盘和鼠标事件，支持：
1. 多快捷键并发处理
2. 防止不同按键互相干扰
3. restore 功能的防自捕获逻辑
4. hold_mode 和 click_mode 支持
"""
from __future__ import annotations
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Dict, List, Optional

from pynput import keyboard, mouse

from . import logger
from core.client.shortcut.key_mapper import *
from core.client.shortcut.key_mapper import KeyMapper
from core.client.shortcut.emulator import ShortcutEmulator
from core.client.shortcut.shortcut_config import Shortcut
from core.client.shortcut.event_handler import ShortcutEventHandler
from core.client.shortcut.task import ShortcutTask

if TYPE_CHECKING:
    from core.client.shortcut.shortcut_config import Shortcut
    from core.client.state import ClientState
    from core.client.app import CapsWriterClient



class _SystemHookRegistry:
    """pynput Windows 系统钩子注册表的只读适配器。

    实测本地库源（pynput._util.win32）：SystemHook.__enter__ 把实例登记到
    SystemHook._HOOKS[threading.current_thread().ident]，随后才
    SetWindowsHookEx 安装句柄（self._hook）；__exit__ 注销并移除。
    ListenerMixin._run 的 _ready 标志先于钩子安装——监听器就绪必须查
    本注册表，不能只看线程存活。lookup 返回非零句柄或 None。
    """

    def __init__(self, system_hook_cls):
        self._cls = system_hook_cls

    def lookup(self, listener):
        ident = getattr(listener, 'ident', None)
        if ident is None:
            return None
        entry = self._cls._HOOKS.get(ident)
        if entry is None:
            return None
        handle = getattr(entry, '_hook', None)
        value = getattr(handle, 'value', handle)   # 兼容 HHOOK(c_void_p)/int
        return value or None


def _default_hook_registry():
    """探测本机 pynput 的系统钩子注册表；非 Windows/无实现返回 None"""
    try:
        from pynput._util.win32 import SystemHook
        return _SystemHookRegistry(SystemHook)
    except Exception:
        return None


class ShortcutManager:
    """
    快捷键管理器

    统一管理多个快捷键，使用 pynput 监听键盘和鼠标事件。
    所有事件处理都在 win32_event_filter 中完成，确保高性能和低延迟。
    """

    def __init__(self, app: CapsWriterClient, shortcuts: List[Shortcut],
                 hook_registry=None):
        """
        初始化快捷键管理器

        Args:
            app: 客户端 App 实例
            shortcuts: 快捷键配置列表
            hook_registry: 原生钩子注册表适配器（lookup(listener)→句柄）；
                None 时自动探测本机 pynput；测试注入受控假注册表
        """
        self.app = app
        self.shortcuts = shortcuts
        self._hook_registry = hook_registry if hook_registry is not None             else _default_hook_registry()

        # 监听器
        self.keyboard_listener: Optional[keyboard.Listener] = None
        self.mouse_listener: Optional[mouse.Listener] = None

        # 快捷键任务映射（key -> ShortcutTask）
        self.tasks: Dict[str, ShortcutTask] = {}

        # 线程池
        self._pool = ThreadPoolExecutor(max_workers=4)

        # 按键模拟器
        self._emulator = ShortcutEmulator()

        # 按键恢复状态追踪
        self._restoring_keys = set()

        # Keep physical-key state so chords and standalone Ctrl/Alt/Shift work.
        self._hotkey_lock = threading.RLock()
        self._pressed_keys = set()
        self._modifier_used = set()
        self._active_keyboard = {}
        self._hotkeys_paused_until = 0.0

        # 事件处理器
        self._event_handler = ShortcutEventHandler(self.tasks, self._pool, self._emulator)

        # —— 热重载边界（统一防护键盘/鼠标/修饰键/UDP 控制的一切开始路径）——
        # _reload_active 为 True 期间：事件过滤器的分发被拒绝（不得穿过新映射）；
        # 事务开始时已在役的旧任务允许完成已入场的启动（随后由退役阶段清流）。
        # _dispatch_lock 是叶子锁：只护卫标志读写与发布准入，不跨任何长段持有。
        self._dispatch_lock = threading.Lock()
        self._reload_active = False
        self._reload_admitted = set()

        # 初始化快捷键任务
        self._init_tasks()

    @property
    def state(self) -> ClientState:
        """快捷访问状态单例"""
        return self.app.state

    def _init_tasks(self) -> None:
        """初始化所有快捷键任务"""
        tasks, control = self._build_task_maps(self.shortcuts)
        self.tasks.update(tasks)
        self.control_task = control

    def _default_threshold(self) -> float:
        from config_client import ClientConfig as Config
        return Config.threshold

    def _make_task(self, shortcut: Shortcut) -> ShortcutTask:
        task = ShortcutTask(self.app, shortcut)
        task._manager_ref = lambda: self
        task.pool = self._pool
        task.threshold = shortcut.get_threshold(self._default_threshold())
        return task

    def _build_task_maps(self, shortcuts: List[Shortcut]):
        """按配置构建任务映射与桌面控制任务（纯构建，不发布）"""
        tasks: Dict[str, ShortcutTask] = {}
        for shortcut in shortcuts:
            if not shortcut.enabled:
                continue
            tasks[shortcut.key] = self._make_task(shortcut)
        control = self._make_task(Shortcut(key='desktop_control', enabled=False))
        return tasks, control

    @staticmethod
    def _validate_shortcuts(shortcuts: List[Shortcut]) -> None:
        """发布前校验全部新条目（任何一项不合法即整体失败）"""
        seen = set()
        for shortcut in shortcuts:
            if not isinstance(shortcut, Shortcut):
                raise ValueError('快捷键条目必须是 Shortcut 实例')
            if not shortcut.key:
                raise ValueError('快捷键 key 不能为空')
            if shortcut.type not in ('keyboard', 'mouse'):
                raise ValueError(f'未知快捷键类型: {shortcut.type}')
            if shortcut.enabled and shortcut.key in seen:
                raise ValueError(f'启用的快捷键重复: {shortcut.key}')
            if shortcut.enabled:
                seen.add(shortcut.key)

    _MODIFIERS = {'ctrl', 'alt', 'shift'}

    def hotkeys_paused(self) -> bool:
        return time.monotonic() < self._hotkeys_paused_until

    # —— 热重载边界 ——
    # 注意：本边界不触碰 _hotkeys_paused_until（桌面端快捷键录入的暂停
    # 窗口），事务前后暂停状态保持不变。

    def admit_dispatch(self) -> bool:
        """事件过滤器分发准入：重载事务进行中拒绝解析与启动新任务"""
        with self._dispatch_lock:
            return not self._reload_active

    def admit_launch(self, task) -> bool:
        """录音发布准入：事务进行中仅放行事务开始时的现役任务
        （其启动由退役阶段按静音生命周期清流），其余一律拒绝，
        防止新旧两个录音重叠后相互清除全局录音状态。"""
        with self._dispatch_lock:
            if not self._reload_active:
                return True
            return task in self._reload_admitted

    def pause_hotkeys(self, seconds: float = 35.0) -> None:
        with self._hotkey_lock:
            self._hotkeys_paused_until = time.monotonic() + seconds
            self._pressed_keys.clear()
            self._modifier_used.clear()
            self._active_keyboard.clear()

    def resume_hotkeys(self) -> None:
        with self._hotkey_lock:
            self._hotkeys_paused_until = 0.0
            self._pressed_keys.clear()
            self._modifier_used.clear()
            self._active_keyboard.clear()

    def _match_keyboard_task(self, key_name: str):
        held_modifiers = self._pressed_keys & self._MODIFIERS
        for task in self.tasks.values():
            if task.shortcut.type != 'keyboard':
                continue
            parts = task.shortcut.key.split('+')
            if parts[-1] == key_name and set(parts[:-1]) == held_modifiers - {key_name}:
                return task
        return None

    def _launch_modifier_hold(self, key_name: str, task) -> None:
        time.sleep(task.threshold)
        with self._hotkey_lock:
            if (not self.hotkeys_paused() and key_name in self._pressed_keys
                    and key_name not in self._modifier_used
                    and self._active_keyboard.get(key_name) is task
                    and not self.state.recording):
                task.launch()

    # ========== 监听器创建 ==========

    def create_keyboard_filter(self):
        """Create one low-level listener for single keys and modifier chords."""
        def win32_event_filter(msg, data):
            if msg not in KEYBOARD_MESSAGES or self.hotkeys_paused():
                return True
            if not self.admit_dispatch():
                return True   # 重载事务进行中：丢弃事件，不解析不启动
            key_name = KeyMapper.vk_to_name(data.vkCode)
            if self._check_emulating(key_name, msg) or self._check_restoring(key_name, msg):
                return True

            task = None
            with self._hotkey_lock:
                if msg in KEY_DOWN_MESSAGES:
                    if key_name not in self._pressed_keys:
                        held = self._pressed_keys & self._MODIFIERS
                        if held:
                            self._modifier_used.update(held)
                            if key_name in self._MODIFIERS:
                                self._modifier_used.add(key_name)
                        self._pressed_keys.add(key_name)

                    task = self._active_keyboard.get(key_name)
                    if task is None:
                        task = self._match_keyboard_task(key_name)
                        if task is not None:
                            self._active_keyboard[key_name] = task
                            if key_name in self._MODIFIERS and task.shortcut.key == key_name:
                                if task.shortcut.hold_mode:
                                    self._pool.submit(self._launch_modifier_hold, key_name, task)
                            else:
                                self._event_handler.handle_keydown(key_name, task)

                elif msg in KEY_UP_MESSAGES:
                    task = self._active_keyboard.pop(key_name, None)
                    if task is not None:
                        if key_name in self._MODIFIERS and task.shortcut.key == key_name:
                            if task.shortcut.hold_mode:
                                if task.is_recording:
                                    task.finish()
                            elif key_name not in self._modifier_used:
                                if task.is_recording:
                                    task.finish()
                                elif not self.state.recording:
                                    task.launch()
                        else:
                            self._event_handler.handle_keyup(key_name, task)

                    # Releasing a chord's modifier also ends a held recording.
                    for trigger, active in list(self._active_keyboard.items()):
                        if (key_name in active.shortcut.key.split('+')[:-1]
                                and active.shortcut.hold_mode and active.is_recording):
                            self._event_handler.handle_keyup(trigger, active)
                    self._pressed_keys.discard(key_name)
                    self._modifier_used.discard(key_name)

            if task is not None and task.shortcut.suppress and self.keyboard_listener:
                self.keyboard_listener.suppress_event()
            return True

        return win32_event_filter

    def create_mouse_filter(self):
        """创建鼠标事件过滤器"""
        def win32_event_filter(msg, data):
            # 只处理 XBUTTON 消息
            if self.hotkeys_paused() or msg not in MOUSE_MESSAGES:
                return True
            if not self.admit_dispatch():
                return True   # 重载事务进行中：丢弃事件，不解析不启动

            # 获取按键标识
            xbutton = (data.mouseData >> 16) & 0xFFFF
            button_name = 'x1' if xbutton == XBUTTON1 else 'x2'

            # 防自捕获检查
            if self._check_emulating(button_name, msg, is_mouse=True):
                return True

            # 查找匹配的快捷键
            if button_name not in self.tasks:
                return True

            task = self.tasks[button_name]

            # 处理鼠标事件
            if msg == WM_XBUTTONDOWN:
                self._event_handler.handle_keydown(button_name, task)
            elif msg == WM_XBUTTONUP:
                self._handle_mouse_keyup(button_name, task)

            # 阻塞事件
            if task.shortcut.suppress and self.mouse_listener:
                self.mouse_listener.suppress_event()

            return True

        return win32_event_filter

    def _handle_mouse_keyup(self, button_name: str, task) -> None:
        """处理鼠标按键释放事件"""
        # 单击模式
        if not task.shortcut.hold_mode:
            self._event_handler.handle_keyup(button_name, task)
            return

        # 长按模式
        if not task.is_recording:
            return

        duration = time.time() - task.recording_start_time
        logger.debug(f"[{button_name}] 松开按键，持续时间: {duration:.3f}s")

        if duration < task.threshold:
            task.cancel()
            if task.shortcut.suppress:
                logger.debug(f"[{button_name}] 安排异步补发鼠标按键")
                self._pool.submit(self._emulator.emulate_mouse_click, button_name)
        else:
            task.finish()

    # ========== 快捷键热重载 ==========

    def restart(self, shortcuts: Optional[List[Shortcut]] = None) -> bool:
        """事务式快捷键热重载（桌面端设置变更后调用）。

        一个统一的重载边界防护全部开始路径（键盘/鼠标/修饰键/UDP 控制）
        贯穿：校验 → 清流 → 映射发布 → 退役 → 监听器提交/回滚：

        - 事务期间 `_reload_active`：事件过滤器拒绝分发（不得穿过新映射）；
          发布准入只放行事务开始时的现役任务（入场启动由退役清流）；
        - 阶段：校验（纯检查）→ 清流（在途任务按静音生命周期结束）→
          本地构建 → 切换映射并退役 → 启动新监听器（提交点，含 bounded
          就绪确认）→ 成功停旧监听器 / 失败整体回滚（旧监听器从未停止，
          旧绑定保持可用）；
        - 线程池全程复用；不触碰 `_hotkeys_paused_until`（桌面端录入
          暂停窗口在事务前后保持原状）。

        返回 True 仅当新监听器就绪、在途录音已按静音生命周期清流结束。
        """
        with self._dispatch_lock:
            self._reload_active = True
        try:
            return self._restart_locked(shortcuts)
        finally:
            with self._dispatch_lock:
                self._reload_active = False
                self._reload_admitted = set()

    def _restart_locked(self, shortcuts):
        if shortcuts is None:
            shortcuts = self.shortcuts
        try:
            self._validate_shortcuts(shortcuts)
        except Exception as e:
            logger.warning(f"快捷键热重载：新配置校验失败，保持旧绑定: {e}")
            return False

        old_map = dict(self.tasks)
        old_control = self.control_task
        old_handler = self._event_handler
        old_kb, old_mouse = self.keyboard_listener, self.mouse_listener
        # 发布准入名单：事务开始时的现役任务（含桌面控制任务）
        with self._dispatch_lock:
            self._reload_admitted = set(old_map.values()) | {old_control}

        # 阶段1：结算——launch 全程持有迁移锁，在锁内等待在途开始落地，
        # 再把录音按静音生命周期结束（绝不 cancel 绕过恢复）
        for task in list(old_map.values()) + [old_control]:
            lock = getattr(task, '_transition_lock', None)
            if lock is None:
                continue
            with lock:
                if task.is_recording:
                    task.finish()

        # 阶段2：构建新任务映射（不发布）
        try:
            new_tasks, new_control = self._build_task_maps(shortcuts)
        except Exception as e:
            logger.warning(f"快捷键热重载：新配置构建失败，保持旧绑定: {e}")
            return False

        # 阶段3：切换映射并退役旧任务（过滤器分发已被边界拒绝；旧任务
        # 在其迁移锁内退役，同时清流阶段1与阶段3之间入场的启动）
        with self._hotkey_lock:
            self._pressed_keys.clear()
            self._modifier_used.clear()
            self._active_keyboard.clear()
        self._restoring_keys.clear()
        self.tasks = new_tasks
        self.control_task = new_control
        self._event_handler = ShortcutEventHandler(new_tasks, self._pool, self._emulator)
        self._set_retired(list(old_map.values()) + [old_control], True)

        # 阶段4：启动新监听器（提交点）+ bounded 就绪确认
        new_kb = new_mouse = None
        try:
            if any(s.type == 'keyboard' for s in shortcuts if s.enabled):
                new_kb = keyboard.Listener(win32_event_filter=self.create_keyboard_filter())
                new_kb.start()
            if any(s.type == 'mouse' for s in shortcuts if s.enabled):
                new_mouse = mouse.Listener(win32_event_filter=self.create_mouse_filter())
                new_mouse.start()
            if not self._listener_ready(new_kb) or not self._listener_ready(new_mouse):
                raise RuntimeError('新监听器未在时限内就绪')
        except Exception as e:
            # 回滚：停掉可能已启动的新监听器，恢复旧映射/任务/位图；
            # 旧监听器从未被停止，旧绑定保持可用
            logger.warning(f"快捷键热重载：新监听器就绪失败，已回滚旧绑定: {e}")
            for listener in (new_kb, new_mouse):
                try:
                    if listener is not None:
                        listener.stop()
                except Exception:
                    pass
            self._restore_after_failed_restart(old_map, old_control, old_handler)
            return False

        # 阶段5：提交——停旧监听器，换引用，记录生效配置
        for listener in (old_kb, old_mouse):
            try:
                if listener is not None:
                    listener.stop()
            except Exception as e:
                logger.debug(f"停止旧监听器时发生错误: {e}")
        self.keyboard_listener, self.mouse_listener = new_kb, new_mouse
        self.shortcuts = shortcuts
        enabled_keys = ", ".join(sorted(new_tasks)) or "无启用快捷键"
        logger.info(f"快捷键热重载完成：{enabled_keys}")
        return True

    def _listener_ready(self, listener, timeout: float = 2.0) -> bool:
        """bounded 就绪确认：以监听器线程在钩子注册表中登记的非零原生
        句柄为准（实测库源：ListenerMixin._run 的 _ready 先于钩子安装，
        is_alive 只代表线程存活；句柄也不在监听器实例上，而在
        SystemHook._HOOKS[线程 ident]）。

        无注册表（非 Windows/注入 None）或监听器无线程 ident（测试替身）
        时退化为 存活 + 短暂宽限期。超时未就绪/句柄为零/监听器死亡
        均判为未就绪，由调用方回滚。"""
        if listener is None:
            return True
        registry = self._hook_registry
        native = registry is not None and getattr(listener, 'ident', None) is not None
        if not native:
            time.sleep(0.05)
            return listener.is_alive()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not listener.is_alive():
                return False
            if registry.lookup(listener):
                return True
            time.sleep(0.02)
        return False

    def _set_retired(self, tasks, retired: bool) -> None:
        """在每条任务的迁移锁内设置退役标志；退役同时清流滑入的录音"""
        for task in tasks:
            lock = getattr(task, '_transition_lock', None)
            if lock is None:
                continue
            with lock:
                task._retired = retired
                if retired and task.is_recording:
                    task.finish()

    def _restore_after_failed_restart(self, old_map, old_control, old_handler) -> None:
        with self._hotkey_lock:
            self._pressed_keys.clear()
            self._modifier_used.clear()
            self._active_keyboard.clear()
        self._restoring_keys.clear()
        self.tasks = old_map
        self.control_task = old_control
        self._event_handler = old_handler
        # 解除退役（旧绑定恢复可用）；清流回滚窗口内滑入的启动
        self._set_retired(list(old_map.values()) + [old_control], False)

    # ========== 按键恢复管理 ==========

    def schedule_restore(self, key: str) -> None:
        """
        安排按键恢复（延迟执行，避免在事件处理中阻塞）

        Args:
            key: 要恢复的按键

        注意：标志清除只在按键释放事件中处理（_check_restoring），
        避免在线程中提前清除导致主线程收到重复消息。
        """
        from pynput import keyboard

        self._restoring_keys.add(key)

        def do_restore():
            import time
            time.sleep(0.05)  # 延迟 50ms
            if key == 'caps_lock':
                controller = keyboard.Controller()
                controller.press(keyboard.Key.caps_lock)
                controller.release(keyboard.Key.caps_lock)

        self._pool.submit(do_restore)

    def is_restoring(self, key: str) -> bool:
        """检查是否正在恢复指定按键"""
        return key in self._restoring_keys

    def clear_restoring_flag(self, key: str) -> None:
        """清除恢复标志"""
        self._restoring_keys.discard(key)

    # ========== 防自捕获检查 ==========

    def _check_emulating(self, key_name: str, msg: int, is_mouse: bool = False) -> bool:
        """检查是否正在模拟按键"""
        if not self._emulator.is_emulating(key_name):
            return False

        # 松开时清除标志
        if is_mouse:
            if msg == WM_XBUTTONUP:
                self._emulator.clear_emulating_flag(key_name)
        else:
            if msg in (WM_KEYUP, WM_SYSKEYUP):
                self._emulator.clear_emulating_flag(key_name)

        return True  # 放行

    def _check_restoring(self, key_name: str, msg: int) -> bool:
        """检查是否正在恢复按键"""
        if not self.is_restoring(key_name):
            return False

        if msg in (WM_KEYUP, WM_SYSKEYUP):
            self.clear_restoring_flag(key_name)

        return True  # 放行

    # ========== 公共接口 ==========

    def start(self) -> None:
        """启动所有监听器"""
        has_keyboard = any(s.type == 'keyboard' for s in self.shortcuts if s.enabled)
        has_mouse = any(s.type == 'mouse' for s in self.shortcuts if s.enabled)

        if has_keyboard:
            if self.keyboard_listener and self.keyboard_listener.is_alive():
                logger.debug("键盘监听器已在运行，跳过启动")
            else:
                self.keyboard_listener = keyboard.Listener(
                    win32_event_filter=self.create_keyboard_filter()
                )
                self.keyboard_listener.start()
                logger.info("键盘监听器已启动")

        if has_mouse:
            if self.mouse_listener and self.mouse_listener.is_alive():
                logger.debug("鼠标监听器已在运行，跳过启动")
            else:
                self.mouse_listener = mouse.Listener(
                    win32_event_filter=self.create_mouse_filter()
                )
                self.mouse_listener.start()
                logger.info("鼠标监听器已启动")

        # 打印所有启用的快捷键
        for shortcut in self.shortcuts:
            if shortcut.enabled:
                mode = "长按" if shortcut.hold_mode else "单击"
                toggle = "可恢复" if shortcut.is_toggle_key() else "普通键"
                logger.info(f"  [{shortcut.key}] {mode}模式, 阻塞:{shortcut.suppress}, {toggle}")

    def stop(self) -> None:
        """停止所有监听器和清理资源"""
        self.pause_hotkeys()
        if self.keyboard_listener:
            try:
                self.keyboard_listener.stop()
                logger.debug("键盘监听器已停止")
            except Exception:
                pass
            finally:
                self.keyboard_listener = None
                
        if self.mouse_listener:
            try:
                self.mouse_listener.stop()
                logger.debug("鼠标监听器已停止")
            except Exception:
                pass
            finally:
                self.mouse_listener = None

        # 取消所有任务
        for task in list(self.tasks.values()) + [self.control_task]:
            if task.is_recording:
                task.cancel()

        # 关闭线程池
        self._pool.shutdown(wait=False)
        logger.debug("快捷键管理器线程池已关闭")
