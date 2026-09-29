# coding: utf-8
"""
客户端连接配置测试：URL 兼容迁移、凭据存储（DPAPI）
"""

import os
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from config_client import ClientConfig  # noqa: E402


def _reload_connection_module():
    # 保证读到当前 ClientConfig 的值
    import importlib
    import core.client.connection.websocket_manager as wm
    importlib.reload(wm)
    return wm


class BuildUrlTests(unittest.TestCase):
    def setUp(self):
        self._orig = (ClientConfig.server_url, ClientConfig.addr, ClientConfig.port)

    def tearDown(self):
        ClientConfig.server_url, ClientConfig.addr, ClientConfig.port = self._orig

    def test_legacy_addr_port(self):
        ClientConfig.server_url = ''
        ClientConfig.addr = '192.168.0.104'
        ClientConfig.port = '6016'
        wm = _reload_connection_module()
        self.assertEqual(wm.build_server_url(), 'ws://192.168.0.104:6016')

    def test_full_url_preferred(self):
        ClientConfig.server_url = 'wss://voice.example.com'
        wm = _reload_connection_module()
        self.assertEqual(wm.build_server_url(), 'wss://voice.example.com')

    def test_full_url_with_path_and_trailing_slash(self):
        ClientConfig.server_url = 'wss://voice.example.com/asr/'
        wm = _reload_connection_module()
        self.assertEqual(wm.build_server_url(), 'wss://voice.example.com/asr')

    def test_invalid_scheme_rejected(self):
        ClientConfig.server_url = 'https://voice.example.com'
        wm = _reload_connection_module()
        with self.assertRaises(ValueError):
            wm.build_server_url()

    def test_plain_http_rejected(self):
        ClientConfig.server_url = 'http://voice.example.com'
        wm = _reload_connection_module()
        with self.assertRaises(ValueError):
            wm.build_server_url()


class CredentialStoreTests(unittest.TestCase):
    """凭据存储：Windows 上走 DPAPI（CurrentUser），其他平台明文 0600"""

    def setUp(self):
        self._orig = (ClientConfig.server_token, ClientConfig.credential_store)

    def tearDown(self):
        ClientConfig.server_token, ClientConfig.credential_store = self._orig

    def _load(self):
        import importlib
        import core.client.connection.credentials as cred
        importlib.reload(cred)
        return cred

    def test_save_load_clear(self):
        cred = self._load()
        with tempfile_dir() as td:
            path = os.path.join(td, 'credentials.json')
            self.assertEqual(cred.load_token(path), '')
            cred.save_token('cw.test-token-123', path)
            self.assertEqual(cred.load_token(path), 'cw.test-token-123')
            # 文件中无明文（Windows DPAPI）
            if os.name == 'nt':
                with open(path, encoding='utf-8') as f:
                    content = f.read()
                self.assertNotIn('cw.test-token-123', content)
                self.assertIn('dpapi', content)
            cred.clear_token(path)
            self.assertEqual(cred.load_token(path), '')

    def test_overwrite(self):
        cred = self._load()
        with tempfile_dir() as td:
            path = os.path.join(td, 'credentials.json')
            cred.save_token('cw.first', path)
            cred.save_token('cw.second', path)
            self.assertEqual(cred.load_token(path), 'cw.second')

    def test_corrupt_file_returns_empty(self):
        cred = self._load()
        with tempfile_dir() as td:
            path = os.path.join(td, 'credentials.json')
            with open(path, 'w', encoding='utf-8') as f:
                f.write('{broken')
            self.assertEqual(cred.load_token(path), '')


import contextlib  # noqa: E402


@contextlib.contextmanager
def tempfile_dir():
    import tempfile
    td = tempfile.TemporaryDirectory()
    try:
        yield td.name
    finally:
        td.cleanup()


class HandshakeHeaderTests(unittest.TestCase):
    def setUp(self):
        self._orig = (ClientConfig.server_token, ClientConfig.credential_store)

    def tearDown(self):
        ClientConfig.server_token, ClientConfig.credential_store = self._orig

    def test_no_token_no_headers(self):
        ClientConfig.server_token = ''
        import core.client.connection.websocket_manager as wm
        headers = wm._handshake_headers()
        self.assertEqual(headers, {})

    def test_plaintext_token_used(self):
        ClientConfig.server_token = 'cw.direct'
        ClientConfig.credential_store = os.path.join(os.environ.get('TEMP', '/tmp'), 'nonexistent-creds.json')
        import core.client.connection.websocket_manager as wm
        headers = wm._handshake_headers()
        self.assertEqual(headers, {'Authorization': 'Bearer cw.direct'})


if __name__ == '__main__':
    unittest.main()
