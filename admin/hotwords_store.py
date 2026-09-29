# coding: utf-8
"""
热词文件读取/写入（hot-server.txt）

- 限定只能操作配置指定的热词文件，不接受网页端任意路径；
- 原子替换写入并保留备份；
- 服务端在模型加载时读取热词，保存后需重启识别进程才能生效（UI 需提示）。
"""

from __future__ import annotations

import os
import shutil
import threading
from pathlib import Path
from typing import Dict, Union

from .config_admin import AdminConfig as Cfg

MAX_HOTWORDS_BYTES = 256 * 1024

_lock = threading.RLock()


def read_hotwords() -> Dict[str, Union[str, int, None]]:
    path: Path = Cfg.hotwords_path
    if not path.exists():
        return {'text': '', 'mtime': None, 'exists': False}
    with _lock:
        raw = path.read_bytes()[:MAX_HOTWORDS_BYTES + 1]
    if len(raw) > MAX_HOTWORDS_BYTES:
        raise ValueError('热词文件过大，为安全起见拒绝在网页中读取；请手工整理文件')
    return {
        'text': raw.decode('utf-8-sig', errors='replace'),
        'mtime': int(path.stat().st_mtime),
        'exists': True,
    }


def write_hotwords(text: str) -> None:
    if not isinstance(text, str):
        raise ValueError('热词内容必须是文本')
    data = text.encode('utf-8')
    if len(data) > MAX_HOTWORDS_BYTES:
        raise ValueError('热词内容超过 256KB 上限')
    if '\x00' in text:
        raise ValueError('热词内容包含非法字符')
    path: Path = Cfg.hotwords_path
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            shutil.copy2(path, path.with_name(path.name + '.bak'))
        tmp = path.with_name(path.name + '.tmp')
        tmp.write_bytes(data)
        os.replace(tmp, path)
