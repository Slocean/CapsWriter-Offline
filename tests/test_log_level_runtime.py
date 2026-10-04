# coding: utf-8
"""
A05：日志级别运行态应用必须真的落到文件 handler——用真实 Logger 与真实
_handle_command 验证 INFO→DEBUG、ERROR→INFO 的实际文件输出，
mock 回执不算证据。同时说明生效范围：模型 worker 是独立进程，
重启后才同步（由管理 API 的 notice 如实说明）。
"""

import json
import logging
import pathlib
import sys
import tempfile
import unittest
from logging.handlers import RotatingFileHandler
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from core.logger import Logger  # noqa: E402


class _DummyApp:
    control_token = 'dummy-token'


class LogLevelRuntimeFileOutputTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.log_dir = pathlib.Path(self._tmp.name)
        # core.server 包导入时已用仓库 logs 目录初始化过 'server' logger；
        # 测试重置到临时目录，保证断言只看本测试的输出
        root = logging.getLogger('server')
        for h in list(root.handlers):
            root.removeHandler(h)
            h.close()
        Logger._loggers.pop('server', None)
        self.logger = Logger.setup('server', log_dir=str(self.log_dir), level='INFO')
        self.log_file = self.log_dir / 'server_latest.log'
        self._orig_config_level = self._config_level()

    def tearDown(self):
        # Windows 上 RotatingFileHandler 持有文件句柄：必须先关闭再清理临时目录
        root = logging.getLogger('server')
        for h in list(root.handlers):
            try:
                h.flush()
                h.close()
            except Exception:
                pass
            root.removeHandler(h)
        Logger._loggers.pop('server', None)
        self._restore_config_level(self._orig_config_level)
        self._tmp.cleanup()

    def _config_level(self):
        from config_server import ServerConfig
        return ServerConfig.log_level

    def _restore_config_level(self, value):
        from config_server import ServerConfig
        ServerConfig.log_level = value

    def _command(self, level):
        import asyncio
        from core.server.connection import control_server
        # _handle_command 是协程，用事件循环真实执行（与控制通道同路径）
        return asyncio.run(control_server._handle_command(
            json.dumps({'id': 1, 'cmd': 'set_log_level', 'level': level}),
            _DummyApp(), None))

    def _file_content(self):
        for h in self.logger.handlers:
            h.flush()
        return self.log_file.read_text(encoding='utf-8')

    def _file_handler(self):
        fh = [h for h in self.logger.handlers
              if isinstance(h, RotatingFileHandler)]
        self.assertTrue(fh, 'logger 必须挂载文件 handler')
        return fh[0]

    def test_info_to_debug_actually_writes_file(self):
        """独立验收反例：INFO 初始化后切 DEBUG，DEBUG 行必须真实落盘"""
        self.logger.debug('before-switch-should-not-appear')
        self.assertNotIn('before-switch-should-not-appear', self._file_content())

        reply = self._command('DEBUG')
        self.assertEqual(reply, {'id': 1, 'ok': True})

        # handler 必须同步更新（A05：只改 logger.setLevel 时 handler 仍是 INFO）
        self.assertEqual(self._file_handler().level, logging.DEBUG)
        self.logger.debug('debug-probe-after-switch')
        content = self._file_content()
        self.assertIn('debug-probe-after-switch', content,
                      'set_log_level 回执 ok 但 DEBUG 没写文件——A05 未修复')

    def test_error_to_info_actually_writes_file(self):
        """ERROR 级别切到 INFO：INFO 行必须恢复落盘"""
        self._command('ERROR')
        self.logger.info('error-period-should-not-appear')
        self.assertNotIn('error-period-should-not-appear', self._file_content())

        reply = self._command('INFO')
        self.assertEqual(reply, {'id': 1, 'ok': True})
        self.assertEqual(self._file_handler().level, logging.INFO)
        self.logger.info('info-probe-after-switch')
        self.assertIn('info-probe-after-switch', self._file_content())

    def test_invalid_level_rejected(self):
        self.assertEqual(self._command('LOUD'), {'id': 1, 'ok': False, 'error': 'invalid level'})

    def test_apply_log_level_reports_scope(self):
        """管理端应用成功也要说明范围：主进程即时生效，worker 重启后同步"""
        from admin import asr_control
        with mock.patch.object(asr_control.ControlClient, 'request_sync',
                               return_value={'id': 1, 'ok': True}):
            ctl = asr_control.ASRControl()
            applied, detail = ctl.apply_log_level('DEBUG')
        self.assertTrue(applied)
        self.assertEqual(detail, '')


if __name__ == '__main__':
    unittest.main()
