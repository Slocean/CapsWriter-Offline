# coding: utf-8
"""
UDP 控制模块

通过 UDP 信号控制录音的开始和停止，使外部程序能够触发语音转录。

命令协议：
- START: 开始录音
- STOP: 停止录音
"""

from __future__ import annotations

import os
import socket
import sys
import threading
from typing import TYPE_CHECKING

from config_client import ClientConfig as Config
from . import logger

if TYPE_CHECKING:
    from core.client.shortcut.shortcut_manager import ShortcutManager



class UDPController:
    """
    UDP 控制器
    
    在后台线程监听 UDP 端口，接收控制命令来开始/停止录音。
    """
    
    def __init__(self, shortcut_manager: ShortcutManager):
        """
        初始化 UDP 控制器

        Args:
            shortcut_manager: 快捷键管理器实例，用于调用录音控制方法
        """
        self.manager = shortcut_manager
        self.running = False
        self._thread = None
        self._sock = None
    
    def start(self) -> None:
        """启动 UDP 监听"""
        if self.running and self._thread and self._thread.is_alive():
            logger.debug("UDP 控制器已在运行，跳过启动")
            return
        
        self.running = True
        self._thread = threading.Thread(target=self._listen, daemon=True, name="UDPController")
        self._thread.start()
        logger.info(f"UDP 控制器已启动，监听端口: {Config.udp_control_port}")
    
    def stop(self) -> None:
        """停止 UDP 监听"""
        if not self.running:
            return
            
        self.running = False
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            finally:
                self._sock = None
        logger.info("UDP 控制器已停止")
    
    def _listen(self) -> None:
        """监听循环"""
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind((Config.udp_control_addr, Config.udp_control_port))
            self._sock.settimeout(0.5)
            
            logger.debug(f"UDP 控制器绑定到 {Config.udp_control_addr}:{Config.udp_control_port}")
            
            while self.running:
                try:
                    data, addr = self._sock.recvfrom(1024)
                    command = data.decode('utf-8').strip().upper()
                    self._handle_command(command, addr)
                except socket.timeout:
                    continue
                except Exception as e:
                    if self.running:
                        logger.error(f"UDP 控制器接收错误: {e}")
        
        except Exception as e:
            logger.error(f"UDP 控制器启动失败: {e}")
        finally:
            if self._sock:
                self._sock.close()
    
    def _handle_command(self, command: str, addr: tuple) -> None:
        """
        处理接收到的命令

        Args:
            command: 命令字符串 (START/STOP)
            addr: 发送方地址
        """
        state = self.manager.state

        if command == 'START':
            if getattr(state, 'shutdown_pending', False):
                logger.debug("UDP 控制：忽略 START 命令（客户端正在关闭）")
            elif not state.recording:
                logger.info(f"UDP 控制：开始录音 (来自 {addr[0]}:{addr[1]})")
                self.manager.control_task.launch()
            else:
                logger.debug("UDP 控制：忽略 START 命令（已在录音中）")

        elif command == 'STOP':
            if state.recording:
                logger.info(f"UDP 控制：停止录音 (来自 {addr[0]}:{addr[1]})")
                # 停止所有录音任务，包括桌面按钮对应的独立任务
                for task in list(self.manager.tasks.values()) + [self.manager.control_task]:
                    if task.is_recording:
                        task.finish()
            else:
                logger.debug("UDP 控制：忽略 STOP 命令（未在录音）")

            # 无论是否在录音都回执 STOPPED：录音中的路径在 finish() 同步
            # 完成输出静音恢复之后才会执行到这里，回执即证明恢复已完成。
            self._reply(addr, b'STOPPED')

        elif command == 'PREPARE_SHUTDOWN' or command.startswith('PREPARE_SHUTDOWN|'):
            self._handle_prepare_shutdown(command, addr)

        elif command == 'RELOAD_HOTKEYS':
            self._handle_reload_hotkeys(addr)

        elif command == 'PAUSE_HOTKEYS':
            self.manager.pause_hotkeys()
            self._sock.sendto(b'PAUSED', addr)

        elif command == 'RESUME_HOTKEYS':
            self.manager.resume_hotkeys()
            self._sock.sendto(b'RESUMED', addr)

        else:
            logger.warning(f"UDP 控制：未知命令 '{command}' (来自 {addr[0]}:{addr[1]})")

    def _load_shortcuts_fresh(self):
        """直读 config_client.py 源码字节并现场 compile+exec——绕过
        importlib.reload 的 pyc 缓存（缓存键是 mtime+size，同大小同
        mtime 的改写会被旧字节码顶掉）。解析/校验失败抛异常，调用方
        不发布任何状态。返回 (新模块对象, Shortcut 列表)。
        """
        import pathlib
        import types as _types

        import config_client as published
        from ..shortcut.shortcut_config import Shortcut

        path = getattr(published, '__file__', None)
        if not path:
            raise RuntimeError('config_client 缺少 __file__，无法直读源码')
        source = pathlib.Path(path).read_bytes()      # 现场直读，无任何缓存
        code = compile(source, str(path), 'exec')     # 不生成/不消费 pyc
        module = _types.ModuleType('config_client')
        module.__file__ = str(path)
        exec(code, module.__dict__)
        cfg = getattr(module, 'ClientConfig', None)
        if cfg is None or not hasattr(cfg, 'shortcuts'):
            raise RuntimeError('配置缺少 ClientConfig.shortcuts')
        entries = cfg.shortcuts
        if not isinstance(entries, list):
            raise RuntimeError('shortcuts 必须是列表')
        shortcuts = [Shortcut(**sc) for sc in entries]   # 构造即校验字段
        return module, shortcuts

    def _handle_reload_hotkeys(self, addr: tuple) -> None:
        """桌面端快捷键设置变更后的热重载（写盘已由桌面端完成）。

        事务顺序：直读源码并构建/校验新快捷键（失败不动任何状态）→
        发布新配置模块 → manager.restart 事务切换（内部结算在途任务、
        按静音生命周期清流、新监听器就绪才算成功，失败回滚旧绑定）→
        回执。restart 返回 False 或抛异常时回滚模块发布并回执
        RELOAD_FAILED（旧快捷键保持生效）；成功回执 RELOADED。
        """
        from ..shortcut.shortcut_config import Shortcut  # noqa: F401 (语义锚定)

        try:
            module, shortcuts = self._load_shortcuts_fresh()
        except Exception as e:
            logger.warning(f"UDP 控制：快捷键配置读取/校验失败（保持旧快捷键）: {e}")
            self._reply(addr, b'RELOAD_FAILED')
            return

        previous = sys.modules.get('config_client')
        sys.modules['config_client'] = module   # restart 内的 Config 读取到新配置
        try:
            applied = self.manager.restart(shortcuts)
            if applied is False:
                raise RuntimeError('新快捷键监听器未就绪，已回滚旧绑定')
        except Exception as e:
            if previous is not None:
                sys.modules['config_client'] = previous   # 回滚发布
            logger.warning(f"UDP 控制：快捷键重载失败（保持旧快捷键生效）: {e}")
            self._reply(addr, b'RELOAD_FAILED')
            return
        # 清理确认：本次清流若留有未完成的输出静音恢复，不回执成功
        # （桌面端将走重启兜底，其 PREPARE_SHUTDOWN 握手会重试恢复）
        from core.client.audio.output_mute import pending_restore_count
        if pending_restore_count() > 0:
            logger.warning("UDP 控制：快捷键已重载，但存在未完成的输出静音恢复，不回执成功")
            self._reply(addr, b'RELOAD_FAILED')
            return
        logger.info("UDP 控制：快捷键已重载")
        self._reply(addr, b'RELOADED')

    def _reply(self, addr: tuple, data: bytes) -> None:
        if self._sock is None:
            return
        try:
            self._sock.sendto(data, addr)
        except Exception as e:
            logger.warning(f"UDP 控制：回执 {data!r} 到 {addr} 失败: {e}")

    def _handle_prepare_shutdown(self, command: str, addr: tuple) -> None:
        """桌面端终止客户端前的专用握手（普通 STOP 不受影响）。

        - 先落下关闭闩锁：此后任何输入路径（快捷键/UDP）都无法建立
          新的静音会话，确认与终止之间不会出现重新静音；
        - 同步结束全部录音任务（finish 内部完成输出静音恢复）；
        - 兜底清场并重试未完成的恢复，拿到真实恢复结果后才回执：
          SHUTDOWN_READY|<pid>[|<token>]   已无录音且全部可达设备恢复成功；
          SHUTDOWN_RESTORE_FAILED|<pid>[|<token>]  仍有可达设备恢复失败，
          桌面端必须放弃终止（不得 Kill）。
        token 为请求中的 nonce，原样回带以关联本次请求。
        """
        token = command.split('|', 1)[1].strip() if '|' in command else ''
        state = self.manager.state
        state.shutdown_pending = True
        try:
            tasks = list(self.manager.tasks.values()) + [self.manager.control_task]
            # 先等每条任务的在途状态迁移（launch/finish/兜底回调）结算完毕
            # 再结束录音：回执绝不先于并发中的开始/结束动作，杜绝
            # "确认之后才落地的新静音会话"。新 START 已被闩锁拦下。
            for task in tasks:
                lock = getattr(task, '_transition_lock', None)
                if lock is not None:
                    with lock:
                        pass
            for task in tasks:
                if task.is_recording:
                    task.finish()
        except Exception as e:
            logger.warning(f"UDP 控制：PREPARE_SHUTDOWN 结束录音任务时出错: {e}")

        from core.client.audio.output_mute import force_release_output_mute
        restored = force_release_output_mute('prepare-shutdown')

        verb = 'SHUTDOWN_READY' if restored else 'SHUTDOWN_RESTORE_FAILED'
        payload = f"{verb}|{os.getpid()}"
        if token:
            payload += f"|{token}"
        logger.info(f"UDP 控制：PREPARE_SHUTDOWN 回执 {payload} (来自 {addr[0]}:{addr[1]})")
        self._reply(addr, payload.encode('ascii'))
