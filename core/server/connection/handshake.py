# coding: utf-8
"""
WebSocket 握手鉴权决策模块

在 WebSocket 升级完成前判断是否接受连接：
- 连接数上限；
- 客户端令牌校验（Authorization: Bearer / X-CapsWriter-Token）；
- 可信局域网兼容策略（auth_mode='lan_legacy' 时，直连且未带网关标记的
  可信网段沿用旧行为；任何经反代/网关转发的连接一律要求令牌）。

decide() 是纯函数，便于单元测试；server_manager 在 process_request 中调用。
"""

from __future__ import annotations

import ipaddress
from typing import Iterable, Mapping, Optional, Tuple

from config_server import ServerConfig as Config
from .auth import TokenStore, token_error

__all__ = ['AuthDecision', 'decide', 'HandshakeAuthorizer']

# (允许?) / 拒绝时: (http_status, reason)
AuthDecision = Tuple[bool, int, str]

_TRUSTED_NETS = None


def _trusted_networks() -> list:
    global _TRUSTED_NETS
    if _TRUSTED_NETS is None:
        nets = []
        for raw in getattr(Config, 'lan_trusted_networks', []):
            try:
                nets.append(ipaddress.ip_network(raw, strict=False))
            except ValueError:
                continue
        _TRUSTED_NETS = nets
    return _TRUSTED_NETS


def _is_trusted(remote_ip: Optional[str]) -> bool:
    if not remote_ip:
        return False
    try:
        addr = ipaddress.ip_address(remote_ip.split('%')[0])
    except ValueError:
        return False
    return any(addr in net for net in _trusted_networks())


def extract_token(headers: Mapping[str, str]) -> Optional[str]:
    """从握手请求头提取客户端令牌；没有则返回 None"""
    auth = headers.get('authorization', '')
    if auth.lower().startswith('bearer '):
        token = auth[7:].strip()
        if token:
            return token
    token = headers.get('x-capswriter-token', '').strip()
    if token:
        return token
    return None


def decide(
    remote_ip: Optional[str],
    headers: Mapping[str, str],
    auth_mode: str,
    active_connections: int,
    verify_token,
) -> AuthDecision:
    """
    握手决策

    Args:
        remote_ip: 对端 IP（反代场景为网关地址）
        headers: 握手 HTTP 头（大小写不敏感）
        auth_mode: 'required' 或 'lan_legacy'
        active_connections: 当前已登记连接数（不含本连接）
        verify_token: callable(token) -> Optional[dict]

    Returns:
        (允许?, HTTP 状态码, 原因)
    """
    if active_connections >= Config.ws_max_connections:
        return False, 503, token_error('limit')

    token = extract_token(headers)

    if token is not None:
        entry = verify_token(token)
        if entry is None:
            return False, 401, token_error('invalid')
        if entry.get('last_remote') != remote_ip:
            entry['last_remote'] = remote_ip
        return True, 0, ''

    # 未携带令牌
    if auth_mode != 'lan_legacy':
        return False, 401, token_error('missing')

    forwarded = any(headers.get(marker) for marker in Config.gateway_markers)
    if forwarded:
        # 经反代/网关转发的连接无法从来源 IP 判定真实位置，绝不匿名
        return False, 401, token_error('missing')

    if _is_trusted(remote_ip):
        return True, 0, ''

    return False, 401, token_error('untrusted')


class HandshakeAuthorizer:
    """
    握手鉴权器

    包装 TokenStore 与连接计数，为 websockets 的 process_request 提供回调。
    """

    def __init__(self, store: TokenStore, active_connections=lambda: 0):
        self.store = store
        self.active_connections = active_connections

    def __call__(self, remote_ip: Optional[str], headers: Mapping[str, str]) -> AuthDecision:
        return decide(
            remote_ip=remote_ip,
            headers=headers,
            auth_mode=Config.auth_mode,
            active_connections=self.active_connections(),
            verify_token=self.store.verify,
        )
