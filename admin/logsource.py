# coding: utf-8
"""
服务端日志读取（增量尾部）

- 游标式增量读取，限制单次行数与字节数；
- 服务端清洗敏感字段（客户端令牌、Bearer 头），绝不输出令牌明文；
- 默认不展示识别文本详情由调用方决定（本模块只做脱敏，不做截断语义变更）。
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Dict, Union

from .config_admin import AdminConfig as Cfg

_REDACTIONS = [
    (re.compile(r'(?i)\bbearer\s+[A-Za-z0-9._\-]+'), 'Bearer <redacted>'),
    (re.compile(r'\bcw\.[A-Za-z0-9_\-]{8,}'), '<token>'),
    (re.compile(r'\bctrl\.[A-Za-z0-9_\-]{8,}'), '<control-token>'),
]

_lock = threading.RLock()


def sanitize(line: str) -> str:
    for pattern, repl in _REDACTIONS:
        line = pattern.sub(repl, line)
    return line


def tail(cursor: int = 0, max_lines: int = None, max_bytes: int = None) -> Dict[str, Union[list, int, bool]]:
    """
    读取日志文件中 cursor 字节偏移之后的完整行

    Returns:
        {lines, next_cursor, truncated, size}
    """
    max_lines = min(max_lines or Cfg.log_api_max_lines, Cfg.log_api_max_lines)
    max_bytes = min(max_bytes or Cfg.log_api_max_bytes, Cfg.log_api_max_bytes)
    path: Path = Cfg.log_file
    with _lock:
        try:
            size = path.stat().st_size
        except OSError:
            return {'lines': [], 'next_cursor': 0, 'truncated': False, 'size': 0}
        if cursor < 0 or cursor > size:
            cursor = 0  # 文件轮转/截断后重置
        try:
            with open(path, 'rb') as f:
                f.seek(cursor)
                raw = f.read(max_bytes)
        except OSError:
            return {'lines': [], 'next_cursor': cursor, 'truncated': False, 'size': size}

    # 只保留完整行（最后一个 \n 之前的内容），游标始终对齐行首
    cut = raw.rfind(b'\n')
    if cut == -1:
        raw = b''
    else:
        raw = raw[:cut + 1]

    text = raw.decode('utf-8', errors='replace')
    lines = [sanitize(l) for l in text.splitlines()]
    truncated = False
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
        truncated = True
    return {
        'lines': lines,
        'next_cursor': cursor + len(raw),
        'truncated': truncated,
        'size': size,
    }
