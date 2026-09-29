# coding: utf-8
"""
单密钥接入相关测试：

- 日志脱敏（X-API-Key / Bearer / 旧 cw. 令牌 / 控制通道令牌）；
- 网页设置白名单校验；
- WebSocket 握手资源上限（对外鉴权已移交部署面板机器门，ASR 只保留上限检查）。
"""

import asyncio
import json
import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from admin.logsource import sanitize  # noqa: E402
from admin import settings_store  # noqa: E402
from config_server import ServerConfig as Config  # noqa: E402


class SanitizeTests(unittest.TestCase):
    def test_redacts_api_key_header(self):
        self.assertEqual(
            sanitize('握手校验 X-API-Key: sk-panel-abcdef123456 失败'),
            '握手校验 X-API-Key: <redacted> 失败',
        )

    def test_redacts_bearer(self):
        self.assertEqual(sanitize('Authorization: Bearer abc.1234567890'), 'Authorization: Bearer <redacted>')

    def test_redacts_legacy_client_token(self):
        self.assertEqual(sanitize('旧值 cw.AbCdEfGh12345678 出现'), '旧值 <legacy-token> 出现')

    def test_redacts_control_token(self):
        self.assertEqual(sanitize('ctrl.AbCdEfGh123456789'), '<control-token>')

    def test_keeps_normal_lines(self):
        self.assertEqual(sanitize('普通日志，正常'), '普通日志，正常')


class SettingsValidationTests(unittest.TestCase):
    def test_rejects_unknown_key(self):
        _, err = settings_store.validate_settings({'root_password': 'x'})
        self.assertTrue(err)

    def test_rejects_auth_mode(self):
        # 语音鉴权已移交部署面板机器门，网页不再提供 auth_mode 设置
        _, err = settings_store.validate_settings({'auth_mode': 'required'})
        self.assertTrue(err)

    def test_rejects_bad_enum(self):
        _, err = settings_store.validate_settings({'model_type': 'gpt-5'})
        self.assertTrue(err)

    def test_rejects_out_of_range(self):
        _, err = settings_store.validate_settings({'aligner_idle_timeout': 99999})
        self.assertTrue(err)
        _, err = settings_store.validate_settings({'aligner_idle_timeout': -1})
        self.assertTrue(err)

    def test_rejects_non_bool(self):
        _, err = settings_store.validate_settings({'gpu_boost_enabled': 'yes'})
        self.assertTrue(err)

    def test_accepts_valid(self):
        cleaned, err = settings_store.validate_settings({
            'model_type': 'sensevoice',
            'log_level': 'INFO',
            'aligner_idle_timeout': 0,
            'gpu_boost_enabled': False,
        })
        self.assertEqual(err, '')
        self.assertEqual(cleaned['model_type'], 'sensevoice')

    def test_save_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            settings_store.Cfg.settings_file = pathlib.Path(td) / 'server_settings.json'
            ok, err = settings_store.save_settings({'log_level': 'WARNING'})
            self.assertEqual(err, '')
            ok, err = settings_store.save_settings({'model_type': 'sensevoice'})
            self.assertEqual(err, '')
            values = settings_store.load_settings()
            self.assertEqual(values['log_level'], 'WARNING')
            self.assertEqual(values['model_type'], 'sensevoice')
            self.assertTrue((pathlib.Path(td) / 'server_settings.json.bak').exists())

    def test_old_settings_file_without_auth_mode_still_loads(self):
        # 旧版写入的 server_settings.json 可能包含 auth_mode：加载时被忽略，不影响迁移
        with tempfile.TemporaryDirectory() as td:
            settings_store.Cfg.settings_file = pathlib.Path(td) / 'server_settings.json'
            settings_store.Cfg.settings_file.write_text(
                json.dumps({'auth_mode': 'lan_legacy', 'log_level': 'INFO'}),
                encoding='utf-8')
            values = settings_store.load_settings()
            self.assertEqual(values, {'log_level': 'INFO'})


class WSResourceLimitIntegrationTests(unittest.TestCase):
    """真实 websockets 服务：无鉴权头可连接（鉴权在网关），连接数上限仍然生效"""

    def setUp(self):
        import websockets
        from websockets.datastructures import Headers
        from websockets.http11 import Response

        self._websockets = websockets
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        reject_headers = Headers([('Content-Type', 'text/plain; charset=utf-8')])

        async def handler(connection):
            try:
                async for raw in connection:
                    await connection.send(json.dumps({'echo': json.loads(raw)}))
            finally:
                self.connections -= 1

        async def process_request(connection, request):
            # 与 server_manager._process_request 相同的上限逻辑
            if self.connections >= self.max_connections:
                return Response(503, 'connection limit reached', reject_headers,
                                b'connection limit reached\n')
            self.connections += 1
            return None

        self.max_connections = 1
        self.connections = 0

        async def _start():
            return await websockets.serve(handler, '127.0.0.1', 0,
                                          process_request=process_request, open_timeout=5)

        self.server = self.loop.run_until_complete(_start())
        self.port = self.server.sockets[0].getsockname()[1]

    def tearDown(self):
        self.server.close()
        self.loop.run_until_complete(self.server.wait_closed())
        self.loop.close()

    def _connect_send(self, headers=None):
        async def go():
            kwargs = dict(open_timeout=5, proxy=None)
            if headers:
                kwargs['additional_headers'] = headers
            async with self._websockets.connect(f'ws://127.0.0.1:{self.port}', **kwargs) as ws:
                await ws.send('{"hello": 1}')
                reply = await asyncio.wait_for(ws.recv(), timeout=5)
                return json.loads(reply)
        return self.loop.run_until_complete(go())

    def test_connect_without_credentials_allowed(self):
        # 对外鉴权由部署面板机器门完成，ASR 不再要求任何令牌头
        reply = self._connect_send()
        self.assertEqual(reply, {'echo': {'hello': 1}})

    def test_connect_with_api_key_header_allowed(self):
        # 网关转发的请求会带 X-API-Key；ASR 对其不做校验也不拒绝
        reply = self._connect_send({'X-API-Key': 'placeholder-key'})
        self.assertEqual(reply, {'echo': {'hello': 1}})

    def test_connection_limit_returns_503(self):
        # 占住唯一连接槽位后，第二个连接应在握手阶段被 503 拒绝
        async def go():
            ws = await self._websockets.connect(f'ws://127.0.0.1:{self.port}',
                                                open_timeout=5, proxy=None)
            await asyncio.sleep(0.2)  # 等 handler 登记，模拟 sockets 池计数
            with self.assertRaises(self._websockets.exceptions.InvalidStatus) as ctx:
                await self._websockets.connect(f'ws://127.0.0.1:{self.port}',
                                               open_timeout=5, proxy=None)
            self.assertEqual(ctx.exception.response.status_code, 503)
            await ws.close()

        self.loop.run_until_complete(go())

    def test_limits_not_regressed(self):
        # 移除令牌校验时不得退回 max_size=None 或放宽上限
        self.assertEqual(Config.ws_max_message_size, 64 * 1024 * 1024)
        self.assertGreater(Config.ws_max_connections, 0)
        self.assertTrue(hasattr(Config, 'control_port'))
        self.assertTrue(hasattr(Config, 'control_token_path'))
        self.assertFalse(hasattr(Config, 'auth_mode'))
        self.assertFalse(hasattr(Config, 'tokens_path'))


if __name__ == '__main__':
    unittest.main()
