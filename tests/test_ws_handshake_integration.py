# coding: utf-8
"""
WebSocket 握手鉴权集成测试（真实 websockets 17.x API）

用与 server_manager 相同的 process_request 逻辑拉起真实 WS 服务，
验证：无令牌（LAN 直连放行/拒绝）、有效令牌、错误令牌、经网关转发头。
"""

import asyncio
import json
import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import websockets  # noqa: E402
from websockets.datastructures import Headers  # noqa: E402
from websockets.http11 import Response  # noqa: E402

from core.server.connection.auth import TokenStore  # noqa: E402
from core.server.connection.handshake import HandshakeAuthorizer  # noqa: E402
from config_server import ServerConfig as Config  # noqa: E402


class WSHandshakeIntegrationTests(unittest.TestCase):
    """在 127.0.0.1 上跑真实握手。注意：对服务端而言本测试来源是回环（可信）"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = TokenStore(os.path.join(self._tmp.name, 'tokens.json'))
        self.token, _ = self.store.create('测试客户端')

        authorizer = HandshakeAuthorizer(self.store, active_connections=lambda: 0)
        reject_headers = Headers([('Content-Type', 'text/plain; charset=utf-8')])

        async def handler(connection):
            async for raw in connection:
                await connection.send(json.dumps({'echo': json.loads(raw)}))

        async def process_request(connection, request):
            remote = connection.remote_address
            ip = remote[0] if remote else None
            allowed, status, reason = authorizer(ip, request.headers)
            if not allowed:
                return Response(status, reason, reject_headers, reason.encode('utf-8'))
            return None

        self._original_mode = Config.auth_mode
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        async def _start():
            return await websockets.serve(
                handler, '127.0.0.1', 0,
                process_request=process_request, open_timeout=5,
            )

        self.server = self.loop.run_until_complete(_start())
        self.port = self.server.sockets[0].getsockname()[1]

    def tearDown(self):
        self.server.close()
        self.loop.run_until_complete(self.server.wait_closed())
        Config.auth_mode = self._original_mode
        self.loop.close()
        self._tmp.cleanup()

    def _connect(self, headers=None, extra=None):
        async def go():
            kwargs = dict(open_timeout=5, proxy=None)
            if headers:
                kwargs['additional_headers'] = headers
            if extra:
                kwargs.update(extra)
            async with websockets.connect(f'ws://127.0.0.1:{self.port}', **kwargs) as ws:
                await ws.send('{"hello": 1}')
                reply = await asyncio.wait_for(ws.recv(), timeout=5)
                return json.loads(reply)
        return self.loop.run_until_complete(go())

    def test_loopback_without_token_allowed_in_lan_legacy(self):
        Config.auth_mode = 'lan_legacy'
        reply = self._connect()
        self.assertEqual(reply, {'echo': {'hello': 1}})

    def test_loopback_without_token_rejected_in_required_mode(self):
        Config.auth_mode = 'required'
        with self.assertRaises(websockets.exceptions.InvalidStatus) as ctx:
            self._connect()
        self.assertEqual(ctx.exception.response.status_code, 401)

    def test_valid_token_accepted(self):
        Config.auth_mode = 'required'
        reply = self._connect({'Authorization': f'Bearer {self.token}'})
        self.assertEqual(reply, {'echo': {'hello': 1}})

    def test_invalid_token_rejected(self):
        Config.auth_mode = 'required'
        with self.assertRaises(websockets.exceptions.InvalidStatus) as ctx:
            self._connect({'Authorization': 'Bearer cw.invalidtoken123'})
        self.assertEqual(ctx.exception.response.status_code, 401)

    def test_revoked_token_rejected(self):
        Config.auth_mode = 'required'
        _, entry = self.store.create('临时')
        self.store.revoke(entry['id'])
        with self.assertRaises(websockets.exceptions.InvalidStatus) as ctx:
            self._connect({'Authorization': 'Bearer ' + 'cw.' + entry['hash'][:20]})
        self.assertEqual(ctx.exception.response.status_code, 401)

    def test_forwarded_header_requires_token_in_lan_legacy(self):
        Config.auth_mode = 'lan_legacy'
        with self.assertRaises(websockets.exceptions.InvalidStatus) as ctx:
            self._connect({'X-Forwarded-For': '203.0.113.5'})
        self.assertEqual(ctx.exception.response.status_code, 401)

    def test_forwarded_with_valid_token_ok(self):
        Config.auth_mode = 'lan_legacy'
        reply = self._connect({
            'X-Forwarded-For': '203.0.113.5',
            'Authorization': f'Bearer {self.token}',
        })
        self.assertEqual(reply, {'echo': {'hello': 1}})

    def test_last_remote_recorded(self):
        Config.auth_mode = 'required'
        self._connect({'Authorization': f'Bearer {self.token}'})
        entry = self.store.summary()[0]
        self.assertIsNotNone(entry['last_used_at'])


if __name__ == '__main__':
    unittest.main()
