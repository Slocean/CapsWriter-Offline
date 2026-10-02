# coding: utf-8
"""
客户端部署面板 API Key 凭据存储

远程接入只使用部署面板已有的一把 API Key（网关机器门校验 X-API-Key 头）。
Key 保存在当前 Windows 用户的 DPAPI 加密文件中（%LOCALAPPDATA%），
不进入配置文件、日志或便携包；拷贝便携包到另一台电脑后需重新录入。

历史版本曾在此存储过 CapsWriter 自建的 `cw.` 客户端令牌（JSON 键
`server_token`）。这类旧值不是面板 Key，绝不能作为 API Key 发送；
本模块只读取新键 `api_key`，并提供旧值检测供 UI 提示重新录入。

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

__all__ = ['default_credentials_path', 'load_api_key', 'load_api_key_status', 'save_api_key',
           'clear_api_key', 'legacy_token_present', 'LEGACY_TOKEN_PREFIX']

APP_DIR_NAME = 'CapsWriterOffline'
CREDENTIALS_FILE = 'credentials.json'
API_KEY_KEY = 'api_key'          # 新键：部署面板 API Key
LEGACY_KEY = 'server_token'      # 旧键：CapsWriter 自建客户端令牌（已废弃）
LEGACY_TOKEN_PREFIX = 'cw.'


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

def _read_store_checked(path: Path) -> tuple[dict, bool]:
    """
    读取存储 JSON：返回 (data, parsed)。解析失败不再吞成空 dict——
    调用方需要区分「确实没有 Key」与「读取失败」（A03：读取失败时不能
    证明凭据不存在，绝不能当作无 Key 回原文）。
    """
    try:
        return json.loads(path.read_text(encoding='utf-8')), True
    except Exception:
        return {}, False


def _plain_payload_checked(path: Path) -> tuple[dict, bool]:
    """
    返回 (payload, read_ok)：存储不存在 → ({}, True)（确实没有）；
    存储损坏 / JSON 解析失败 / DPAPI 解密失败 → ({}, False)（读取失败，
    不能证明 Key 不存在）。
    """
    raw, parsed = _read_store_checked(path)
    if not parsed:
        return {}, False
    if raw.get('protected') == 'dpapi':
        try:
            blob = base64.b64decode(raw.get('blob', ''))
            return json.loads(_dpapi_unprotect(blob).decode('utf-8')), True
        except Exception:
            return {}, False
    return raw, True


def _read_store(path: Path) -> dict:
    return _read_store_checked(path)[0]


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
    payload, _ok = _plain_payload_checked(path)
    return payload


def load_api_key(path: os.PathLike | str | None = None) -> str:
    """读取部署面板 API Key；不存在或损坏返回空串。绝不在日志中输出值。"""
    return load_api_key_status(path)[0]


def load_api_key_status(path: os.PathLike | str | None = None) -> tuple[str, bool]:
    """
    读取部署面板 API Key 并区分「确实没有」与「读取失败」（A03）。

    返回 (key, read_ok)：存储不存在 → ('', True)（确实没有，正常按无 Key
    处理）；存储损坏 / JSON 解析失败 / DPAPI 解密失败 / 读取异常 →
    ('', False)（读取失败，不能证明 Key 不存在——调用方不得当作无 Key
    回原文，应按读取失败处理）。绝不在日志中输出 Key 值。
    """
    try:
        p = Path(path) if path else default_credentials_path()
        if not p.exists():
            return '', True
        payload, ok = _plain_payload_checked(p)
        key = payload.get(API_KEY_KEY, '')
        return (key if isinstance(key, str) else ''), ok
    except Exception:
        return '', False


def legacy_token_present(path: os.PathLike | str | None = None) -> bool:
    """检测是否残留旧版 cw. 客户端令牌（UI 应提示重新录入面板 Key）"""
    try:
        p = Path(path) if path else default_credentials_path()
        if not p.exists():
            return False
        value = _plain_payload(p).get(LEGACY_KEY, '')
        return bool(value)
    except Exception:
        return False


def save_api_key(key: str, path: os.PathLike | str | None = None) -> None:
    """保存部署面板 API Key（DPAPI 加密）；空串等价于清除"""
    p = Path(path) if path else default_credentials_path()
    if not key:
        clear_api_key(p)
        return
    data = _plain_payload(p)
    data[API_KEY_KEY] = key
    # 顺带清掉已废弃的旧令牌值，避免长期残留
    if LEGACY_KEY in data:
        del data[LEGACY_KEY]
    _write_store(p, data)


def clear_api_key(path: os.PathLike | str | None = None) -> None:
    p = Path(path) if path else default_credentials_path()
    try:
        p.unlink(missing_ok=True)
    except OSError:
        pass
