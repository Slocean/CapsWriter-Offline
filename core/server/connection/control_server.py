# coding: utf-8
"""
ASR 控制通道（管理进程专用）

在同一事件循环中额外监听 127.0.0.1:<control_port> 的 WebSocket：
- 仅接受本机回环来源；
- 握手要求与 logs/server_control.token 中一致的令牌（Authorization: Bearer）；
- 只接受白名单命令：status / shutdown / set_log_level；
- 不接触音频、不接入识别队列，识别进程停止或崩溃时管理页仍能独立显示故障。

协议：每条消息一个 JSON 请求 {"id": n, "cmd": "...", ...args}
      回复 {"id": n, "ok": true/false, ...}；未知命令返回 ok=false。
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional, Set

import websockets

from config_server import ServerConfig as Config
from .. import logger

if TYPE_CHECKING:
    from ..app import CapsWriterServer

__all__ = ['ensure_control_token', 'start_control_server']

_STATUS_KEYS_WHITELIST = frozenset({
    'version', 'model_type', 'addr', 'port', 'pid', 'uptime_s',
    'connections', 'worker_alive', 'queue_in', 'busy', 'log_level',
})


def ensure_control_token(path: os.PathLike | str) -> str:
    """读取（首次则生成）控制通道令牌文件；明文只保存在该文件中"""
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            token = p.read_text(encoding='utf-8').strip()
            if token:
                return token
        token = 'ctrl.' + secrets.token_urlsafe(32)
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(token)
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
        return token
    except OSError as e:
        logger.error(f"控制通道令牌文件不可用: {e}")
        return ''


def _collect_status(app: 'CapsWriterServer') -> Dict[str, Any]:
    state = app.state
    proc = state.recognize_process
    started = getattr(app, 'started_at', None)
    last_result = getattr(state, 'last_result_time', 0.0)
    log_level = getattr(Config, 'log_level', 'INFO')
    status = {
        'version': app.version,
        'model_type': Config.model_type,
        'addr': str(Config.addr),
        'port': str(Config.port),
        'pid': os.getpid(),
        'uptime_s': round(time.time() - started, 1) if started else None,
        'connections': len(state.sockets),
        'worker_alive': bool(proc is not None and proc.is_alive()),
        'queue_in': state.queue_in.qsize() if state.queue_in is not None else None,
        'busy': (time.time() - last_result) < 5.0 if last_result else False,
        'log_level': log_level,
    }
    return {k: status[k] for k in _STATUS_KEYS_WHITELIST}


async def control_handler(websocket, app: 'CapsWriterServer') -> None:
    """控制通道连接处理（仅回环 + 令牌）"""
    remote = websocket.remote_address
    if not remote or remote[0] not in ('127.0.0.1', '::1'):
        logger.warning(f"控制通道拒绝非回环来源: {remote}")
        return

    auth = websocket.request.headers.get('authorization', '') if websocket.request else ''
    expected = getattr(app, 'control_token', '')
    if not expected or auth != f'Bearer {expected}':
        logger.warning(f"控制通道拒绝无效令牌: {remote[0]}")
        await websocket.close(code=4401, reason='unauthorized')
        return

    logger.info(f"控制通道已连接: {remote[0]}:{remote[1]}")
    try:
        async for raw in websocket:
            reply = await _handle_command(raw, app, websocket)
            await websocket.send(json.dumps(reply, ensure_ascii=False))
    except websockets.ConnectionClosed:
        pass
    except Exception as e:
        logger.error(f"控制通道异常: {e}")
    finally:
        logger.info("控制通道连接结束")


async def _handle_command(raw, app: 'CapsWriterServer', websocket) -> Dict[str, Any]:
    try:
        req = json.loads(raw)
        req_id = req.get('id')
        cmd = req.get('cmd')
    except Exception:
        return {'id': None, 'ok': False, 'error': 'bad request'}

    if cmd == 'status':
        return {'id': req_id, 'ok': True, 'status': _collect_status(app)}

    if cmd == 'ping':
        return {'id': req_id, 'ok': True}

    if cmd == 'set_log_level':
        level = str(req.get('level', '')).upper()
        import logging
        if level not in ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'):
            return {'id': req_id, 'ok': False, 'error': 'invalid level'}
        Config.log_level = level
        logger.setLevel(getattr(logging, level))
        logger.info(f"日志级别已调整为 {level}（控制通道）")
        return {'id': req_id, 'ok': True}

    if cmd == 'shutdown':
        logger.info("控制通道请求优雅退出")
        asyncio.get_event_loop().call_later(0.1, app.stop)
        return {'id': req_id, 'ok': True, 'status': 'shutting down'}

    return {'id': req_id, 'ok': False, 'error': 'unknown command'}


async def start_control_server(app: 'CapsWriterServer'):
    """
    启动控制通道服务；失败（如端口被占）仅记录告警，不影响主识别服务
    """
    port = int(getattr(Config, 'control_port', 0) or 0)
    if port <= 0:
        return None
    app.control_token = ensure_control_token(Config.control_token_path)
    if not app.control_token:
        return None
    handler = lambda ws: control_handler(ws, app)
    try:
        server = await websockets.serve(
            handler,
            '127.0.0.1',
            port,
            max_size=64 * 1024,
            max_queue=(8, 4),
        )
        logger.info(f"控制通道已监听 127.0.0.1:{port}")
        return server
    except OSError as e:
        logger.warning(f"控制通道启动失败（管理页将使用 PID/端口监控）: {e}")
        return None
