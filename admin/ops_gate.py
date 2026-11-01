# coding: utf-8
"""
跨模块操作互斥闸门

同一时间至多允许一个「重操作」执行：ASR 启动/停止/重启、模型下载任务、
模型目录删除。用途是封死下载/删除/启停之间的竞争窗口（例如 ASR 正在
加载模型目录时同名目录被下载发布覆盖）。

- 非阻塞获取：被占用时立即返回 False，由调用方转成 409 冲突；
- 持有者标签进入审计与错误信息，便于说明当前被什么操作占用；
- 闸门只管互斥，不做任何 IO；持有时长由各操作自己决定
  （ASR 动作持有到动作线程结束，下载任务持有到任务线程结束）。
"""

from __future__ import annotations

import threading
from typing import Optional


class OpsGate:
    def __init__(self):
        self._lock = threading.Lock()
        self._owner: Optional[str] = None

    def acquire(self, owner: str) -> bool:
        with self._lock:
            if self._owner is not None:
                return False
            self._owner = owner
            return True

    def release(self, owner: str) -> None:
        """只有当前持有者本人能释放，防止迟到的 finally 误清别人的锁"""
        with self._lock:
            if self._owner == owner:
                self._owner = None

    @property
    def owner(self) -> Optional[str]:
        with self._lock:
            return self._owner

    def busy(self) -> bool:
        return self.owner is not None


OPS_GATE = OpsGate()
