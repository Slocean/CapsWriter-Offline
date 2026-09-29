# coding: utf-8
"""
客户端令牌管理模块

为语音 WebSocket 握手提供独立的高熵客户端令牌：
- 令牌明文只在创建/轮换响应中出现一次，服务端只保存 SHA-256 哈希；
- 校验使用常数时间比较（hmac.compare_digest）；
- 存储文件采用「临时文件 + 原子替换」写入，替换前保留 .bak 备份；
- 每个令牌可单独吊销或轮换，支持记录创建时间与末次使用。

该模块不得在任何日志或异常信息中输出令牌明文。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

__all__ = ['TokenStore', 'hash_token', 'token_error']


class TokenStore:
    """
    客户端令牌存储

    Attributes:
        path: JSON 存储文件路径（只包含令牌哈希等元数据）
    """

    SCHEME = 'cw.'           # 令牌前缀，便于日志脱敏与误贴识别
    MAX_TOKENS = 64          # 防止无限增长
    TOUCH_FLUSH_INTERVAL = 60.0  # 末次使用时间的落盘节流（秒）

    def __init__(self, path: os.PathLike | str):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = {'version': 1, 'tokens': []}
        self._last_touch_flush = 0.0
        self._load()

    # ---------- 持久化 ----------

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding='utf-8'))
            tokens = raw.get('tokens')
            if isinstance(tokens, list):
                self._data = {'version': 1, 'tokens': [t for t in tokens if isinstance(t, dict)]}
        except Exception:
            # 文件损坏时保守起见拒绝所有令牌，不覆盖已有文件（人工恢复后可用）
            self._data = {'version': 1, 'tokens': []}

    def _flush(self, backup: bool = True) -> None:
        """原子写入存储文件；替换前保留 .bak 备份"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._data, ensure_ascii=False, indent=2)
        tmp = self.path.with_name(self.path.name + '.tmp')
        tmp.write_text(payload, encoding='utf-8')
        if backup and self.path.exists():
            try:
                os.replace(self.path, self.path.with_name(self.path.name + '.bak'))
            except OSError:
                pass
        os.replace(tmp, self.path)

    # ---------- 查询与校验 ----------

    @staticmethod
    def hash_token(token: str) -> str:
        return hashlib.sha256(token.encode('utf-8')).hexdigest()

    @property
    def tokens(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._data['tokens'])

    def verify(self, token: str) -> Optional[Dict[str, Any]]:
        """
        校验令牌，返回对应条目；无效/吊销返回 None

        比较使用常数时间函数；命中时更新并（节流）落盘末次使用信息。
        """
        if not token or not isinstance(token, str):
            return None
        digest = self.hash_token(token)
        with self._lock:
            for entry in self._data['tokens']:
                stored = entry.get('hash', '')
                if not hmac.compare_digest(stored, digest):
                    continue
                if entry.get('revoked'):
                    return None
                self._touch(entry)
                return entry
            return None

    def _touch(self, entry: Dict[str, Any]) -> None:
        now = time.time()
        entry['last_used_at'] = now
        if now - self._last_touch_flush >= self.TOUCH_FLUSH_INTERVAL:
            try:
                self._flush()
                self._last_touch_flush = now
            except OSError:
                pass

    # ---------- 管理 ----------

    def create(self, name: str) -> tuple[str, Dict[str, Any]]:
        """
        创建新令牌

        Returns:
            (明文令牌, 条目)。明文只在此时可用，之后无法再取回。
        """
        name = (name or '').strip()[:64] or '未命名客户端'
        token = self.SCHEME + secrets.token_urlsafe(32)
        entry = {
            'id': secrets.token_hex(4),
            'name': name,
            'hash': self.hash_token(token),
            'created_at': time.time(),
            'last_used_at': None,
            'last_remote': None,
            'revoked': False,
        }
        with self._lock:
            self._data['tokens'] = [t for t in self._data['tokens'] if not t.get('revoked')][-self.MAX_TOKENS + 1:]
            self._data['tokens'].append(entry)
            self._flush()
        return token, entry

    def rotate(self, entry_id: str) -> Optional[tuple[str, Dict[str, Any]]]:
        """轮换令牌：旧令牌立即失效，返回新明文"""
        with self._lock:
            entry = self._find(entry_id)
            if entry is None or entry.get('revoked'):
                return None
        token, new_entry = self.create(entry.get('name', '未命名客户端'))
        with self._lock:
            old = self._find(entry_id)
            if old is not None:
                old['revoked'] = True
                old['rotated_to'] = new_entry['id']
            self._flush()
        return token, new_entry

    def revoke(self, entry_id: str) -> bool:
        """吊销令牌（幂等：重复吊销返回 True；不存在返回 False）"""
        with self._lock:
            entry = self._find(entry_id)
            if entry is None:
                return False
            if not entry.get('revoked'):
                entry['revoked'] = True
                self._flush()
            return True

    def _find(self, entry_id: str) -> Optional[Dict[str, Any]]:
        for entry in self._data['tokens']:
            if hmac.compare_digest(entry.get('id', ''), entry_id or ''):
                return entry
        return None

    def get(self, entry_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            entry = self._find(entry_id)
            return dict(entry) if entry else None

    def summary(self) -> List[Dict[str, Any]]:
        """管理接口用的脱敏列表：绝不含哈希以外的敏感值"""
        with self._lock:
            return [
                {
                    'id': e.get('id'),
                    'name': e.get('name'),
                    'created_at': e.get('created_at'),
                    'last_used_at': e.get('last_used_at'),
                    'last_remote': e.get('last_remote'),
                    'revoked': bool(e.get('revoked')),
                }
                for e in self._data['tokens']
            ]


def token_error(kind: str) -> str:
    """可诊断但不含令牌值的拒绝原因"""
    return {
        'missing': 'missing token',
        'invalid': 'invalid token',
        'revoked': 'revoked token',
        'untrusted': 'untrusted source without token',
        'limit': 'connection limit reached',
    }.get(kind, 'authentication failed')
