# coding: utf-8
"""
管理操作审计日志

追加式 JSONL，记录登录、令牌管理、配置修改、进程控制等关键操作。
不含令牌明文或配置值，只记录动作与结果。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Union

from .config_admin import AdminConfig as Cfg

_lock = threading.Lock()


def record(action: str, ip: str = '', ok: bool = True, detail: str = '') -> None:
    entry = {
        'time': time.strftime('%Y-%m-%d %H:%M:%S'),
        'ts': time.time(),
        'ip': ip,
        'action': action,
        'ok': bool(ok),
        'detail': detail[:200],
    }
    path: Path = Cfg.audit_log
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            with open(path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(entry, ensure_ascii=False) + '\n')
    except OSError:
        pass


def tail(lines: int = 50) -> list:
    path: Path = Cfg.audit_log
    try:
        with _lock:
            raw = path.read_text(encoding='utf-8').splitlines()
        out = []
        for line in raw[-lines:]:
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return out
    except OSError:
        return []
