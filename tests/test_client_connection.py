# coding: utf-8
"""
客户端连接配置测试：URL 兼容迁移、部署面板 API Key 凭据存储（DPAPI）、
旧版 cw. 令牌不可被静默当作面板 Key 发送。
"""

import contextlib
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


@contextlib.contextmanager
def tempfile_dir():
    import tempfile
    td = tempfile.TemporaryDirectory()
    try:
        yield td.name
    finally:
        td.cleanup()


class CredentialStoreTests(unittest.TestCase):
    """凭据存储：Windows 上走 DPAPI（CurrentUser），其他平台明文 0600"""

    def setUp(self):
        self._orig = (ClientConfig.api_key, ClientConfig.credential_store)

    def tearDown(self):
        ClientConfig.api_key, ClientConfig.credential_store = self._orig

    def _load(self):
        import importlib
        import core.client.connection.credentials as cred
        importlib.reload(cred)
        return cred

    def test_save_load_clear(self):
        cred = self._load()
        with tempfile_dir() as td:
            path = os.path.join(td, 'credentials.json')
            self.assertEqual(cred.load_api_key(path), '')
            cred.save_api_key('sk-panel-test-123', path)
            self.assertEqual(cred.load_api_key(path), 'sk-panel-test-123')
            # 文件中无明文（Windows DPAPI）
            if os.name == 'nt':
                with open(path, encoding='utf-8') as f:
                    content = f.read()
                self.assertNotIn('sk-panel-test-123', content)
                self.assertIn('dpapi', content)
            cred.clear_api_key(path)
            self.assertEqual(cred.load_api_key(path), '')

    def test_overwrite(self):
        cred = self._load()
        with tempfile_dir() as td:
            path = os.path.join(td, 'credentials.json')
            cred.save_api_key('sk-first', path)
            cred.save_api_key('sk-second', path)
            self.assertEqual(cred.load_api_key(path), 'sk-second')

    def test_corrupt_file_returns_empty(self):
        cred = self._load()
        with tempfile_dir() as td:
            path = os.path.join(td, 'credentials.json')
            with open(path, 'w', encoding='utf-8') as f:
                f.write('{broken')
            self.assertEqual(cred.load_api_key(path), '')

    def test_legacy_token_detected(self):
        """旧版 cw. 令牌残留时可被检测（供 UI 提示重新录入）"""
        cred = self._load()
        with tempfile_dir() as td:
            path = os.path.join(td, 'credentials.json')
            self.assertFalse(cred.legacy_token_present(path))
            # 模拟旧版格式：明文 JSON 里带 server_token（非 Windows 测试环境的旧格式）
            import json
            with open(path, 'w', encoding='utf-8') as f:
                json.dump({'server_token': 'cw.legacytokenvalue123'}, f)
            self.assertTrue(cred.legacy_token_present(path))
            # 旧令牌不能被 load_api_key 读出
            self.assertEqual(cred.load_api_key(path), '')

    def test_saving_api_key_clears_legacy(self):
        cred = self._load()
        with tempfile_dir() as td:
            path = os.path.join(td, 'credentials.json')
            import json
            with open(path, 'w', encoding='utf-8') as f:
                json.dump({'server_token': 'cw.legacytokenvalue123'}, f)
            cred.save_api_key('sk-new', path)
            self.assertEqual(cred.load_api_key(path), 'sk-new')
            self.assertFalse(cred.legacy_token_present(path))


class HandshakeHeaderTests(unittest.TestCase):
    """单密钥：握手使用 X-API-Key；旧 cw. 令牌绝不能作为 Key 发送"""

    def setUp(self):
        self._orig = (ClientConfig.api_key, ClientConfig.credential_store,
                      getattr(ClientConfig, 'server_token', ''))

    def tearDown(self):
        ClientConfig.api_key, ClientConfig.credential_store = self._orig[0], self._orig[1]
        if self._orig[2]:
            ClientConfig.server_token = self._orig[2]
        elif hasattr(ClientConfig, 'server_token'):
            try:
                delattr(ClientConfig, 'server_token')
            except Exception:
                pass

    def _no_store(self):
        ClientConfig.credential_store = os.path.join(os.environ.get('TEMP', '/tmp'), 'nonexistent-creds.json')

    def test_no_key_no_headers(self):
        ClientConfig.api_key = ''
        self._no_store()
        wm = _reload_connection_module()
        self.assertEqual(wm._handshake_headers(), {})

    def test_api_key_sent_as_x_api_key(self):
        ClientConfig.api_key = 'sk-panel-1'
        self._no_store()
        wm = _reload_connection_module()
        self.assertEqual(wm._handshake_headers(), {'X-API-Key': 'sk-panel-1'})

    def test_no_bearer_authorization_header(self):
        """不再发送旧版 Authorization: Bearer 头"""
        ClientConfig.api_key = 'sk-panel-1'
        self._no_store()
        wm = _reload_connection_module()
        self.assertNotIn('Authorization', wm._handshake_headers())

    def test_legacy_config_token_ignored(self):
        """旧配置 server_token 中的 cw. 令牌被忽略，不产生任何鉴权头"""
        ClientConfig.api_key = ''
        ClientConfig.server_token = 'cw.legacytokenvalue'
        self._no_store()
        wm = _reload_connection_module()
        self.assertEqual(wm._handshake_headers(), {})
        self.assertTrue(wm.has_legacy_credentials())

    def test_legacy_store_token_ignored(self):
        """旧凭据文件中的 cw. 令牌被忽略"""
        ClientConfig.api_key = ''
        ClientConfig.server_token = ''
        wm = _reload_connection_module()
        import importlib
        import core.client.connection.credentials as cred
        with tempfile_dir() as td:
            path = os.path.join(td, 'credentials.json')
            import json
            with open(path, 'w', encoding='utf-8') as f:
                json.dump({'server_token': 'cw.legacytokenvalue123'}, f)
            importlib.reload(cred)
            ClientConfig.credential_store = path
            wm = _reload_connection_module()
            self.assertEqual(wm._handshake_headers(), {})
            self.assertTrue(wm.has_legacy_credentials())

    def test_key_from_store_used_when_config_empty(self):
        ClientConfig.api_key = ''
        wm = _reload_connection_module()
        import importlib
        import core.client.connection.credentials as cred
        with tempfile_dir() as td:
            path = os.path.join(td, 'credentials.json')
            importlib.reload(cred)
            cred.save_api_key('sk-from-store', path)
            ClientConfig.credential_store = path
            wm = _reload_connection_module()
            self.assertEqual(wm._handshake_headers(), {'X-API-Key': 'sk-from-store'})


if __name__ == '__main__':
    unittest.main()
