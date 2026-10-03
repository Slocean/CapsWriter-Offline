# coding: utf-8
"""
快捷键任务模块

管理单个快捷键的录音任务状态
"""

from __future__ import annotations
import asyncio
import threading
import time
from threading import Event
from typing import TYPE_CHECKING, Optional

from . import logger
from core.tools.my_status import Status
 
if TYPE_CHECKING:
    from core.client.shortcut.shortcut_config import Shortcut
    from core.client.state import ClientState
    from core.client.audio.recorder import AudioRecorder
    from core.client.app import CapsWriterClient



class ShortcutTask:
    """
    单个快捷键的录音任务

    跟踪每个快捷键独立的录音状态，防止互相干扰。
    """

    def __init__(self, app: CapsWriterClient, shortcut: Shortcut, recorder_class=None):
        """
        初始化快捷键任务

        Args:
            app: 客户端 App 实例
            shortcut: 快捷键配置
            recorder_class: AudioRecorder 类（可选，用于延迟导入）
        """
        self.app = app
        self.shortcut = shortcut
        self._recorder_class = recorder_class

        # 任务状态
        self.task: Optional[asyncio.Future] = None
        self.recording_start_time: float = 0.0
        self.is_recording: bool = False

        # hold_mode 状态跟踪
        self.pressed: bool = False
        self.released: bool = True
        self.event: Event = Event()

        # 线程池（用于 countdown）
        self.pool = None

        # 录音输出静音租约（launch 时获取，finish/cancel 时释放）
        self._mute_lease = None
        # 录音会话代号：识别录音协程兜底回调归属，避免误伤更新的会话
        self._mute_generation = 0
        # 退役标志：热重载替换映射后，晚到的旧任务 launch 一律 no-op
        # （防池化 countdown/补发回调在替换后把已删除的任务拉起录音）
        self._retired = False
        # 录音状态迁移锁：launch/finish/cancel/兜底回调全部原子化——
        # 会话代号在任何状态发布之前预留，旧回调的检查与清理不可能
        # 插入到新会话的建立过程中（RLock：已完成的 future 会在
        # launch 持锁期间内联执行回调）
        self._transition_lock = threading.RLock()

        # 录音状态动画
        self._status = Status('开始录音', spinner='point')

    @property
    def state(self) -> ClientState:
        """快捷访问状态单例"""
        return self.app.state

    def _acquire_mute_lease(self):
        """录音开始前静音全部本地播放设备，返回租约（结束时必须 release）。

        开关关闭/平台不支持/静音失败时返回 None；任何失败都只记日志，
        绝不阻断录音。租约在本方法内登记：重复获取复用现有租约，
        防止重复 launch 覆盖租约导致引用计数泄漏。
        """
        if self._mute_lease is not None:
            return self._mute_lease
        from config_client import ClientConfig as Config
        from core.client.audio.output_mute import acquire_output_mute_lease
        enabled = getattr(Config, 'mute_output_while_recording', True)
        self._mute_lease = acquire_output_mute_lease(
            reason=f'shortcut:{self.shortcut.key}', enabled=enabled)
        return self._mute_lease

    def _release_mute_lease(self) -> None:
        """恢复各播放设备原静音状态（幂等；异常只记日志）"""
        lease, self._mute_lease = self._mute_lease, None
        if lease is None:
            return
        try:
            lease.release()
        except Exception as e:
            logger.warning(f"恢复输出静音状态失败: {e}")

    def _get_recorder(self) -> AudioRecorder:
        """获取 AudioRecorder 实例"""
        if self._recorder_class is None:
            from core.client.audio.recorder import AudioRecorder
            self._recorder_class = AudioRecorder
        return self._recorder_class(self.app)

    def launch(self) -> None:
        """启动录音任务（原子迁移：重复 START 幂等，不重建会话、不换租约）"""
        with self._transition_lock:
            if self._retired:
                logger.debug(f"[{self.shortcut.key}] 任务已退役，忽略开始录音")
                return
            if self.is_recording:
                logger.debug(f"[{self.shortcut.key}] 忽略重复开始录音（会话进行中）")
                return
            if getattr(self.state, 'shutdown_pending', False):
                # 关闭闩锁已落下：确认恢复完成之后、进程终止之前，
                # 任何输入路径（快捷键/UDP）都不得再建立新的静音会话
                logger.debug(f"[{self.shortcut.key}] 客户端正在关闭，忽略开始录音")
                return

            logger.info(f"[{self.shortcut.key}] 触发：开始录音")

            try:
                # 先预留会话代号（任何状态发布之前），再静音、再放行采集
                self._mute_generation += 1
                generation = self._mute_generation

                # 先静音播放设备，再允许录音状态放行音频采集：
                # 麦克风保持可用，本机扬声器/耳机输出在录音期间不可闻
                self._mute_lease = self._acquire_mute_lease()

                # 静音建立期间关闭闩锁可能已落下（PREPARE_SHUTDOWN 与
                # 本迁移并发）：放弃本次开始并立即恢复，保证回执之后
                # 不存在任何活动的静音会话
                if getattr(self.state, 'shutdown_pending', False):
                    logger.info(
                        f"[{self.shortcut.key}] 关闭闩锁已落下，放弃本次开始录音")
                    self._release_mute_lease()
                    return

                # 重载边界发布准入：事务进行中拒绝非现役任务的发布，
                # 防止新旧录音重叠后一方 finish 清掉全局录音状态
                manager = self._safe_manager()
                if manager is not None and not manager.admit_launch(self):
                    self._release_mute_lease()
                    logger.debug(
                        f"[{self.shortcut.key}] 重载事务进行中，放弃本次开始录音")
                    return

                # 记录开始时间
                self.recording_start_time = time.time()
                self.is_recording = True

                # 将开始标志放入队列
                asyncio.run_coroutine_threadsafe(
                    self.state.queue_in.put({'type': 'begin', 'time': self.recording_start_time, 'data': None}),
                    self.app.loop
                )

                # 更新录音状态
                self.state.start_recording(self.recording_start_time)

                # 打印动画：正在录音
                self._status.start()

                # 启动识别任务（协程意外终止时由回调兜底恢复，见 _on_recorder_done）
                recorder = self._get_recorder()
                self.task = asyncio.run_coroutine_threadsafe(
                    recorder.record_and_send(),
                    self.app.loop,
                )
                self.task.add_done_callback(
                    lambda future: self._on_recorder_done(future, generation))
            except Exception:
                # 启动失败绝不能把系统静音带走，录音状态标志一并复位
                self._release_mute_lease()
                self._reset_recording_state()
                raise

    def _safe_manager(self):
        """取 manager 引用（测试/无管理器场景返回 None）"""
        ref = getattr(self, '_manager_ref', None)
        if not callable(ref):
            return None
        try:
            return ref()
        except Exception:
            return None

    def _reset_recording_state(self) -> None:
        """复位本任务的录音状态标志（异常路径兜底）"""
        self.is_recording = False
        self.state.stop_recording()
        try:
            self._status.stop()
        except Exception:
            pass

    def _on_recorder_done(self, future, generation: int) -> None:
        """录音协程结束兜底：未走 finish/cancel 就终止时复位并恢复输出。

        身份检查与清理在同一把迁移锁内原子完成：正常完成/取消路径已
        复位标志，此回调为 no-op；旧会话的回调（代号不匹配）不得干扰
        更新的录音会话。
        """
        with self._transition_lock:
            if generation != self._mute_generation or not self.is_recording:
                return
            logger.warning(
                f"[{self.shortcut.key}] 录音协程异常终止，兜底复位录音状态并恢复输出静音")
            self._reset_recording_state()
            self._release_mute_lease()

    def cancel(self) -> None:
        """取消录音任务（时间过短）"""
        with self._transition_lock:
            if not self.is_recording:
                return

            logger.debug(f"[{self.shortcut.key}] 取消录音任务（时间过短）")

            self.is_recording = False
            self.state.stop_recording()
            self._release_mute_lease()
            self._status.stop()

            if self.task is not None:
                self.task.cancel()
            self.task = None

    def finish(self) -> None:
        """完成录音任务"""
        with self._transition_lock:
            if not self.is_recording:
                return

            logger.info(f"[{self.shortcut.key}] 释放：完成录音")

            self.is_recording = False
            self.state.stop_recording()
            # 录音真正结束：立即恢复输出，不等 ASR/LLM 完成
            self._release_mute_lease()
            self._status.stop()

            asyncio.run_coroutine_threadsafe(
                self.state.queue_in.put({
                    'type': 'finish',
                    'time': time.time(),
                    'data': None
                }),
                self.app.loop
            )

            # 执行 restore（可恢复按键 + 非阻塞模式）
            # 阻塞模式下按键不会发送到系统，状态不会改变，不需要恢复
            if self.shortcut.is_toggle_key() and not self.shortcut.suppress:
                self._restore_key()

    def _restore_key(self) -> None:
        """恢复按键状态（防自捕获逻辑由 ShortcutManager 处理）"""
        # 通知管理器执行 restore
        # 防自捕获：管理器会设置 flag 再发送按键
        manager = self._manager_ref()
        if manager:
            logger.debug(f"[{self.shortcut.key}] 自动恢复按键状态 (suppress={self.shortcut.suppress})")
            manager.schedule_restore(self.shortcut.key)
        else:
            logger.warning(f"[{self.shortcut.key}] manager 引用丢失，无法 restore")
