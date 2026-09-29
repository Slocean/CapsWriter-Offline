# coding: utf-8
"""
管理进程适配打包版 ASR 的测试（复核 P0-07）：

- asr_work_dir 覆盖后，PID/控制令牌/日志/热词/server_settings.json 全部指向
  实际 ASR 工作目录（除非显式覆盖）；
- asr_launch_cmd 配置后用 EXE 启动命令，不再依赖源码 start_server.py；
- 旧版 ASR（无控制通道、无 PID 登记）→ controllable=False，状态不误报已停止。
"""

import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from admin.config_admin import AdminConfig as Cfg, load_overrides  # noqa: E402
from admin import asr_control  # noqa: E402


class PackagedAsrConfigTests(unittest.TestCase):
    def setUp(self):
        self._snapshot = {k: getattr(Cfg, k) for k in (
            'asr_work_dir', 'asr_launch_cmd', 'asr_entry', 'asr_pid_file',
            'asr_control_token_path', 'hotwords_path', 'log_file', 'settings_file',
            'repo_dir')}

    def tearDown(self):
        for k, v in self._snapshot.items():
            setattr(Cfg, k, v)

    def test_work_dir_override_redirects_derived_paths(self):
        with tempfile.TemporaryDirectory() as td:
            override = pathlib.Path(td) / 'admin_settings.json'
            work = pathlib.Path(td) / 'asr-runtime'
            work.mkdir()
            override.write_text(json.dumps({
                'asr_work_dir': str(work),
                'asr_launch_cmd': [str(work / 'start_server.exe')],
            }), encoding='utf-8')
            load_overrides(override)
            self.assertEqual(pathlib.Path(Cfg.asr_work_dir), work)
            self.assertEqual(Cfg.asr_launch_cmd, [str(work / 'start_server.exe')])
            self.assertEqual(Cfg.asr_entry, work / 'start_server.py')
            self.assertEqual(Cfg.asr_pid_file, work / 'logs' / 'server.pid')
            self.assertEqual(Cfg.asr_control_token_path, work / 'logs' / 'server_control.token')
            self.assertEqual(Cfg.hotwords_path, work / 'hot-server.txt')
            self.assertEqual(Cfg.log_file, work / 'logs' / 'server_latest.log')
            self.assertEqual(Cfg.settings_file, work / 'server_settings.json')

    def test_explicit_overrides_win_over_derived(self):
        with tempfile.TemporaryDirectory() as td:
            override = pathlib.Path(td) / 'admin_settings.json'
            work = pathlib.Path(td) / 'asr-runtime'
            work.mkdir()
            custom_log = pathlib.Path(td) / 'custom.log'
            override.write_text(json.dumps({
                'asr_work_dir': str(work),
                'log_file': str(custom_log),
            }), encoding='utf-8')
            load_overrides(override)
            self.assertEqual(pathlib.Path(Cfg.log_file), custom_log)
            self.assertEqual(Cfg.asr_pid_file, work / 'logs' / 'server.pid')

    def test_launch_command_from_config(self):
        Cfg.asr_launch_cmd = ['C:/asr/start_server.exe']
        ctl = asr_control.ASRControl()
        self.assertEqual(ctl._launch_command(), ['C:/asr/start_server.exe'])

    def test_launch_command_defaults_to_python_entry(self):
        Cfg.asr_launch_cmd = []
        ctl = asr_control.ASRControl()
        cmd = ctl._launch_command()
        self.assertEqual(cmd[1:], [str(Cfg.asr_entry)])
        self.assertIn('python', cmd[0].lower())

    def test_start_refuses_missing_work_dir(self):
        Cfg.asr_launch_cmd = ['C:/asr/start_server.exe']
        Cfg.asr_work_dir = pathlib.Path('Z:/nonexistent-asr-dir')
        ctl = asr_control.ASRControl()
        action = {'type': 'start', 'force': False, 'progress': '', 'message': ''}
        with self.assertRaises(RuntimeError) as ctx:
            ctl._start_process(action)
        self.assertIn('工作目录', str(ctx.exception))


class ControllableStatusTests(unittest.TestCase):
    """旧版 ASR：控制通道不通 + 无 PID 登记 → controllable=False，不误报已停止"""

    def setUp(self):
        self._snapshot = {k: getattr(Cfg, k) for k in (
            'asr_pid_file', 'asr_control_token_path', 'asr_host', 'asr_port')}

    def tearDown(self):
        for k, v in self._snapshot.items():
            setattr(Cfg, k, v)

    def test_old_version_reports_stale_uncontrollable(self):
        with tempfile.TemporaryDirectory() as td:
            Cfg.asr_pid_file = pathlib.Path(td) / 'server.pid'      # 无 PID 文件
            Cfg.asr_control_token_path = pathlib.Path(td) / 't.token'
            Cfg.asr_host = '127.0.0.1'
            # 占住一个端口模拟“端口有服务但控制通道不可达”
            import socket
            sock = socket.socket()
            sock.bind(('127.0.0.1', 0))
            sock.listen(50)  # backlog 要够大：status() 的端口探测不会被未 accept 卡死
            Cfg.asr_port = sock.getsockname()[1]
            try:
                with mock.patch.object(asr_control.ControlClient, 'status_sync', return_value=None):
                    ctl = asr_control.ASRControl()
                    st = ctl.status()
                self.assertEqual(st['state'], 'stale')
                self.assertFalse(st['controllable'])
                self.assertFalse(st['pid_registered'])
                self.assertTrue(st['port_open'])
                self.assertIn('旧版', st['hint'])
            finally:
                sock.close()

    def test_stopped_state_when_port_closed(self):
        with tempfile.TemporaryDirectory() as td:
            Cfg.asr_pid_file = pathlib.Path(td) / 'server.pid'
            Cfg.asr_control_token_path = pathlib.Path(td) / 't.token'
            import socket
            sock = socket.socket()
            sock.bind(('127.0.0.1', 0))
            free_port = sock.getsockname()[1]
            sock.close()  # 关闭 = 端口无人监听
            Cfg.asr_port = free_port
            with mock.patch.object(asr_control.ControlClient, 'status_sync', return_value=None):
                ctl = asr_control.ASRControl()
                st = ctl.status()
            self.assertEqual(st['state'], 'stopped')
            self.assertFalse(st['controllable'])


if __name__ == '__main__':
    unittest.main()
