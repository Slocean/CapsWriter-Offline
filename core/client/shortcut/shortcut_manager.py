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
from core.client.shortcut.event_handler import ShortcutEventHandler
from core.client.shortcut.task import ShortcutTask

if TYPE_CHECKING:
    from core.client.shortcut.shortcut_config import Shortcut
    from core.client.state import ClientState
    from core.client.app import CapsWriterClient



class ShortcutManager:
    """
    快捷键管理器

    统一管理多个快捷键，使用 pynput 监听键盘和鼠标事件。
    所有事件处理都在 win32_event_filter 中完成，确保高性能和低延迟。
    """

    def __init__(self, app: CapsWriterClient, shortcuts: List[Shortcut]):
        """
        初始化快捷键管理器

        Args:
            app: 客户端 App 实例
            shortcuts: 快捷键配置列表
        """
        self.app = app
        self.shortcuts = shortcuts

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

        # 初始化快捷键任务
        self._init_tasks()

    @property
    def state(self) -> ClientState:
        """快捷访问状态单例"""
        return self.app.state

    def _init_tasks(self) -> None:
        """初始化所有快捷键任务"""
        from config_client import ClientConfig as Config

        for shortcut in self.shortcuts:
            if not shortcut.enabled:
                continue

            task = ShortcutTask(self.app, shortcut)
            task._manager_ref = lambda: self  # 弱引用，用于回调
            task.pool = self._pool
            task.threshold = shortcut.get_threshold(Config.threshold)
            self.tasks[shortcut.key] = task

        # The GUI's UDP button must work even if every keyboard shortcut is off.
        from core.client.shortcut.shortcut_config import Shortcut
        self.control_task = ShortcutTask(self.app, Shortcut(key='desktop_control', enabled=False))
        self.control_task._manager_ref = lambda: self
        self.control_task.pool = self._pool

    _MODIFIERS = {'ctrl', 'alt', 'shift'}

    def hotkeys_paused(self) -> bool:
        return time.monotonic() < self._hotkeys_paused_until

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
