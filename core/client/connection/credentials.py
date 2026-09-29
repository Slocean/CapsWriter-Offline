# coding: utf-8
"""
客户端令牌凭据存储

令牌保存在当前 Windows 用户的 DPAPI 加密文件中（%LOCALAPPDATA%），
不进入配置文件、日志或便携包；拷贝便携包到另一台电脑后需重新录入令牌。

- Windows：CryptProtectData / CryptUnprotectData（CurrentUser 范围，经 ctypes）
- 其他平台：退化为仅当前用户可读的明文 JSON 文件（0o600）

桌面客户端（C#）与 Python 客户端共用同一存储路径与加密格式。
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import sys
from pathlib import Path

from config_client import ClientConfig

__all__ = ['default_credentials_path', 'load_token', 'save_token', 'clear_token']

APP_DIR_NAME = 'CapsWriterOffline'
CREDENTIALS_FILE = 'credentials.json'
TOKEN_KEY = 'server_token'


def default_credentials_path() -> Path:
    override = (getattr(ClientConfig, 'credential_store', '') or '').strip()
    if override:
        return Path(override)
    base = os.environ.get('LOCALAPPDATA')
    if not base:
        base = Path.home() / 'AppData' / 'Local'
    return Path(base) / APP_DIR_NAME / CREDENTIALS_FILE


# ---------- Windows DPAPI ----------

class _DATA_BLOB(ctypes.Structure):
    _fields_ = [('cbData', ctypes.c_ulong), ('pbData', ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> _DATA_BLOB:
    buf = ctypes.create_string_buffer(data, len(data))
    return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _dpapi_protect(data: bytes) -> bytes:
    blob_in = _blob(data)
    blob_out = _DATA_BLOB()
    CRYPTPROTECT_UI_FORBIDDEN = 0x1
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(blob_in), None, None, None, None,
        CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out),
    ):
        raise OSError('CryptProtectData failed')
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _dpapi_unprotect(data: bytes) -> bytes:
    blob_in = _blob(data)
    blob_out = _DATA_BLOB()
    CRYPTPROTECT_UI_FORBIDDEN = 0x1
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None,
        CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out),
    ):
        raise OSError('CryptUnprotectData failed')
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


# ---------- 读写 ----------

def _read_store(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return {}


def _write_store(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    if sys.platform == 'win32':
        blob = _dpapi_protect(payload.encode('utf-8'))
        path.write_text(json.dumps({'protected': 'dpapi', 'blob': base64.b64encode(blob).decode('ascii')}),
                        encoding='utf-8')
    else:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(payload)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def _plain_payload(path: Path) -> dict:
    raw = _read_store(path)
    if raw.get('protected') == 'dpapi':
        try:
            blob = base64.b64decode(raw.get('blob', ''))
            return json.loads(_dpapi_unprotect(blob).decode('utf-8'))
        except Exception:
            return {}
    return raw


def load_token(path: os.PathLike | str | None = None) -> str:
    """读取令牌；损坏或不存在返回空串。绝不在日志中输出值。"""
    try:
        p = Path(path) if path else default_credentials_path()
        if not p.exists():
            return ''
        token = _plain_payload(p).get(TOKEN_KEY, '')
        return token if isinstance(token, str) else ''
    except Exception:
        return ''


def save_token(token: str, path: os.PathLike | str | None = None) -> None:
    """保存令牌（DPAPI 加密）；空串等价于清除"""
    p = Path(path) if path else default_credentials_path()
    if not token:
        clear_token(p)
        return
    data = _plain_payload(p)
    data[TOKEN_KEY] = token
    _write_store(p, data)


def clear_token(path: os.PathLike | str | None = None) -> None:
    p = Path(path) if path else default_credentials_path()
    try:
        p.unlink(missing_ok=True)
    except OSError:
        pass
