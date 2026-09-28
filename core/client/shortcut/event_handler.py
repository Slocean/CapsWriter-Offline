# coding: utf-8
"""
事件处理器

处理键盘和鼠标事件的逻辑
"""

import time
from . import logger



class ShortcutEventHandler:
    """
    快捷键事件处理器

    处理按键按下和释放的逻辑，包括录音启动、取消、完成等
    """

    def __init__(self, tasks, pool, emulator):
        """
        初始化事件处理器

        Args:
            tasks: 快捷键任务字典
            pool: 线程池
            emulator: 快捷键模拟器
        """
        self.tasks = tasks
        self.pool = pool
        self.emulator = emulator

    def handle_keydown(self, key_name, task) -> None:
        """Handle the first physical key-down; repeated key-downs are ignored."""
        if not task.is_recording and task.state.recording:
            return
        if task.shortcut.hold_mode:
            if not task.is_recording:
                task.launch()
            return
        if task.pressed:
            return
        task.pressed = True
        if task.is_recording:
            task.finish()
        else:
            task.launch()

    def handle_keyup(self, key_name, task) -> None:
        """处理按键释放事件"""
        if not task.shortcut.hold_mode:
            task.pressed = False
            return

        # 长按模式
        if not task.is_recording:
            return

        duration = time.time() - task.recording_start_time
        logger.debug(f"[{key_name}] 松开，持续时间: {duration:.2f}s")

        if duration < task.threshold:
            self._handle_short_press(key_name, task)
        else:
            task.finish()

    def _handle_short_press(self, key_name, task) -> None:
        """处理短按情况"""
        cancel_start = time.perf_counter()
        task.cancel()
        cancel_time = (time.perf_counter() - cancel_start) * 1000
        logger.debug(f"[{key_name}] task.cancel() 耗时: {cancel_time:.2f}ms")

        if task.shortcut.suppress and '+' not in task.shortcut.key:
            logger.debug(f"[{key_name}] 安排异步补发按键")
            self.pool.submit(self.emulator.emulate_key, key_name)
