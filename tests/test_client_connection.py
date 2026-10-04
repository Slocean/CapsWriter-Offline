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
from unittest import mock

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


class R05OutputRedactionTests(unittest.TestCase):
    """R05/A03：连接成功的 console 回显不得带 query；JSON 形式与任意文本先脱敏"""

    REPO = pathlib.Path(__file__).resolve().parents[1]

    def test_redact_text_masks_key_like_values(self):
        wm = _reload_connection_module()
        text = wm.redact_text('握手失败 X-API-Key: dpk-fake-secret-123456 被拒')
        self.assertNotIn('dpk-fake-secret-123456', text)
        self.assertIn('<redacted>', text)
        self.assertNotIn('tok123456789', wm.redact_text('url?token=tok123456789'))
        self.assertNotIn('abc123456789', wm.redact_text('Authorization: Bearer abc123456789'))

    def test_redact_text_masks_json_field_forms(self):
        """A03 反例：JSON 字段形式不得原样漏出"""
        wm = _reload_connection_module()
        for text in (
            '{"api_key": "dpk-fake-secret-123456"}',
            '{"X-API-Key": "dpk-fake-secret-123456"}',
            "异常上下文 {'server_token': 'dpk-fake-secret-123456'} 溢出",
            "url error for api_key = 'dpk-fake-secret-123456' request",
        ):
            out = wm.redact_text(text)
            self.assertNotIn('dpk-fake-secret-123456', out, f'未脱敏: {text!r}')
            self.assertIn('<redacted>', out)

    def test_redact_text_keeps_normal_text(self):
        wm = _reload_connection_module()
        self.assertEqual(wm.redact_text('普通错误消息'), '普通错误消息')

    def test_connect_success_console_uses_sanitized_url(self):
        """源码契约：连接成功的 console 输出走 sanitize_url_for_log（R05）"""
        source = (self.REPO / 'core' / 'client' / 'connection' / 'websocket_manager.py').read_text(encoding='utf-8')
        self.assertIn('已连接服务端: {sanitize_url_for_log(url)}', source)
        # 不得再有裸 {url} 直接进 console
        import re
        bare = [m for m in re.findall(r'console\.print\(f[^\n]*\{url\}', source)]
        self.assertEqual(bare, [], f'console 仍在输出原始 URL: {bare}')

    def test_connect_success_log_line_sanitized(self):
        """WS 连接 URL 带 query 时，控制台与日志输出都必须去掉 query（R05 反例）"""
        wm = _reload_connection_module()
        url = 'wss://voice.example.com/?api_key=dpk-fake-secret-123456'
        safe = wm.sanitize_url_for_log(url)
        self.assertNotIn('dpk-fake-secret-123456', safe)
        self.assertNotIn('dpk-fake-secret-123456', wm.redact_text(url))


class CredentialUrlRejectionTests(unittest.TestCase):
    """A03：凭据不得随 URL 传递——build_server_url 拒绝凭据查询参数与 fragment，
    普通路径/查询允许（经校验）"""

    def setUp(self):
        self._orig = ClientConfig.server_url

    def tearDown(self):
        ClientConfig.server_url = self._orig

    def _build(self):
        return _reload_connection_module().build_server_url()

    def test_credential_query_rejected(self):
        for url in (
            'wss://voice.example.com/?api_key=dpk-fake-secret-123456',
            'wss://voice.example.com/asr?token=abc123',
            'wss://voice.example.com/?API_KEY=dpk-fake-secret-123456',
            'wss://voice.example.com/?room=1&secret=xyz',
            'wss://voice.example.com/?x-api-key=dpk-fake-secret-123456',
            'wss://voice.example.com/?X-API-KEY=dpk-fake-secret-123456',
        ):
            ClientConfig.server_url = url
            with self.assertRaises(ValueError, msg=url):
                self._build()

    def test_fragment_rejected(self):
        ClientConfig.server_url = 'wss://voice.example.com/a#frag'
        with self.assertRaises(ValueError):
            self._build()

    def test_normal_path_and_query_allowed(self):
        ClientConfig.server_url = 'wss://voice.example.com/asr'
        self.assertEqual(self._build(), 'wss://voice.example.com/asr')
        ClientConfig.server_url = 'wss://voice.example.com/asr?room=1&mode=fast'
        self.assertEqual(self._build(), 'wss://voice.example.com/asr?room=1&mode=fast')

    def test_encoded_credential_names_rejected_like_desktop(self):
        """A03 一致性：参数名归一化与桌面端同一策略——单轮/双重 %xx 编码、
        %5f、'+' 前缀、大小写变换都不能绕过凭据名拒绝（双重编码 %2561pi_key
        是桌面拒绝而客户端放行的不一致反例）"""
        for url in (
            'wss://voice.example.com/?%61pi_key=dpk-fake-only-12345',
            'wss://voice.example.com/?%2561pi_key=dpk-fake-only-12345',
            'wss://voice.example.com/?api%5fkey=dpk-fake-only-12345',
            'wss://voice.example.com/?+api_key=dpk-fake-only-12345',
            'wss://voice.example.com/?API-KEY=dpk-fake-only-12345',
            'wss://voice.example.com/?X_API_KEY=dpk-fake-only-12345',
            'wss://voice.example.com/?q=1&%2561pi_key=x',
        ):
            ClientConfig.server_url = url
            with self.assertRaises(ValueError, msg=url):
                self._build()

    def test_normal_query_with_plus_and_encoded_value_allowed(self):
        """合法普通查询（含 '+' 与编码值）仍放行，不误伤"""
        ClientConfig.server_url = 'wss://voice.example.com/?q=normal'
        self.assertEqual(self._build(), 'wss://voice.example.com/?q=normal')
        ClientConfig.server_url = 'wss://voice.example.com/?a+b=c&name=%20x'
        self.assertEqual(self._build(), 'wss://voice.example.com/?a+b=c&name=%20x')


class KnownKeyRedactionTests(unittest.TestCase):
    """A03：redact_text 必须精确替换当前 DPAPI 已知 Key（含异常文本裸回显），
    清洗失败输出固定摘要不回传原文。全程使用假 Key，不触碰真实 Key。"""

    FAKE = 'dpk-round3-fake-99887766'

    def setUp(self):
        self._orig_credential_store = ClientConfig.credential_store

    def tearDown(self):
        ClientConfig.credential_store = self._orig_credential_store

    def _wm(self):
        import importlib
        import core.client.connection.credentials as cred
        importlib.reload(cred)
        import core.client.connection.websocket_manager as wm
        importlib.reload(wm)
        return wm, cred

    def _with_stored_fake_key(self):
        import os
        import tempfile
        td = tempfile.TemporaryDirectory()
        path = os.path.join(td.name, 'credentials.json')
        ClientConfig.credential_store = path
        wm, cred = self._wm()
        cred.save_api_key(self.FAKE, path)
        wm, cred = self._wm()          # 重载让 load_api_key 指向新路径
        return wm, td

    def test_known_key_replaced_in_bare_exception_text(self):
        wm, td = self._with_stored_fake_key()
        try:
            out = wm.redact_text(f'连接失败: InvalidStatus response={self.FAKE} trace...')
            self.assertNotIn(self.FAKE, out, '已知 Key 在异常文本中裸回显')
            self.assertIn('<redacted>', out)
        finally:
            td.cleanup()

    def test_known_key_replaced_in_json_header_url_forms(self):
        wm, td = self._with_stored_fake_key()
        try:
            for text in (
                f'{{"api_key": "{self.FAKE}"}}',
                f'X-API-Key: {self.FAKE} 被拒',
                f'wss://voice.example.com/?api_key={self.FAKE}',
                f"{{'server_token': '{self.FAKE}'}}",
            ):
                out = wm.redact_text(text)
                self.assertNotIn(self.FAKE, out, f'未脱敏: {text!r}')
                self.assertIn('<redacted>', out)
        finally:
            td.cleanup()

    def test_redaction_failure_returns_fixed_summary(self):
        """清洗自身失败（匹配引擎异常）必须输出固定摘要，绝不回传原文"""
        wm, td = self._with_stored_fake_key()
        try:
            class _Boom:
                @staticmethod
                def sub(*_a, **_k):
                    raise RuntimeError('regex engine boom')
            with mock.patch.object(wm, '_JSON_STYLE_REDACTIONS', [_Boom()]):
                out = wm.redact_text(f'raw text with {self.FAKE} inside')
            self.assertNotIn(self.FAKE, out, '清洗失败不得回传原文')
            self.assertIn('已省略', out)
        finally:
            td.cleanup()

    def test_no_key_still_masks_patterns(self):
        """无已存 Key 时模式打码仍生效（正常输出不退化）"""
        import os
        ClientConfig.credential_store = os.path.join(
            os.environ.get('TEMP', '/tmp'), 'nonexistent-round3-creds.json')
        wm, cred = self._wm()
        out = wm.redact_text('握手失败 X-API-Key: dpk-fake-secret-123456 被拒')
        self.assertNotIn('dpk-fake-secret-123456', out)
        self.assertIn('<redacted>', out)


class HandshakeKeyRedactionContextTests(unittest.TestCase):
    """A03：成功取得握手 Key 后，凭据读取抛错/变空/损坏，裸已知 Key 仍被
    内存脱敏上下文保护；读取失败且无会话上下文时固定摘要，绝不回原文。
    全程假 Key，不读/不展示真实值。"""

    FAKE = 'dpk-stage4-fake-55667788'

    def setUp(self):
        self._orig_credential_store = ClientConfig.credential_store

    def tearDown(self):
        ClientConfig.credential_store = self._orig_credential_store

    def _wm(self):
        import importlib
        import core.client.connection.credentials as cred
        importlib.reload(cred)
        import core.client.connection.websocket_manager as wm
        importlib.reload(wm)
        return wm, cred

    def _handshake_with_fake_key(self):
        """成功取得握手 Key（进入内存脱敏上下文），返回 (wm, 临时目录, 存储路径)"""
        import os
        import tempfile
        td = tempfile.TemporaryDirectory()
        path = os.path.join(td.name, 'credentials.json')
        ClientConfig.credential_store = path
        wm, cred = self._wm()
        cred.save_api_key(self.FAKE, path)
        wm, cred = self._wm()          # 重载让 load_api_key 指向新路径
        headers = wm._handshake_headers('wss://voice.example.com')
        self.assertEqual(headers, {'X-API-Key': self.FAKE}, '前提：握手成功取得假 Key')
        return wm, td, path

    def test_bare_key_protected_after_read_raises(self):
        """凭据读取抛错后（read 失败），裸已知 Key 仍必须被会话上下文保护"""
        wm, td, _path = self._handshake_with_fake_key()
        try:
            with mock.patch.object(wm, 'load_api_key_status',
                                   side_effect=RuntimeError('cred store boom')):
                out = wm.redact_text(f'握手失败 X-API-Key: {self.FAKE} 被拒')
            self.assertNotIn(self.FAKE, out, '凭据读取抛错后裸 Key 不得回原文')
            self.assertIn('<redacted>', out)
        finally:
            td.cleanup()

    def test_bare_key_protected_after_store_corrupt(self):
        """凭据存储损坏（DPAPI/JSON 读取失败）后，裸已知 Key 仍被保护"""
        wm, td, path = self._handshake_with_fake_key()
        try:
            with open(path, 'w', encoding='utf-8') as f:
                f.write('{broken')
            out = wm.redact_text(f'连接失败: InvalidStatus {self.FAKE} trace')
            self.assertNotIn(self.FAKE, out, '存储损坏后裸 Key 不得回原文')
            self.assertIn('<redacted>', out)
        finally:
            td.cleanup()

    def test_bare_key_protected_after_store_emptied(self):
        """凭据变空（Key 被清除）后，已用于握手的 Key 仍被会话上下文保护"""
        wm, td, path = self._handshake_with_fake_key()
        try:
            import json
            with open(path, 'w', encoding='utf-8') as f:
                json.dump({'api_key': ''}, f)
            out = wm.redact_text(f'error with {self.FAKE} inside')
            self.assertNotIn(self.FAKE, out, '凭据变空后已用 Key 不得回原文')
            self.assertIn('<redacted>', out)
        finally:
            td.cleanup()

    def test_read_failure_without_session_context_fails_closed(self):
        """从未取得握手 Key 且凭据读取失败：不能证明无凭据，必须固定摘要"""
        import os
        ClientConfig.credential_store = os.path.join(
            os.environ.get('TEMP', '/tmp'), 'nonexistent-stage4-creds.json')
        wm, _cred = self._wm()
        with mock.patch.object(wm, 'load_api_key_status',
                               side_effect=RuntimeError('cred store boom')):
            out = wm.redact_text('普通错误 dpk-something-secret-value')
        self.assertIn('已省略', out, '读取失败且无上下文时必须固定摘要（失败不降级）')
        self.assertNotIn('dpk-something-secret-value', out)

    def test_corrupt_store_without_session_context_fails_closed(self):
        """存储损坏（读取失败）且无会话上下文：同样固定摘要，绝不回原文"""
        import os
        import tempfile
        td = tempfile.TemporaryDirectory()
        path = os.path.join(td.name, 'credentials.json')
        ClientConfig.credential_store = path
        wm, _cred = self._wm()
        try:
            with open(path, 'w', encoding='utf-8') as f:
                f.write('{broken')
            out = wm.redact_text('plain error message')
            self.assertIn('已省略', out, '存储损坏且无上下文时必须固定摘要')
        finally:
            td.cleanup()


class _FakeState:
    def __init__(self, websocket, connected=True):
        self.websocket = websocket
        self.is_connected = connected


class _FakeApp:
    def __init__(self, websocket):
        self.state = _FakeState(websocket)
        self.loop = None


class _FakeWebsocket:
    def __init__(self, exc):
        self._exc = exc

    async def send(self, _payload):
        raise self._exc

    async def recv(self):
        raise self._exc


class _FakeAudioMessage:
    def to_json(self):
        return '{}'


class SendReceiveRedactionTests(unittest.TestCase):
    """A03：send/receive 通用异常统一脱敏并在 except 块外抛出——原异常
    （含假 Key 原文）不进入异常链，真实日志（含 traceback）不泄露 Key。
    全程假 Key（已存入凭据存储并用于握手，属于已知 Key）。"""

    FAKE = 'dpk-stage4-fake-55667788'

    def setUp(self):
        self._orig_credential_store = ClientConfig.credential_store

    def tearDown(self):
        ClientConfig.credential_store = self._orig_credential_store

    def _known_key_wm(self):
        """存储假 Key 并完成握手（已知 Key），返回 (wm, 临时目录)"""
        import os
        import tempfile
        import importlib
        import core.client.connection.credentials as cred
        import core.client.connection.websocket_manager as wm
        td = tempfile.TemporaryDirectory()
        path = os.path.join(td.name, 'credentials.json')
        ClientConfig.credential_store = path
        importlib.reload(cred)
        importlib.reload(wm)
        cred.save_api_key(self.FAKE, path)
        importlib.reload(cred)
        importlib.reload(wm)
        headers = wm._handshake_headers('wss://voice.example.com')
        self.assertEqual(headers, {'X-API-Key': self.FAKE}, '前提：握手成功取得假 Key')
        return wm, td

    def _run_send(self, wm, exc):
        import asyncio
        mgr = wm.WebSocketManager(_FakeApp(_FakeWebsocket(exc)))
        loop = asyncio.new_event_loop()
        try:
            with self.assertRaises(wm.CommunicationError) as ctx:
                loop.run_until_complete(mgr.send(_FakeAudioMessage()))
        finally:
            loop.close()
        return ctx.exception

    def _run_receive(self, wm, exc, raw=None):
        import asyncio
        if raw is not None:
            class _RawWs:
                async def recv(self_inner):
                    return raw
            mgr = wm.WebSocketManager(_FakeApp(_RawWs()))
        else:
            mgr = wm.WebSocketManager(_FakeApp(_FakeWebsocket(exc)))
        loop = asyncio.new_event_loop()
        try:
            with self.assertRaises(wm.CommunicationError) as ctx:
                loop.run_until_complete(mgr.receive())
        finally:
            loop.close()
        return ctx.exception

    def test_send_failure_redacts_known_key_and_drops_chain(self):
        wm, td = self._known_key_wm()
        try:
            ce = self._run_send(wm, RuntimeError(f'connect failed with {self.FAKE} in payload'))
            self.assertNotIn(self.FAKE, str(ce), '发送异常文本不得带 Key 原文')
            self.assertIn('<redacted>', str(ce))
            self.assertTrue(ce.__suppress_context__, '异常链必须被抑制（__suppress_context__）')
            self.assertIsNone(ce.__cause__)
            import traceback
            formatted = ''.join(traceback.format_exception(type(ce), ce, ce.__traceback__))
            self.assertNotIn(self.FAKE, formatted, 'traceback（含异常链）不得带 Key 原文')
        finally:
            td.cleanup()

    def test_receive_failure_redacts_known_key_and_drops_chain(self):
        wm, td = self._known_key_wm()
        try:
            ce = self._run_receive(
                wm, RuntimeError(f'recv broke: header X-API-Key: {self.FAKE} echoed'))
            self.assertNotIn(self.FAKE, str(ce), '接收异常文本不得带 Key 原文')
            self.assertIn('<redacted>', str(ce))
            self.assertTrue(ce.__suppress_context__)
            import traceback
            formatted = ''.join(traceback.format_exception(type(ce), ce, ce.__traceback__))
            self.assertNotIn(self.FAKE, formatted)
        finally:
            td.cleanup()

    def test_receive_parse_failure_drops_raw_document(self):
        """解析失败：原始报文/异常链不得随 CommunicationError 回传"""
        wm, td = self._known_key_wm()
        try:
            ce = self._run_receive(wm, None, raw=f'{{"api_key": "{self.FAKE}"')
            self.assertNotIn(self.FAKE, str(ce), '解析失败不得把原报文带出')
            self.assertTrue(ce.__suppress_context__)
        finally:
            td.cleanup()

    def test_real_logger_output_hides_key(self):
        """真实日志输出（core.logger 文件 handler + except 块内 exc_info=True，
        同 file_transcriber/result_processor 的记录方式）：异常与异常链不落盘"""
        import tempfile
        from core.logger import Logger
        wm, td = self._known_key_wm()
        try:
            ce_send = self._run_send(wm, RuntimeError(f'send died with {self.FAKE} in frame'))
            ce_recv = self._run_receive(wm, RuntimeError(f'recv {self.FAKE}'))
            with tempfile.TemporaryDirectory() as log_td:
                lg = Logger.setup('client-stage4', log_dir=str(log_td), level='DEBUG',
                                  log_filename='client_stage4')
                try:
                    raise ce_send        # 复现 except 块内记录：exc_info 带上真实异常链
                except Exception:
                    lg.error(f"转录发送异常: {ce_send}", exc_info=True)   # 同 file_transcriber
                lg.debug(f"连接异常中断: {ce_send}")                      # 同 result_processor
                try:
                    raise ce_recv
                except Exception:
                    lg.error(f"接收消息错误: {ce_recv}", exc_info=True)   # 同 file_transcriber receive
                content = (pathlib.Path(log_td) / 'client_stage4_latest.log').read_text(encoding='utf-8')
                for h in list(lg.handlers):   # 关闭 handler，释放文件供临时目录清理
                    h.flush()
                    lg.removeHandler(h)
                    h.close()
            self.assertIn('转录发送异常', content, '日志必须真实写出（验证的是日志内容而非桩）')
            self.assertNotIn(self.FAKE, content, '真实日志（含 traceback）不得带假 Key 原文')
        finally:
            td.cleanup()


class CredentialNameListConsistencyTests(unittest.TestCase):
    """桌面（C# RemoteUrlValidationError）与客户端（_normalize_query_name +
    _CRED_PARAM_RE）的凭据名清单必须保持同一拒绝策略"""

    REPO = pathlib.Path(__file__).resolve().parents[1]

    def test_desktop_and_client_credential_name_sets_identical(self):
        import re
        wm = _reload_connection_module()
        m = re.search(r'\(\?:(.+)\)\$', wm._CRED_PARAM_RE.pattern)
        self.assertIsNotNone(m)
        client_names = set(m.group(1).split('|'))
        cs = (self.REPO / 'desktop' / 'CapsWriterDesktop.cs').read_text(encoding='utf-8-sig')
        m2 = re.search(r'Regex\.IsMatch\(decoded,\s*@"\^\(\?:(.+?)\)\$"\)', cs)
        self.assertIsNotNone(m2, '桌面端凭据名正则未找到（RemoteUrlValidationError）')
        desktop_names = set(m2.group(1).split('|'))
        self.assertEqual(client_names, desktop_names,
                         f'桌面端与客户端凭据名清单不一致: 仅客户端有 '
                         f'{sorted(client_names - desktop_names)}，仅桌面端有 '
                         f'{sorted(desktop_names - client_names)}')


class CloseAndConfigErrorRedactionTests(unittest.TestCase):
    """A03 补充：close 异常与 connect 配置校验错误同样做已知 Key 脱敏 +
    原异常链隔离；真实日志/traceback 不含标记；普通行为不变。全程假 Key。"""

    FAKE = 'dpk-stage5-fake-11223344'

    def setUp(self):
        self._orig_credential_store = ClientConfig.credential_store
        self._orig_server_url = getattr(ClientConfig, 'server_url', '')

    def tearDown(self):
        ClientConfig.credential_store = self._orig_credential_store
        ClientConfig.server_url = self._orig_server_url

    def _known_key_wm(self):
        """存入假 Key 并完成握手（进入内存脱敏上下文），返回 (wm, 临时目录)"""
        import os
        import tempfile
        import importlib
        import core.client.connection.credentials as cred
        import core.client.connection.websocket_manager as wm
        td = tempfile.TemporaryDirectory()
        path = os.path.join(td.name, 'credentials.json')
        ClientConfig.credential_store = path
        importlib.reload(cred)
        importlib.reload(wm)
        cred.save_api_key(self.FAKE, path)
        importlib.reload(cred)
        importlib.reload(wm)
        headers = wm._handshake_headers('wss://voice.example.com')
        self.assertEqual(headers, {'X-API-Key': self.FAKE}, '前提：握手成功取得假 Key')
        return wm, td

    def _close_handlers(self, lg):
        for h in list(lg.handlers):
            h.flush()
            lg.removeHandler(h)
            h.close()

    def test_close_failure_redacts_known_key_and_drops_chain(self):
        """已用假 Key 触发 close 裸错误：真实日志（exc_info=True，同文件模式
        file_runner 记录方式）与 traceback 不含 Key 标记"""
        import asyncio
        import tempfile
        import traceback
        from core.logger import Logger
        wm, td = self._known_key_wm()
        try:
            class _BadClose:
                async def close(self_inner):
                    raise RuntimeError(f'close handshake failed with {self.FAKE}')
            mgr = wm.WebSocketManager(_FakeApp(_BadClose()))
            loop = asyncio.new_event_loop()
            try:
                with self.assertRaises(wm.CommunicationError) as ctx:
                    loop.run_until_complete(mgr.close())
            finally:
                loop.close()
            ce = ctx.exception
            self.assertNotIn(self.FAKE, str(ce), 'close 异常文本不得带 Key 原文')
            self.assertIn('<redacted>', str(ce))
            self.assertTrue(ce.__suppress_context__, '异常链必须被抑制')
            self.assertIsNone(ce.__cause__)
            formatted = ''.join(traceback.format_exception(type(ce), ce, ce.__traceback__))
            self.assertNotIn(self.FAKE, formatted, 'traceback 不得带 Key 原文')
            # 真实日志回显路径：except 块内 exc_info=True（同 file_runner）
            with tempfile.TemporaryDirectory() as log_td:
                lg = Logger.setup('client-stage5-close', log_dir=str(log_td), level='DEBUG',
                                  log_filename='client_stage5_close')
                try:
                    raise ce
                except Exception:
                    lg.error(f"文件模式运行异常: {ce}", exc_info=True)
                content = (pathlib.Path(log_td) / 'client_stage5_close_latest.log').read_text(encoding='utf-8')
                self._close_handlers(lg)
            self.assertIn('文件模式运行异常', content, '日志必须真实写出（验证日志内容而非桩）')
            self.assertNotIn(self.FAKE, content, '真实日志（exc_info traceback）不得带假 Key')
        finally:
            td.cleanup()

    def test_normal_close_unchanged(self):
        """普通连接关闭行为不变：已连接照常关闭并清引用，未连接安全返回"""
        wm = _reload_connection_module()
        closed = {'n': 0}

        class _OkClose:
            async def close(self_inner):
                closed['n'] += 1

        import asyncio
        app = _FakeApp(_OkClose())
        mgr = wm.WebSocketManager(app)
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(mgr.close())
        finally:
            loop.close()
        self.assertEqual(closed['n'], 1)
        self.assertIsNone(app.state.websocket)
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(mgr.close())   # 已断开：安全返回不报错
        finally:
            loop.close()
        self.assertEqual(closed['n'], 1)

    def test_connect_config_error_log_redacts_stored_key(self):
        """已存假 Key 作为无效 URL 片段（非法 scheme 前缀）触发地址校验错误：
        旧实现错误消息逐字回显原配置片段 → 真实 logger.error 输出不得含 Key"""
        import asyncio
        import tempfile
        from core.logger import Logger
        wm, td = self._known_key_wm()
        try:
            # 非法 scheme 前缀 + 片段含已存假 Key：scheme 错误消息会回显片段原文
            ClientConfig.server_url = f'ftp#{self.FAKE}'
            with tempfile.TemporaryDirectory() as log_td:
                lg = Logger.setup('client-stage5-config', log_dir=str(log_td), level='DEBUG',
                                  log_filename='client_stage5_config')

                class _DisconnectedState:
                    websocket = None
                    is_connected = False

                class _DisconnectedApp:
                    state = _DisconnectedState()
                    loop = None

                mgr = wm.WebSocketManager(_DisconnectedApp())
                loop = asyncio.new_event_loop()
                try:
                    with mock.patch.object(wm, 'logger', lg):
                        ok = loop.run_until_complete(mgr.connect())
                finally:
                    loop.close()
                content = (pathlib.Path(log_td) / 'client_stage5_config_latest.log').read_text(encoding='utf-8')
                self._close_handlers(lg)
            self.assertFalse(ok, '配置校验失败必须返回 False，不得继续连接')
            self.assertIn('服务端地址配置无效', content, '配置错误日志必须真实写出')
            self.assertNotIn(self.FAKE, content, '真实配置错误日志不得含已存假 Key')
        finally:
            td.cleanup()


if __name__ == '__main__':
    unittest.main()
