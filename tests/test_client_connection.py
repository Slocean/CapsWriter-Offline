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
        self._orig = (getattr(ClientConfig, 'api_key', ''), ClientConfig.credential_store)

    def tearDown(self):
        ClientConfig.credential_store = self._orig[1]
        if self._orig[0]:
            ClientConfig.api_key = self._orig[0]
        elif hasattr(ClientConfig, 'api_key'):
            try:
                delattr(ClientConfig, 'api_key')
            except Exception:
                pass

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
    """单密钥：仅 wss:// 携带 X-API-Key 且 Key 只来自 DPAPI 凭据存储；
    配置文件中的明文 api_key/server_token 一律忽略；明文 ws:// 绝不带 Key"""

    def setUp(self):
        self._orig = (getattr(ClientConfig, 'api_key', ''),
                      ClientConfig.credential_store,
                      getattr(ClientConfig, 'server_token', ''),
                      getattr(ClientConfig, 'server_url', ''))

    def tearDown(self):
        ClientConfig.credential_store = self._orig[1]
        ClientConfig.server_url = self._orig[3]
        for name in ('api_key', 'server_token'):
            old = self._orig[0] if name == 'api_key' else self._orig[2]
            if old:
                setattr(ClientConfig, name, old)
            elif hasattr(ClientConfig, name):
                try:
                    delattr(ClientConfig, name)
                except Exception:
                    pass

    def _no_store(self):
        ClientConfig.credential_store = os.path.join(os.environ.get('TEMP', '/tmp'), 'nonexistent-creds.json')

    def test_wss_with_store_key_sends_x_api_key(self):
        wm = _reload_connection_module()
        import importlib
        import core.client.connection.credentials as cred
        with tempfile_dir() as td:
            path = os.path.join(td, 'credentials.json')
            importlib.reload(cred)
            cred.save_api_key('sk-panel-1', path)
            ClientConfig.credential_store = path
            wm = _reload_connection_module()
            self.assertEqual(wm._handshake_headers('wss://voice.example.com'),
                             {'X-API-Key': 'sk-panel-1'})

    def test_config_plaintext_key_ignored(self):
        """P1-13：配置文件里的明文 Key 绝不优先、绝不发送，只提示迁移"""
        ClientConfig.api_key = 'sk-config-plaintext'
        self._no_store()
        wm = _reload_connection_module()
        self.assertEqual(wm._handshake_headers('wss://voice.example.com'), {})
        self.assertTrue(wm.has_legacy_credentials())

    def test_config_plaintext_key_does_not_override_store(self):
        """配置明文不得覆盖 DPAPI 存储里的新 Key"""
        wm = _reload_connection_module()
        import importlib
        import core.client.connection.credentials as cred
        with tempfile_dir() as td:
            path = os.path.join(td, 'credentials.json')
            importlib.reload(cred)
            cred.save_api_key('sk-from-store', path)
            ClientConfig.credential_store = path
            ClientConfig.api_key = 'sk-config-plaintext'
            wm = _reload_connection_module()
            self.assertEqual(wm._handshake_headers('wss://voice.example.com'),
                             {'X-API-Key': 'sk-from-store'})

    def test_no_key_no_headers(self):
        self._no_store()
        wm = _reload_connection_module()
        self.assertEqual(wm._handshake_headers('wss://voice.example.com'), {})
        self.assertEqual(wm._handshake_headers('ws://192.168.0.104:6016'), {})

    def test_no_bearer_authorization_header(self):
        """不再发送旧版 Authorization: Bearer 头"""
        wm = _reload_connection_module()
        import importlib
        import core.client.connection.credentials as cred
        with tempfile_dir() as td:
            path = os.path.join(td, 'credentials.json')
            importlib.reload(cred)
            cred.save_api_key('sk-panel-1', path)
            ClientConfig.credential_store = path
            wm = _reload_connection_module()
            self.assertNotIn('Authorization', wm._handshake_headers('wss://voice.example.com'))

    def test_plaintext_ws_never_sends_key(self):
        """明文 ws:// 绝不携带面板 Key（LAN 直连与误填的远程地址都一样）"""
        wm = _reload_connection_module()
        import importlib
        import core.client.connection.credentials as cred
        with tempfile_dir() as td:
            path = os.path.join(td, 'credentials.json')
            importlib.reload(cred)
            cred.save_api_key('sk-panel-1', path)
            ClientConfig.credential_store = path
            wm = _reload_connection_module()
            self.assertEqual(wm._handshake_headers('ws://192.168.0.104:6016'), {})
            self.assertEqual(wm._handshake_headers('ws://voice.example.com'), {})

    def test_legacy_config_token_ignored(self):
        """旧配置 server_token 中的 cw. 令牌被忽略，不产生任何鉴权头"""
        ClientConfig.server_token = 'cw.legacytokenvalue'
        self._no_store()
        wm = _reload_connection_module()
        self.assertEqual(wm._handshake_headers('wss://voice.example.com'), {})
        self.assertTrue(wm.has_legacy_credentials())

    def test_legacy_store_token_ignored(self):
        """旧凭据文件中的 cw. 令牌被忽略"""
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
            self.assertEqual(wm._handshake_headers('wss://voice.example.com'), {})
            self.assertTrue(wm.has_legacy_credentials())


class BuildUrlSecurityTests(unittest.TestCase):
    """URL 安全：拒绝 userinfo，日志脱敏"""

    def setUp(self):
        self._orig = ClientConfig.server_url

    def tearDown(self):
        ClientConfig.server_url = self._orig

    def test_userinfo_rejected(self):
        ClientConfig.server_url = 'wss://user:pass@voice.example.com'
        wm = _reload_connection_module()
        with self.assertRaises(ValueError):
            wm.build_server_url()

    def test_sanitize_url_for_log_strips_query(self):
        wm = _reload_connection_module()
        self.assertEqual(wm.sanitize_url_for_log('wss://voice.example.com/?token=secret'),
                         'wss://voice.example.com/')
        self.assertEqual(wm.sanitize_url_for_log('wss://voice.example.com/asr#frag'),
                         'wss://voice.example.com/asr')


if __name__ == '__main__':
    unittest.main()
