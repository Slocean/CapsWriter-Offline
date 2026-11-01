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

    def test_r10_explicit_path_overrides_are_path_objects(self):
        """R10：显式覆盖的路径必须是 Path（运行时调用 .stat/.read_text/.parent）"""
        with tempfile.TemporaryDirectory() as td:
            override = pathlib.Path(td) / 'admin_settings.json'
            work = pathlib.Path(td) / 'asr-runtime'
            work.mkdir()
            custom_log = pathlib.Path(td) / 'custom.log'
            custom_pid = pathlib.Path(td) / 'custom.pid'
            custom_settings = pathlib.Path(td) / 'custom-settings.json'
            override.write_text(json.dumps({
                'asr_work_dir': str(work),
                'log_file': str(custom_log),
                'asr_pid_file': str(custom_pid),
                'settings_file': str(custom_settings),
            }), encoding='utf-8')
            load_overrides(override)
            for key, expected in (('log_file', custom_log), ('asr_pid_file', custom_pid),
                                  ('settings_file', custom_settings)):
                value = getattr(Cfg, key)
                self.assertIsInstance(value, pathlib.Path, f'{key} 运行时类型应为 Path，实际 {type(value)}')
                self.assertEqual(value, expected)
            # 真实用途：logsource.tail 会 .stat()/.read_bytes()——显式覆盖后必须可用
            custom_log.write_text('hello\n', encoding='utf-8')
            from admin import logsource
            self.assertEqual(logsource.tail(cursor=0)['lines'], ['hello'])

    def test_r10_launch_cmd_string_and_invalid_type(self):
        """R10：启动命令必须是字符串数组——单字符串收编为列表，坏类型提示后忽略"""
        with tempfile.TemporaryDirectory() as td:
            override = pathlib.Path(td) / 'admin_settings.json'
            override.write_text(json.dumps({'asr_launch_cmd': 'C:/asr/start_server.exe'}),
                                encoding='utf-8')
            load_overrides(override)
            self.assertEqual(Cfg.asr_launch_cmd, ['C:/asr/start_server.exe'])
            override.write_text(json.dumps({'asr_launch_cmd': {'exe': 1}}), encoding='utf-8')
            load_overrides(override)
            self.assertEqual(Cfg.asr_launch_cmd, [], '坏类型必须被忽略并回退默认')

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
            'asr_pid_file', 'asr_control_token_path', 'asr_host', 'asr_port',
            'asr_work_dir', 'asr_launch_cmd')}

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

    def test_r03_stopped_state_allows_start(self):
        """R03：新版已安全停止（端口空闲、无互斥锁、启动配置有效）必须允许启动"""
        with tempfile.TemporaryDirectory() as td:
            Cfg.asr_pid_file = pathlib.Path(td) / 'server.pid'
            Cfg.asr_control_token_path = pathlib.Path(td) / 't.token'
            import socket
            sock = socket.socket()
            sock.bind(('127.0.0.1', 0))
            free_port = sock.getsockname()[1]
            sock.close()
            Cfg.asr_port = free_port
            Cfg.asr_work_dir = pathlib.Path(td)          # 存在的工作目录
            Cfg.asr_launch_cmd = [str(pathlib.Path(td) / 'start_server.exe')]
            with mock.patch.object(asr_control.ControlClient, 'status_sync', return_value=None):
                ctl = asr_control.ASRControl()
                st = ctl.status()
            self.assertEqual(st['state'], 'stopped')
            self.assertTrue(st['can_start'], '已停止的新版应允许启动')
            self.assertFalse(st['can_stop'])
            self.assertIsNone(st.get('hint'), '可启动的停止态不应有阻断提示')

    def test_r03_stale_state_blocks_start_and_stop(self):
        """R03：端口被未知服务占用（旧版）时启动/停止都必须拒绝"""
        with tempfile.TemporaryDirectory() as td:
            Cfg.asr_pid_file = pathlib.Path(td) / 'server.pid'
            Cfg.asr_control_token_path = pathlib.Path(td) / 't.token'
            import socket
            sock = socket.socket()
            sock.bind(('127.0.0.1', 0))
            sock.listen(50)
            Cfg.asr_port = sock.getsockname()[1]
            try:
                with mock.patch.object(asr_control.ControlClient, 'status_sync', return_value=None):
                    ctl = asr_control.ASRControl()
                    st = ctl.status()
                self.assertEqual(st['state'], 'stale')
                self.assertFalse(st['can_start'])
                self.assertFalse(st['can_stop'])
                self.assertFalse(st['can_restart'])
            finally:
                sock.close()

    def test_r03_stop_on_unverified_target_rejected_by_submit(self):
        """R03/R07：不可核身目标（无控制通道、无 PID、非本进程拉起）提交停止 → not_allowed"""
        with tempfile.TemporaryDirectory() as td:
            Cfg.asr_pid_file = pathlib.Path(td) / 'server.pid'
            Cfg.asr_control_token_path = pathlib.Path(td) / 't.token'
            import socket
            sock = socket.socket()
            sock.bind(('127.0.0.1', 0))
            free_port = sock.getsockname()[1]
            sock.close()
            Cfg.asr_port = free_port
            with mock.patch.object(asr_control.ControlClient, 'status_sync', return_value=None):
                ctl = asr_control.ASRControl()
                result = ctl.submit('stop')
            self.assertFalse(result['ok'])
            self.assertEqual(result['error'], 'not_allowed')

    def test_r07_stop_unverifiable_raises_not_done(self):
        """R07：优雅退出无回执、无可核身 PID 时停止必须失败，不得写 done"""
        with tempfile.TemporaryDirectory() as td:
            Cfg.asr_pid_file = pathlib.Path(td) / 'server.pid'   # 无 PID 文件
            Cfg.asr_control_token_path = pathlib.Path(td) / 't.token'
            import socket
            sock = socket.socket()
            sock.bind(('127.0.0.1', 0))
            free_port = sock.getsockname()[1]
            sock.close()
            Cfg.asr_port = free_port
            saved_timeout = Cfg.asr_stop_timeout
            Cfg.asr_stop_timeout = 0.5
            try:
                with mock.patch.object(asr_control.ControlClient, 'status_sync', return_value=None), \
                     mock.patch.object(asr_control.ControlClient, 'request_sync', return_value=None):
                    ctl = asr_control.ASRControl()
                    action = {'type': 'stop', 'force': False, 'progress': '', 'message': ''}
                    with self.assertRaises(RuntimeError) as ctx:
                        ctl._stop_process(action)
                    self.assertIn('拒绝执行停止', str(ctx.exception))
            finally:
                Cfg.asr_stop_timeout = saved_timeout

    def test_r07_terminate_tree_argv_and_returncode(self):
        """R07：taskkill 不传空参数；返回码必须被核对（非 0 → False）"""
        if sys.platform != 'win32':
            self.skipTest('Windows 专属')
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return mock.Mock(returncode=1)

        with mock.patch.object(asr_control.subprocess, 'run', side_effect=fake_run):
            ok = asr_control._terminate_tree(4242, force=False)
            ok2 = asr_control._terminate_tree(4242, force=True)
        self.assertFalse(ok, 'taskkill 返回码 1 必须判为失败')
        self.assertFalse(ok2)
        self.assertEqual(calls[0], ['taskkill', '/PID', '4242', '/T'],
                         '非 force 不得出现空参数')
        self.assertEqual(calls[1], ['taskkill', '/PID', '4242', '/F', '/T'])

    def test_r07_stop_failure_marks_action_failed(self):
        """R07：_run_action 在停止抛错时把动作写为 failed（前端不会显示完成）"""
        with tempfile.TemporaryDirectory() as td:
            Cfg.asr_pid_file = pathlib.Path(td) / 'server.pid'
            Cfg.asr_control_token_path = pathlib.Path(td) / 't.token'
            import socket
            sock = socket.socket()
            sock.bind(('127.0.0.1', 0))
            free_port = sock.getsockname()[1]
            sock.close()
            Cfg.asr_port = free_port
            with mock.patch.object(asr_control.ControlClient, 'status_sync', return_value=None), \
                 mock.patch.object(asr_control.ControlClient, 'request_sync', return_value=None):
                ctl = asr_control.ASRControl()
                # 直接执行动作线程体：submit 层会被 can_stop 拦下（另测），
                # 这里验证动作执行层失败时写 failed 而不是 done
                action = {'type': 'stop', 'force': False, 'state': 'pending',
                          'progress': '', 'message': '', 'id': 't1'}
                ctl._run_action(action)
                self.assertEqual(action['state'], 'failed', '停止失败不得写 done')
                self.assertTrue(action['message'], '失败动作必须带原因')


class SlowStartPidRegistrationTests(unittest.TestCase):
    """R06：PID 登记记录 OS 真实创建时间——慢启动进程不会被自己的核对判为身份不符"""

    def test_slow_registration_passes_identity_check(self):
        import subprocess as sp
        import time as _t
        with tempfile.TemporaryDirectory() as td:
            pid_file = pathlib.Path(td) / 'server.pid'
            child_src = (
                'import sys, time\n'
                'sys.path.insert(0, r"%s")\n'
                'time.sleep(2.5)\n'
                'from core.tools.win_instance import write_pid_file\n'
                'write_pid_file(r"%s")\n'
                'time.sleep(4)\n'          # 保持存活，父进程要对该 PID 读创建时间
            ) % (str(pathlib.Path(__file__).resolve().parents[1]), str(pid_file))
            proc = sp.Popen([sys.executable, '-c', child_src],
                            stdout=sp.DEVNULL, stderr=sp.DEVNULL)
            try:
                deadline = _t.time() + 15
                while _t.time() < deadline and not pid_file.exists():
                    _t.sleep(0.2)
                self.assertTrue(pid_file.exists(), '子进程未在期限内写入 PID 文件')
                import json
                entry = json.loads(pid_file.read_text(encoding='utf-8'))
                self.assertGreater(int(entry['pid']), 0)
                from core.tools import win_instance
                # 独立读 OS 创建时间与登记值对比：修复前登记的是 time.time()
                # （比创建时间晚 ≈ sleep 时长），容差 2s 必然超限
                actual = win_instance.process_creation_time(int(entry['pid']))
                self.assertIsNotNone(actual)
                self.assertLess(abs(actual - float(entry['created_at'])), 1.0,
                                '登记时间应等于 OS 进程创建时间（R06）')
                self.assertTrue(
                    asr_control.winproc.pid_matches(int(entry['pid']), float(entry['created_at']),
                                                    expected_exe=entry.get('exe')),
                    '慢启动进程被身份核对拒绝——R06 未修复')
            finally:
                proc.wait(timeout=20)


class ApplyLogLevelTests(unittest.TestCase):
    """R12：log_level 经控制通道即时应用；失败如实返回 False"""

    def test_apply_log_level_success_and_failure(self):
        with mock.patch.object(asr_control.ControlClient, 'request_sync',
                               return_value={'id': 1, 'ok': True}):
            ctl = asr_control.ASRControl()
            applied, _ = ctl.apply_log_level('INFO')
            self.assertTrue(applied)
        with mock.patch.object(asr_control.ControlClient, 'request_sync', return_value=None):
            ctl = asr_control.ASRControl()
            applied, detail = ctl.apply_log_level('INFO')
            self.assertFalse(applied)
            self.assertTrue(detail)


class A01StopVerificationTests(unittest.TestCase):
    """
    A01（行为级，真实 Windows 进程、不加载模型）：

    - 父进程存活、子进程监听：停止必须清掉整棵核身过的树并核实端口/互斥锁；
    - 父进程已死（无法核身）：拒绝执行停止，不得报 done；
    - 终止失败、控制服务仍在响应、互斥锁遗留：终态核验必须失败。
    """

    _CHILD_CODE = (
        'import socket, sys, time\n'
        's = socket.socket()\n'
        's.bind(("127.0.0.1", 0))\n'
        's.listen(50)\n'
        'print(s.getsockname()[1], flush=True)\n'
        'time.sleep(60)\n'
    )
    _PARENT_CODE = (
        'import subprocess, sys, time\n'
        'child = subprocess.Popen([sys.executable, "-c", sys.argv[1]], stdout=subprocess.PIPE)\n'
        'port = child.stdout.readline().decode().strip()\n'
        'open(sys.argv[4], "w").write(str(child.pid))\n'   # 记录子进程 pid 供测试显式清理
        'sys.path.insert(0, sys.argv[2])\n'
        'from core.tools.win_instance import write_pid_file\n'
        'write_pid_file(sys.argv[3])\n'
        'print(port, flush=True)\n'
        'time.sleep(60)\n'
    )

    def setUp(self):
        self._snapshot = {k: getattr(Cfg, k) for k in (
            'asr_pid_file', 'asr_control_token_path', 'asr_host', 'asr_port',
            'asr_work_dir', 'asr_launch_cmd', 'asr_stop_timeout')}

    def tearDown(self):
        for k, v in self._snapshot.items():
            setattr(Cfg, k, v)

    def _spawn_tree(self, td, parent_exits_immediately=False):
        """
        启动真实父子树：父（写 PID 文件）+ 监听子进程。
        parent_exits_immediately=True 时父进程打印端口后立即退出
        （模拟「控制通道 shutdown 让父先退出」），子进程继续监听。
        返回 (parent, port, pid_file, child_pid_file)。
        """
        import subprocess as sp
        import time as _t
        repo = str(pathlib.Path(__file__).resolve().parents[1])
        pid_file = pathlib.Path(td) / 'server.pid'
        child_pid_file = pathlib.Path(td) / 'child.pid'
        code = self._PARENT_CODE if not parent_exits_immediately else (
            'import subprocess, sys, time\n'
            'child = subprocess.Popen([sys.executable, "-c", sys.argv[1]], stdout=subprocess.PIPE)\n'
            'port = child.stdout.readline().decode().strip()\n'
            'open(sys.argv[4], "w").write(str(child.pid))\n'
            'sys.path.insert(0, sys.argv[2])\n'
            'from core.tools.win_instance import write_pid_file\n'
            'write_pid_file(sys.argv[3])\n'
            'print(port, flush=True)\n'
            'time.sleep(0.3)\n'          # 父进程随即退出，子进程仍在监听
        )
        parent = sp.Popen([sys.executable, '-c', code,
                           self._CHILD_CODE, repo, str(pid_file), str(child_pid_file)],
                          stdout=sp.PIPE)
        port = int(parent.stdout.readline().decode().strip())
        deadline = _t.time() + 10
        while _t.time() < deadline and not pid_file.exists():
            _t.sleep(0.1)
        self.assertTrue(pid_file.exists(), '父进程未在期限内写 PID 文件')
        return parent, port, pid_file, child_pid_file

    def _cleanup_child(self, child_pid_file):
        """finally 显式清理已记录的子进程——不能只 taskkill 已死父进程 /T"""
        import subprocess as sp
        try:
            child_pid = int(child_pid_file.read_text().strip())
        except Exception:
            return
        sp.run(['taskkill', '/F', '/T', '/PID', str(child_pid)],
               capture_output=True, timeout=15)

    def _port_closed(self, port):
        import socket
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=0.5):
                return False
        except OSError:
            return True

    def test_parent_alive_child_listening_stop_cleans_tree_and_verifies(self):
        """父进程存活、子进程监听：停止后子进程/端口必须真的清干净（A01 反例）"""
        import subprocess as sp
        with tempfile.TemporaryDirectory() as td:
            parent, port, pid_file, child_pid_file = self._spawn_tree(td)
            try:
                Cfg.asr_port = port
                Cfg.asr_pid_file = pid_file
                Cfg.asr_control_token_path = pathlib.Path(td) / 't.token'
                Cfg.asr_stop_timeout = 1.0
                with mock.patch.object(asr_control.ControlClient, 'status_sync', return_value=None), \
                     mock.patch.object(asr_control.ControlClient, 'request_sync', return_value=None):
                    ctl = asr_control.ASRControl()
                    action = {'type': 'stop', 'force': False, 'state': 'pending',
                              'progress': '', 'message': '', 'id': 'a1'}
                    ctl._run_action(action)
                self.assertEqual(action['state'], 'done',
                                 f'清理成功才可 done，实际: {action["message"]}')
                self.assertTrue(self._port_closed(port),
                                '动作 done 但子进程端口仍监听——A01 未修复')
                parent.wait(timeout=10)
                self.assertNotEqual(parent.returncode, None, '父进程未退出')
            finally:
                self._cleanup_child(child_pid_file)
                if parent.poll() is None:
                    parent.kill()
                    parent.wait(timeout=10)

    def test_owned_proc_shutdown_parent_exits_child_listening(self):
        """
        A01 原始反例：控制器拥有 _proc 且已记录父子，控制通道 shutdown 回执
        ok 后父进程先退出、子进程仍监听——必须清理核身过的子进程，
        端口/控制/互斥锁全部关闭后才 done。
        """
        with tempfile.TemporaryDirectory() as td:
            parent, port, pid_file, child_pid_file = self._spawn_tree(
                td, parent_exits_immediately=True)
            try:
                Cfg.asr_port = port
                Cfg.asr_pid_file = pathlib.Path(td) / 'unused.pid'   # 根来自 _proc
                Cfg.asr_control_token_path = pathlib.Path(td) / 't.token'
                Cfg.asr_stop_timeout = 2.0
                with mock.patch.object(asr_control.ControlClient, 'status_sync', return_value=None), \
                     mock.patch.object(asr_control.ControlClient, 'request_sync',
                                       return_value={'id': 1, 'ok': True}):
                    ctl = asr_control.ASRControl()
                    ctl._proc = parent                     # 控制器拥有的受管进程
                    action = {'type': 'stop', 'force': False, 'state': 'pending',
                              'progress': '', 'message': '', 'id': 'a1b'}
                    ctl._run_action(action)
                self.assertEqual(action['state'], 'done',
                                 f'子进程清理成功才可 done，实际: {action["message"]}')
                self.assertTrue(self._port_closed(port),
                                '父已退出但子进程端口仍监听就报 done——A01 原始反例未修复')
                self.assertNotEqual(parent.returncode, None, '受管父进程应已退出')
            finally:
                self._cleanup_child(child_pid_file)
                if parent.poll() is None:
                    parent.kill()
                    parent.wait(timeout=10)

    def test_owned_proc_child_unkillable_blocks_restart(self):
        """子进程无法终止时停止必须 failed，且 restart 不得越过失败启动新实例"""
        with tempfile.TemporaryDirectory() as td:
            parent, port, pid_file, child_pid_file = self._spawn_tree(
                td, parent_exits_immediately=True)
            try:
                Cfg.asr_port = port
                Cfg.asr_pid_file = pathlib.Path(td) / 'unused.pid'
                Cfg.asr_control_token_path = pathlib.Path(td) / 't.token'
                Cfg.asr_stop_timeout = 1.0
                with mock.patch.object(asr_control.ControlClient, 'status_sync', return_value=None), \
                     mock.patch.object(asr_control.ControlClient, 'request_sync',
                                       return_value={'id': 1, 'ok': True}), \
                     mock.patch.object(asr_control, '_terminate_tree', return_value=False), \
                     mock.patch.object(asr_control.ASRControl, '_wait_identity_gone', return_value=False):
                    ctl = asr_control.ASRControl()
                    ctl._proc = parent
                    start_mock = mock.patch.object(ctl, '_start_process')
                    with start_mock as m_start:
                        action = {'type': 'restart', 'force': False, 'state': 'pending',
                                  'progress': '', 'message': '', 'id': 'a1c'}
                        ctl._run_action(action)
                        self.assertEqual(action['state'], 'failed',
                                         '停止失败时 restart 必须 failed')
                        self.assertIn('停止未完成', action['message'])
                        m_start.assert_not_called()
            finally:
                self._cleanup_child(child_pid_file)
                if parent.poll() is None:
                    parent.kill()
                    parent.wait(timeout=10)

    def test_pid_file_points_to_dead_process_refused(self):
        """PID 文件指向已死进程且无控制通道/无受管进程：拒绝停止（failed）"""
        import subprocess as sp
        with tempfile.TemporaryDirectory() as td:
            import json as _json
            from core.tools import win_instance
            dead = sp.Popen([sys.executable, '-c', 'import sys; sys.exit(0)'])
            dead.wait(timeout=10)
            pid_file = pathlib.Path(td) / 'server.pid'
            pid_file.write_text(_json.dumps({
                'pid': dead.pid,
                'created_at': win_instance.process_creation_time(dead.pid),
                'exe': win_instance.process_image_path(dead.pid),
            }), encoding='utf-8')
            import socket
            sock = socket.socket(); sock.bind(('127.0.0.1', 0)); free = sock.getsockname()[1]; sock.close()
            Cfg.asr_port = free
            Cfg.asr_pid_file = pid_file
            Cfg.asr_control_token_path = pathlib.Path(td) / 't.token'
            Cfg.asr_stop_timeout = 0.5
            with mock.patch.object(asr_control.ControlClient, 'status_sync', return_value=None), \
                 mock.patch.object(asr_control.ControlClient, 'request_sync', return_value=None):
                ctl = asr_control.ASRControl()
                action = {'type': 'stop', 'force': False, 'state': 'pending',
                          'progress': '', 'message': '', 'id': 'a2'}
                ctl._run_action(action)
            self.assertEqual(action['state'], 'failed',
                             '无法核身的目标必须 failed，不得 done')
            self.assertIn('拒绝执行停止', action['message'])

    def test_terminate_failure_reports_failed(self):
        """终止失败（taskkill 无效）：动作必须 failed，不得报完成"""
        with tempfile.TemporaryDirectory() as td:
            Cfg.asr_pid_file = pathlib.Path(td) / 'server.pid'
            Cfg.asr_control_token_path = pathlib.Path(td) / 't.token'
            import socket
            sock = socket.socket(); sock.bind(('127.0.0.1', 0)); free = sock.getsockname()[1]; sock.close()
            Cfg.asr_port = free
            Cfg.asr_stop_timeout = 0.5
            # 用真实存活进程做根（本测试进程自身没有 PID 文件——改为写一个真实进程）
            import subprocess as sp
            dummy = sp.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
            pid_file = pathlib.Path(td) / 'server.pid'
            import json as _json
            from core.tools import win_instance
            try:
                entry = {'pid': dummy.pid, 'created_at': win_instance.process_creation_time(dummy.pid),
                         'exe': win_instance.process_image_path(dummy.pid), 'cmd': 'x'}
                pid_file.write_text(_json.dumps(entry), encoding='utf-8')
                with mock.patch.object(asr_control.ControlClient, 'status_sync', return_value=None), \
                     mock.patch.object(asr_control.ControlClient, 'request_sync', return_value=None), \
                     mock.patch.object(asr_control.ASRControl, '_wait_identity_gone', return_value=False), \
                     mock.patch.object(asr_control, '_terminate_tree', return_value=False):
                    ctl = asr_control.ASRControl()
                    action = {'type': 'stop', 'force': False, 'state': 'pending',
                              'progress': '', 'message': '', 'id': 'a3'}
                    ctl._run_action(action)
                self.assertEqual(action['state'], 'failed')
                self.assertIn('无法终止', action['message'])
            finally:
                dummy.kill(); dummy.wait(timeout=10)

    def test_stop_verified_rejects_control_alive_mutex_port(self):
        """终态核验单元：未知树(None)不得当空树放行；控制服务在响应 /
        互斥锁遗留 / 端口占用都必须判失败"""
        import socket
        sock = socket.socket(); sock.bind(('127.0.0.1', 0)); free = sock.getsockname()[1]; sock.close()
        Cfg.asr_port = free
        Cfg.asr_pid_file = pathlib.Path(tempfile.gettempdir()) / 'nonexistent-a01.pid'
        Cfg.asr_control_token_path = pathlib.Path(tempfile.gettempdir()) / 'nonexistent-a01.token'
        # 未知树（未捕获任何身份证据）：不得当空树放行（审计 no_tree_stop_verified）
        with mock.patch.object(asr_control.ControlClient, 'status_sync', return_value=None):
            ctl = asr_control.ASRControl()
            ok, why = ctl._stop_verified(None)
            self.assertFalse(ok, '未知树不得当空树放行')
            self.assertIn('未知树', why)
        # 控制服务仍在响应：判失败（树已捕获时同样如此）
        dead_tree = {'root': {'pid': 3999999, 'created_at': 1.0, 'exe': None},
                     'children': [], 'children_enumerated': True}
        with mock.patch.object(asr_control.ControlClient, 'status_sync',
                               return_value={'id': 1, 'ok': True, 'status': {}}):
            ctl = asr_control.ASRControl()
            ok, why = ctl._stop_verified(dead_tree)
            self.assertFalse(ok)
            self.assertIn('控制服务', why)
        with mock.patch.object(asr_control.ControlClient, 'status_sync', return_value=None):
            ctl = asr_control.ASRControl()
            from core.tools.win_instance import InstanceMutex
            with InstanceMutex():
                ok, why = ctl._stop_verified(dead_tree)
                self.assertFalse(ok)
                self.assertIn('互斥锁', why)
            ok, why = ctl._stop_verified(dead_tree)
            self.assertTrue(ok, f'干净环境的已核身树应通过，实际: {why}')

    def test_root_pid_reuse_not_counted_as_leftover(self):
        """根 PID 被复用成无关进程（创建时间/exe 不匹配）：不算核身遗留、不得误杀"""
        import socket
        import subprocess as sp
        sock = socket.socket(); sock.bind(('127.0.0.1', 0)); free = sock.getsockname()[1]; sock.close()
        Cfg.asr_port = free
        Cfg.asr_pid_file = pathlib.Path(tempfile.gettempdir()) / 'nonexistent-a01b.pid'
        Cfg.asr_control_token_path = pathlib.Path(tempfile.gettempdir()) / 'nonexistent-a01b.token'
        unrelated = sp.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        try:
            with mock.patch.object(asr_control.ControlClient, 'status_sync', return_value=None):
                ctl = asr_control.ASRControl()
                tree = {'root': {'pid': unrelated.pid, 'created_at': 1.0, 'exe': 'Z:/no-such.exe'},
                        'children': [], 'children_enumerated': True}
                ok, why = ctl._stop_verified(tree)
                self.assertFalse(ok)
                self.assertNotIn('进程仍存活', why,
                                 '复用 PID 的无关进程不得算作核身遗留（会误杀）')
                self.assertIn('根进程身份无法核实', why)
        finally:
            unrelated.kill(); unrelated.wait(timeout=10)

    def test_enumeration_failure_is_not_empty_tree(self):
        """A01：后代枚举失败必须阻断终态核验，不能当空树成功"""
        import socket
        sock = socket.socket(); sock.bind(('127.0.0.1', 0)); free = sock.getsockname()[1]; sock.close()
        Cfg.asr_port = free
        Cfg.asr_pid_file = pathlib.Path(tempfile.gettempdir()) / 'nonexistent-a01c.pid'
        Cfg.asr_control_token_path = pathlib.Path(tempfile.gettempdir()) / 'nonexistent-a01c.token'
        with mock.patch.object(asr_control.ControlClient, 'status_sync', return_value=None):
            ctl = asr_control.ASRControl()
            tree = {'root': {'pid': 999999, 'created_at': 1.0, 'exe': None},
                    'children': [], 'children_enumerated': False}
            ok, why = ctl._stop_verified(tree)
            self.assertFalse(ok)
            self.assertIn('枚举失败', why)

    def test_merge_tree_keeps_recorded_children(self):
        """A01：刷新树必须保留已记录后代（父退出后脱离枚举的 worker 不丢）"""
        with mock.patch.object(asr_control.ControlClient, 'status_sync', return_value=None):
            ctl = asr_control.ASRControl()
            old = {'root': {'pid': 100, 'created_at': 1.0, 'exe': None},
                   'children': [(200, 1.5), (201, 1.6)], 'children_enumerated': True}
            new = {'root': {'pid': 100, 'created_at': 1.0, 'exe': None},
                   'children': [(202, 1.7)], 'children_enumerated': True}
            merged = ctl._merge_tree(old, new)
            pids = {pid for pid, _ in merged['children']}
            self.assertEqual(pids, {200, 201, 202}, '刷新不得丢弃已记录的后代身份')
            self.assertTrue(merged['children_enumerated'])
            # 任一次枚举失败 → 清单标记为未核实
            new_bad = dict(new, children_enumerated=False)
            merged_bad = ctl._merge_tree(old, new_bad)
            self.assertFalse(merged_bad['children_enumerated'])

    def test_start_refuses_when_mutex_held(self):
        """终止失败后互斥锁仍被占：启动必须拒绝，不得拉起第二实例"""
        with tempfile.TemporaryDirectory() as td:
            import socket
            sock = socket.socket(); sock.bind(('127.0.0.1', 0)); free = sock.getsockname()[1]; sock.close()
            Cfg.asr_port = free
            Cfg.asr_pid_file = pathlib.Path(td) / 'server.pid'
            Cfg.asr_control_token_path = pathlib.Path(td) / 't.token'
            Cfg.asr_work_dir = pathlib.Path(td)
            Cfg.asr_launch_cmd = [str(pathlib.Path(td) / 'start_server.exe')]
            from core.tools.win_instance import InstanceMutex
            with mock.patch.object(asr_control.ControlClient, 'status_sync', return_value=None):
                ctl = asr_control.ASRControl()
                with InstanceMutex():
                    st = ctl.status()
                    self.assertFalse(st['can_start'], '互斥锁被占时不得允许启动')
                    with self.assertRaises(RuntimeError) as ctx:
                        ctl._start_process({'type': 'start', 'force': False, 'progress': '', 'message': ''})
                    self.assertIn('双实例', str(ctx.exception))


if __name__ == '__main__':
    unittest.main()
