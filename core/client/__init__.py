# coding: utf-8
"""
客户端模块

提供 CapsWriter 客户端的所有功能模块。

模块架构：
- state: 客户端状态管理
- connection/: WebSocket 连接管理
- audio/: 音频相关（录制、流、文件管理）
- shortcut/: 快捷键处理（原 input/）
- output/: 结果处理和输出（原 processing/）
- udp/: UDP 控制
- transcribe/: 文件转录
- diary/: 日记写入
- ui/: 用户界面
"""

from config_client import ClientConfig as Config
from core.logger import get_logger, setup_logger

# 直接在这里配置主日志级别
setup_logger('client', level=Config.log_level)
logger = get_logger('client')


def __getattr__(name):
    # 惰性导入门面类：子模块（如 connection/）不再连带拉起
    # numpy/sounddevice/pystray 等重型依赖
    if name == 'CapsWriterClient':
        from core.client.app import CapsWriterClient
        return CapsWriterClient
    raise AttributeError(name)


__all__ = [
    'CapsWriterClient',
]

