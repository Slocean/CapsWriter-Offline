# coding: utf-8
"""
A01 精简修复阶段行为回归（进程身份）：

- 每一次终止（优雅、升级强杀、逐个子进程清理）调用前都重新核对记录身份；
- _wait_identity_gone 判定记录身份（PID+创建时间/exe）是否退出，不是裸 PID；
- 优雅终止后 PID 被复用成无关身份：绝不发强杀，停止失败、restart 不启动；
- 停止清理与最终核验消费同一份累计身份清单，新快照空列表不丢已记录 worker。

模拟 PID 复用的用例一律 patch 身份函数并 mock _terminate_tree（绝不真正
探测/终止任何真实进程）；涉及真实进程的用例 finally 按记录身份安全清理。
全程不部署服务器、不加载模型。
"""

import ctypes
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from admin.config_admin import AdminConfig as Cfg  # noqa: E402
from admin import asr_control  # noqa: E402


class _IdentityTestBase(unittest.TestCase):
    def setUp(self):
        self._snapshot = {k: getattr(Cfg, k) for k in (
            'asr_pid_file', 'asr_control_token_path', 'asr_host', 'asr_port',
            'asr_stop_timeout')}

    def tearDown(self):
        for k, v in self._snapshot.items():
            setattr(Cfg, k, v)

    def _closed_port(self):
        import socket
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        sock.close()
        return port

    def _write_pid_file(self, td, pid, created_at, exe):
        pid_file = pathlib.Path(td) / 'server.pid'
        pid_file.write_text(json.dumps({'pid': pid, 'created_at': created_at, 'exe': exe}),
                            encoding='utf-8')
        return pid_file

    def _action(self, kind):
        return {'type': kind, 'force': False, 'state': 'pending',
                'progress': '', 'message': '', 'id': 'stage4'}


class ForceKillIdentityTests(_IdentityTestBase):
    """A01-force：升级强杀前必须按记录身份重新核对，PID 复用绝不发强杀"""

    def test_pid_reused_after_graceful_terminate_never_force_killed(self):
        """
        行为回归（A01-force）：第一次优雅终止后原进程退出、同 PID 显示另一
        创建时间/exe 的无关身份——禁止向新身份发强杀；停止失败（failed）；
        restart 不得越过失败启动新实例。
        """
        # 打补丁后 pid_alive 恒 True，_terminate_tree 被 mock，绝不真正探测/终止
        FAKE_PID = 3999996
        with tempfile.TemporaryDirectory() as td:
            Cfg.asr_pid_file = self._write_pid_file(td, FAKE_PID, 1111.0, 'Z:/fake-asr.exe')
            Cfg.asr_control_token_path = pathlib.Path(td) / 't.token'
            Cfg.asr_port = self._closed_port()
            Cfg.asr_stop_timeout = 0.6
            calls = []
            phase = {'graceful_sent': False}

            def fake_matches(pid, created, tolerance=2.0, expected_exe=None):
                if pid == FAKE_PID and phase['graceful_sent']:
                    return False   # 优雅终止后同 PID 显示另一创建时间/exe
                return True

            def fake_terminate(pid, force):
                calls.append((pid, force))
                if not force:
                    phase['graceful_sent'] = True
                return True

            with mock.patch.object(asr_control.winproc, 'pid_alive', return_value=True), \
                 mock.patch.object(asr_control.winproc, 'pid_state', return_value='alive'), \
                 mock.patch.object(asr_control.winproc, 'pid_matches', side_effect=fake_matches), \
                 mock.patch.object(asr_control.winproc, 'child_processes', return_value=[]), \
                 mock.patch.object(asr_control, '_terminate_tree', side_effect=fake_terminate), \
                 mock.patch.object(asr_control.winproc, 'asr_mutex_exists', return_value=False), \
                 mock.patch.object(asr_control.ControlClient, 'status_sync', return_value=None), \
                 mock.patch.object(asr_control.ControlClient, 'request_sync', return_value=None):
                ctl = asr_control.ASRControl()
                start_mock = mock.patch.object(ctl, '_start_process')
                with start_mock as m_start:
                    action = self._action('restart')
                    ctl._run_action(action)
            self.assertEqual(action['state'], 'failed', '停止未完成必须 failed，不得 done')
            self.assertIn('停止未完成', action['message'])
            self.assertEqual(calls, [(FAKE_PID, False)],
                             f'只能优雅终止一次，绝不能向复用 PID 发强杀: {calls}')
            m_start.assert_not_called()

    def test_force_kill_sent_when_recorded_identity_still_matches(self):
        """记录身份仍匹配（未被复用）时升级强杀照常执行，并真的终止进程"""
        import subprocess as sp
        sleeper = sp.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                           stdout=sp.DEVNULL, stderr=sp.DEVNULL)
        try:
            from core.tools import win_instance
            created = win_instance.process_creation_time(sleeper.pid)
            exe = win_instance.process_image_path(sleeper.pid)
            with tempfile.TemporaryDirectory() as td:
                Cfg.asr_pid_file = self._write_pid_file(td, sleeper.pid, created, exe)
                Cfg.asr_control_token_path = pathlib.Path(td) / 't.token'
                Cfg.asr_port = self._closed_port()
                Cfg.asr_stop_timeout = 0.5
                with mock.patch.object(asr_control.winproc, 'asr_mutex_exists', return_value=False), \
                     mock.patch.object(asr_control.ControlClient, 'status_sync', return_value=None), \
                     mock.patch.object(asr_control.ControlClient, 'request_sync', return_value=None), \
                     mock.patch.object(asr_control.ASRControl, '_wait_identity_gone',
                                       side_effect=[False, True]):
                    ctl = asr_control.ASRControl()
                    action = self._action('stop')
                    ctl._run_action(action)
            self.assertEqual(action['state'], 'done',
                             f'身份匹配的强杀成功后必须 done，实际: {action["message"]}')
            self.assertIsNotNone(sleeper.poll(), '真实 taskkill 强杀必须真的终止进程')
        finally:
            if sleeper.poll() is None:
                sleeper.kill()
                sleeper.wait(timeout=10)


class WaitIdentityGoneTests(_IdentityTestBase):
    """等待判断的是记录身份（PID+创建时间/exe）是否退出，不是裸 PID"""

    def test_wait_judges_recorded_identity(self):
        import subprocess as sp
        import time as _t
        sleeper = sp.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                           stdout=sp.DEVNULL, stderr=sp.DEVNULL)
        try:
            from core.tools import win_instance
            ident = {'pid': sleeper.pid,
                     'created_at': win_instance.process_creation_time(sleeper.pid),
                     'exe': win_instance.process_image_path(sleeper.pid)}
            ctl = asr_control.ASRControl()
            self.assertTrue(ctl._identity_alive(ident), '真实存活且身份匹配应判为活')
            self.assertFalse(ctl._wait_identity_gone(ident, 0.6),
                             '记录身份仍存活时等待必须超时返回 False')
            sp.run(['taskkill', '/F', '/T', '/PID', str(sleeper.pid)],
                   capture_output=True, timeout=15)
            deadline = _t.time() + 5
            while _t.time() < deadline and ctl._identity_alive(ident):
                _t.sleep(0.1)
            self.assertTrue(ctl._wait_identity_gone(ident, 5),
                            '记录身份已退出必须立即返回 True')
        finally:
            if sleeper.poll() is None:
                sleeper.kill()
                sleeper.wait(timeout=10)

    def test_pid_reuse_counts_as_recorded_identity_gone(self):
        """PID 活着但创建时间/exe 不再匹配（被复用）：记录身份视为已退出，
        绝不能据此向新身份发终止（等待不得把裸 PID 存活当成原进程存活）"""
        ctl = asr_control.ASRControl()
        ident = {'pid': 3999992, 'created_at': 1111.0, 'exe': 'Z:/no-such.exe'}
        with mock.patch.object(asr_control.winproc, 'pid_state', return_value='alive'), \
             mock.patch.object(asr_control.winproc, 'pid_matches', return_value=False):
            self.assertFalse(ctl._identity_alive(ident),
                             'PID 复用（身份不匹配）不得判为记录身份存活')
            self.assertTrue(ctl._wait_identity_gone(ident, 1.0),
                            '等待应判断记录身份已退出，而不是裸 PID 仍存活')

    def test_missing_creation_time_is_identity_unavailable(self):
        """创建时间缺失（采集失败）按身份不明处理：不算存活、不算退出可杀"""
        ctl = asr_control.ASRControl()
        with mock.patch.object(asr_control.winproc, 'pid_alive', return_value=True):
            self.assertFalse(ctl._identity_alive({'pid': 1234, 'created_at': None, 'exe': None}))
            self.assertFalse(ctl._identity_alive(None))


class AccumulatedTreeTests(_IdentityTestBase):
    """A01-merged-tree：停止清理与最终核验消费同一份累计身份清单"""

    def test_cleanup_and_final_verify_consume_accumulated_children(self):
        """
        行为回归（A01-merged-tree）：worker 脱离父进程后新快照枚举不到
        （fresh children 为空），清理与最终核验仍必须消费累计清单中的
        worker——`self._recorded_tree() or tree` 绕过累计记录的反例。
        """
        # 全部身份函数打补丁、_terminate_tree 被 mock，绝不真正探测/终止
        FAKE_ROOT = 3999988
        FAKE_CHILD = 3999990
        with tempfile.TemporaryDirectory() as td:
            Cfg.asr_pid_file = self._write_pid_file(td, FAKE_ROOT, 1111.0, 'Z:/fake-asr.exe')
            Cfg.asr_control_token_path = pathlib.Path(td) / 't.token'
            Cfg.asr_port = self._closed_port()
            Cfg.asr_stop_timeout = 0.6
            calls = []
            enumerated = {'first': True}

            def fake_child_processes(root_pid):
                # 首次枚举记录到 worker；之后 worker 脱离父进程，枚举不到（空）
                if root_pid == FAKE_ROOT and enumerated['first']:
                    enumerated['first'] = False
                    return [(FAKE_CHILD, 2222.0)]
                return []

            def fake_terminate(pid, force):
                calls.append((pid, force))
                return True

            def fake_ct(pid, *a, **k):
                # 单次读取的实际创建时间按 PID 返回记录值（本地比较语义）
                return 2222.0 if pid == FAKE_CHILD else 1111.0

            with mock.patch.object(asr_control.winproc, 'pid_alive', return_value=True), \
                 mock.patch.object(asr_control.winproc, 'pid_state', return_value='alive'), \
                 mock.patch.object(asr_control.winproc, 'pid_matches', return_value=True), \
                 mock.patch.object(asr_control.winproc, 'process_creation_time', side_effect=fake_ct), \
                 mock.patch.object(asr_control.winproc, 'child_processes',
                                   side_effect=fake_child_processes), \
                 mock.patch.object(asr_control, '_terminate_tree', side_effect=fake_terminate), \
                 mock.patch.object(asr_control.winproc, 'asr_mutex_exists', return_value=False), \
                 mock.patch.object(asr_control.ControlClient, 'status_sync', return_value=None), \
                 mock.patch.object(asr_control.ControlClient, 'request_sync', return_value=None), \
                 mock.patch.object(asr_control.ASRControl, '_wait_identity_gone', return_value=True):
                ctl = asr_control.ASRControl()
                action = self._action('stop')
                ctl._run_action(action)
        # 清理阶段消费累计清单：worker 虽已脱离父进程枚举，仍被按记录身份强杀
        self.assertIn((FAKE_CHILD, True), calls,
                      '清理必须消费累计身份清单（新快照空列表不得丢掉已记录 worker）')
        self.assertIn((FAKE_ROOT, False), calls)
        # 最终核验消费累计清单：worker 仍"存活"（补丁），必须判失败且带 child# 标记
        self.assertEqual(action['state'], 'failed', '核验不过必须 failed')
        self.assertIn('child#', action['message'],
                      f'最终核验必须消费累计清单（旧实现换用新快照会丢掉 child#）: {action["message"]}')

    def test_merge_tree_new_snapshot_empty_children_keeps_recorded(self):
        """单元补充：新快照 children 为空（worker 脱离枚举）不得清空累计清单"""
        ctl = asr_control.ASRControl()
        old = {'root': {'pid': 100, 'created_at': 1.0, 'exe': None},
               'children': [(200, 1.5), (201, 1.6)], 'children_enumerated': True}
        new = {'root': {'pid': 100, 'created_at': 1.0, 'exe': None},
               'children': [], 'children_enumerated': True}
        merged = ctl._merge_tree(old, new)
        self.assertEqual({p for p, _ in merged['children']}, {200, 201},
                         '新快照空列表不得丢掉曾记录的 worker')
        self.assertTrue(merged['children_enumerated'])


class PartialEnumerationTests(_IdentityTestBase):
    """A01 补充：部分身份采集失败 ≠ 无后代——不确定状态必须传到控制器"""

    def test_worker_identity_read_failure_blocks_done(self):
        """
        行为回归（A01-partial-enumeration）：快照中仍存活的 worker 创建时间
        读取失败（不监听任何端口）——worker 仍活着时停止不得 done、不得
        restart；不确定身份 PID 绝不终止（根无论是否退出都一样，"父退出后
        不得 done"由 test_stop_verified_unknown_child_identity_blocks 的
        根已死分支覆盖）。
        finally 按核实身份（真实 PID）清理空进程。
        """
        import subprocess as sp
        import time as _t
        with tempfile.TemporaryDirectory() as td:
            repo = str(pathlib.Path(__file__).resolve().parents[1])
            pid_file = pathlib.Path(td) / 'server.pid'
            worker_pid_file = pathlib.Path(td) / 'worker.pid'
            parent_code = (
                'import subprocess, sys, time\n'
                'worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdout=subprocess.DEVNULL)\n'
                'open(sys.argv[2], "w").write(str(worker.pid))\n'
                'sys.path.insert(0, sys.argv[3])\n'
                'from core.tools.win_instance import write_pid_file\n'
                'write_pid_file(sys.argv[1])\n'
                'time.sleep(5)\n'        # 父进程存活贯穿停止流程；worker 持续睡眠（不监听端口）
            )
            parent = sp.Popen([sys.executable, '-c', parent_code, str(pid_file),
                               str(worker_pid_file), repo],
                              stdout=sp.DEVNULL, stderr=sp.DEVNULL)
            try:
                deadline = _t.time() + 10
                while _t.time() < deadline and not (pid_file.exists() and worker_pid_file.exists()):
                    _t.sleep(0.1)
                self.assertTrue(pid_file.exists() and worker_pid_file.exists(),
                                '子进程未在期限内写 PID/worker 文件')
                worker_pid = int(worker_pid_file.read_text().strip())
                Cfg.asr_pid_file = pid_file
                Cfg.asr_control_token_path = pathlib.Path(td) / 't.token'
                Cfg.asr_port = self._closed_port()   # 端口/控制/互斥锁都已关闭
                Cfg.asr_stop_timeout = 0.6
                from core.tools import win_instance
                real_ct = win_instance.process_creation_time

                def fake_ct(pid, *a, **k):
                    if pid == worker_pid:
                        return None      # 有效故障注入：worker 身份读取失败
                    return real_ct(pid, *a, **k)

                calls = []

                def rec_term(pid, force):
                    calls.append((pid, force))
                    return True          # 只记录调用，不真杀（确定性）

                with mock.patch.object(asr_control.winproc, 'process_creation_time', side_effect=fake_ct), \
                     mock.patch.object(asr_control, '_terminate_tree', side_effect=rec_term), \
                     mock.patch.object(asr_control.winproc, 'asr_mutex_exists', return_value=False), \
                     mock.patch.object(asr_control.ControlClient, 'status_sync', return_value=None), \
                     mock.patch.object(asr_control.ControlClient, 'request_sync', return_value=None), \
                     mock.patch.object(asr_control.ASRControl, '_wait_identity_gone', return_value=True):
                    ctl = asr_control.ASRControl()
                    start_mock = mock.patch.object(ctl, '_start_process')
                    with start_mock as m_start:
                        action = self._action('restart')
                        ctl._run_action(action)
                self.assertEqual(action['state'], 'failed',
                                 f'worker 身份无法核实不得 done，实际: {action["message"]}')
                self.assertIn('停止未完成', action['message'])
                self.assertNotIn((worker_pid, True), calls,
                                 '不确定身份（创建时间读取失败）的 PID 绝不能被强杀')
                self.assertNotIn((worker_pid, False), calls,
                                 '不确定身份的 PID 绝不能被终止')
                m_start.assert_not_called()
            finally:
                # finally 按核实身份清理：真实 PID 强杀 worker 与父进程
                try:
                    worker_pid = int(worker_pid_file.read_text().strip())
                    sp.run(['taskkill', '/F', '/T', '/PID', str(worker_pid)],
                           capture_output=True, timeout=15)
                except Exception:
                    pass
                if parent.poll() is None:
                    parent.kill()
                    parent.wait(timeout=10)

    def test_stop_verified_unknown_child_identity_blocks(self):
        """单元：不确定身份 (pid, None) 子进程存活 → 不可核实必须失败
        （父存活与父已退出都一样）；pid 已死 → 确定已退出，可放行"""
        Cfg.asr_port = self._closed_port()
        Cfg.asr_pid_file = pathlib.Path(tempfile.gettempdir()) / 'nonexistent-stage5.pid'
        Cfg.asr_control_token_path = pathlib.Path(tempfile.gettempdir()) / 'nonexistent-stage5.token'
        tree = {'root': {'pid': 100, 'created_at': 1.0, 'exe': None},
                'children': [(999, None)], 'children_enumerated': True}
        with mock.patch.object(asr_control.winproc, 'pid_state', return_value='alive'), \
             mock.patch.object(asr_control.winproc, 'asr_mutex_exists', return_value=False), \
             mock.patch.object(asr_control.ControlClient, 'status_sync', return_value=None):
            ctl = asr_control.ASRControl()
            ok, why = ctl._stop_verified(tree)
            self.assertFalse(ok, '不确定身份子进程存活时不得 done')
            self.assertIn('无法核实', why)
        # 父进程已退出（pid 100 死）、不确定身份 worker（999）仍活着：不得 done
        with mock.patch.object(asr_control.winproc, 'pid_state',
                               side_effect=lambda pid: 'alive' if pid == 999 else 'exited'), \
             mock.patch.object(asr_control.winproc, 'asr_mutex_exists', return_value=False), \
             mock.patch.object(asr_control.ControlClient, 'status_sync', return_value=None):
            ctl = asr_control.ASRControl()
            ok, why = ctl._stop_verified(tree)
            self.assertFalse(ok, '父退出但不确定身份 worker 仍活着不得 done')
            self.assertIn('无法核实', why)
        # 不确定身份子进程已退出（pid_state=exited 证实）→ 确定已退出，可放行
        with mock.patch.object(asr_control.winproc, 'pid_state', return_value='exited'), \
             mock.patch.object(asr_control.winproc, 'asr_mutex_exists', return_value=False), \
             mock.patch.object(asr_control.ControlClient, 'status_sync', return_value=None):
            ctl = asr_control.ASRControl()
            ok, why = ctl._stop_verified(tree)
            self.assertTrue(ok, f'不确定身份子进程已证实退出应放行，实际: {why}')

    def test_stop_verified_read_failure_child_blocks(self):
        """A01-read 审计复现：退出码读取失败被当成已退出 → 曾错误放行 done；
        现在 pid_state=unknown 必须阻断。已知身份子读取创建时间失败同样阻断。"""
        Cfg.asr_port = self._closed_port()
        Cfg.asr_pid_file = pathlib.Path(tempfile.gettempdir()) / 'nonexistent-stage6.pid'
        Cfg.asr_control_token_path = pathlib.Path(tempfile.gettempdir()) / 'nonexistent-stage6.token'
        # (pid, None) 不确定身份子 + 退出码读取失败（unknown）→ 不得 done
        tree_unknown = {'root': {'pid': 100, 'created_at': 1.0, 'exe': None},
                        'children': [(999, None)], 'children_enumerated': True}
        with mock.patch.object(asr_control.winproc, 'pid_state', return_value='unknown'), \
             mock.patch.object(asr_control.winproc, 'asr_mutex_exists', return_value=False), \
             mock.patch.object(asr_control.ControlClient, 'status_sync', return_value=None):
            ctl = asr_control.ASRControl()
            ok, why = ctl._stop_verified(tree_unknown)
            self.assertFalse(ok, '读取失败(unknown)不得等价已退出，必须阻断 done')
            self.assertIn('无法核实', why)
        # 已知身份子 (pid, 1.5) 创建时间读取失败 → child-unverified 阻断，不得 done
        # （根身份照常核实通过，故障只注入到子进程）
        tree_known = {'root': {'pid': 100, 'created_at': 1.0, 'exe': None},
                      'children': [(999, 1.5)], 'children_enumerated': True}

        def fake_ct(pid, *a, **k):
            return None if pid == 999 else 1.0

        with mock.patch.object(asr_control.winproc, 'pid_state', return_value='alive'), \
             mock.patch.object(asr_control.winproc, 'process_creation_time', side_effect=fake_ct), \
             mock.patch.object(asr_control.winproc, 'pid_matches', return_value=True), \
             mock.patch.object(asr_control.winproc, 'asr_mutex_exists', return_value=False), \
             mock.patch.object(asr_control.ControlClient, 'status_sync', return_value=None):
            ctl = asr_control.ASRControl()
            alive = ctl._leftovers(tree_known)
            ok, why = ctl._stop_verified(tree_known)
            self.assertIn(('child-unverified', 999), alive,
                          '已知身份子读取创建时间失败必须记为不可核实（不能等价死亡/复用）')
            self.assertFalse(ok, '已知身份子创建时间读取失败必须阻断 done')
            self.assertIn('仍存活', why)

    def test_unknown_child_never_terminated_by_cleanup(self):
        """单元：不确定身份子进程不进入终止清单（绝不凭未知 PID 终止）；
        已知身份子进程照常进入清理"""
        ctl = asr_control.ASRControl()
        tree = {'root': {'pid': 100, 'created_at': 1.0, 'exe': None},
                'children': [(999, None), (888, 1.5)], 'children_enumerated': True}
        with mock.patch.object(asr_control.winproc, 'pid_state', return_value='alive'), \
             mock.patch.object(asr_control.winproc, 'process_creation_time', return_value=1.5), \
             mock.patch.object(asr_control.winproc, 'pid_matches', return_value=True):
            alive = ctl._leftovers(tree)
            idents = ctl._child_idents(tree)
            with mock.patch.object(asr_control, '_terminate_tree') as m_term:
                for ident in idents:
                    if not ctl._identity_alive(ident, tolerance=0.5):
                        continue
                    m_term(ident['pid'], force=True)
        self.assertNotIn(('child', 999), alive, '不确定身份子进程不得算作可终止遗留')
        self.assertIn(('child', 888), alive, '已知身份子进程照常进入可终止遗留')
        for call in m_term.call_args_list:
            self.assertNotEqual(call.args[0], 999, '不确定身份 PID 绝不能被终止')
        self.assertIn(888, [c.args[0] for c in m_term.call_args_list],
                      '已知身份子进程必须被清理循环终止')


class MidwayEnumerationTests(unittest.TestCase):
    """A01 补充：OS 枚举中断不得把部分清单当完整已核实清单"""

    def test_midway_failure_returns_none_and_clean_end_returns_full(self):
        if sys.platform != 'win32':
            self.skipTest('Windows 专属')
        import ctypes
        fake = mock.MagicMock()
        fake.CreateToolhelp32Snapshot.return_value = 4242
        fake.CloseHandle.return_value = 1
        P32W = asr_control.winproc.PROCESSENTRY32W
        entries = [(100, 0), (200, 100), (201, 100)]   # 根 100 + 后代 200/201

        def _fill(ref, pid, ppid):
            e = ctypes.cast(ref, ctypes.POINTER(P32W)).contents
            e.dwSize = ctypes.sizeof(P32W)
            e.th32ProcessID = pid
            e.th32ParentProcessID = ppid

        def first(snap, ref):
            _fill(ref, *entries[0])
            return 1

        def make_next(fail_at, end_err):
            state = {'i': 0}   # 已完成的 NextW 次数

            def nxt(snap, ref):
                i = state['i']
                if i + 1 >= len(entries):
                    fake.GetLastError.return_value = end_err
                    return 0
                if i >= fail_at:
                    fake.GetLastError.return_value = 5   # 枚举中断（非 NO_MORE_FILES）
                    return 0
                state['i'] = i + 1
                _fill(ref, *entries[i + 1])
                return 1
            return nxt

        with mock.patch.object(ctypes.windll, 'kernel32', fake), \
             mock.patch.object(asr_control.winproc, 'process_creation_time', return_value=111.0):
            # 枚举中断：读完根+1个后代后 Process32NextW 以错误码 5 失败
            fake.Process32FirstW.side_effect = first
            fake.Process32NextW.side_effect = make_next(fail_at=1, end_err=5)
            fake.GetLastError.return_value = 5
            self.assertIsNone(asr_control.winproc.child_processes(100),
                              '枚举中断必须返回 None，部分清单不得当完整已核实清单')
            # 正常遍历结束（ERROR_NO_MORE_FILES=18）：返回完整清单
            fake.Process32NextW.side_effect = make_next(fail_at=99, end_err=18)
            fake.GetLastError.return_value = 18
            out = asr_control.winproc.child_processes(100)
            self.assertEqual({p for p, _c in out}, {200, 201},
                             '正常遍历结束必须返回完整后代清单')


class PidStateTests(unittest.TestCase):
    """A01-read：pid_state 三态——读取失败/权限失败是 unknown，不是已退出"""

    def _patch_kernel32(self, open_ret, err, exit_ok=True):
        fake = mock.MagicMock()
        fake.OpenProcess.return_value = open_ret
        fake.GetLastError.return_value = err
        fake.GetExitCodeProcess.return_value = exit_ok
        fake.CloseHandle.return_value = 1
        return fake

    def test_exit_code_read_failure_is_unknown_not_dead(self):
        """审计复现：GetExitCodeProcess 读取失败曾被 pid_alive 当已退出并放行
        done——pid_state 必须返回 unknown（fail-closed）"""
        if sys.platform != 'win32':
            self.skipTest('Windows 专属')
        fake = self._patch_kernel32(open_ret=1234, err=0, exit_ok=False)
        with mock.patch.object(ctypes.windll, 'kernel32', fake):
            self.assertEqual(asr_control.winproc.pid_state(1234), 'unknown',
                             '退出码读取失败必须判 unknown，不得当已退出')
            self.assertFalse(asr_control.winproc.pid_alive(1234))
            self.assertNotEqual(asr_control.winproc.pid_state(1234), 'exited')

    def test_access_denied_is_unknown(self):
        """审计复现：OpenProcess 拒绝访问曾被当已退出——必须判 unknown"""
        if sys.platform != 'win32':
            self.skipTest('Windows 专属')
        fake = self._patch_kernel32(open_ret=0, err=5)
        with mock.patch.object(ctypes.windll, 'kernel32', fake):
            self.assertEqual(asr_control.winproc.pid_state(1234), 'unknown',
                             '权限拒绝必须判 unknown，不得当已退出')
            self.assertFalse(asr_control.winproc.pid_alive(1234))

    def test_nonexistent_pid_is_exited(self):
        """系统明确无此 PID（ERROR_INVALID_PARAMETER）→ 证实退出（正常停止
        流程可以完成）"""
        if sys.platform != 'win32':
            self.skipTest('Windows 专属')
        fake = self._patch_kernel32(open_ret=0, err=87)
        with mock.patch.object(ctypes.windll, 'kernel32', fake):
            self.assertEqual(asr_control.winproc.pid_state(1234), 'exited')
            self.assertFalse(asr_control.winproc.pid_alive(1234))

    def test_real_pid_states(self):
        """真实进程：存活 → alive；taskkill 后 → exited（证实退出）"""
        if sys.platform != 'win32':
            self.skipTest('Windows 专属')
        import subprocess as sp
        import time as _t
        sleeper = sp.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                           stdout=sp.DEVNULL, stderr=sp.DEVNULL)
        try:
            self.assertEqual(asr_control.winproc.pid_state(sleeper.pid), 'alive')
            self.assertTrue(asr_control.winproc.pid_alive(sleeper.pid))
            sp.run(['taskkill', '/F', '/T', '/PID', str(sleeper.pid)],
                   capture_output=True, timeout=15)
            deadline = _t.time() + 5
            while _t.time() < deadline and asr_control.winproc.pid_state(sleeper.pid) == 'alive':
                _t.sleep(0.1)
            self.assertEqual(asr_control.winproc.pid_state(sleeper.pid), 'exited',
                             'taskkill 后必须证实退出（exited），不是 unknown')
            self.assertFalse(asr_control.winproc.pid_alive(sleeper.pid))
        finally:
            if sleeper.poll() is None:
                sleeper.kill()
                sleeper.wait(timeout=10)
        self.assertEqual(asr_control.winproc.pid_state(0), 'exited')
        self.assertEqual(asr_control.winproc.pid_state(-1), 'exited')

    def test_pid_state_exception_fails_closed(self):
        """查询异常 → unknown（fail-closed），绝不判已退出"""
        with mock.patch.object(asr_control.winproc, 'pid_state',
                               side_effect=RuntimeError('boom')):
            self.assertEqual(asr_control._pid_state(1234), 'unknown')


class ControlOnlyTreeCaptureTests(_IdentityTestBase):
    """A01-control：control_ok + status.pid、无 PID 文件、无 _proc 时必须经
    认证控制通道核身并捕获树——未知树不得当空树报 done"""

    def test_recorded_tree_uses_control_status_pid(self):
        """无 PID 文件、无 _proc：经认证控制 status.pid + OS ctime/exe 核身
        后捕获根身份"""
        import subprocess as sp
        import time as _t
        sleeper = sp.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                           stdout=sp.DEVNULL, stderr=sp.DEVNULL)
        try:
            Cfg.asr_pid_file = pathlib.Path(tempfile.gettempdir()) / 'nonexistent-stage6-ctl.pid'
            Cfg.asr_control_token_path = pathlib.Path(tempfile.gettempdir()) / 'nonexistent-stage6-ctl.token'
            with mock.patch.object(asr_control.ControlClient, 'status_sync',
                                   return_value={'ok': True, 'status': {'pid': sleeper.pid}}):
                ctl = asr_control.ASRControl()
                tree = ctl._recorded_tree()
            self.assertIsNotNone(tree, 'control status.pid 必须作为根身份来源，未知树不得当空树')
            self.assertEqual(tree['root']['pid'], sleeper.pid)
            self.assertIsNotNone(tree['root']['created_at'], '根身份必须核身（OS 创建时间）')
            self.assertEqual(tree['root']['via'], 'control')
            self.assertTrue(tree['children_enumerated'])
        finally:
            if sleeper.poll() is None:
                sleeper.kill()
                sleeper.wait(timeout=10)

    def test_recorded_tree_control_pid_ctime_failure_keeps_uncertain_root(self):
        """控制 status.pid 存在但创建时间读取失败：根记 created_at=None 不确定
        （不丢失、不当作空树），_stop_verified 按身份无法核实拒绝 done"""
        import subprocess as sp
        sleeper = sp.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                           stdout=sp.DEVNULL, stderr=sp.DEVNULL)
        try:
            Cfg.asr_pid_file = pathlib.Path(tempfile.gettempdir()) / 'nonexistent-stage6-ctl2.pid'
            Cfg.asr_control_token_path = pathlib.Path(tempfile.gettempdir()) / 'nonexistent-stage6-ctl2.token'
            Cfg.asr_port = self._closed_port()
            phase = {'after_tree': False}

            def fake_status(*a, **k):
                # 树捕获后控制通道关闭（模拟 shutdown 完成）
                if phase['after_tree']:
                    return None
                return {'ok': True, 'status': {'pid': sleeper.pid}}

            with mock.patch.object(asr_control.ControlClient, 'status_sync', side_effect=fake_status), \
                 mock.patch.object(asr_control.winproc, 'process_creation_time', return_value=None), \
                 mock.patch.object(asr_control.winproc, 'asr_mutex_exists', return_value=False):
                ctl = asr_control.ASRControl()
                tree = ctl._recorded_tree()
                phase['after_tree'] = True
                self.assertIsNotNone(tree, '不确定根必须捕获（created_at=None），不得当空树')
                self.assertIsNone(tree['root']['created_at'])
                ok, why = ctl._stop_verified(tree)
            self.assertFalse(ok, '根身份读取失败必须拒绝 done')
            self.assertIn('无法核实', why)
        finally:
            if sleeper.poll() is None:
                sleeper.kill()
                sleeper.wait(timeout=10)

    def test_control_only_shutdown_consumes_captured_tree(self):
        """
        行为回归（A01-control 审计反例）：control_ok=True、status.pid 有值、
        无 PID 文件、无 _proc；shutdown 后控制/端口/互斥锁全关、worker 不监听
        端口——旧实现 _recorded_tree=None → done 且零终止；现在必须经控制
        通道捕获完整树，清理消费该树，worker 未退出即 failed、restart 不启动。
        finally 按核实身份（真实 PID）清理空进程。
        """
        import subprocess as sp
        import time as _t
        with tempfile.TemporaryDirectory() as td:
            repo = str(pathlib.Path(__file__).resolve().parents[1])
            worker_pid_file = pathlib.Path(td) / 'worker.pid'
            parent_code = (
                'import subprocess, sys, time\n'
                'worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdout=subprocess.DEVNULL)\n'
                'open(sys.argv[1], "w").write(str(worker.pid))\n'
                'time.sleep(5)\n'
            )
            parent = sp.Popen([sys.executable, '-c', parent_code, str(worker_pid_file)],
                              stdout=sp.DEVNULL, stderr=sp.DEVNULL)
            try:
                deadline = _t.time() + 10
                while _t.time() < deadline and not worker_pid_file.exists():
                    _t.sleep(0.1)
                self.assertTrue(worker_pid_file.exists(), '子进程未在期限内写 worker 文件')
                worker_pid = int(worker_pid_file.read_text().strip())
                Cfg.asr_pid_file = pathlib.Path(td) / 'unused.pid'   # 无可用 PID 文件
                Cfg.asr_control_token_path = pathlib.Path(td) / 't.token'
                Cfg.asr_port = self._closed_port()
                Cfg.asr_stop_timeout = 0.6
                phase = {'shutdown': False}

                def fake_status(*a, **k):
                    if phase['shutdown']:
                        return None   # 控制通道随 shutdown 关闭
                    return {'ok': True, 'status': {'pid': parent.pid, 'connections': 0}}

                def fake_request(cmd, **kwargs):
                    if cmd == 'shutdown':
                        phase['shutdown'] = True
                    return None   # 未确认退出请求

                calls = []

                def rec_term(pid, force):
                    calls.append((pid, force))
                    return True          # 只记录，不真杀（确定性）

                with mock.patch.object(asr_control.ControlClient, 'status_sync', side_effect=fake_status), \
                     mock.patch.object(asr_control.ControlClient, 'request_sync', side_effect=fake_request), \
                     mock.patch.object(asr_control, '_terminate_tree', side_effect=rec_term), \
                     mock.patch.object(asr_control.winproc, 'asr_mutex_exists', return_value=False), \
                     mock.patch.object(asr_control.ASRControl, '_wait_identity_gone', return_value=True):
                    ctl = asr_control.ASRControl()
                    start_mock = mock.patch.object(ctl, '_start_process')
                    with start_mock as m_start:
                        action = self._action('restart')
                        ctl._run_action(action)
                self.assertEqual(action['state'], 'failed',
                                 f'捕获的树未清干净不得 done（旧实现未知树当空树）: {action["message"]}')
                self.assertIn('停止未完成', action['message'])
                self.assertIn((parent.pid, False), calls, '捕获的根必须被优雅终止')
                self.assertIn((worker_pid, True), calls,
                              '捕获的非监听 worker 必须被清理（树证据不得缺失）')
                m_start.assert_not_called()
            finally:
                # finally 按核实身份（真实 PID）清理空进程
                try:
                    worker_pid = int(worker_pid_file.read_text().strip())
                    sp.run(['taskkill', '/F', '/T', '/PID', str(worker_pid)],
                           capture_output=True, timeout=15)
                except Exception:
                    pass
                if parent.poll() is None:
                    parent.kill()
                    parent.wait(timeout=10)


class RootAbsentChildTests(unittest.TestCase):
    """A01-root：快照无 root 但 PPID=root 的后代仍在——不得当空树"""

    def test_snapshot_without_root_keeps_children_with_root_ppid(self):
        """审计复现：root 不在快照（核身与快照间退出）、子/孙 PPID 仍指向
        root → child_processes 必须收集后代，不得返回 []"""
        if sys.platform != 'win32':
            self.skipTest('Windows 专属')
        import ctypes
        fake = mock.MagicMock()
        fake.CreateToolhelp32Snapshot.return_value = 4242
        fake.CloseHandle.return_value = 1
        P32W = asr_control.winproc.PROCESSENTRY32W
        entries = [(200, 100), (201, 200)]   # root 100 已不在快照；子 200 + 孙 201

        def _fill(ref, pid, ppid):
            e = ctypes.cast(ref, ctypes.POINTER(P32W)).contents
            e.dwSize = ctypes.sizeof(P32W)
            e.th32ProcessID = pid
            e.th32ParentProcessID = ppid

        state = {'i': 0}

        def first(snap, ref):
            state['i'] = 0
            _fill(ref, *entries[0])
            return 1

        def nxt(snap, ref):
            i = state['i'] + 1
            if i >= len(entries):
                fake.GetLastError.return_value = 18
                return 0
            state['i'] = i
            _fill(ref, *entries[i])
            return 1

        with mock.patch.object(ctypes.windll, 'kernel32', fake), \
             mock.patch.object(asr_control.winproc, 'process_creation_time', return_value=55.0):
            fake.Process32FirstW.side_effect = first
            fake.Process32NextW.side_effect = nxt
            fake.GetLastError.return_value = 18
            out = asr_control.winproc.child_processes(100)
            # 快照无 root 且无 PPID=root 后代 → 合法空
            def first2(snap, ref):
                _fill(ref, 300, 0)   # 与 100 无关的进程
                return 1

            def nxt2(snap, ref):
                fake.GetLastError.return_value = 18
                return 0

            fake.Process32FirstW.side_effect = first2
            fake.Process32NextW.side_effect = nxt2
            fake.GetLastError.return_value = 18
            empty = asr_control.winproc.child_processes(100)
        self.assertEqual({p for p, _c in out}, {200, 201},
                         '快照无 root 但 PPID=root 的后代不得当空树（审计反例）')
        self.assertEqual(empty, [], '无 root 且无其后代才是合法空')

    def test_root_gone_between_verify_and_snapshot_children_kept(self):
        """真实进程：父在核身与快照之间退出（taskkill），子仍活着且 PPID 指向
        已退出父——child_processes 不得丢后代。finally 按真实身份清理。"""
        if sys.platform != 'win32':
            self.skipTest('Windows 专属')
        import subprocess as sp
        import time as _t
        with tempfile.TemporaryDirectory() as td:
            repo = str(pathlib.Path(__file__).resolve().parents[1])
            pid_file = pathlib.Path(td) / 'parent.pid'
            worker_pid_file = pathlib.Path(td) / 'worker.pid'
            parent_code = (
                'import subprocess, sys, time\n'
                'worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdout=subprocess.DEVNULL)\n'
                'open(sys.argv[1], "w").write(str(worker.pid))\n'
                'open(sys.argv[2], "w").write(str(__import__("os").getpid()))\n'
                'time.sleep(60)\n'
            )
            parent = sp.Popen([sys.executable, '-c', parent_code,
                               str(worker_pid_file), str(pid_file)],
                              stdout=sp.DEVNULL, stderr=sp.DEVNULL)
            try:
                deadline = _t.time() + 10
                while _t.time() < deadline and not (pid_file.exists() and worker_pid_file.exists()):
                    _t.sleep(0.1)
                self.assertTrue(pid_file.exists() and worker_pid_file.exists())
                parent_pid = int(pid_file.read_text().strip())
                worker_pid = int(worker_pid_file.read_text().strip())
                sp.run(['taskkill', '/F', '/PID', str(parent_pid)],
                       capture_output=True, timeout=15)
                deadline = _t.time() + 5
                while _t.time() < deadline and asr_control.winproc.pid_state(parent_pid) != 'exited':
                    _t.sleep(0.1)
                self.assertEqual(asr_control.winproc.pid_state(parent_pid), 'exited',
                                 '父进程必须已证实退出')
                _t.sleep(0.3)   # 等内核把已退出父进程移出快照
                out = asr_control.winproc.child_processes(parent_pid)
                self.assertIn(worker_pid, [p for p, _c in out],
                              '父退出后 PPID 仍指向父的子进程不得被丢弃（审计反例）')
            finally:
                try:
                    worker_pid = int(worker_pid_file.read_text().strip())
                    sp.run(['taskkill', '/F', '/T', '/PID', str(worker_pid)],
                           capture_output=True, timeout=15)
                except Exception:
                    pass
                if parent.poll() is None:
                    parent.kill()
                    parent.wait(timeout=10)


class MergeStableUnionTests(_IdentityTestBase):
    """A01-merge：None/float 稳定并集，全部已捕获不同 (PID,ctime) 身份保留"""

    def test_merge_tree_none_and_float_same_pid_no_typeerror(self):
        """None/float 双向刷新无 TypeError；全部已捕获身份保留（None 不丢、
        不覆盖、不转死亡；不得只保留第一个 float）"""
        ctl = asr_control.ASRControl()
        uncertain = {'root': {'pid': 100, 'created_at': 1.0, 'exe': None},
                     'children': [(200, None), (201, 1.6)], 'children_enumerated': True}
        known = {'root': {'pid': 100, 'created_at': 1.0, 'exe': None},
                 'children': [(200, 1.5), (202, 1.7)], 'children_enumerated': True}
        expected_pairs = {(200, None), (200, 1.5), (201, 1.6), (202, 1.7)}
        merged = ctl._merge_tree(uncertain, known)
        self.assertEqual(set(merged['children']), expected_pairs,
                         '全部已捕获不同 (PID,ctime) 身份必须保留（None 不得丢/被覆盖）')
        self.assertTrue(merged['children_enumerated'])
        merged2 = ctl._merge_tree(known, uncertain)
        self.assertEqual(set(merged2['children']), expected_pairs, '并集与刷新方向无关')
        both = ctl._merge_tree(uncertain, uncertain)
        self.assertEqual(set(both['children']), {(200, None), (201, 1.6)},
                         '读取失败不得转死亡：不确定记录必须保留')

    def test_merge_keeps_distinct_identities_and_alive_new_worker_blocks(self):
        """
        审计复现（A01-gap4）：同 PID 的 (pid,2.0) 与 (pid,3.0) 都必须保留；
        新快照确证 3.0 是该根后代且 worker 存活时，merged 终态核验不得
        True（旧实现只保留第一 float 会丢掉新身份而放行）。
        """
        ctl = asr_control.ASRControl()
        root = {'pid': 3999900, 'created_at': 1.0, 'exe': None}
        old = {'root': root, 'children': [(3999901, 2.0)], 'children_enumerated': True}
        new = {'root': root, 'children': [(3999901, 3.0)], 'children_enumerated': True}
        merged = ctl._merge_tree(old, new)
        self.assertEqual(set(merged['children']), {(3999901, 2.0), (3999901, 3.0)},
                         '不同已捕获身份必须全部保留（不得丢掉新记录）')
        Cfg.asr_port = self._closed_port()
        Cfg.asr_pid_file = pathlib.Path(tempfile.gettempdir()) / 'nonexistent-stage7.pid'
        Cfg.asr_control_token_path = pathlib.Path(tempfile.gettempdir()) / 'nonexistent-stage7.token'
        # 活的新 worker（实际 ctime 3.0 = 新记录）：必须阻断 done
        with mock.patch.object(asr_control.winproc, 'pid_state',
                               side_effect=lambda pid: 'alive' if pid == 3999901 else 'exited'), \
             mock.patch.object(asr_control.winproc, 'process_creation_time', return_value=3.0), \
             mock.patch.object(asr_control.winproc, 'asr_mutex_exists', return_value=False), \
             mock.patch.object(asr_control.ControlClient, 'status_sync', return_value=None):
            alive = ctl._leftovers(merged)
            ok, why = ctl._stop_verified(merged)
            self.assertIn(('child', 3999901), alive, '存活的新记录身份必须算作可终止遗留')
            self.assertFalse(ok, '活的新 worker(3.0)必须阻断 done')
            self.assertIn('仍存活', why)
        # 两个已捕获身份都已消失（实际 ctime 5.0，PID 复用）→ 不计入、不阻断
        with mock.patch.object(asr_control.winproc, 'pid_state',
                               side_effect=lambda pid: 'alive' if pid == 3999901 else 'exited'), \
             mock.patch.object(asr_control.winproc, 'process_creation_time', return_value=5.0), \
             mock.patch.object(asr_control.winproc, 'asr_mutex_exists', return_value=False), \
             mock.patch.object(asr_control.ControlClient, 'status_sync', return_value=None):
            alive = ctl._leftovers(merged)
            ok, why = ctl._stop_verified(merged)
            self.assertNotIn(('child', 3999901), alive,
                             '全部记录身份均已消失（PID 复用）不算可终止遗留')
            self.assertTrue(ok, f'两个记录身份均已消失且端口/互斥锁干净应放行: {why}')


class RootGateTests(_IdentityTestBase):
    """A01-read：未知根必须 fail-closed——只有证实退出才放行"""

    def test_unknown_root_fail_closed(self):
        """审计复现：root.created_at=None 且 pid_state=unknown 曾放行 done——
        未知根必须 fail-closed、记为不可核实（不终止）；证实 exited 才放行"""
        tree = {'root': {'pid': 3999900, 'created_at': None, 'exe': None},
                'children': [], 'children_enumerated': True}
        Cfg.asr_port = self._closed_port()
        Cfg.asr_pid_file = pathlib.Path(tempfile.gettempdir()) / 'nonexistent-stage7c.pid'
        Cfg.asr_control_token_path = pathlib.Path(tempfile.gettempdir()) / 'nonexistent-stage7c.token'
        with mock.patch.object(asr_control.winproc, 'pid_state', return_value='unknown'), \
             mock.patch.object(asr_control.winproc, 'asr_mutex_exists', return_value=False), \
             mock.patch.object(asr_control.ControlClient, 'status_sync', return_value=None):
            ctl = asr_control.ASRControl()
            ok, why = ctl._stop_verified(tree)
            self.assertFalse(ok, '未知根（读取失败）必须阻断 done')
            self.assertIn('无法核实', why)
            alive = ctl._leftovers(tree)
            self.assertIn(('root-unverified', 3999900), alive,
                          '未知根必须记为不可核实（绝不终止）')
        with mock.patch.object(asr_control.winproc, 'pid_state', return_value='exited'), \
             mock.patch.object(asr_control.winproc, 'asr_mutex_exists', return_value=False), \
             mock.patch.object(asr_control.ControlClient, 'status_sync', return_value=None):
            ctl = asr_control.ASRControl()
            ok, why = ctl._stop_verified(tree)
            self.assertTrue(ok, f'未知根证实退出（exited）应放行: {why}')

    def test_unknown_root_blocks_restart_action(self):
        """完整 restart 动作：根 created_at=None（采集失败）且 pid_state=unknown →
        停止 failed（不 done）、不终止未知根、不启动新实例。finally 按真实
        PID 清理空进程。"""
        import subprocess as sp
        sleeper = sp.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                           stdout=sp.DEVNULL, stderr=sp.DEVNULL)
        try:
            Cfg.asr_pid_file = pathlib.Path(tempfile.gettempdir()) / 'nonexistent-stage7b.pid'
            Cfg.asr_control_token_path = pathlib.Path(tempfile.gettempdir()) / 'nonexistent-stage7b.token'
            Cfg.asr_port = self._closed_port()
            Cfg.asr_stop_timeout = 0.6
            calls = []
            with mock.patch.object(asr_control.ControlClient, 'status_sync', return_value=None), \
                 mock.patch.object(asr_control.ControlClient, 'request_sync', return_value=None), \
                 mock.patch.object(asr_control.winproc, 'process_creation_time', return_value=None), \
                 mock.patch.object(asr_control.winproc, 'pid_state', return_value='unknown'), \
                 mock.patch.object(asr_control.winproc, 'asr_mutex_exists', return_value=False), \
                 mock.patch.object(asr_control, '_terminate_tree',
                                   side_effect=lambda pid, force:
                                       calls.append((pid, force)) or True):
                ctl = asr_control.ASRControl()
                ctl._proc = sleeper   # 本进程拉起的受管进程（根身份采集失败）
                start_mock = mock.patch.object(ctl, '_start_process')
                with start_mock as m_start:
                    action = self._action('restart')
                    ctl._run_action(action)
            self.assertEqual(action['state'], 'failed', '未知根必须 failed，不得 done')
            self.assertIn('无法核实', action['message'])
            self.assertEqual(calls, [], '未知根绝不能被终止')
            m_start.assert_not_called()
        finally:
            if sleeper.poll() is None:
                sleeper.kill()
                sleeper.wait(timeout=10)

    def test_control_drops_before_tree_capture_refuses_restart(self):
        """
        审计复现（A01-control）：初次 status 在线、捕获树时控制失联 →
        tree=None；旧实现 shutdown 后端口/控制/互斥锁关闭即 done 且 restart
        启动——现在必须在 shutdown 前安全拒绝：failed、不发退出请求、不启动。
        终态核验层未知树同样不得当空树放行。
        """
        with tempfile.TemporaryDirectory() as td:
            Cfg.asr_pid_file = pathlib.Path(td) / 'unused.pid'
            Cfg.asr_control_token_path = pathlib.Path(td) / 't.token'
            Cfg.asr_port = self._closed_port()
            with mock.patch.object(asr_control.ASRControl, 'status',
                                   return_value={'control_ok': True, 'pid_registered': False}), \
                 mock.patch.object(asr_control.ControlClient, 'status_sync', return_value=None), \
                 mock.patch.object(asr_control.ControlClient, 'request_sync',
                                   return_value={'ok': True}) as m_req, \
                 mock.patch.object(asr_control, '_tcp_open', return_value=False), \
                 mock.patch.object(asr_control.winproc, 'asr_mutex_exists', return_value=False):
                ctl = asr_control.ASRControl()
                start_mock = mock.patch.object(ctl, '_start_process')
                with start_mock as m_start:
                    action = self._action('restart')
                    ctl._run_action(action)
                self.assertEqual(action['state'], 'failed',
                                 f'未捕获树必须 failed，不得 done: {action["message"]}')
                self.assertIn('拒绝执行停止', action['message'])
                m_req.assert_not_called()   # shutdown 前已拒绝，未发退出请求
                m_start.assert_not_called()
                # 终态核验层：未知树不得当空树
                with mock.patch.object(asr_control.ControlClient, 'status_sync', return_value=None), \
                     mock.patch.object(asr_control, '_tcp_open', return_value=False), \
                     mock.patch.object(asr_control.winproc, 'asr_mutex_exists', return_value=False):
                    ok, why = ctl._stop_verified(None)
                self.assertFalse(ok, '未知树不得当空树放行')
                self.assertIn('未知树', why)


class KnownChildSecondReadTests(_IdentityTestBase):
    """A01-gap3：已知身份子的第二次 OS 读取失败不得等价复用放行 done"""

    def test_known_child_second_read_failure_blocks(self):
        """审计复现：_leftovers 第一次读取成功(2.0)，生产 pid_matches 内部
        第二次 OS 读取失败 → 曾被误判复用放行 done——现在单次读取实际值后
        本地比较，不再把读取失败等价复用；存活 worker 阻断 done。"""
        from core.tools import win_instance as wi
        tree = {'root': {}, 'children': [(3999901, 2.0)], 'children_enumerated': True}
        Cfg.asr_port = self._closed_port()
        Cfg.asr_pid_file = pathlib.Path(tempfile.gettempdir()) / 'nonexistent-stage7d.pid'
        Cfg.asr_control_token_path = pathlib.Path(tempfile.gettempdir()) / 'nonexistent-stage7d.token'
        with mock.patch.object(asr_control.winproc, 'pid_state', return_value='alive'), \
             mock.patch.object(asr_control.winproc, 'process_creation_time', return_value=2.0), \
             mock.patch.object(wi, 'process_creation_time', return_value=None), \
             mock.patch.object(asr_control.winproc, 'asr_mutex_exists', return_value=False), \
             mock.patch.object(asr_control.ControlClient, 'status_sync', return_value=None):
            ctl = asr_control.ASRControl()
            alive = ctl._leftovers(tree)
            ok, why = ctl._stop_verified(tree)
        self.assertIn(('child', 3999901), alive, '实际身份可读且与记录匹配 → 可终止遗留')
        self.assertFalse(ok, '存活 worker 必须阻断 done（第二次读取失败不得等价复用）')
        self.assertIn('仍存活', why)


if __name__ == '__main__':
    unittest.main()
