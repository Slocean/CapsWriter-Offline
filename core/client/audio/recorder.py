# coding: utf-8
"""
音频录制模块

提供 AudioRecorder 类用于管理录音会话，包括开始录音、
发送音频数据到服务端、结束录音等功能。
"""

from __future__ import annotations

import asyncio
import base64
import time
import uuid
from typing import TYPE_CHECKING, Optional

import numpy as np
import websockets

from config_client import ClientConfig as Config
from core.client.state import console
from core.client.audio.file_manager import AudioFileManager
from core.client.audio.pause_segmenter import PauseSegmenter
from core.client.connection import WebSocketManager
from core.protocol import AudioMessage
from core.logger import LIFECYCLE_PREFIX, Logger
from . import logger

# 生命周期遥测通道：INFO 语义、不受用户 log_level 过滤（见 core/logger.py）
lifecycle = Logger.telemetry('client')

if TYPE_CHECKING:
    from core.client.state import ClientState
    from core.client.app import CapsWriterClient

# 日志记录器


class AudioRecorder:
    """
    音频录制器
    
    管理一次完整的录音会话，包括：
    - 从音频流接收数据
    - 可选地保存到本地文件
    - 将音频数据发送到识别服务端
    """
    
    def __init__(self, app: CapsWriterClient):
        """
        初始化录制器
        
        Args:
            app: 客户端 App 实例
        """
        self.app = app
        self.task_id: Optional[str] = None
        self._file_manager: Optional[AudioFileManager] = None
        self._start_time: float = 0.0
        self._duration: float = 0.0
        self._cache: list = []

    @property
    def state(self) -> ClientState:
        """快捷访问状态单例"""
        return self.app.state

    @property
    def _ws_manager(self) -> WebSocketManager:
        """快捷访问桥接到 app.ws"""
        return self.app.ws
    
    async def _send_message(self, message: AudioMessage) -> None:
        """发送消息到服务端"""
        if not self._ws_manager.is_connected:
            if message.is_final:
                self.state.pop_audio_file(message.task_id)
                console.print('    服务端未连接，无法发送\n')
                logger.warning("服务端未连接，无法发送音频数据")
                # 真实失败：任务从未提交，等待方按 id 终态。
                # 走遥测通道的 error 语义：任何 log_level（含 CRITICAL）不丢。
                lifecycle.error(f"{LIFECYCLE_PREFIX}发送失败 {message.task_id}")
            return

        if message.is_final:
            # 在 await 挂起前先登记提交意图：低延迟服务端可能在 send 返回
            # 前就推回最终结果（完成事件先于提交回执到达）。等待方先持有
            # 该任务；成功保持等完成；失败/异常/取消在下方按同一 id 终态。
            lifecycle.info(f"{LIFECYCLE_PREFIX}提交 {message.task_id}")
        try:
            success = await self._ws_manager.send(message)
        except BaseException:
            # 含 asyncio.CancelledError：send 中途被打断，该 id 不会再有
            # 完成事件，按发送失败终态，避免等待方永久亮灯
            if message.is_final:
                lifecycle.error(f"{LIFECYCLE_PREFIX}发送失败 {message.task_id}")
            raise
        if not success and message.is_final:
            self.state.pop_audio_file(message.task_id)
            # 具体错误日志由 WebSocketManager 记录；遥测 error 语义终态
            lifecycle.error(f"{LIFECYCLE_PREFIX}发送失败 {message.task_id}")
    
    async def record_and_send(self) -> None:
        """
        录音并发送数据

        从队列中读取音频数据，保存到文件（如果启用），
        并发送到服务端进行识别。
        """
        try:
            # 生成唯一任务 ID
            self.task_id = str(uuid.uuid1())
            logger.debug(f"创建录音任务，任务ID: {self.task_id}")

            self._start_time = 0.0
            self._duration = 0.0
            self._cache = []

            if Config.live_output and getattr(Config, "pause_segmented", False):
                await self._record_pause_segmented()
                return

            # 音频文件管理
            file_path = None
            if Config.save_audio:
                self._file_manager = AudioFileManager()
            
            # 从队列读取数据
            while task := await self.state.queue_in.get():
                self.state.queue_in.task_done()
                
                if task['type'] == 'begin':
                    self._start_time = task['time']
                    logger.debug(f"录音开始，时间戳: {self._start_time}")
                    
                elif task['type'] == 'data':
                    # 在阈值之前积攒音频数据
                    if task['time'] - self._start_time < Config.threshold:
                        self._cache.append(task['data'])
                        continue
                    
                    # 创建音频文件
                    if Config.save_audio and self._file_manager and file_path is None:
                        file_path, _ = self._file_manager.create(
                            task['data'].shape[1],
                            self._start_time
                        )
                        self.state.register_audio_file(self.task_id, file_path)
                        logger.debug(f"创建音频文件: {file_path}")
                    
                    # 获取音频数据
                    if self._cache:
                        data = np.concatenate(self._cache)
                        self._cache.clear()
                    else:
                        data = task['data']
                    
                    # 保存音频至本地文件
                    self._duration += len(data) / 48000
                    if Config.save_audio and self._file_manager:
                        self._file_manager.write(data)
                    
                    # 发送音频数据用于识别
                    message = AudioMessage(
                        task_id=self.task_id,
                        source='mic',
                        data=base64.b64encode(
                            np.mean(data[::3], axis=1).tobytes()
                        ).decode('utf-8'),
                        is_final=False,
                        time_start=self._start_time,
                        seg_duration=Config.mic_seg_duration,
                        seg_overlap=Config.mic_seg_overlap,
                        context=Config.context,
                        language=Config.language,
                    )
                    asyncio.create_task(self._send_message(message))
                    
                elif task['type'] == 'finish':
                    # 如果有缓存的数据未发送，先发送缓存
                    if self._cache:
                        data = np.concatenate(self._cache)
                        self._cache.clear()
                        
                        self._duration += len(data) / 48000
                        if Config.save_audio and self._file_manager:
                            self._file_manager.write(data)

                        message = AudioMessage(
                            task_id=self.task_id,
                            source='mic',
                            data=base64.b64encode(
                                np.mean(data[::3], axis=1).tobytes()
                            ).decode('utf-8'),
                            is_final=False,
                            time_start=self._start_time,
                            seg_duration=Config.mic_seg_duration,
                            seg_overlap=Config.mic_seg_overlap,
                            context=Config.context,
                            language=Config.language,
                        )
                        asyncio.create_task(self._send_message(message))

                    # 完成写入本地文件
                    if Config.save_audio and self._file_manager:
                        self._file_manager.finish()
                        logger.debug("完成音频文件写入")
                    
                    console.print(f'任务标识：{self.task_id}')
                    console.print(f'    录音时长：{self._duration:.2f}s')
                    logger.info(f"录音任务完成，任务ID: {self.task_id}, 时长: {self._duration:.2f}s")
                    
                    # 告诉服务端音频片段结束了
                    message = AudioMessage(
                        task_id=self.task_id,
                        source='mic',
                        data='',
                        is_final=True,
                        time_start=self._start_time,
                        seg_duration=Config.mic_seg_duration,
                        seg_overlap=Config.mic_seg_overlap,
                        context=Config.context,
                        language=Config.language,
                    )
                    asyncio.create_task(self._send_message(message))
                    break

        except asyncio.CancelledError:
            # 录音被取消（短按时间过短 / 单击模式超时取消）。
            # 此时本会话的 begin 以及阈值前积攒的 data 可能还残留在全局
            # queue_in 中，而该队列由所有录音会话共用。若不清理，下一次
            # record_and_send() 会读到带旧 task_id 语义的脏数据，导致
            # 「前文丢失 / 旧结果混入」的串线问题。
            drained = 0
            while True:
                try:
                    self.state.queue_in.get_nowait()
                    self.state.queue_in.task_done()
                    drained += 1
                except asyncio.QueueEmpty:
                    break
            logger.debug(
                f"录音任务被取消，已排空队列残留 {drained} 条，任务ID: {self.task_id}"
            )
            raise

        except Exception as e:
            logger.error(f"录音任务错误: {e}", exc_info=True)
    
    async def _send_phrase(self, audio: np.ndarray) -> None:
        """Send one speech phrase as a final task; the server needs no changes."""
        if audio is None or len(audio) == 0:
            return
        task_id = str(uuid.uuid4())
        self.task_id = task_id
        mono = np.mean(audio[::3], axis=1, dtype=np.float32)
        message = AudioMessage(
            task_id=task_id,
            source='mic',
            data=base64.b64encode(mono.tobytes()).decode('ascii'),
            is_final=True,
            time_start=max(self._start_time, time.time() - len(audio) / 48000),
            seg_duration=60,
            seg_overlap=0,
            context=Config.context,
            language=Config.language,
        )
        logger.info(f"停顿后发送语音: {len(audio) / 48000:.2f}s, 任务ID: {task_id}")
        await self._send_message(message)

    async def _record_pause_segmented(self) -> None:
        """Keep the mic open, sending voiced phrases only after a pause."""
        segmenter = PauseSegmenter(pause_seconds=float(getattr(Config, "pause_seconds", 0.75)))
        file_path = None
        phrase_count = 0
        if Config.save_audio:
            self._file_manager = AudioFileManager()
        try:
            while task := await self.state.queue_in.get():
                self.state.queue_in.task_done()
                if task['type'] == 'begin':
                    self._start_time = task['time']
                elif task['type'] == 'data':
                    if not self._start_time:
                        continue
                    data = task['data']
                    self._duration += len(data) / 48000
                    if Config.save_audio and self._file_manager:
                        if file_path is None:
                            file_path, _ = self._file_manager.create(data.shape[1], self._start_time)
                        self._file_manager.write(data)
                    phrase = segmenter.feed(data)
                    if phrase is not None:
                        phrase_count += 1
                        await self._send_phrase(phrase)
                elif task['type'] == 'finish':
                    phrase = segmenter.flush()
                    if phrase is not None:
                        phrase_count += 1
                        await self._send_phrase(phrase)
                    logger.info(
                        f"停顿分段录音完成: {self._duration:.2f}s, "
                        f"有效语音 {phrase_count} 段"
                    )
                    break
        finally:
            if self._file_manager:
                self._file_manager.finish()

    def get_file_manager(self) -> Optional[AudioFileManager]:
        """获取当前的文件管理器"""
        return self._file_manager
