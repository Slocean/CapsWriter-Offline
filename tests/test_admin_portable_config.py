# coding: utf-8
"""
阶段八便携包配置行为实测：

- admin_settings.json 相对路径按 REPO_DIR（管理端所在包根）解析，
  不依赖进程 cwd（管理端从任意 cwd 启动、目录带空格时同样正确）；
- asr_launch_cmd 相对 exe 路径解析到 asr_work_dir 绝对路径——Windows
  CreateProcess 按「调用方 cwd」搜索相对路径，不假设 cwd=工作目录；
- 'python' 等找不到的裸命令保持原值（走 PATH），绝对路径原样保留。
"""

import json
import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from admin.config_admin import AdminConfig as Cfg, load_overrides  # noqa: E402
from admin import asr_control  # noqa: E402


class PortableConfigTests(unittest.TestCase):
    def setUp(self):
        self._snapshot = {k: getattr(Cfg, k) for k in (
            'asr_work_dir', 'asr_launch_cmd', 'asr_entry', 'asr_pid_file',
            'asr_control_token_path', 'hotwords_path', 'log_file', 'settings_file',
            'repo_dir', 'data_dir', 'static_dir', 'password_file', 'audit_log')}
        self._cwd = os.getcwd()

    def tearDown(self):
        for k, v in self._snapshot.items():
            setattr(Cfg, k, v)
        os.chdir(self._cwd)

    def test_relative_asr_work_dir_resolves_against_repo_dir(self):
        """相对 asr_work_dir 按 REPO_DIR 解析为绝对路径，不得依赖进程 cwd"""
        with tempfile.TemporaryDirectory() as td:
            override = pathlib.Path(td) / 'admin_settings.json'
            override.write_text(json.dumps({'asr_work_dir': 'sub/asr-runtime'}),
                                encoding='utf-8')
            os.chdir(td)   # 模拟管理端从其他 cwd 启动
            load_overrides(override)
            os.chdir(self._cwd)   # 退出前恢复 cwd，避免临时目录清理失败
            self.assertTrue(pathlib.Path(Cfg.asr_work_dir).is_absolute(),
                            '相对 asr_work_dir 必须解析为绝对路径')
            self.assertEqual(pathlib.Path(Cfg.asr_work_dir),
                             pathlib.Path(Cfg.repo_dir) / 'sub' / 'asr-runtime',
                             '必须按 REPO_DIR（包根）解析，不得按 cwd 解析')
            self.assertEqual(pathlib.Path(Cfg.asr_pid_file),
                             pathlib.Path(Cfg.repo_dir) / 'sub' / 'asr-runtime' / 'logs' / 'server.pid',
                             '派生 PID 路径必须指向解析后的实际 ASR 工作目录')
            self.assertEqual(pathlib.Path(Cfg.asr_control_token_path),
                             pathlib.Path(Cfg.repo_dir) / 'sub' / 'asr-runtime' / 'logs' / 'server_control.token')
            self.assertEqual(pathlib.Path(Cfg.log_file),
                             pathlib.Path(Cfg.repo_dir) / 'sub' / 'asr-runtime' / 'logs' / 'server_latest.log')
            self.assertEqual(pathlib.Path(Cfg.hotwords_path),
                             pathlib.Path(Cfg.repo_dir) / 'sub' / 'asr-runtime' / 'hot-server.txt')
            self.assertEqual(pathlib.Path(Cfg.settings_file),
                             pathlib.Path(Cfg.repo_dir) / 'sub' / 'asr-runtime' / 'server_settings.json')

    def test_portable_dot_work_dir_resolves_to_repo_root(self):
        """便携 admin_settings.json：asr_work_dir '.' 解析到包根，launch_cmd
        相对 exe 解析到包根下的 start_server.exe"""
        with tempfile.TemporaryDirectory() as td:
            override = pathlib.Path(td) / 'admin_settings.json'
            override.write_text(json.dumps({'asr_work_dir': '.',
                                            'asr_launch_cmd': ['start_server.exe']}),
                                encoding='utf-8')
            os.chdir(td)
            load_overrides(override)
            os.chdir(self._cwd)
            self.assertEqual(pathlib.Path(Cfg.asr_work_dir), pathlib.Path(Cfg.repo_dir),
                             "asr_work_dir '.' 必须解析到 REPO_DIR（包根）")
            self.assertEqual(pathlib.Path(Cfg.asr_pid_file),
                             pathlib.Path(Cfg.repo_dir) / 'logs' / 'server.pid')

    def test_relative_settings_file_resolves_against_repo_dir(self):
        with tempfile.TemporaryDirectory() as td:
            override = pathlib.Path(td) / 'admin_settings.json'
            override.write_text(json.dumps({'settings_file': 'my-server-settings.json'}),
                                encoding='utf-8')
            os.chdir(td)
            load_overrides(override)
            os.chdir(self._cwd)
            self.assertEqual(pathlib.Path(Cfg.settings_file),
                             pathlib.Path(Cfg.repo_dir) / 'my-server-settings.json')
            self.assertTrue(pathlib.Path(Cfg.settings_file).is_absolute())

    def test_resolve_launch_exe_with_space_in_work_dir(self):
        """目录带空格：相对 exe 解析到 asr_work_dir 绝对路径（其他 cwd 启动）"""
        with tempfile.TemporaryDirectory() as td:
            spaced = pathlib.Path(td) / 'Caps Writer Package'
            spaced.mkdir()
            exe = spaced / 'start_server.exe'
            exe.write_bytes(b'MZ')
            Cfg.asr_work_dir = spaced
            Cfg.repo_dir = pathlib.Path(td) / 'repo'
            Cfg.repo_dir.mkdir()
            os.chdir(td)   # CreateProcess 的相对路径搜索不经过 work_dir
            resolved = asr_control.ASRControl._resolve_launch_exe('start_server.exe')
            os.chdir(self._cwd)
            self.assertEqual(resolved, str(exe),
                             '相对 exe 必须解析到 asr_work_dir 下的绝对路径')
            Cfg.asr_launch_cmd = ['start_server.exe']
            ctl = asr_control.ASRControl()
            cmd = ctl._launch_command()
            self.assertEqual(cmd, [str(exe)],
                             '启动命令必须是含空格绝对路径的 argv 列表（subprocess 负责加引号）')

    def test_resolve_launch_exe_bare_python_unchanged(self):
        """'python' 等找不到的裸命令保持原值（走 PATH 搜索）"""
        Cfg.asr_work_dir = pathlib.Path('Z:/nonexistent-portable-test')
        Cfg.repo_dir = pathlib.Path('Z:/nonexistent-portable-repo')
        self.assertEqual(asr_control.ASRControl._resolve_launch_exe('python'), 'python')

    def test_resolve_launch_exe_absolute_unchanged(self):
        self.assertEqual(asr_control.ASRControl._resolve_launch_exe('C:/x/start_server.exe'),
                         'C:/x/start_server.exe')

    def test_absolute_paths_unchanged_by_load_overrides(self):
        """绝对路径覆盖原样保留（回归：不得被相对解析逻辑改写）"""
        with tempfile.TemporaryDirectory() as td:
            override = pathlib.Path(td) / 'admin_settings.json'
            work = pathlib.Path(td) / 'asr-runtime'
            work.mkdir()
            override.write_text(json.dumps({'asr_work_dir': str(work)}), encoding='utf-8')
            os.chdir(td)
            load_overrides(override)
            os.chdir(self._cwd)
            self.assertEqual(pathlib.Path(Cfg.asr_work_dir), work)
            self.assertEqual(pathlib.Path(Cfg.asr_pid_file), work / 'logs' / 'server.pid')


if __name__ == '__main__':
    unittest.main()
