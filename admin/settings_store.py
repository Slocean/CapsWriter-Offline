# coding: utf-8
"""
网页可修改设置的存储与校验

第一版仅开放白名单字段；写入前先校验，再原子替换并保留备份。
config_server.py 在进程启动时读取此文件作为运行时覆盖。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .config_admin import AdminConfig as Cfg

__all__ = ['SETTINGS_SCHEMA', 'load_settings', 'save_settings', 'validate_settings']

SETTINGS_SCHEMA: Dict[str, Dict[str, Any]] = {
    'model_type': {
        'label': '识别模型',
        'type': 'enum',
        'choices': ['qwen_asr', 'fun_asr_nano', 'sensevoice', 'paraformer'],
        'restart': True,   # 修改后需重启识别进程
    },
    'log_level': {
        'label': '日志级别',
        'type': 'enum',
        'choices': ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'],
        'restart': False,
    },
    'auth_mode': {
        'label': '鉴权模式',
        'type': 'enum',
        'choices': ['lan_legacy', 'required'],
        'restart': False,
        'help': 'required=所有客户端必须持令牌连接；lan_legacy=可信局域网直连免令牌（公网路径仍强制令牌）',
    },
    'aligner_idle_timeout': {
        'label': '对齐引擎空闲释放（秒）',
        'type': 'int',
        'min': 0,
        'max': 3600,
        'restart': False,
    },
    'gpu_boost_enabled': {
        'label': 'GPU 预加速',
        'type': 'bool',
        'restart': False,
    },
}

_lock = threading.RLock()


def load_settings() -> Dict[str, Any]:
    with _lock:
        try:
            data = json.loads(Cfg.settings_file.read_text(encoding='utf-8'))
            if isinstance(data, dict):
                return {k: v for k, v in data.items() if k in SETTINGS_SCHEMA}
        except Exception:
            pass
        return {}


def validate_settings(values: Any) -> Tuple[Optional[Dict[str, Any]], str]:
    """校验提交的设置；返回 (规范化后的增量, 错误信息)"""
    if not isinstance(values, dict):
        return None, '设置必须是 JSON 对象'
    cleaned: Dict[str, Any] = {}
    for key, value in values.items():
        spec = SETTINGS_SCHEMA.get(key)
        if spec is None:
            return None, f'不允许修改设置项: {key}'
        if spec['type'] == 'enum':
            if value not in spec['choices']:
                return None, f'{spec["label"]} 的取值必须是 {"/".join(spec["choices"])}'
        elif spec['type'] == 'int':
            if isinstance(value, bool) or not isinstance(value, int):
                return None, f'{spec["label"]} 必须是整数'
            if not (spec['min'] <= value <= spec['max']):
                return None, f'{spec["label"]} 必须在 {spec["min"]}–{spec["max"]} 之间'
        elif spec['type'] == 'bool':
            if not isinstance(value, bool):
                return None, f'{spec["label"]} 必须是布尔值'
        cleaned[key] = value
    return cleaned, ''


def save_settings(values: Dict[str, Any]) -> Tuple[bool, str]:
    """合并写入设置文件（校验 → 原子替换 → 备份）"""
    with _lock:
        cleaned, err = validate_settings(values)
        if err:
            return False, err
        current = {}
        try:
            current = json.loads(Cfg.settings_file.read_text(encoding='utf-8'))
            if not isinstance(current, dict):
                current = {}
        except Exception:
            current = {}
        current.update(cleaned)
        Cfg.settings_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = Cfg.settings_file.with_name(Cfg.settings_file.name + '.tmp')
        tmp.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding='utf-8')
        if Cfg.settings_file.exists():
            try:
                os.replace(Cfg.settings_file, Cfg.settings_file.with_name(Cfg.settings_file.name + '.bak'))
            except OSError:
                pass
        os.replace(tmp, Cfg.settings_file)
        return True, ''
