# coding: utf-8
"""
ASR 控制通道客户端（管理进程侧）

与识别进程内的回环控制通道通信：查询状态、请求优雅退出、调整日志级别。
令牌来自 logs/server_control.token（ASR 启动时生成）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Optional

import websockets


class ControlClient:
    """短连接控制通道客户端（每次请求建立/关闭，简单可靠）"""

    def __init__(self, url: str, token_path: Path):
        self.url = url
        self.token_path = Path(token_path)

    def _token(self) -> str:
        try:
            return self.token_path.read_text(encoding='utf-8').strip()
        except OSError:
            return ''

    async def request(self, cmd: str, timeout: float = 3.0, **args) -> Optional[dict]:
        """发送一条命令并等待回复；通道不可用或超时返回 None"""
        token = self._token()
        if not token:
            return None
        try:
            async with websockets.connect(
                self.url,
                additional_headers={'Authorization': f'Bearer {token}'},
                open_timeout=timeout,
                close_timeout=2,
                max_size=256 * 1024,
            ) as ws:
                await ws.send(json.dumps({'id': 1, 'cmd': cmd, **args}))
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                    reply = json.loads(raw)
                    if reply.get('id') == 1:
                        return reply
        except Exception:
            return None

    def status_sync(self, timeout: float = 3.0) -> Optional[dict]:
        """线程上下文中获取 ASR 状态"""
        return self.request_sync('status', timeout=timeout)

    def request_sync(self, cmd: str, timeout: float = 3.0, **args) -> Optional[dict]:
        """线程上下文中发送命令"""
        try:
            return asyncio.run(self.request(cmd, timeout=timeout, **args))
        except Exception:
            return None
