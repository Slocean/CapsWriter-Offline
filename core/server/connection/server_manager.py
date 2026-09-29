# coding: utf-8
"""
WebSocket 管理器 (SocketManager)

负责维护 ASR 服务器的异步通讯层，包括 WebSocket Server 的生命周期管理、
握手鉴权、资源上限、心跳监控、数据发送任务的编排。
"""

import asyncio
import functools
import websockets
from websockets.datastructures import Headers
from websockets.http11 import Response

from config_server import ServerConfig as Config
from .ws_recv import ws_recv
from .ws_send import ws_send
from .control_server import start_control_server
from .. import logger # Server module logger

_REJECT_HEADERS = Headers([('Content-Type', 'text/plain; charset=utf-8'),
                           ('Connection', 'close')])


class SocketManager:
    """
    WebSocket 网络管理器

    负责拉起并维护 WebSocket Server 以及识别结果的异步发送任务。
    对外客户端的鉴权由部署面板「网关 → 机器门」在转发前完成（X-API-Key），
    本进程只在握手阶段执行资源上限检查。
    """
    def __init__(self, app):
        self.app = app
        self._is_running = False
        self._server = None  # websockets.serve 返回的 server 对象
        self._control_server = None

    def _check_port(self):
        """检查端口可用性"""
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((Config.addr, int(Config.port)))
                return True
            except socket.error:
                logger.error(f"端口冲突：{Config.addr}:{Config.port} 已被占用，请检查是否已有服务端正在运行。")
                return False

    def _reject(self, status: int, reason: str) -> Response:
        return Response(status, reason, _REJECT_HEADERS, (reason + '\n').encode('utf-8'))

    async def _process_request(self, connection, request):
        """
        握手阶段拦截：超出资源上限的连接在此拒绝，
        不会进入 ws_recv，也不会分配音频缓存或识别会话。
        """
        remote = connection.remote_address
        remote_ip = remote[0] if remote else None

        if len(self.app.state.sockets) >= Config.ws_max_connections:
            logger.warning(f"连接被拒绝（连接数已达上限 {Config.ws_max_connections}）: {remote_ip}")
            return self._reject(503, 'connection limit reached')
        return None

    async def start(self):
        """
        启动 WebSocket 网络服务
        """
        if self._is_running: return

        # 0. 启动前自检环境
        if not self._check_port():
            input("\n按回车键退出...")
            return

        self._is_running = True

        loop = self.app.loop

        # 1. 优化守护线程执行器 (防止阻塞事件循环)
        from core.tools.daemon_executor import SimpleDaemonExecutor
        loop.set_default_executor(SimpleDaemonExecutor())

        # 2. 准备连接处理器 (注入 app 引用)
        handler = functools.partial(ws_recv, app=self.app)

        # 3. 启动服务（握手阶段执行资源上限检查；对外鉴权由部署面板机器门完成）
        logger.info(
            f"正在拉起 WebSocket 服务 (监听: {Config.addr}:{Config.port}, "
            f"消息上限: {Config.ws_max_message_size // (1024 * 1024)}MB, "
            f"连接上限: {Config.ws_max_connections})"
        )

        async with websockets.serve(
            handler,
            Config.addr,
            Config.port,
            subprotocols=["binary"],
            max_size=Config.ws_max_message_size,
            max_queue=Config.ws_max_queue,
            process_request=self._process_request,
        ) as server:
            self._server = server  # 保存 server 引用，用于外部关闭

            # 3.1 控制通道（管理进程专用，失败不影响主服务）
            self._control_server = await start_control_server(self.app)

            # 4. 进入识别结果发送循环 (作为主阻塞任务)
            logger.info("WebSocket 发送协程已就绪")
            await ws_send(self.app)

        self._is_running = False
        logger.info("SocketManager: WebSocket 服务已退出")

    def stop(self):
        """停止 WebSocket 网络服务"""
        # 主动关闭 WebSocket 服务器，让 ws_send 的 await 尽快返回
        if self._server:
            self._server.close()
        if self._control_server:
            self._control_server.close()
        self._is_running = False
