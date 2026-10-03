# coding: utf-8
"""
UDP RELOAD_HOTKEYS 热重载回归（真实加载器路径）

评审反例 continuation-20261004-reload-cache-review 的永久回归：
importlib.reload 的 pyc 缓存按 mtime+size 命中，同大小同 mtime 的改写
会静默加载旧配置并仍回执 RELOADED。实现改为直读源码字节现场
compile+exec，本测试用真实 UDP 处理器 + 独立临时配置驱动真实加载器
（不 mock importlib.reload）：

- f6 → f7 同大小同 mtime 改写：两次 RELOAD 各自加载最新磁盘内容；
- 语法错误/结构非法的配置：RELOAD_FAILED，旧模块与旧绑定不动；
- PAUSE_HOTKEYS / RESUME_HOTKEYS 的 PAUSED / RESUMED 回执保持不变。
"""

import importlib
import json
import os
import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.client.udp.udp_control import UDPController


def config_source(key: str) -> str:
    return ("class ClientConfig:\n"
            "    shortcuts = [{'key': '" + key + "', 'type': 'keyboard', "
            "'enabled': True, 'hold_mode': False}]\n")


class _FakeManager:
    def __init__(self):
        self.state = SimpleNamespace(recording=False)
        self.tasks = {}
        self.control_task = SimpleNamespace(is_recording=False)
        self.restart_keys = []

    def restart(self, shortcuts):
        self.restart_keys.append([s.key for s in shortcuts])
        return True


class UdpReloadFreshLoaderTests(unittest.TestCase):
    def setUp(self):
        self._prev_module = sys.modules.get('config_client')
        self._tmp = tempfile.TemporaryDirectory(prefix='reload-fresh-')
        self.path = pathlib.Path(self._tmp.name) / 'config_client.py'
        self.path.write_text(config_source('f6'), encoding='utf-8')
        sys.path.insert(0, self._tmp.name)
        sys.modules.pop('config_client', None)
        importlib.invalidate_caches()
        import config_client   # noqa: F401 —— 建立与生产一致的已导入模块状态
        self.manager = _FakeManager()
        self.controller = UDPController(self.manager)
        self.sent = []
        self.controller._sock = SimpleNamespace(
            sendto=lambda data, addr: self.sent.append(bytes(data).decode('ascii')))
        self.addr = ('127.0.0.1', 65100)

    def tearDown(self):
        sys.path.remove(self._tmp.name)
        if self._prev_module is not None:
            sys.modules['config_client'] = self._prev_module
        else:
            sys.modules.pop('config_client', None)
        importlib.invalidate_caches()
        self._tmp.cleanup()

    def _edit_same_stat(self, key: str) -> None:
        """同大小同 mtime 的改写：pyc 缓存键完全不变"""
        stamp = self.path.stat()
        self.path.write_text(config_source(key), encoding='utf-8')
        os.utime(self.path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))

    def test_same_size_same_mtime_edit_applies_latest_disk_content(self):
        self.controller._handle_command('RELOAD_HOTKEYS', self.addr)
        self._edit_same_stat('f7')
        self.controller._handle_command('RELOAD_HOTKEYS', self.addr)

        self.assertEqual(self.manager.restart_keys, [['f6'], ['f7']])
        self.assertEqual(self.sent, ['RELOADED', 'RELOADED'])

    def test_broken_config_fails_without_touching_state(self):
        self.controller._handle_command('RELOAD_HOTKEYS', self.addr)
        self.assertEqual(self.sent, ['RELOADED'])
        good_keys = self.manager.restart_keys

        self.path.write_text('class ClientConfig:\n  shortcuts = [语法错误\n', encoding='utf-8')
        self.controller._handle_command('RELOAD_HOTKEYS', self.addr)

        self.assertEqual(self.sent, ['RELOADED', 'RELOAD_FAILED'])
        self.assertEqual(self.manager.restart_keys, good_keys)   # 旧绑定未动
        # 失败回滚：sys.modules 里的 config_client 保持最近一次成功发布的内容
        shortcuts = __import__('config_client').ClientConfig.shortcuts
        self.assertEqual([sc['key'] for sc in shortcuts], ['f6'])

    def test_structural_invalid_shortcuts_rejected(self):
        self.path.write_text(
            "class ClientConfig:\n"
            "    shortcuts = {'not': 'a list'}\n", encoding='utf-8')
        self.controller._handle_command('RELOAD_HOTKEYS', self.addr)
        self.assertEqual(self.sent, ['RELOAD_FAILED'])
        self.assertEqual(self.manager.restart_keys, [])


class UdpReloadCleanupHonestyTests(unittest.TestCase):
    """恢复失败不得谎报清理成功：存在未完成静音恢复时不回执 RELOADED"""

    def test_pending_restore_reports_failure(self):
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
        from core.client.audio import output_mute as output_mute_pkg
        from test_output_mute_owner import FakeEndpointBackend

        manager = _FakeManager()
        controller = UDPController(manager)
        sent = []
        controller._sock = SimpleNamespace(
            sendto=lambda data, addr: sent.append(bytes(data).decode('ascii')))

        owner = output_mute_pkg.OutputMuteOwner(backend=FakeEndpointBackend())
        owner._pending_restore['stuck'] = output_mute_pkg.EndpointSnapshot('stuck', '', False)
        owner._backend.add_render('stuck', muted=False, fail_set=True)
        prev = output_mute_pkg._OWNER
        output_mute_pkg._OWNER = owner
        try:
            controller._handle_command('RELOAD_HOTKEYS', ('127.0.0.1', 65300))
        finally:
            output_mute_pkg._OWNER = prev

        # 快捷键已实际切换（restart 成功），但清理未确认 → 不回执成功
        self.assertEqual(len(manager.restart_keys), 1)
        self.assertEqual(sent, ['RELOAD_FAILED'])


class UdpPauseResumeAckTests(unittest.TestCase):
    def test_pause_resume_acks_unchanged(self):
        paused = []

        class _M:
            state = SimpleNamespace(recording=False, shutdown_pending=False)
            tasks = {}
            control_task = SimpleNamespace(is_recording=False)

            @staticmethod
            def pause_hotkeys():
                paused.append('pause')

            @staticmethod
            def resume_hotkeys():
                paused.append('resume')

        controller = UDPController(_M())
        controller._sock = SimpleNamespace(
            sendto=lambda data, addr: paused.append(bytes(data).decode('ascii')))
        addr = ('127.0.0.1', 65200)
        controller._handle_command('PAUSE_HOTKEYS', addr)
        controller._handle_command('RESUME_HOTKEYS', addr)
        self.assertEqual(paused, ['pause', 'PAUSED', 'resume', 'RESUMED'])


if __name__ == '__main__':
    unittest.main()
