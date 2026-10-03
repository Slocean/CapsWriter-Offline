# coding: utf-8
"""
UDP STOP 回执测试

桌面客户端在重启/退出前发送 UDP STOP 并等待 `STOPPED` 回执后才终止
客户端进程。回执承诺：

- 未在录音时立即回执（不阻塞正常退出）；
- 在录音时，先同步完成所有录音任务的 finish()（含输出静音恢复），
  恢复完成之后才回执。
"""

import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.client.udp.udp_control import UDPController
from core.client.audio import output_mute as output_mute_pkg
from test_output_mute_owner import FakeEndpointBackend


class _FakeState:
    def __init__(self, recording):
        self.recording = recording


class _FakeTask:
    def __init__(self, recording=True):
        self.is_recording = recording
        self.finished = False

    def finish(self):
        self.finished = True


class _FakeManager:
    def __init__(self, recording, control_task_recording=True):
        self.state = _FakeState(recording)
        self.tasks = {'caps_lock': _FakeTask(recording)}
        self.control_task = _FakeTask(control_task_recording)


class _FakeSock:
    def __init__(self):
        self.sent = []

    def sendto(self, data, addr):
        self.sent.append((bytes(data), addr))


class UdpStopAckTests(unittest.TestCase):
    def _controller(self, recording):
        manager = _FakeManager(recording)
        controller = UDPController(manager)
        controller._sock = _FakeSock()
        return controller, manager

    def test_stop_while_recording_finishes_then_acks(self):
        controller, manager = self._controller(recording=True)
        addr = ('127.0.0.1', 50000)

        controller._handle_command('STOP', addr)

        self.assertTrue(manager.tasks['caps_lock'].finished)
        self.assertTrue(manager.control_task.finished)
        self.assertEqual(controller._sock.sent, [(b'STOPPED', addr)])

    def test_stop_while_idle_acks_immediately(self):
        """未在录音时也要立即回执，桌面端才不会空等超时"""
        controller, manager = self._controller(recording=False)
        addr = ('127.0.0.1', 50001)

        controller._handle_command('STOP', addr)

        self.assertFalse(manager.tasks['caps_lock'].finished)
        self.assertEqual(controller._sock.sent, [(b'STOPPED', addr)])

    def test_send_failure_does_not_break_stop(self):
        controller, manager = self._controller(recording=True)
        controller._sock.sendto = mock.Mock(side_effect=OSError('网络不可用'))

        controller._handle_command('STOP', ('127.0.0.1', 50002))   # 不抛

        self.assertTrue(manager.control_task.finished)

    def test_prepare_shutdown_latches_finishes_and_acks_with_pid(self):
        import os
        controller, manager = self._controller(recording=True)
        addr = ('127.0.0.1', 50003)

        controller._handle_command('PREPARE_SHUTDOWN|deadbeef', addr)

        # 闩锁落下、全部录音任务被同步结束、回执关联 PID 与本次请求 nonce
        self.assertTrue(manager.state.shutdown_pending)
        self.assertTrue(manager.tasks['caps_lock'].finished)
        self.assertTrue(manager.control_task.finished)
        self.assertEqual(controller._sock.sent,
                         [(f'SHUTDOWN_READY|{os.getpid()}|deadbeef'.encode('ascii'), addr)])

    def test_prepare_shutdown_reports_restore_failure(self):
        import os
        owner = output_mute_pkg.OutputMuteOwner(backend=FakeEndpointBackend())
        # 预置一个恢复始终失败的可达设备：清场必须如实报告失败
        owner._pending_restore['stuck'] = output_mute_pkg.EndpointSnapshot('stuck', '', False)
        backend = owner._backend
        backend.add_render('stuck', muted=False, fail_set=True)

        prev_owner = output_mute_pkg._OWNER
        output_mute_pkg._OWNER = owner
        try:
            controller, manager = self._controller(recording=True)
            addr = ('127.0.0.1', 50004)
            controller._handle_command('PREPARE_SHUTDOWN|feed1234', addr)
            self.assertEqual(
                controller._sock.sent,
                [(f'SHUTDOWN_RESTORE_FAILED|{os.getpid()}|feed1234'.encode('ascii'), addr)])
        finally:
            output_mute_pkg._OWNER = prev_owner

    def test_prepare_shutdown_is_idempotent(self):
        import os
        controller, manager = self._controller(recording=True)
        addr = ('127.0.0.1', 50005)

        controller._handle_command('PREPARE_SHUTDOWN|aa', addr)
        controller._handle_command('PREPARE_SHUTDOWN|bb', addr)   # 重复请求同样安全

        self.assertTrue(manager.state.shutdown_pending)
        self.assertEqual(controller._sock.sent, [
            (f'SHUTDOWN_READY|{os.getpid()}|aa'.encode('ascii'), addr),
            (f'SHUTDOWN_READY|{os.getpid()}|bb'.encode('ascii'), addr),
        ])


if __name__ == '__main__':
    unittest.main()
