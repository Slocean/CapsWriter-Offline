# coding: utf-8
"""
WebSocket 连接管理模块

提供 WebSocketManager 类用于管理与服务端的 WebSocket 连接，
包括连接建立、重连、消息发送和连接状态检查。
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Optional
from urllib.parse import unquote, urlsplit

import websockets
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

from config_client import ClientConfig as Config
from core.protocol import AudioMessage, RecognitionMessage
from ..state import console
from .. import logger
import asyncio

from .credentials import load_api_key, load_api_key_status, legacy_token_present, LEGACY_TOKEN_PREFIX


if TYPE_CHECKING:
    from core.client.state import ClientState
    from ..app import CapsWriterClient


class CommunicationError(Exception):
    """通信层通用异常"""
    pass


def build_server_url() -> str:
    """
    依据配置生成服务端完整 URL

    - 配置了 server_url（ws:// 或 wss://）时优先使用；
    - 否则按 addr + port 拼 ws://（兼容旧配置）。

    Raises:
        ValueError: server_url 非法——协议必须是 ws/wss、不能携带 userinfo、
                    不能携带 fragment、查询参数不能有凭据名（A03：把凭据
                    放进 URL 会绕开「Key 仅存 DPAPI」的产品要求）
    """
    raw = (getattr(Config, 'server_url', '') or '').strip()
    if raw:
        if not raw.lower().startswith(('ws://', 'wss://')):
            raise ValueError(f"server_url 必须以 ws:// 或 wss:// 开头: {raw.split(':')[0]}://…")
        after_scheme = raw.split('://', 1)[1]
        if '@' in after_scheme.split('/', 1)[0].split('?', 1)[0]:
            raise ValueError("server_url 不能携带用户信息（user:pass@），凭据请录入客户端凭据存储")
        # A03：fragment 一律拒绝（WS 地址没有合法用途，且可能夹带凭据）；
        # 查询参数允许普通参数，但凭据名参数（api_key/token/…）拒绝——
        # 参数名归一化与桌面端同一策略（最多两轮 %xx 解码，%61pi_key、
        # %2561pi_key 等编码形式不能绕过）
        if '#' in raw:
            raise ValueError("server_url 不能携带 fragment（#）；凭据请录入客户端凭据存储")
        query = urlsplit(raw).query
        for raw_pair in query.split('&'):
            if not raw_pair:
                continue
            raw_name = raw_pair.split('=', 1)[0]
            if _CRED_PARAM_RE.match(_normalize_query_name(raw_name)):
                raise ValueError(f"server_url 查询参数不能携带凭据（{_normalize_query_name(raw_name)}）；"
                                 "Key 请录入客户端凭据存储")
        return raw.rstrip('/')
    return f"ws://{Config.addr}:{Config.port}"


def _normalize_query_name(raw_name: str) -> str:
    """
    查询参数名归一化（A03，与桌面端 RemoteUrlValidationError 同一策略）：
    最多两轮 %xx 解码、'+' 视为空格、去首尾空白、小写——%61pi_key、
    %2561pi_key 等编码形式与大小写变换不能绕过凭据名拒绝。
    """
    name = raw_name
    for _ in range(2):
        if '%' not in name:
            break
        try:
            decoded = unquote(name)
        except Exception:
            break
        if decoded == name:
            break
        name = decoded
    return name.replace('+', ' ').strip().lower()


def _config_api_key() -> str:
    """配置文件中的 api_key 字段（P1-13：已弃用，检测到即提示迁移，绝不发送）"""
    return (getattr(Config, 'api_key', '') or '').strip()


def _warn_legacy_config_key() -> None:
    """配置文件里残留明文 Key：提示迁移到 DPAPI 凭据存储，不回显值"""
    if _config_api_key():
        logger.warning("config_client.py 中残留明文 api_key/server_token 字段（已弃用，不会发送）；"
                       "请在桌面客户端「服务器」卡片重新录入部署面板 API Key（将以当前用户加密保存）")


def sanitize_url_for_log(url: str) -> str:
    """日志用 URL：去掉 query/fragment，避免任何随 URL 传递的敏感值进入日志"""
    base = (url or '').split('#', 1)[0]
    return base.split('?', 1)[0]


def redact_text(text: str) -> str:
    """
    控制台/异常文本统一脱敏（R05/A03）：

    - 先做当前 DPAPI 已知 Key 的精确替换（绝不经 argv/日志传递 Key 值）；
    - 凭据读取失败/损坏（read_ok=False）不能证明 Key 不存在：回退到当前
      连接握手实际使用的 Key（内存脱敏上下文）；无法保护任何已知 Key 时
      输出固定摘要，绝不回原文；
    - 再打码 JSON 字段与 header/query/Bearer 形式；
    - 清洗自身失败时输出固定摘要，绝不回传原文（失败不降级）。
    """
    try:
        out = str(text or '')
        try:
            key, read_ok = load_api_key_status()
            key = (key or '').strip()
        except Exception:
            key, read_ok = '', False
        session_key = (_ACTIVE_REDACTION_KEY or '').strip()
        if not read_ok and not session_key:
            # 读取失败 ≠ 没有 Key：无法证明文本里没有凭据，固定摘要，绝不回原文
            return _REDACT_FAILED_SUMMARY
        if not read_ok:
            key = ''          # 读不出的 Key 无法精确替换；已用 Key 由会话上下文保护
        for known in (key, session_key):
            if known:
                out = out.replace(known, '<redacted>')
        for pattern, repl in _JSON_STYLE_REDACTIONS:
            out = pattern.sub(repl, out)
        return _TEXT_STYLE_REDACTION.sub(r'\1<redacted>', out)
    except Exception:
        return _REDACT_FAILED_SUMMARY


_REDACT_FAILED_SUMMARY = '（输出清洗失败，可能包含敏感值，内容已省略）'

# 当前连接握手实际使用的 Key（A03：仅内存脱敏上下文，绝不写日志/异常）。
# 凭据存储随后读取失败或变空时，redact_text 仍能保护这个已使用过的 Key。
_ACTIVE_REDACTION_KEY = ''


def _remember_handshake_key(key: str) -> None:
    """记录当前连接握手实际使用的 Key（仅内存，供脱敏上下文；绝不写日志）"""
    global _ACTIVE_REDACTION_KEY
    if key:
        _ACTIVE_REDACTION_KEY = key


_CRED_PARAM_RE = re.compile(
    r'(?i)^(?:x[_-]?api[_-]?key|api[_-]?key|apikey|access[_-]?token|server[_-]?token|'
    r'secret|password|passwd|token|auth|authorization)$')

# JSON / Python 字面量："api_key": "…" 与 'api_key': '…'（A03 反例）
_JSON_STYLE_REDACTIONS = [
    (re.compile(r'(?i)("(?:x-api-key|api-key|api_key|apikey|server_token|token|authorization|password|secret)"\s*:\s*)"[^"]*"'),
     r'\1"<redacted>"'),
    (re.compile(r"(?i)('(?:x-api-key|api-key|api_key|apikey|server_token|token|authorization|password|secret)'\s*:\s*)'[^']*'"),
     r"\1'<redacted>'"),
    (re.compile(r'(?i)\b(api_key|server_token)\s*=\s*[\'"][^\'"]*[\'"]'),
     r"\1='<redacted>'"),
]
_TEXT_STYLE_REDACTION = re.compile(
    r'(?i)(x-api-key\s*[:=]\s*|api_key\s*=\s*|token\s*=\s*|bearer\s+)[A-Za-z0-9._\-]{8,}')


def _handshake_headers(url: str) -> dict:
    """
    握手请求头：仅 wss:// 加密连接携带部署面板 API Key（网关机器门校验）。

    Key 只从当前 Windows 用户的 DPAPI 凭据存储读取（P1-13：配置文件中的
    api_key 字段已弃用，检测到残留值时提示迁移且绝不发送）。明文 ws://
    （局域网直连或误填的远程地址）绝不携带 Key。旧版 cw. 客户端令牌绝不
    能作为面板 Key 发送——检测到时忽略并提示。Key 值绝不写入日志或异常。
    """
    if not (url or '').lower().startswith('wss://'):
        return {}
    _warn_legacy_config_key()
    key = load_api_key().strip()
    if not key:
        return {}
    if key.startswith(LEGACY_TOKEN_PREFIX):
        logger.warning("检测到旧版 CapsWriter 客户端令牌（cw. 开头），已忽略；"
                       "请在客户端重新录入部署面板 API Key")
        return {}
    _remember_handshake_key(key)   # A03：已用于握手的 Key 进入内存脱敏上下文
    return {'X-API-Key': key}


def has_legacy_credentials() -> bool:
    """是否存在需要用户重新录入的旧值（配置文件明文字段或旧凭据存储）"""
    has_config_key = bool(_config_api_key())
    if not has_config_key:
        legacy_config = (getattr(Config, 'server_token', '') or '').strip()
        has_config_key = bool(legacy_config)
    return has_config_key or (not load_api_key().strip() and legacy_token_present())


class WebSocketManager:
    """
    WebSocket 连接管理器

    负责管理与识别服务端的 WebSocket 连接，提供自动重连和
    错误处理功能。

    Attributes:
        app: 客户端 App 实例
        max_retries: 最大重试次数
    """

    def __init__(self, app: CapsWriterClient):
        """
        初始化 WebSocket 管理器

        Args:
            app: 客户端 App 实例
        """
        self.app = app
        self._connect_fail_logged = False  # 断联后只记一次失败日志

    @property
    def state(self) -> ClientState:
        """快捷访问状态单例"""
        return self.app.state
    
    @property
    def is_connected(self) -> bool:
        """检查是否已连接"""
        return self.state.is_connected
    
    async def connect(self) -> bool:
        """
        建立 WebSocket 连接

        尝试连接到配置的服务端地址，如果失败会自动重试。

        Returns:
            连接是否成功
        """
        # 如果已连接，直接返回
        if self.is_connected:
            return True

        # 清理旧连接
        if self.state.websocket is not None:
            self.state.websocket = None

        try:
            url = build_server_url()
        except ValueError as e:
            # A03：已知 Key 先脱敏再进日志——scheme 错误消息会回显原配置
            # 片段（可能含已存 Key），原异常不逐字回显
            logger.error(f"服务端地址配置无效: {redact_text(e)}")
            console.print(f'[bold red]服务端地址配置无效，请检查“远程地址”设置[/bold red]\n')
            return False

        headers = _handshake_headers(url)
        secure = url.lower().startswith('wss://')
        if not secure:
            console.print(f'[grey50]提示：ws:// 为明文连接，仅适合可信局域网[/grey50]')
            if (getattr(Config, 'server_url', '') or '').strip():
                # 用户显式填了远程 ws:// 地址：绝不静默带 Key 降级，明确要求 wss://
                console.print('[yellow]远程地址使用明文 ws://，为避免泄露未携带部署面板 API Key；'
                              '远程接入请改用 wss:// 地址[/yellow]')
        if not headers and secure and has_legacy_credentials():
            console.print('[yellow]检测到旧版客户端令牌，远程接入需要部署面板 API Key；请重新录入[/yellow]')

        try:
            if not self._connect_fail_logged:
                logger.debug(f"正在连接服务端 {sanitize_url_for_log(url)}")

            kwargs = dict(
                uri=url,
                subprotocols=["binary"],
                max_size=None,
                max_queue=None,  # 防止文件过大时，只发送，来不及消费结果，接收队列填满导致 pause_reading
            )

            # websockets>=16.0 默认走代理，本地连接需显式禁用，但 14 才引入这个参数
            if tuple(int(v) for v in websockets.__version__.split(".")) >= (14,):
                kwargs["proxy"] = None
                if headers:
                    kwargs["additional_headers"] = headers
            elif headers:
                kwargs["extra_headers"] = headers

            # wss:// 始终验证服务端证书（使用系统信任库），不提供关闭校验的选项
            self.state.websocket = await websockets.connect(**kwargs)

            # R05：连接成功的控制台输出同样不带 query（Key 不进 URL 也不进回显）
            console.print(f'[bold green]已连接服务端: {sanitize_url_for_log(url)}[/bold green]\n')
            logger.info(f"WebSocket 建立成功: {sanitize_url_for_log(url)}")
            self._connect_fail_logged = False
            return True

        except (ConnectionRefusedError, TimeoutError):
            if not self._connect_fail_logged:
                logger.debug(f"连接服务端 {sanitize_url_for_log(url)} 被拒绝或超时")
                self._connect_fail_logged = True
        except websockets.exceptions.InvalidStatus as e:
            # 握手被拒绝（如网关机器门 401/403）——给出可操作的提示
            status = getattr(getattr(e, 'response', None), 'status_code', None)
            if not self._connect_fail_logged:
                logger.error(f"连接服务端 {sanitize_url_for_log(url)} 被拒绝（HTTP {status}）：请检查部署面板 API Key 是否正确、是否已授权语音站点")
                self._connect_fail_logged = True
            console.print(f'[bold red]服务端拒绝连接 (HTTP {status})：请检查部署面板 API Key[/bold red]\n')
        except Exception as e:
            if not self._connect_fail_logged:
                logger.debug(f"连接服务端 {sanitize_url_for_log(url)} 失败: {redact_text(e)}")
                self._connect_fail_logged = True

        return False
    
    async def send(self, message: AudioMessage) -> bool:
        """
        发送消息到服务端
        
        Args:
            message: 要发送的 AudioMessage 对象
            
        Returns:
            发送是否成功
        """
        if not self.is_connected:
            logger.warning("无法发送消息：WebSocket 未连接")
            return False

        closed = False
        detail = None
        try:
            await self.state.websocket.send(message.to_json())
            return True

        except (websockets.exceptions.ConnectionClosedError, websockets.exceptions.ConnectionClosedOK):
            closed = True

        except Exception as e:
            # 异常文本统一脱敏（R05/A03）：原异常不得随 str(e) 进入新异常
            detail = redact_text(e)

        if closed:
            self.state.websocket = None
            raise CommunicationError("发送失败：连接已断开") from None
        # 在 except 块外抛出：原始异常不进入异常链（__context__ 为空），
        # traceback 不可能把含 Key 的原文带回日志（A03）
        raise CommunicationError(f"发送消息时发生未知错误: {detail}") from None
    
    async def receive(self) -> Optional[RecognitionMessage]:
        """
        接收服务端消息
        
        Returns:
            解析后的 RecognitionMessage 对象，如果失败返回 None
        """
        if not self.is_connected:
            logger.warning("无法接收消息：WebSocket 未连接")
            return None

        closed = False
        parse_failed = False
        detail = None
        try:
            raw_message = await self.state.websocket.recv()
            data = json.loads(raw_message)
            return RecognitionMessage.from_dict(data)

        except (websockets.exceptions.ConnectionClosedError, websockets.exceptions.ConnectionClosedOK):
            closed = True

        except json.JSONDecodeError as e:
            parse_failed = True
            # 异常文本统一脱敏；原异常不得随异常链带回日志（A03）
            detail = redact_text(e)

        except Exception as e:
            detail = redact_text(e)

        if closed:
            self.state.websocket = None
            raise CommunicationError("接收失败：连接已断开") from None
        # 在 except 块外抛出：原始异常不进入异常链（__context__ 为空），
        # traceback 不可能把含 Key 的原文带回日志（A03）
        if parse_failed:
            raise CommunicationError(f"消息解析失败: {detail}") from None
        raise CommunicationError(f"接收消息时发生未知错误: {detail}") from None
    
    async def close(self) -> None:
        """关闭 WebSocket 连接（异常同样脱敏并隔离异常链，A03）"""
        if self.state.websocket is None:
            return

        detail = None
        try:
            await self.state.websocket.close()
        except Exception as e:
            # 关闭异常统一脱敏（R05/A03）：原异常不得随 str(e)/异常链进入日志
            detail = redact_text(e)
        else:
            self.state.websocket = None
            logger.info("WebSocket 连接已关闭")

        if detail is not None:
            # 在 except 块外抛出：原始异常不进入异常链（__context__ 为空），
            # 文件模式 logger(exc_info=True) 的 traceback 不可能带回 Key 原文
            raise CommunicationError(f"关闭连接时发生错误: {detail}") from None

    def close_sync(self) -> None:
        """
        从同步上下文（如 teardown）关闭连接
        
        使用 run_coroutine_threadsafe 安全地将关闭操作调度到已有的事件循环。
        如果事件循环未运行，则直接置空连接引用。
        """
        if self.state.websocket is None:
            return

        loop = self.app.loop
        if loop and loop.is_running():
            asyncio.run_coroutine_threadsafe(self.close(), loop)
            logger.debug("已调度 WebSocket 关闭（threadsafe）")
        else:
            # 事件循环已停止，直接清空引用
            self.state.websocket = None
            logger.debug("事件循环已停，直接置空 WebSocket 引用")
