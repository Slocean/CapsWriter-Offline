# coding: utf-8
"""
管理端会话与认证

- 管理员密码：PBKDF2-HMAC-SHA256 哈希存储；首次启动自动生成并只在控制台显示一次；
- 会话：内存态随机 token，HttpOnly Cookie，带过期；登录成功后重置会话；
- CSRF：写操作要求 X-CSRF-Token 头与 csrf Cookie 一致（double submit），
  并校验 Origin/Referer；
- 登录失败限速：每 IP 每分钟失败次数受限。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from pathlib import Path
from typing import Dict, Optional

from .config_admin import AdminConfig as Cfg

PBKDF2_ITERATIONS = 240_000


def _hash_password(password: str, salt: bytes) -> str:
    dk = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, PBKDF2_ITERATIONS)
    return dk.hex()


class AdminAuth:
    def __init__(self):
        self._lock = threading.RLock()
        self._sessions: Dict[str, Dict] = {}
        self._login_failures: Dict[str, list] = {}
        self._ensure_password()

    # ---------- 密码 ----------

    def _ensure_password(self) -> None:
        Cfg.data_dir.mkdir(parents=True, exist_ok=True)
        if Cfg.password_file.exists():
            return
        password = 'cw-admin-' + secrets.token_urlsafe(12)
        self._store_password(password)
        print('=' * 62)
        print('  CapsWriter 管理端首次启动，已生成管理员密码（仅显示这一次）：')
        print(f'      {password}')
        print('  请立即保存。修改密码：python -m admin set-password')
        print('=' * 62)

    def _store_password(self, password: str) -> None:
        salt = secrets.token_bytes(16)
        data = {
            'salt': salt.hex(),
            'hash': _hash_password(password, salt),
            'iterations': PBKDF2_ITERATIONS,
            'created_at': time.time(),
        }
        tmp = Cfg.password_file.with_suffix('.tmp')
        tmp.write_text(json.dumps(data), encoding='utf-8')
        tmp.replace(Cfg.password_file)

    def set_password(self, password: str) -> None:
        if not password or len(password) < 8:
            raise ValueError('密码至少 8 位')
        with self._lock:
            self._store_password(password)
            self._sessions.clear()

    def verify_password(self, password: str) -> bool:
        try:
            data = json.loads(Cfg.password_file.read_text(encoding='utf-8'))
            salt = bytes.fromhex(data['salt'])
            iterations = int(data.get('iterations', PBKDF2_ITERATIONS))
        except Exception:
            return False
        dk = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, iterations)
        expected = data.get('hash', '')
        return hmac.compare_digest(dk.hex(), expected)

    # ---------- 登录限速 ----------

    def login_allowed(self, ip: str) -> bool:
        with self._lock:
            now = time.time()
            fails = [t for t in self._login_failures.get(ip, []) if now - t < 60]
            self._login_failures[ip] = fails
            return len(fails) < Cfg.login_rate_limit

    def record_login_failure(self, ip: str) -> None:
        with self._lock:
            self._login_failures.setdefault(ip, []).append(time.time())

    # ---------- 会话 ----------

    def login(self) -> str:
        """创建新会话，返回会话 token"""
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions = {
                k: v for k, v in self._sessions.items()
                if v['expires_at'] > time.time()
            }
            self._sessions[token] = {
                'expires_at': time.time() + Cfg.session_ttl,
                'csrf': secrets.token_urlsafe(24),
                'created_at': time.time(),
            }
        return token

    def logout(self, session_token: str) -> None:
        with self._lock:
            self._sessions.pop(session_token, None)

    def session(self, session_token: str) -> Optional[Dict]:
        if not session_token:
            return None
        with self._lock:
            session = self._sessions.get(session_token)
            if not session:
                return None
            if session['expires_at'] <= time.time():
                self._sessions.pop(session_token, None)
                return None
            return dict(session)
